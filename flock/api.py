"""Local JSON API so a UI can watch and drive Flock. Stdlib only, no new dependencies.

  python -m flock.api                 # serves on http://127.0.0.1:8787
  python -m flock.api --port 9000

Read:
  GET  /api/agents                    who the agents are, what each does, how often it acted
  GET  /api/findings                  every finding (newest id first)
  GET  /api/findings/<id>             one finding plus its audit trail
  GET  /api/log                       the whole audit log, including denied attempts
  GET  /api/rules                     approved rules plus the Learner's proposals
  GET  /api/score                     scoreboard vs data/answer_key.csv
  GET  /api/portal                    disputes Clicky filed in the sandbox portal

Write (humans only; every write still goes through the gatekeeper):
  POST /api/findings/<id>/approve     {"actor": "thabhelo", "reason": "..."}  then Clicky files it
  POST /api/findings/<id>/reject      {"actor": "thabhelo", "reason": "..."}
  POST /api/rules/<id>/approve        {"actor": "thabhelo"}
  POST /api/run                       {"reset": true, "forget": false, "demo_approve": false}

Hosting: set FLOCK_API_TOKEN and every POST must carry "Authorization: Bearer <token>" (reads stay open).
Set FLOCK_CORS_ORIGIN to the dashboard's origin to narrow CORS from "*". PORT and HOST are read from the env.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flock import pipeline
from flock.agents import clicky, learner
from flock.gatekeeper import Denied, transition
from flock.models import TRANSITIONS, Actor
from flock.store import ROOT, JsonStore

sys.path.insert(0, str(ROOT))
from scripts.score import score  # noqa: E402

# Roster for the UI. Order is pipeline order. "actor" matches the id written into the audit log.
AGENTS: list[dict[str, str]] = [
    {"actor": "collector", "name": "Collector", "owner": "Maja", "role": "agent",
     "job": "Pulls the raw statements, payouts, bank lines, contracts and emails into data/raw."},
    {"actor": "formatter", "name": "Formatter", "owner": "Maja", "role": "agent",
     "job": "Turns each platform's messy export into the standard tables in data/std."},
    {"actor": "matcher", "name": "Matcher", "owner": "Thabhelo", "role": "agent",
     "job": "Connects every register order to its payout line and bank deposit."},
    {"actor": "deal_keeper", "name": "Deal Keeper", "owner": "Thabhelo", "role": "agent",
     "job": "Reads contracts, amendments and emails into deal terms and ranks them by authority."},
    {"actor": "checkers", "name": "Checkers", "owner": "Thabhelo", "role": "agent",
     "job": "Compares what the deal says to what was paid. Plain Python does the money math."},
    {"actor": "panel", "name": "Panel", "owner": "Preet", "role": "agent",
     "job": "Three judges vote on each finding. Agreement decides alone; disagreement goes to a human."},
    {"actor": "ranker", "name": "Ranker", "owner": "Preet", "role": "agent",
     "job": "Scores recoverable findings by dollars times win odds minus effort, and sets the budget."},
    {"actor": "dispute_writer", "name": "Dispute Writer", "owner": "Preet", "role": "agent",
     "job": "Drafts the message to the platform. A human reads it before anything is sent."},
    {"actor": "gatekeeper", "name": "Gatekeeper", "owner": "Ira", "role": "gatekeeper",
     "job": "The one door. Checks every move against the state machine and writes the audit log."},
    {"actor": "clicky", "name": "Clicky", "owner": "Preet", "role": "agent",
     "job": "Files approved disputes in the sandbox portal. The gatekeeper records the filing."},
    {"actor": "learner", "name": "Learner", "owner": "Preet", "role": "agent",
     "job": "Turns repeated human decisions into proposed rules. Humans approve rules before they apply."},
]

JUDGES = [
    {"id": "judge.strict", "name": "Strict auditor", "stance": "Only calls recoverable when the evidence clearly proves the platform broke the deal."},
    {"id": "judge.operator", "name": "Operator", "stance": "Wants every dollar back, but will not fight a fair charge."},
    {"id": "judge.skeptic", "name": "Skeptic", "stance": "Looks for incomplete, ambiguous evidence or a source without authority."},
]

PORTAL = ROOT / "data" / "sandbox_portal" / "disputes.jsonl"
TOKEN = os.environ.get("FLOCK_API_TOKEN", "")
CORS_ORIGIN = os.environ.get("FLOCK_CORS_ORIGIN", "*")


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _captured(fn, *a, **kw) -> tuple[Any, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*a, **kw)
    return out, buf.getvalue()


class Api:
    def __init__(self) -> None:
        self.store = JsonStore()

    # reads -----------------------------------------------------------
    def agents(self) -> dict[str, Any]:
        log = self.store.log()
        counts: dict[str, int] = {}
        last: dict[str, str] = {}
        for e in log:
            counts[e["actor"]] = counts.get(e["actor"], 0) + 1
            last[e["actor"]] = e["at"]
        roster = [{**a, "actions": counts.get(a["actor"], 0), "last_at": last.get(a["actor"])} for a in AGENTS]
        humans = sorted({e["actor"] for e in log if e["role"] == "human"})
        return {"agents": roster, "judges": JUDGES, "humans": humans,
                "transitions": [{"from": s, "action": a, "to": t, "roles": sorted(r)} for (s, a), (t, r) in TRANSITIONS.items()]}

    def findings(self) -> list[dict[str, Any]]:
        return sorted(self.store.all(), key=lambda f: f["finding_id"])

    def finding(self, fid: str) -> dict[str, Any]:
        return {"finding": self.store.get(fid), "log": self.store.log(fid)}

    def rules(self) -> dict[str, Any]:
        return {"approved": learner.approved_rules(), "proposed": learner._read(learner._proposed_path(self.store))}

    def scoreboard(self) -> dict[str, Any]:
        result, text = _captured(score)
        return {**result, "text": text.strip()}

    # writes ----------------------------------------------------------
    def decide(self, fid: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        actor = Actor(id=str(body.get("actor") or "human"), role="human")
        after = transition(self.store, fid, action, actor, reason=str(body.get("reason") or f"{action}d from the dashboard"))
        filed = clicky.file_approved(self.store) if action == "approve" else []
        proposed = learner.propose(self.store)
        return {"finding": self.store.get(fid), "filed": filed, "proposed": proposed, "was": after["state"]}

    def approve_rule(self, rid: str, body: dict[str, Any]) -> dict[str, Any]:
        return learner.approve(rid, approver=Actor(id=str(body.get("actor") or "human"), role="human"), store=self.store)

    def run(self, body: dict[str, Any]) -> dict[str, Any]:
        _, text = _captured(pipeline.run, reset=bool(body.get("reset", True)),
                            demo_approve=bool(body.get("demo_approve", False)), forget=bool(body.get("forget", False)))
        return {"output": text.strip(), "findings": self.findings()}


class Handler(BaseHTTPRequestHandler):
    api = Api()

    def _send(self, code: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, {})

    def do_GET(self) -> None:  # noqa: N802
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        a = self.api
        try:
            if parts == ["api", "agents"]:
                return self._send(200, a.agents())
            if parts == ["api", "findings"]:
                return self._send(200, a.findings())
            if len(parts) == 3 and parts[:2] == ["api", "findings"]:
                return self._send(200, a.finding(parts[2]))
            if parts == ["api", "log"]:
                return self._send(200, a.store.log())
            if parts == ["api", "rules"]:
                return self._send(200, a.rules())
            if parts == ["api", "score"]:
                return self._send(200, a.scoreboard())
            if parts == ["api", "portal"]:
                return self._send(200, _jsonl(PORTAL))
            if parts in (["api"], []):
                return self._send(200, {"ok": True, "routes": [l.strip() for l in __doc__.splitlines() if l.strip().startswith(("GET", "POST"))]})
            self._send(404, {"error": "no such route"})
        except KeyError as e:
            self._send(404, {"error": f"not found: {e}"})

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_POST(self) -> None:  # noqa: N802
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        a = self.api
        if not self._authorized():
            return self._send(401, {"error": "This backend needs a token. Add it in the dashboard (Authorization: Bearer ...)."})
        try:
            body = self._body()
            if len(parts) == 4 and parts[:2] == ["api", "findings"] and parts[3] in ("approve", "reject"):
                return self._send(200, a.decide(parts[2], parts[3], body))
            if len(parts) == 4 and parts[:2] == ["api", "rules"] and parts[3] == "approve":
                return self._send(200, a.approve_rule(parts[2], body))
            if parts == ["api", "run"]:
                return self._send(200, a.run(body))
            self._send(404, {"error": "no such route"})
        except Denied as e:
            self._send(409, {"error": str(e)})
        except (KeyError, StopIteration) as e:
            self._send(404, {"error": f"not found: {e}"})
        except Exception as e:  # keep the dev server alive, tell the UI what broke
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"api  {self.command} {self.path}  {args[1] if len(args) > 1 else ''}\n")


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    print(f"Flock API on http://{host}:{port}/api  (ctrl-c to stop)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    a = ap.parse_args()
    serve(a.host, a.port)
