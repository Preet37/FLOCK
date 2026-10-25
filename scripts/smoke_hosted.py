"""Smoke test a running Flock API (local or the Railway deploy): every GET, its shape, and optionally a dataset run.

  python scripts/smoke_hosted.py https://web-production-812b4.up.railway.app
  python scripts/smoke_hosted.py http://127.0.0.1:8787 --token-file ~/.flock_token --run --stores 12 --weeks 2

Reads need no token. --run (needs --token-file, a file holding FLOCK_API_TOKEN) posts {"dataset": "demo", ...}, polls
GET /api/jobs/<id> until it finishes, then checks the GETs again. Also checks that a POST without the token gets 401
and that a path-traversal screenshot request gets 404. Prints a pass/fail table; exits 1 if anything failed.
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


def call(base: str, method: str, path: str, body: dict | None = None, token: str | None = None,
         timeout: float = 60) -> tuple[int, Any, float]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    ms = (time.monotonic() - t0) * 1000
    try:
        return code, json.loads(raw or b"null"), ms
    except ValueError:
        return code, raw, ms


def has(*keys: str) -> Callable[[Any], str | None]:
    def check(out: Any) -> str | None:
        if not isinstance(out, dict):
            return f"expected an object, got {type(out).__name__}"
        missing = [k for k in keys if k not in out]
        return f"missing {', '.join(missing)}" if missing else None
    return check


def findings_ok(out: Any) -> str | None:
    if not isinstance(out, list):
        return "expected a list"
    bad = [f.get("finding_id") for f in out if not (0 <= f.get("risk", -1) <= 100 and f.get("risk_label"))]
    return f"no risk on {bad[:3]}" if bad else None


def metrics_ok(out: Any) -> str | None:
    if not isinstance(out, dict) or [c.get("id") for c in out.get("cards", [])] != \
            ["agent_time", "accuracy", "cost", "money_saved"]:
        return "expected cards agent_time, accuracy, cost, money_saved"
    for c in out["cards"]:
        miss = [k for k in ("value", "unit", "formula", "inputs", "sources") if k not in c]
        if miss:
            return f"{c['id']} missing {', '.join(miss)}"
    return None


def transactions_ok(out: Any) -> str | None:
    err = has("total", "limit", "offset", "rows", "summary")(out)
    if err:
        return err
    if out["rows"] and not all({"txn_id", "status", "flags", "net", "deposit_status"} <= set(r) for r in out["rows"]):
        return "rows missing txn_id / status / flags / net / deposit_status"
    return None


GETS: list[tuple[str, Callable[[Any], str | None]]] = [
    ("/api", has("ok", "routes")),
    ("/api/health", has("status", "store", "counts", "last_run")),
    ("/api/agents", has("agents", "judges", "transitions")),
    ("/api/agents/clicky", has("activity", "has_screen", "screens")),
    ("/api/agents/panel", has("activity", "children")),
    ("/api/findings", findings_ok),
    ("/api/findings?sort=risk", findings_ok),
    ("/api/transactions?limit=5", transactions_ok),
    ("/api/metrics", metrics_ok),
    ("/api/log", lambda o: None if isinstance(o, list) else "expected a list"),
    ("/api/rules", has("approved", "proposed")),
    ("/api/score", has("caught", "total", "false_alarms")),
    ("/api/portal", lambda o: None if isinstance(o, list) else "expected a list"),
    ("/api/progress", has("status")),
    ("/api/jobs", has("running", "jobs")),
]


def check_gets(base: str, results: list[tuple[str, bool, str]]) -> None:
    for path, check in GETS:
        code, out, ms = call(base, "GET", path)
        err = None if code == 200 else f"HTTP {code}: {out}"
        err = err or check(out)
        results.append((f"GET {path}", err is None, err or f"{ms:.0f}ms"))
    code, out, _ = call(base, "GET", "/api/findings")
    if code == 200 and isinstance(out, list) and out:
        fid = out[0]["finding_id"]
        code, one, ms = call(base, "GET", f"/api/findings/{fid}")
        err = None if code == 200 else f"HTTP {code}"
        err = err or has("finding", "log")(one)
        results.append((f"GET /api/findings/{fid}", err is None, err or f"{ms:.0f}ms"))
    code, out, _ = call(base, "GET", "/api/screens/..%2f..%2fREADME.md")
    results.append(("GET /api/screens/<traversal> -> 404", code == 404, f"HTTP {code}"))
    code, out, _ = call(base, "GET", "/api/no-such-route")
    results.append(("GET unknown route -> JSON 404", code == 404 and isinstance(out, dict), f"HTTP {code}"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Smoke test a Flock API")
    ap.add_argument("url")
    ap.add_argument("--token-file", help="file holding FLOCK_API_TOKEN (needed for --run)")
    ap.add_argument("--run", action="store_true", help="trigger a dataset run and poll the job")
    ap.add_argument("--seed", type=int, default=2025)
    ap.add_argument("--profile", default="ca_bagel_chain")
    ap.add_argument("--stores", type=int, default=12)
    ap.add_argument("--weeks", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=600, help="seconds to wait for the job")
    a = ap.parse_args(argv)
    token = Path(a.token_file).expanduser().read_text().strip() if a.token_file else None
    results: list[tuple[str, bool, str]] = []

    check_gets(a.url, results)
    code, out, _ = call(a.url, "POST", "/api/run", {"dataset": "demo"})
    health = call(a.url, "GET", "/api/health")[1]
    if isinstance(health, dict) and health.get("auth_required_for_post"):
        results.append(("POST /api/run without token -> 401", code == 401, f"HTTP {code}"))
    else:
        results.append(("POST /api/run without token -> 401", False, "server has no FLOCK_API_TOKEN set"))

    if a.run:
        if not token:
            results.append(("POST /api/run dataset", False, "--run needs --token-file"))
        else:
            body = {"dataset": "demo", "seed": a.seed, "profile": a.profile, "stores": a.stores, "weeks": a.weeks}
            code, job, ms = call(a.url, "POST", "/api/run", body, token)
            ok = code == 202 and isinstance(job, dict) and job.get("job_id")
            results.append(("POST /api/run dataset -> 202 job", bool(ok), f"HTTP {code} {job if not ok else job['job_id']}"))
            if ok:
                end = time.monotonic() + a.timeout
                status, j = "running", {}
                while status == "running" and time.monotonic() < end:
                    time.sleep(2)
                    _, j, _ = call(a.url, "GET", f"/api/jobs/{job['job_id']}")
                    status = j.get("status", "?") if isinstance(j, dict) else "?"
                    prog = (j or {}).get("progress") or {}
                    print(f"  job {job['job_id']}: {status} {prog.get('stage') or ''} "
                          f"{prog.get('done') or ''}/{prog.get('total') or ''}", file=sys.stderr)
                score = (j.get("result") or {}).get("score") or {}
                results.append(("job finished", status == "done",
                                f"{status} in {j.get('seconds')}s; caught {score.get('caught')}/{score.get('total')}"
                                if status == "done" else str(j.get("error") or status)))
                code, t, _ = call(a.url, "GET", "/api/transactions?limit=1")
                results.append(("transactions after run", code == 200 and t.get("total", 0) > 0,
                                f"{t.get('total')} rows" if isinstance(t, dict) else f"HTTP {code}"))
                check_gets(a.url, results)

    w = max(len(r[0]) for r in results)
    print(f"{'check':<{w}}  result  detail")
    for name, ok, detail in results:
        print(f"{name:<{w}}  {'PASS' if ok else 'FAIL'}    {detail}")
    failed = sum(1 for r in results if not r[1])
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
