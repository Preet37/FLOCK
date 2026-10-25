"""Benchmark harness (owner: Thabhelo). The proof table: every contestant judges the SAME findings on held-out seeds.

  python scripts/benchmark.py                                        # rules only, seeds 101-110, the bagel chain, 2 weeks
  python scripts/benchmark.py --contestants rules,claude,local       # add Claude and the GX10 (skipped if no key / offline)
  python scripts/benchmark.py --seeds 101-103 --stores 8 --weeks 1   # smaller and faster
  python scripts/benchmark.py --contestants local-tuned --tuned-model flock-judge-v1
  python scripts/benchmark.py --human sheets/ira_s07_w1.csv --human-scope 101:S07:1 --human-minutes 95
  python scripts/benchmark.py --list                                 # contestants and what each one needs

How it works
  1. prepare  per seed: generate -> collect -> format -> match -> deal terms -> check. All code, no model, so the
              findings are identical for every contestant. Cached in data/benchmarks/seeds/<seed>/ (findings.json,
              answer_key.csv, std/orders.csv). Never touches data/answer_key.csv, mappings.json, rules.json or data/state.
  2. judge    a contestant is a provider setup for the panel (flock/llm.py env vars). panel.decide() on every finding,
              with no learned rules and equal judge weights, each call timed. A live contestant whose key is missing is
              recorded as skipped, never silently run on the offline rules. Votes that fell back to the rules after an
              API error are counted and shown.
  3. score    scripts/score.py logic against that seed's answer key: traps caught, false alarms, correct dollars, sent to
              a human. Plus latency (p50 / p95 seconds per finding), throughput (findings per minute), estimated cost per
              1,000 findings (tokens ~ chars / 4 at list prices; override with --price), and works-offline.
  4. write    data/benchmarks/results.json (what the dashboard's Benchmark tab reads) and results.csv. Every run is kept,
              good or bad; `current` holds the latest run per contestant.

The human contestant: a teammate who did not build the generator works one store-week from the raw files with a spreadsheet
and a timer, and lists what they found as a CSV: order_id, outcome (correct | recoverable | conflict), amount. --human-scope
SEED:STORE[:WEEK] says which store-week they worked; the answer key is filtered to it and anything they flagged as
recoverable that the key calls correct (or is not a trap at all) is a false alarm. --human-minutes is their timer.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import importlib.util
import json
import os
import shutil
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flock import llm, pipeline  # noqa: E402
from flock.agents import collector, panel  # noqa: E402
from flock.progress import NullProgress  # noqa: E402

OUT_DIR = ROOT / "data" / "benchmarks"
SEEDS_DIR = OUT_DIR / "seeds"
RESULTS_JSON = OUT_DIR / "results.json"
RESULTS_CSV = OUT_DIR / "results.csv"
DEFAULT_SEEDS = "101-110"
DEFAULT_PROFILE = "ca_bagel_chain"
CHARS_PER_TOKEN = 4  # estimate; the OpenAI-compatible providers don't all report usage

# USD per 1M tokens (input, output), list prices (flock/llm.py LIST_PRICES). Override any model with --price model=in,out.
PRICES: dict[str, tuple[float, float]] = dict(llm.LIST_PRICES)

# Every FLOCK_* knob the provider layer reads. A contestant starts from a clean slate of these, so the shell's
# FLOCK_OFFLINE=1 never leaks into a live contestant and a live key never leaks into "rules".
PROVIDER_ENV = ["FLOCK_OFFLINE", "FLOCK_PROVIDER", "FLOCK_MODEL", "FLOCK_JUDGE_PROVIDERS", "FLOCK_JUDGE_MODELS",
                "FLOCK_REVIEWER_PROVIDER", "FLOCK_REVIEWER_MODEL", "FLOCK_WRITER_PROVIDER", "FLOCK_WRITER_MODEL",
                "FLOCK_MAPPER_PROVIDER", "FLOCK_MAPPER_MODEL", "FLOCK_AUDIT_RATE"]

CONTESTANTS: dict[str, dict[str, Any]] = {
    "rules": {"label": "Rules only", "offline": True, "needs": "nothing",
              "env": {"FLOCK_OFFLINE": "1"}},
    "claude": {"label": "Claude, all three judges", "offline": False, "needs": "ANTHROPIC_API_KEY",
               "env": {"FLOCK_JUDGE_PROVIDERS": "anthropic", "FLOCK_REVIEWER_PROVIDER": "anthropic",
                       "FLOCK_WRITER_PROVIDER": "offline", "FLOCK_MAPPER_PROVIDER": "offline"}},
    "local": {"label": "Local, off the shelf (GX10)", "offline": True, "needs": "FLOCK_LOCAL_URL reachable",
              "env": {"FLOCK_JUDGE_PROVIDERS": "local", "FLOCK_REVIEWER_PROVIDER": "offline",
                      "FLOCK_WRITER_PROVIDER": "offline", "FLOCK_MAPPER_PROVIDER": "offline"}},
    "local-tuned": {"label": "Local, tuned on training seeds", "offline": True,
                    "needs": "FLOCK_LOCAL_URL reachable and --tuned-model (or FLOCK_TUNED_MODEL)",
                    "env": {"FLOCK_JUDGE_PROVIDERS": "local", "FLOCK_JUDGE_MODELS": "${TUNED}",
                            "FLOCK_REVIEWER_PROVIDER": "offline", "FLOCK_WRITER_PROVIDER": "offline",
                            "FLOCK_MAPPER_PROVIDER": "offline"}},
    "creao": {"label": "CREAO hosted agents", "offline": False,
              "needs": "CREAO_API_KEY (project key, funded USD wallet) and creao_agents.json (scripts/creao_setup.py --create)",
              "env": {"FLOCK_JUDGE_PROVIDERS": "creao", "FLOCK_REVIEWER_PROVIDER": "offline",
                      "FLOCK_WRITER_PROVIDER": "offline", "FLOCK_MAPPER_PROVIDER": "offline"}},
    "groq": {"label": "Groq, Llama 3.3 70B", "offline": False, "needs": "GROQ_API_KEY",
             "env": {"FLOCK_JUDGE_PROVIDERS": "groq", "FLOCK_REVIEWER_PROVIDER": "offline",
                     "FLOCK_WRITER_PROVIDER": "offline", "FLOCK_MAPPER_PROVIDER": "offline"}},
    "nvidia": {"label": "NVIDIA build, Llama 3.3 70B", "offline": False, "needs": "NVIDIA_API_KEY",
               "env": {"FLOCK_JUDGE_PROVIDERS": "nvidia", "FLOCK_REVIEWER_PROVIDER": "offline",
                       "FLOCK_WRITER_PROVIDER": "offline", "FLOCK_MAPPER_PROVIDER": "offline"}},
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


generator = _load("flock_generator", ROOT / "generator" / "generate.py")
scoreboard = _load("flock_scoreboard", ROOT / "scripts" / "score.py")


# ------------------------------------------------------------------------------------------------------------------
# 1. prepare: the same findings for everyone
# ------------------------------------------------------------------------------------------------------------------
def parse_seeds(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


_RAW_PATH = __import__("re").compile(r"(?:\S*?/)?\braw/")   # any folder prefix (or none) up to the raw data folder


def production_paths(f: dict[str, Any]) -> dict[str, Any]:
    """Evidence cites data/raw/<source>/<file> as it would in production, not the seed's cache folder. Every
    contestant then sees the same shape a real run shows, and no seed number leaks into a model's prompt."""
    fix = lambda text: _RAW_PATH.sub("data/raw/", str(text)) if text else text

    def src(path: str) -> str:
        path = fix(path)
        if path.startswith(("data/", "/")) or "raw/" in path:
            return path
        return "data/raw/" + path  # the formatter cites files relative to the raw folder it was given

    ev = [{**e, **({"source_file": src(e["source_file"])} if e.get("source_file") else {}),
           **({"what": fix(e["what"])} if e.get("what") else {})} for e in f.get("evidence", [])]
    return {**f, "evidence": ev, "reason": fix(f.get("reason", ""))}


