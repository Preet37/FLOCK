"""Judge load test: push N findings through the three-judge panel (panel.decide) and time it.

    python scripts/judge_load.py --n 6 --concurrency 3                         # whatever FLOCK_JUDGE_* says
    python scripts/judge_load.py --provider local --models qwen2.5-coder:14b --n 6 --concurrency 3
    eval "$(scripts/gx10.sh env)"; python scripts/judge_load.py --n 60 --concurrency 8   # the GX10, 3 models
    python scripts/judge_load.py --source seed --seed 4821 --n 40              # findings from a fresh generated seed
    FLOCK_OFFLINE=1 python scripts/judge_load.py --n 100                       # the rules-only baseline

Findings come from fixtures/fake_findings.json + fixtures/adversarial_findings.json (--source mixed, the default;
or fake / adversarial), repeated with new ids to reach --n. --source seed runs flock.demo's generator, collector and
formatter + detection into a temp folder (temp copy of mappings.json; no committed file is touched) and takes the
findings it detects.

Each finding goes through the real panel.decide (round 1, a debate round on a split, reviewer memo / audit sample
when the reviewer is configured), --concurrency findings at a time (each runs its three judges in parallel).
Learned rules are not applied (rules=[]), so every finding reaches the judges.

Reports p50 / p95 seconds per finding, findings per minute, per-judge model + latency, offline fallbacks, and writes
results/judge_load_<provider>_<UTC timestamp>.json. The JSON is the latency / throughput input for
scripts/benchmark.py; its schema is docs/benchmark_schema.md. results/ is gitignored (raw dumps).
Exit code 1 if any judge fell back to offline rules or a finding raised.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flock import llm  # noqa: E402
from flock.agents import panel  # noqa: E402
from flock.models import Finding  # noqa: E402

SCHEMA = "flock.judge_load/1"
FIXTURES = {"fake": ROOT / "fixtures" / "fake_findings.json",
            "adversarial": ROOT / "fixtures" / "adversarial_findings.json"}
RESULTS = ROOT / "results"
DEFAULT_TIMEOUT = "120"  # a cold 30B model on first call takes longer than flock's default 30s


def percentile(xs: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in 0..100), the same as numpy's default."""
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 3)


def fixture_findings(source: str, n: int) -> list[dict[str, Any]]:
    names = ["fake", "adversarial"] if source == "mixed" else [source]
    base = [Finding(**row).to_dict() for name in names for row in json.loads(FIXTURES[name].read_text())]
    return repeat(base, n)


