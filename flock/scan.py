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
Every store is a "worker" agent and the deposit reconciler a "checker" agent in the live feed (flock/agents_live.py):
spawned when queued, working while a process has it, done with its row and finding counts. The tracker also learns
which agent found each finding (tracker.owner), so judges and investigators nest under it on the dashboard.
"""
from __future__ import annotations

import csv
import os
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from flock import agents_live
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
           context: dict | None = None, agents: agents_live.Tracker | None = None) -> list[dict[str, Any]]:
    """Findings (dicts) for std tables {name: Path}. Same results whichever path runs.
    context (small path only): gets stories, terms_for and orders for the Journal Entry Drafter.
    agents: the run's live tracker (worker per store, checker for deposits); None records nothing."""
    progress = ensure(progress)
    agents = agents_live.ensure(agents)
    paths = {k: v for k, v in tables.items() if isinstance(v, (str, Path))}
    size = count_rows(paths.get("orders")) + count_rows(paths.get("payout_lines"))
    if size < split_rows or len(paths) < len(tables):
        return _detect_small(tables, terms, progress, context, agents)
    return _detect_split(paths, terms, progress, workers or workers_default(), agents)


def _store_index(t: dict[str, list[dict]]) -> dict[str, str]:
    """Any order id / platform order id / payout line id -> store_id (to say which worker found a finding)."""
    who: dict[str, str] = {}
    for ln in t["payout_lines"]:
        for k in ("order_id", "platform_order_id", "payout_line_id"):
            if ln.get(k) and ln.get("store_id"):
                who[ln[k]] = ln["store_id"]
    for o in t["orders"]:
        if o.get("store_id"):
            who[o["order_id"]] = o["store_id"]
    return who


def _detect_small(tables: dict[str, Any], terms: list[dict[str, Any]], progress: Progress,
                  context: dict | None = None,
                  agents: agents_live.Tracker | None = None) -> list[dict[str, Any]]:
    """One process for everything; each store still shows as a worker (all working at once, then done)."""
    agents = agents_live.ensure(agents)
    t = matcher.load_tables(tables)
    who = _store_index(t)
    stores = sorted(set(who.values()))
    crew = {s: agents.spawn("worker", subject=s, agent_id=f"worker:{s}").working("matching orders to payouts")
            for s in stores}
    checker = agents.spawn("checker", subject="deposits", agent_id="checker:deposits").working(
        f"reconciling payouts with {len(t['bank_deposits']):,} bank deposits") if t["bank_deposits"] else None
    agents.flush()
    stories, match_findings = matcher.match(t)
    progress.stage("match+check", total=len(stories), unit="stories")
    terms_for = deal_keeper.TermIndex(terms)
    found = [f.to_dict() for f in match_findings]
    found += [f.to_dict() for f in checkers.run_all(stories, terms_for)]
    if context is not None and stories:
        context.update(stories=stories, terms_for=terms_for, orders=t["orders"])
    progress.update(len(stories))

    per: dict[str, int] = {}
    for f in found:
        owner = checker if f["check_type"] == "deposit" else crew.get(
            next((who[x] for x in f["order_ids"] if x in who), None))
        if owner is not None:
            agents.owner[f["finding_id"]] = owner.id
            per[owner.id] = per.get(owner.id, 0) + 1
    matched: dict[str, int] = {}
    for st in stories:
        if st.get("kind") == "order":
            matched[st["store_id"]] = matched.get(st["store_id"], 0) + 1
    for s, w in crew.items():
        w.done(f"{matched.get(s, 0):,} orders matched, {per.get(w.id, 0)} findings", findings=per.get(w.id, 0))
    if checker is not None:
        n = sum(1 for st in stories if st.get("kind") == "deposit")
        checker.done(f"{n:,} deposits reconciled, {per.get(checker.id, 0)} findings", findings=per.get(checker.id, 0))
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
                  workers: int, agents: agents_live.Tracker | None = None) -> list[dict[str, Any]]:
    agents = agents_live.ensure(agents)
    tmp = Path(tempfile.mkdtemp(prefix="flock_scan_"))
    try:
        parts, groups, statements = _split(paths, tmp, progress)
        deposits = matcher.load_tables({"bank_deposits": paths.get("bank_deposits")})["bank_deposits"]
        checker = agents.spawn("checker", subject="deposits", agent_id="checker:deposits").working(
            f"reconciling {len(groups):,} payouts with {len(deposits):,} bank deposits")
        deposit_stories = matcher.reconcile_deposits([], deposits, groups=groups)
        deposit_of = {(ds["channel"], pid): ds["deposit_id"] for ds in deposit_stories for pid in ds["payout_ids"]}
        found = [f.to_dict() for f in checkers.run_all(deposit_stories, lambda s: {})]
        for f in found:
            agents.owner[f["finding_id"]] = checker.id
        checker.done(f"{len(deposit_stories):,} deposits reconciled, {len(found)} findings", findings=len(found))

        extra = {k: paths[k] for k in EXTRA_TABLES if paths.get(k)}
        tasks = [(store, part, extra, statements, deposit_of, terms) for store, part in sorted(parts.items())]
        crew = {t[0]: agents.spawn("worker", subject=t[0], agent_id=f"worker:{t[0]}", summary="queued")
                for t in tasks}
        agents.flush()
        total = sum(count_rows(p) for part in parts.values() for p in part.values())
        progress.stage(f"match+check x{min(workers, len(tasks)) or 1}", total=total)
        results: dict[str, list[dict[str, Any]]] = {}
        done = 0

        def finished(store: str, out: list[dict[str, Any]], n: int) -> None:
            nonlocal done
            results[store] = out
            for f in out:
                agents.owner[f["finding_id"]] = crew[store].id
            crew[store].done(f"{n:,} rows, {len(out)} findings", rows=n, findings=len(out))
            done += n
            progress.update(done)

        if workers <= 1 or len(tasks) <= 1:
            for task in tasks:
                crew[task[0]].working(f"matching + checking store {task[0]}")
                finished(*_run_store(task))
        else:
            # `workers` stores in flight at a time, so a worker shows "working" exactly while a process has it
            with ProcessPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
                queue, running = list(tasks), set()

                def submit() -> None:
                    task = queue.pop(0)
                    crew[task[0]].working(f"matching + checking store {task[0]}")
                    running.add(pool.submit(_run_store, task))

                for _ in range(min(workers, len(queue))):
                    submit()
                agents.flush()
                while running:
                    fut = next(as_completed(running))
                    running.discard(fut)
                    finished(*fut.result())
                    if queue:
                        submit()
        for store in sorted(results):
            found += results[store]
        return found
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