def seed_dir(seed: int, profile: str, stores: int, days: int) -> Path:
    return SEEDS_DIR / f"{profile}-{stores}x{days}-t{len(generator.TRAPS)}-{seed}"  # a new trap invalidates the cache


def prepare(seed: int, profile: str, stores: int | None, days: int, trap_rate: float | None = None,
            force: bool = False, quiet: bool = False) -> dict[str, Any]:
    """Generate -> collect -> format -> detect for one seed. Cached; returns {dir, findings, key, orders}."""
    prof = generator.load_profile(profile)
    n_stores = stores or prof["default_stores"]
    d = seed_dir(seed, prof["name"], n_stores, days)
    findings_path, key_path, meta_path = d / "findings.json", d / "answer_key.csv", d / "meta.json"
    if not force and findings_path.exists() and key_path.exists():
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        return {"dir": d, "findings": json.loads(findings_path.read_text()), "key": key_path,
                "orders": d / "std" / "orders.csv", "seed": seed, **meta}
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    out, raw, std = d / "out", d / "raw", d / "std"
    mappings = d / "mappings.json"
    shutil.copy(ROOT / "mappings.json", mappings)  # a copy: unseen layouts get confirmed here, never in the repo
    t0 = time.monotonic()
    world = generator.generate(seed, n_stores, days, None, out, prof, trap_rate, None)
    collector.collect(out, raw)
    with contextlib.redirect_stdout(open(os.devnull, "w")) if quiet else contextlib.nullcontext():
        found = pipeline.detect_real(raw=raw, std=std, mappings=mappings, confirm=True, interactive=False,
                                     progress=NullProgress())
    shutil.copy(out / "answer_key.csv", key_path)
    found = [production_paths(f) for f in found]
    findings_path.write_text(json.dumps(found))
    meta = {"profile": prof["name"], "stores": n_stores, "days": days, "order_count": len(world["orders"]),
            "key_rows": len(world["key"]), "prepare_seconds": round(time.monotonic() - t0, 1)}
    meta_path.write_text(json.dumps(meta))
    shutil.rmtree(out, ignore_errors=True)  # raw + std are kept: the human contestant works from raw
    return {"dir": d, "findings": found, "key": key_path, "orders": std / "orders.csv", "seed": seed, **meta}