def repeat(base: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """The first n findings, cycling through base with suffixed ids (F-0001, ..., F-0001-r2) when n > len(base)."""
    if not base:
        raise SystemExit("no findings to judge")
    out = []
    for i in range(n):
        f = copy.deepcopy(base[i % len(base)])
        if i >= len(base):
            f["finding_id"] = f"{f['finding_id']}-r{i // len(base) + 1}"
        out.append(f)
    return out


def seed_findings(seed: int, n: int, stores: int, days: int, profile: str | None) -> list[dict[str, Any]]:
    """Generate -> collect -> format -> detect in a temp folder (flock.demo's pieces); nothing committed is written."""
    from flock import demo, pipeline
    from flock.agents import collector
    from flock.progress import NullProgress

    ws = Path(tempfile.mkdtemp(prefix="flock_judge_load_"))
    try:
        out, raw, std, mappings = ws / "out", ws / "raw", ws / "std", ws / "mappings.json"
        shutil.copy(ROOT / "mappings.json", mappings)
        prof = demo.generator.load_profile(profile or demo.generator.DEFAULT_PROFILE)
        demo.generator.generate(seed, stores, days, None, out, prof, None, NullProgress())
        collector.collect(out, raw)
        found = pipeline.detect_real(raw, std, mappings, confirm=True, interactive=False, progress=NullProgress())
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    found = [Finding(**f).to_dict() if not isinstance(f, dict) else f for f in found]
    print(f"seed {seed}: {len(found)} findings detected ({stores} stores x {days} days)", file=sys.stderr)
    return repeat(sorted(found, key=lambda f: -abs(f["gap"])), n)


class Timer:
    """Wraps panel.vote / panel.revise to time every judge call (decide() looks them up as module globals)."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._vote, self._revise = panel.vote, panel.revise

    def __enter__(self):
        def vote(judge, f):
            t = time.perf_counter()
            v = self._vote(judge, f)
            self._log(judge, 1, time.perf_counter() - t, v)
            return v

        def revise(judge, f, own, others):
            t = time.perf_counter()
            v = self._revise(judge, f, own, others)
            if v is not None:
                self._log(judge, 2, time.perf_counter() - t, v)
            return v

        panel.vote, panel.revise = vote, revise
        return self

    def __exit__(self, *exc):
        panel.vote, panel.revise = self._vote, self._revise

    def _log(self, judge: str, rnd: int, secs: float, v: dict[str, Any]) -> None:
        err = v.get("fallback") or v.get("debate_error")
        with self._lock:
            self.calls.append({"judge": judge, "round": rnd, "seconds": secs, "model": v.get("model") or "offline-rules",
                               "error": err})


def judge_routes() -> list[dict[str, Any]]:
    out = []
    for i, j in enumerate(panel.JUDGES):
        r = llm.route("judge", i)
        live = llm.available(r)
        h = llm.host(r[0]) if live else ""
        out.append({"judge": j, "route": llm.label(r) if live else "offline-rules", "provider": r[0] if live else "offline",
                    "host": h, "on_this_network": (not live) or llm.on_this_network(h),
                    "offline_reason": llm.offline_reason(r)})
    return out


def run(findings: list[dict[str, Any]], concurrency: int) -> dict[str, Any]:
    per_finding: list[dict[str, Any]] = []
    lock = threading.Lock()

    def one(f: dict[str, Any]) -> None:
        t = time.perf_counter()
        try:
            d, err = panel.decide(f, rules=[]), None
        except Exception as e:  # a crash is a result too; keep going
            d, err = {}, f"{type(e).__name__}: {e}"[:200]
        row = {"finding_id": f["finding_id"], "check_type": f["check_type"], "seconds": round(time.perf_counter() - t, 3),
               "outcome": d.get("outcome"), "confidence": d.get("confidence"),
               "debated": any(v.get("round") == 2 for v in d.get("votes", [])),
               "fallbacks": sum(1 for v in d.get("votes", []) if v.get("fallback")), "error": err}
        with lock:
            per_finding.append(row)

    llm.reset_calls()
    t0 = time.perf_counter()
    with Timer() as timer, ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, findings))
    wall = time.perf_counter() - t0
    return {"wall": wall, "per_finding": sorted(per_finding, key=lambda r: r["finding_id"]), "calls": timer.calls}


def summarize(res: dict[str, Any], routes: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    rows, calls, wall = res["per_finding"], res["calls"], res["wall"]
    secs = [r["seconds"] for r in rows]
    judges = []
    for rt in routes:
        mine = [c for c in calls if c["judge"] == rt["judge"]]
        lat = [c["seconds"] for c in mine]
        judges.append({"judge": rt["judge"], "model": rt["route"],
                       "answered_by": sorted({c["model"] for c in mine}), "calls": len(mine),
                       "p50": percentile(lat, 50), "p95": percentile(lat, 95),
                       "mean": round(sum(lat) / len(lat), 3) if lat else None,
                       "fallbacks": sum(1 for c in mine if c["error"])})
    errors = [c["error"] for c in calls if c["error"]] + [r["error"] for r in rows if r["error"]]
    live = [rt for rt in routes if rt["provider"] != "offline"]
    offline = all(rt["on_this_network"] for rt in routes)
    providers = sorted({rt["provider"] for rt in routes})
    models = sorted({rt["route"] for rt in routes})
    cost = 0.0 if offline else args.cost_per_1k
    outcomes: dict[str, int] = {}
    for r in rows:
        outcomes[str(r["outcome"])] = outcomes.get(str(r["outcome"]), 0) + 1
    return {
        "schema": SCHEMA,
        "contestant": args.contestant or ("rules-only" if not live else "+".join(models)),
        "models": models,
        "n": len(rows),
        "p50": percentile(secs, 50),
        "p95": percentile(secs, 95),
        "per_min": round(len(rows) / wall * 60, 2) if wall > 0 else None,
        "cost_per_1k": cost,
        "offline": offline,
        "errors": len(errors),
        # extras (benchmark.py may ignore these)
        "provider": "+".join(providers),
        "concurrency": args.concurrency,
        "source": args.source if args.source != "seed" else f"seed:{args.seed}",
        "wall_seconds": round(wall, 3),
        "mean": round(sum(secs) / len(secs), 3) if secs else None,
        "max": round(max(secs), 3) if secs else None,
        "judges": judges,
        "fallbacks": sum(r["fallbacks"] for r in rows),
        "debated": sum(1 for r in rows if r["debated"]),
        "outcomes": outcomes,
        "error_samples": sorted(set(errors))[:5],
        "model_calls": llm.airgap_report(),
        "timeout_seconds": llm.timeout(),
        "started_at": args.started_at,
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "findings": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=6, help="findings to judge (default 6)")
    ap.add_argument("--concurrency", type=int, default=3, help="findings in flight at once (default 3)")
    ap.add_argument("--source", choices=("mixed", "fake", "adversarial", "seed"), default="mixed")
    ap.add_argument("--seed", type=int, default=4821, help="--source seed: generator seed")
    ap.add_argument("--stores", type=int, default=4, help="--source seed: stores (default 4)")
    ap.add_argument("--days", type=int, default=14, help="--source seed: days (default 14)")
    ap.add_argument("--profile", default=None, help="--source seed: company profile")
    ap.add_argument("--provider", help="judge provider(s), sets FLOCK_JUDGE_PROVIDERS (e.g. local or local,local,groq)")
    ap.add_argument("--models", help="judge model(s), sets FLOCK_JUDGE_MODELS (one = all three judges)")
    ap.add_argument("--timeout", help=f"seconds per call, sets FLOCK_LLM_TIMEOUT (default: env or {DEFAULT_TIMEOUT})")
    ap.add_argument("--audit-rate", help="sets FLOCK_AUDIT_RATE (reviewer second opinions)")
    ap.add_argument("--contestant", help="name in the benchmark table (default: the judge models)")
    ap.add_argument("--cost-per-1k", type=float, default=None,
                    help="dollars per 1,000 findings for a hosted contestant (local is always 0)")
    ap.add_argument("--out", default=str(RESULTS), help="folder for the JSON (default results/)")
    args = ap.parse_args(argv)
    if args.provider:
        os.environ["FLOCK_JUDGE_PROVIDERS"] = args.provider
        os.environ.pop("FLOCK_OFFLINE", None)
    if args.models:
        if "," not in args.models:  # one model for every judge
            args.models = ",".join([args.models] * len(panel.JUDGES))
        os.environ["FLOCK_JUDGE_MODELS"] = args.models
    if args.timeout or not os.environ.get("FLOCK_LLM_TIMEOUT"):
        os.environ["FLOCK_LLM_TIMEOUT"] = args.timeout or DEFAULT_TIMEOUT
    if args.audit_rate is not None:
        os.environ["FLOCK_AUDIT_RATE"] = args.audit_rate
    args.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    findings = (seed_findings(args.seed, args.n, args.stores, args.days, args.profile) if args.source == "seed"
                else fixture_findings(args.source, args.n))
    routes = judge_routes()
    for rt in routes:
        why = f" ({rt['offline_reason']})" if rt["offline_reason"] else ""
        print(f"{rt['judge']:<16} {rt['route']}{why}")
    print(f"judging {len(findings)} findings ({args.source}), concurrency {args.concurrency}, "
          f"timeout {llm.timeout():.0f}s ...", flush=True)

    out = summarize(run(findings, args.concurrency), routes, args)

    print(f"\n{out['contestant']}: n={out['n']}  p50 {out['p50']}s  p95 {out['p95']}s  "
          f"{out['per_min']} findings/min  wall {out['wall_seconds']}s  offline={out['offline']}  "
          f"cost/1k={out['cost_per_1k']}  errors={out['errors']}")
    for j in out["judges"]:
        print(f"  {j['judge']:<16} {j['model']:<36} calls {j['calls']:>3}  p50 {j['p50']}s  p95 {j['p95']}s  "
              f"fallbacks {j['fallbacks']}")
    print(f"  outcomes {out['outcomes']}  debated {out['debated']}  fallbacks {out['fallbacks']}")
    print(f"  {out['model_calls']['line']}")
    for e in out["error_samples"]:
        print(f"  error: {e}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(args.out) / f"judge_load_{out['provider'].replace('+', '-')}_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    return 1 if out["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
