"""Ping every model Flock is configured to use, and say who answered and how fast.

Use it to verify the GX10 once it's reachable over Tailscale:

    FLOCK_LOCAL_URL=http://gx10:8000/v1 FLOCK_LOCAL_MODEL=<served model> FLOCK_JUDGE_PROVIDERS=local \\
        python scripts/llm_check.py

By default it checks the routes the pipeline would use (3 judges, reviewer, writer, mapper; see flock/llm.py).
--provider local,groq pings those providers directly with their default models instead.
For a local server it also lists the models the server says it is serving (GET /models).
Exit code 1 if any configured route failed, so it works as a pre-demo check.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flock import llm  # noqa: E402
from flock.agents import panel  # noqa: E402

SYSTEM = "You are a health check. Reply with exactly the JSON object you are asked for."
PROMPT = 'Reply with {"ok": true}.'


def routes(providers: list[str] | None) -> dict[tuple[str, str], list[str]]:
    """(provider, model) -> the roles that use it, so a model shared by three judges is pinged once."""
    if providers:
        return {(p, llm.default_model(p)): [p] for p in providers}
    roles = [(j, llm.route("judge", i)) for i, j in enumerate(panel.JUDGES)]
    roles += [(role, llm.route(role)) for role in ("reviewer", "writer", "mapper")]
    out: dict[tuple[str, str], list[str]] = {}
    for role, r in roles:
        out.setdefault(r, []).append(role)
    return out


def served_models(provider: str) -> list[str] | str:
    req = urllib.request.Request(llm.base_url(provider) + "/models")
    key = os.environ.get(llm.OPENAI_COMPATIBLE[provider][2])
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=llm.timeout()) as resp:
            return [m.get("id", "?") for m in json.loads(resp.read().decode()).get("data", [])]
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:120]


def check(r: tuple[str, str]) -> dict:
    if not llm.available(r):
        key = "ANTHROPIC_API_KEY" if r[0] == "anthropic" else llm.OPENAI_COMPATIBLE.get(r[0], (None,) * 5)[2]
        why ="FLOCK_OFFLINE=1" if os.environ.get("FLOCK_OFFLINE") == "1" else (f"no {key}" if key else "offline")
        return {"status": "OFFLINE", "detail": f"{why}: this role uses offline rules"}
    start = time.perf_counter()
    try:
        text, answered_by = llm.complete(SYSTEM, PROMPT, 20, r)
    except Exception as e:
        return {"status": "FAIL", "seconds": time.perf_counter() - start, "detail": f"{type(e).__name__}: {e}"[:200]}
    ok = llm.parse_json_object(text) is not None
    return {"status": "OK" if ok else "ODD", "seconds": time.perf_counter() - start, "answered_by": answered_by,
            "detail": text[:80].replace("\n", " ")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--provider", help="comma list of providers to ping directly (local, groq, nvidia, anthropic)")
    args = ap.parse_args(argv)
    providers = [p.strip().lower() for p in args.provider.split(",") if p.strip()] if args.provider else None

    failed = False
    for r, roles in routes(providers).items():
        res = check(r)
        failed |= res["status"] == "FAIL"
        timing = f" {res['seconds']:.2f}s" if "seconds" in res else ""
        print(f"{res['status']:<8}{llm.label(r):<45}{timing:>8}  roles: {', '.join(roles)}")
        if res.get("answered_by"):
            print(f"        answered by: {res['answered_by']}")
        print(f"        {res['detail']}")
        if r[0] == "local" and res["status"] != "OFFLINE":
            print(f"        {llm.base_url('local')} serves: {served_models('local')}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