# ------------------------------------------------------------------------------------------------------------------
# 2. judge: one contestant on one seed's findings
# ------------------------------------------------------------------------------------------------------------------
class Meter:
    """Counts model calls and estimated tokens by wrapping llm.complete while a contestant runs."""

    def __init__(self) -> None:
        self.calls = 0
        self.in_chars = 0
        self.out_chars = 0
        self.models: dict[str, int] = {}
        self.errors = 0

    @contextlib.contextmanager
    def armed(self):
        real = llm.complete

        def wrapped(system: str, user: str, max_tokens: int, r=None):
            self.calls += 1
            self.in_chars += len(system) + len(user)
            try:
                text, served_by = real(system, user, max_tokens, r)
            except Exception:
                self.errors += 1
                raise
            self.out_chars += len(text)
            self.models[served_by] = self.models.get(served_by, 0) + 1
            return text, served_by

        llm.complete = wrapped
        try:
            yield self
        finally:
            llm.complete = real

    def cost_usd(self, prices: dict[str, tuple[float, float]]) -> float:
        if not self.calls:
            return 0.0
        total = 0.0
        in_tok, out_tok = self.in_chars / CHARS_PER_TOKEN, self.out_chars / CHARS_PER_TOKEN
        for model_id, n in self.models.items():
            share = n / self.calls
            price = next((p for key, p in prices.items() if key in model_id), (0.0, 0.0))
            total += share * (in_tok * price[0] + out_tok * price[1]) / 1_000_000
        return round(total, 6)


