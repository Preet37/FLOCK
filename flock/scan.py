"""The flock at scale: Matcher + Deal Keeper + Checkers over the std tables, one store at a time, in parallel.

Small data (under SPLIT_ROWS order + payout rows) runs exactly as before, in one process. A year of a 56-store
chain is ~2.5M rows, which as Python dicts would not fit comfortably in a laptop's memory, so instead:

  1. split   one streaming pass over orders.csv / payout_lines.csv writes one small file per store, and keeps
             per-payout totals (matcher.PayoutTotals) and the list of statements searched per channel.
  2. deposits payout totals -> bank deposits (matcher.reconcile_deposits with groups) -> check_deposit.
  3. stores  each store's orders + payout lines go through matcher.match_orders + checkers in a worker process
             (FLOCK_WORKERS, default min(8, cores)). Matching never crosses stores (exact ids, and the fallback
             needs the same store), so the findings are the same as one big run.

Every stage reports to a flock.progress.Progress (stage, done/total, rows/sec).
"""
from __future__ import annotations

import csv
import os
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from flock.agents import checkers, deal_keeper, matcher
from flock.progress import Progress, ensure

SPLIT_ROWS = 50_000
EXTRA_TABLES = ("error_charges", "refunds")   # small sidecars: every store worker gets all of them


def count_rows(path: Path | None) -> int:
    if not path or not Path(path).exists():
        return 0
    n = 0
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            n += block.count(b"\n")
    return max(0, n - 1)


def workers_default() -> int:
    env = os.environ.get("FLOCK_WORKERS", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    return max(1, min(8, os.cpu_count() or 1))


def detect(tables: dict[str, Any], terms: list[dict[str, Any]], progress: Progress | None = None,
           workers: int | None = None, split_rows: int = SPLIT_ROWS,
           context: dict | None = None) -> list[dict[str, Any]]:
    """Findings (dicts) for std tables {name: Path}. Same results whichever path runs.
    context (small path only): gets stories, terms_for and orders for the Journal Entry Drafter."""
    progress = ensure(progress)
    paths = {k: v for k, v in tables.items() if isinstance(v, (str, Path))}
    size = count_rows(paths.get("orders")) + count_rows(paths.get("payout_lines"))
    if size < split_rows or len(paths) < len(tables):
        return _detect_small(tables, terms, progress, context)
    return _detect_split(paths, terms, progress, workers or workers_default())


def _detect_small(tables: dict[str, Any], terms: list[dict[str, Any]], progress: Progress,
                  context: dict | None = None) -> list[dict[str, Any]]:
    stories, match_findings = matcher.match(tables)
    progress.stage("match+check", total=len(stories), unit="stories")
    terms_for = deal_keeper.TermIndex(terms)
    found = [f.to_dict() for f in match_findings]
    found += [f.to_dict() for f in checkers.run_all(stories, terms_for)]
    if context is not None and stories:
        context.update(stories=stories, terms_for=terms_for, orders=matcher.load_tables(tables)["orders"])
    progress.update(len(stories))
    return found


def _split(paths: dict[str, Path], tmp: Path, progress: Progress) -> tuple[dict[str, dict[str, Path]], dict, dict]:
    """One streaming pass: per-store files, per-payout totals, statements searched per channel."""
    parts: dict[str, dict[str, Path]] = {}
    totals = matcher.PayoutTotals()
    statements: dict[str, set] = {}
    total = count_rows(paths.get("orders")) + count_rows(paths.get("payout_lines"))
    progress.stage("split by store", total=total)
    done = 0
    for name in ("orders", "payout_lines"):
        src = paths.get(name)
        if not src or not Path(src).exists():
            continue
        handles: dict[str, Any] = {}
        writers: dict[str, Any] = {}
        with Path(src).open(newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if not header:
                continue
            si = header.index("store_id")
            if name == "payout_lines":
                ix = {c: header.index(c) for c in ("channel", "payout_id", "payout_date", "net", "source_file",
                                                    "source_row")}
            for row in reader:
                store = row[si] if si < len(row) else ""
                w = writers.get(store)
                if w is None:
                    p = tmp / f"{name}__{len(handles):05d}.csv"
                    handles[store] = p.open("w", newline="")
                    w = writers[store] = csv.writer(handles[store], lineterminator="\n")
                    w.writerow(header)
                    parts.setdefault(store, {})[name] = p
                w.writerow(row)
                if name == "payout_lines":
                    ln = matcher._clean({c: row[i] for c, i in ix.items()})
                    totals.add(ln)
                    if ln.get("source_file"):
                        statements.setdefault(ln["channel"], set()).add(ln["source_file"])
                done += 1
                if done % 20000 == 0:
                    progress.update(done)
        for h in handles.values():
            h.close()
    progress.update(done)
    return parts, totals.result(), statements


def _run_store(task: tuple) -> tuple[str, list[dict[str, Any]], int]:
    """Worker: one store's orders + payout lines -> order stories -> findings."""
    store, part, extra, statements, deposit_of, terms = task
    t = matcher.load_tables({**extra, **part})
    stories, found = matcher.match_orders(t, deposit_of, statements)
    out = [f.to_dict() for f in found]
    out += [f.to_dict() for f in checkers.run_all(stories, deal_keeper.TermIndex(terms))]
    return store, out, len(t["orders"]) + len(t["payout_lines"])


def _detect_split(paths: dict[str, Path], terms: list[dict[str, Any]], progress: Progress,
                  workers: int) -> list[dict[str, Any]]:
    tmp = Path(tempfile.mkdtemp(prefix="flock_scan_"))
    try:
        parts, groups, statements = _split(paths, tmp, progress)
        deposits = matcher.load_tables({"bank_deposits": paths.get("bank_deposits")})["bank_deposits"]
        deposit_stories = matcher.reconcile_deposits([], deposits, groups=groups)
        deposit_of = {(ds["channel"], pid): ds["deposit_id"] for ds in deposit_stories for pid in ds["payout_ids"]}
        found = [f.to_dict() for f in checkers.run_all(deposit_stories, lambda s: {})]

        extra = {k: paths[k] for k in EXTRA_TABLES if paths.get(k)}
        tasks = [(store, part, extra, statements, deposit_of, terms) for store, part in sorted(parts.items())]
        total = sum(count_rows(p) for part in parts.values() for p in part.values())
        progress.stage(f"match+check x{min(workers, len(tasks)) or 1}", total=total)
        results: dict[str, list[dict[str, Any]]] = {}
        done = 0
        if workers <= 1 or len(tasks) <= 1:
            for task in tasks:
                store, out, n = _run_store(task)
                results[store] = out
                done += n
                progress.update(done)
        else:
            with ProcessPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
                futures = [pool.submit(_run_store, task) for task in tasks]
                for fut in as_completed(futures):
                    store, out, n = fut.result()
                    results[store] = out
                    done += n
                    progress.update(done)
        for store in sorted(results):
            found += results[store]
        return found
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