@contextlib.contextmanager
def contestant_env(env: dict[str, str]):
    saved = {k: os.environ.get(k) for k in PROVIDER_ENV}
    for k in PROVIDER_ENV:
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in env.items() if v})
    llm._client_obj = None  # a fresh Anthropic client per contestant (timeouts etc. read at creation)
    try:
        yield
    finally:
        for k in PROVIDER_ENV:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def resolve_env(name: str, tuned_model: str | None) -> dict[str, str]:
    env = dict(CONTESTANTS[name]["env"])
    for k, v in env.items():
        if v == "${TUNED}":
            env[k] = tuned_model or os.environ.get("FLOCK_TUNED_MODEL", "")
    return env


def unavailable_reason(name: str) -> str | None:
    """Inside the contestant's env: why this contestant cannot really run, or None."""
    if name == "rules":
        return None
    if name == "local-tuned" and not os.environ.get("FLOCK_JUDGE_MODELS"):
        return "no tuned model named (--tuned-model or FLOCK_TUNED_MODEL)"
    for i in range(len(panel.JUDGES)):
        r = llm.route("judge", i)
        if not llm.available(r):
            return f"judge {i + 1} route {llm.label(r)} has no key or is offline"
    return None


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 3)


class _Memory:
    """The scoreboard reads findings through store.all(); this is that, for a list."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self.rows


def judge(name: str, prepared: dict[str, Any], env: dict[str, str], prices: dict[str, tuple[float, float]],
          workers: int = 1, quiet: bool = False) -> dict[str, Any]:
    findings = prepared["findings"]
    row: dict[str, Any] = {"contestant": name, "label": CONTESTANTS[name]["label"], "seed": prepared["seed"],
                           "findings": len(findings), "offline": CONTESTANTS[name]["offline"], "status": "ok"}
    with contestant_env(env):
        why = unavailable_reason(name)
        if why:
            row.update(status="skipped", note=why, caught=0, total=0, missed=0, false_alarms=0, correct_dollars=0.0,
                       to_human=0, p50_s=None, p95_s=None, per_minute=None, cost_per_1000=None, fallbacks=0, models={})
            return row
        meter = Meter()
        creao_before = llm.creao_usage()
        latencies: list[float] = []
        judged: list[dict[str, Any]] = []

        def one(f: dict[str, Any]) -> dict[str, Any]:
            t = time.monotonic()
            patch = panel.decide(f, rules=[], weights=None)
            latencies.append(time.monotonic() - t)
            return {**f, **patch, "state": "JUDGED"}

        wall = time.monotonic()
        with meter.armed(), contextlib.redirect_stderr(open(os.devnull, "w")) if quiet else contextlib.nullcontext():
            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    judged = list(pool.map(one, findings))
            else:
                judged = [one(f) for f in findings]
        wall = time.monotonic() - wall
        row["models"] = meter.models
        creao_after = llm.creao_usage()  # CREAO only; zeros elsewhere
        row["creao_usd"] = round(creao_after["usd"] - creao_before["usd"], 6)
        row["credits"] = round(creao_after["credits"] - creao_before["credits"], 4)
    fallbacks = sum(1 for f in judged for v in f.get("votes", []) if v.get("fallback"))
    live_votes = sum(len(f.get("votes", [])) for f in judged)
    s = scoreboard.score(_Memory(judged), verbose=False, runs_path=prepared["dir"] / "no_runs.jsonl",
                         answer_key=prepared["key"])
    row.update(caught=s["caught"], total=s["total"], missed=s["missed"], false_alarms=s["false_alarms"],
               correct_dollars=round(s["recovered"], 2), to_human=s["to_human"],
               p50_s=percentile(latencies, 0.5), p95_s=percentile(latencies, 0.95),
               per_minute=round(len(findings) / wall * 60, 1) if wall > 0 else None,
               seconds=round(wall, 2), calls=meter.calls, cost_usd=meter.cost_usd(prices),
               cost_per_1000=round(meter.cost_usd(prices) / max(len(findings), 1) * 1000, 4),
               fallbacks=fallbacks, votes=live_votes, workers=workers,
               traps=[{k: t[k] for k in ("order_id", "trap_type", "expected_outcome", "got", "caught", "false_alarm")}
                      for t in s["traps"]])
    if name != "rules" and live_votes and fallbacks == live_votes:
        row.update(status="unreachable", note="every vote fell back to the offline rules; the model never answered")
    elif fallbacks:
        row["note"] = f"{fallbacks} of {live_votes} votes fell back to the offline rules after an API error"
    if row.get("creao_usd") or row.get("credits"):
        row["cost_usd"] = round((row.get("cost_usd") or 0) + (row.get("creao_usd") or 0), 6)
        row["cost_per_1000"] = round(row["cost_usd"] / max(len(findings), 1) * 1000, 4)
        spent = f"${row['creao_usd']:.4f} on CREAO" if row.get("creao_usd") else f"{row['credits']} CREAO credits"
        row["note"] = (row.get("note") + "; " if row.get("note") else "") + spent
    return row


# ------------------------------------------------------------------------------------------------------------------
# the human contestant
# ------------------------------------------------------------------------------------------------------------------
def store_week_orders(orders_csv: Path, store_id: str, week: int | None) -> set[str]:
    """Order ids for one store (and one 1-based week of the period, if given), from the standard orders table."""
    with orders_csv.open(newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["store_id"] == store_id]
    if week is None or not rows:
        return {r["order_id"] for r in rows}
    days = sorted(datetime.fromisoformat(r["placed_at"]).date() for r in rows if r.get("placed_at"))
    start = days[0]
    return {r["order_id"] for r in rows
            if (datetime.fromisoformat(r["placed_at"]).date() - start).days // 7 + 1 == week}


def judge_human(sheet: Path, prepared: dict[str, Any], scope: str, minutes: float | None) -> dict[str, Any]:
    seed, store_id, *rest = scope.split(":")
    week = int(rest[0]) if rest else None
    in_scope = store_week_orders(prepared["orders"], store_id, week)
    with sheet.open(newline="") as fh:
        said = {r["order_id"].strip(): r for r in csv.DictReader(fh) if r.get("order_id")}
    with prepared["key"].open(newline="") as fh:
        key = [r for r in csv.DictReader(fh) if r["order_id"] in in_scope]
    caught = [r for r in key if said.get(r["order_id"], {}).get("outcome", "").strip() == r["expected_outcome"]
              or (r["expected_outcome"] == "correct" and r["order_id"] not in said)]
    key_ids = {r["order_id"] for r in key}
    false_alarms = [o for o, r in said.items() if r.get("outcome", "").strip() == "recoverable"
                    and (o not in key_ids or next(k for k in key if k["order_id"] == o)["expected_outcome"] == "correct")]
    dollars = sum(float(r["amount"]) for r in caught if r["expected_outcome"] == "recoverable")
    to_human = len(key)  # by definition: a person looked at every one
    row = {"contestant": "human", "label": "A teammate, spreadsheet and timer", "seed": int(seed), "scope": scope,
           "findings": len(said), "offline": True, "status": "ok", "caught": len(caught), "total": len(key),
           "missed": len(key) - len(caught), "false_alarms": len(false_alarms), "correct_dollars": round(dollars, 2),
           "to_human": to_human, "p50_s": None, "p95_s": None, "cost_per_1000": None, "fallbacks": 0, "models": {},
           "human_minutes": minutes, "human_minutes_per_store_week": minutes,
           "per_minute": round(len(said) / minutes, 2) if minutes else None,
           "traps": [{"order_id": r["order_id"], "trap_type": r["trap_type"], "expected_outcome": r["expected_outcome"],
                      "got": said.get(r["order_id"], {}).get("outcome"), "caught": r in caught,
                      "false_alarm": r["order_id"] in false_alarms} for r in key]}
    if not in_scope:
        row.update(status="skipped", note=f"no orders for store {store_id} week {week} in seed {seed}")
    return row


# ------------------------------------------------------------------------------------------------------------------
# 3 + 4. aggregate and write
# ------------------------------------------------------------------------------------------------------------------
def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r["status"] in ("ok",)]
    if not ok:
        return {"status": rows[0]["status"] if rows else "skipped", "note": rows[0].get("note") if rows else None,
                "seeds": [r["seed"] for r in rows]}
    total_f = sum(r["findings"] for r in ok)
    secs = sum(r.get("seconds") or 0 for r in ok)
    cost = sum(r.get("cost_usd") or 0 for r in ok)
    lat = [v for r in ok for v in ([r["p50_s"]] if r.get("p50_s") is not None else [])]
    agg = {"status": "ok", "seeds": [r["seed"] for r in ok], "findings": total_f,
           "caught": sum(r["caught"] for r in ok), "total": sum(r["total"] for r in ok),
           "missed": sum(r["missed"] for r in ok), "false_alarms": sum(r["false_alarms"] for r in ok),
           "correct_dollars": round(sum(r["correct_dollars"] for r in ok), 2),
           "to_human": sum(r["to_human"] for r in ok),
           "p50_s": round(statistics.median(lat), 3) if lat else None,
           "p95_s": max((r["p95_s"] for r in ok if r.get("p95_s") is not None), default=None),
           "per_minute": round(total_f / secs * 60, 1) if secs > 0 else None,
           "cost_per_1000": round(cost / total_f * 1000, 4) if total_f else None,
           "offline": all(r["offline"] for r in ok), "fallbacks": sum(r.get("fallbacks", 0) for r in ok),
           "human_minutes_per_store_week": next((r.get("human_minutes_per_store_week") for r in ok
                                                 if r.get("human_minutes_per_store_week")), None)}
    notes = [r["note"] for r in rows if r.get("note")]
    if notes:
        agg["note"] = "; ".join(dict.fromkeys(notes))
    agg["accuracy"] = round(agg["caught"] / agg["total"], 4) if agg["total"] else None
    return agg


def load_results(path: Path | None = None) -> dict[str, Any]:
    path = path or RESULTS_JSON
    if path.exists():
        return json.loads(path.read_text())
    return {"runs": [], "current": {}}


CSV_COLS = ["run_id", "at", "contestant", "label", "seed", "findings", "caught", "total", "accuracy", "missed",
            "false_alarms", "correct_dollars", "to_human", "p50_s", "p95_s", "per_minute", "cost_per_1000", "offline",
            "fallbacks", "human_minutes_per_store_week", "status", "note", "models"]


def write_results(run: dict[str, Any], results_json: Path | None = None, results_csv: Path | None = None) -> None:
    results_json, results_csv = results_json or RESULTS_JSON, results_csv or RESULTS_CSV  # resolved at call time (tests redirect them)
    data = load_results(results_json)
    data["runs"].append(run)
    for name, agg in run["aggregate"].items():
        data["current"][name] = {**agg, "label": run["labels"][name], "run_id": run["run_id"], "at": run["at"],
                                 "config": run["config"]}
    data["updated_at"] = run["at"]
    results_json.parent.mkdir(parents=True, exist_ok=True)
    results_json.write_text(json.dumps(data, indent=1))
    is_new = not results_csv.exists()
    with results_csv.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS, extrasaction="ignore", lineterminator="\n")
        if is_new:
            w.writeheader()
        for name, rows in run["per_seed"].items():
            for r in rows:
                acc = round(r["caught"] / r["total"], 4) if r.get("total") else None
                w.writerow({**r, "run_id": run["run_id"], "at": run["at"], "accuracy": acc,
                            "models": ",".join(r.get("models", {}))})
            w.writerow({**run["aggregate"][name], "run_id": run["run_id"], "at": run["at"], "contestant": name,
                        "label": run["labels"][name], "seed": "all",
                        "models": ",".join({m for r in rows for m in r.get("models", {})})})


def _fmt_latency(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v * 1000:.0f} ms" if v < 1 else f"{v:.2f} s"


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return ""
    return f"{v:,.{nd}f}" if isinstance(v, float) else f"{v:,}" if isinstance(v, int) else str(v)


def print_table(run: dict[str, Any]) -> None:
    cols = [("contestant", 30), ("caught", 10), ("false alarms", 14), ("correct $", 11), ("to human", 10),
            ("p50", 9), ("p95", 9), ("per min", 10), ("$ / 1000", 10), ("offline", 9), ("status", 12)]
    c = run["config"]
    print(f"\n== Benchmark: held-out seeds {', '.join(str(s) for s in c['seeds'])} | {c['profile']}, "
          f"{c['stores']} stores x {c['days']} days ==")
    print("".join(f"{name:<{w}}" for name, w in cols))
    for name, a in run["aggregate"].items():
        label = run["labels"][name][:29]
        if a["status"] != "ok":
            pad = sum(w for _, w in cols[1:-1])
            print(f"{label:<30}{'':<{pad}}{a['status']:<12} {a.get('note') or ''}")
            continue
        cells = [label, f"{a['caught']}/{a['total']}", _fmt(a["false_alarms"]), _fmt(a["correct_dollars"]),
                 _fmt(a["to_human"]), _fmt_latency(a["p50_s"]), _fmt_latency(a["p95_s"]), _fmt(a["per_minute"], 0),
                 _fmt(a["cost_per_1000"], 4), "yes" if a["offline"] else "no", a["status"]]
        print("".join(f"{v:<{w}}" for v, (_, w) in zip(cells, cols)) + f" {a.get('note') or ''}".rstrip())
    print(f"\nwritten: {_short(RESULTS_JSON)} and {_short(RESULTS_CSV)}")


def _short(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


# ------------------------------------------------------------------------------------------------------------------
def run_benchmark(seeds: list[int], contestants: list[str], profile: str = DEFAULT_PROFILE, stores: int | None = None,
                  weeks: int = 2, days: int | None = None, trap_rate: float | None = None, workers: int = 1,
                  tuned_model: str | None = None, prices: dict[str, tuple[float, float]] | None = None,
                  human: Path | None = None, human_scope: str | None = None, human_minutes: float | None = None,
                  force: bool = False, quiet: bool = True, write: bool = True) -> dict[str, Any]:
    prices = {**PRICES, **(prices or {})}
    days = days or weeks * 7
    unknown = [c for c in contestants if c not in CONTESTANTS]
    if unknown:
        raise SystemExit(f"unknown contestant(s): {', '.join(unknown)}. Known: {', '.join(CONTESTANTS)}")
    prepared = []
    for s in seeds:
        p = prepare(s, profile, stores, days, trap_rate, force=force, quiet=quiet)
        prepared.append(p)
        print(f"seed {s}: {p['order_count']:,} orders, {len(p['findings'])} findings, {p['key_rows']} answer key rows "
              f"({p.get('prepare_seconds', 0)}s to prepare)", flush=True)
    per_seed: dict[str, list[dict[str, Any]]] = {}
    labels: dict[str, str] = {}
    for name in contestants:
        env = resolve_env(name, tuned_model)
        labels[name] = CONTESTANTS[name]["label"]
        rows = []
        for p in prepared:
            r = judge(name, p, env, prices, workers=workers, quiet=quiet)
            rows.append(r)
            print(f"  {name} on seed {p['seed']}: {r['status']}"
                  + (f", caught {r['caught']}/{r['total']}, {r['false_alarms']} false alarms, p50 {r['p50_s']}s"
                     if r["status"] == "ok" else f" ({r.get('note')})"), flush=True)
            if r["status"] == "skipped":
                break  # the same reason applies to every seed
        per_seed[name] = rows
    if human:
        if not human_scope:
            raise SystemExit("--human needs --human-scope SEED:STORE[:WEEK]")
        seed = int(human_scope.split(":")[0])
        p = next((x for x in prepared if x["seed"] == seed), None) or prepare(seed, profile, stores, days, trap_rate,
                                                                                 quiet=quiet)
        per_seed["human"] = [judge_human(Path(human), p, human_scope, human_minutes)]
        labels["human"] = per_seed["human"][0]["label"]
    run = {"run_id": "BM-" + uuid.uuid4().hex[:8], "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "config": {"seeds": seeds, "profile": prepared[0]["profile"] if prepared else profile,
                      "stores": prepared[0]["stores"] if prepared else stores, "days": days, "workers": workers,
                      "tuned_model": tuned_model, "prices": prices},
           "labels": labels, "per_seed": per_seed, "aggregate": {n: aggregate(rows) for n, rows in per_seed.items()}}
    if write:
        write_results(run)
    print_table(run)
    return run


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Flock benchmark: the same findings for every contestant, held-out seeds.")
    ap.add_argument("--seeds", default=DEFAULT_SEEDS, help="e.g. 101-110 or 101,105,200")
    ap.add_argument("--contestants", default="rules", help=f"comma list of: {', '.join(CONTESTANTS)}")
    ap.add_argument("--profile", default=DEFAULT_PROFILE, help=f"company profile: {', '.join(generator.list_profiles())}")
    ap.add_argument("--stores", type=int, default=None, help="default: the profile's store count")
    ap.add_argument("--weeks", type=int, default=2)
    ap.add_argument("--days", type=int, default=None, help="overrides --weeks")
    ap.add_argument("--trap-rate", type=float, default=None)
    ap.add_argument("--workers", type=int, default=1, help="parallel findings per contestant (1 = true per-call latency)")
    ap.add_argument("--tuned-model", default=None, help="model id the local server reports for the tuned judge")
    ap.add_argument("--price", action="append", default=[], help="model=in,out USD per 1M tokens; repeatable")
    ap.add_argument("--human", help="a teammate's sheet: order_id, outcome, amount")
    ap.add_argument("--human-scope", help="SEED:STORE[:WEEK] the sheet covers, e.g. 101:S07:1")
    ap.add_argument("--human-minutes", type=float, help="their timer, minutes for that store-week")
    ap.add_argument("--force", action="store_true", help="regenerate cached seeds")
    ap.add_argument("--verbose", action="store_true", help="show formatter and model chatter")
    ap.add_argument("--list", action="store_true", help="list contestants and what each needs")
    a = ap.parse_args(argv)
    if a.list:
        for name, c in CONTESTANTS.items():
            print(f"{name:<13} {c['label']:<36} offline={'yes' if c['offline'] else 'no':<4} needs: {c['needs']}")
        print(f"{'human':<13} {'A teammate, spreadsheet and timer':<36} offline=yes  needs: --human SHEET --human-scope SEED:STORE[:WEEK]")
        return
    prices = {}
    for spec in a.price:
        model_id, _, io = spec.partition("=")
        i, _, o = io.partition(",")
        prices[model_id] = (float(i), float(o))
    run_benchmark(parse_seeds(a.seeds), [c.strip() for c in a.contestants.split(",") if c.strip()], a.profile,
                  a.stores, a.weeks, a.days, a.trap_rate, a.workers, a.tuned_model, prices,
                  Path(a.human) if a.human else None, a.human_scope, a.human_minutes, a.force, quiet=not a.verbose)


if __name__ == "__main__":
    main()
