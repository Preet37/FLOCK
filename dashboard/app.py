"""Dashboard (owner: Ira). A small Flask server + one HTML page on 127.0.0.1.

  python -m dashboard.app              # http://127.0.0.1:5050
  python -m dashboard.app --port 8000

Reads through the Store protocol (flock.store.get_store, so FLOCK_STORE picks the backend).
It never writes a finding itself: approve / reject call flock.gatekeeper.transition() with the
person's Actor, and filing goes through Clicky, which also uses the gatekeeper.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, request, send_from_directory

from flock import market
from flock.agents import clicky, learner
from flock.gatekeeper import Denied, transition
from flock.models import TRANSITIONS, Actor
from flock.store import ROOT, Store, get_store

STATIC = Path(__file__).resolve().parent / "static"
STATES = ["NEW", "CHECKED", "JUDGED", "APPROVED", "REJECTED", "FILED", "RECOVERED"]
HUMAN_ACTIONS = ("approve", "reject")
DASHBOARD_ACTIONS = HUMAN_ACTIONS + ("recover",)  # recover = a person marks a filed dispute as paid


def _load_scoreboard():
    spec = importlib.util.spec_from_file_location("flock_scoreboard", ROOT / "scripts" / "score.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scoreboard = _load_scoreboard()


def needs_human(f: dict[str, Any]) -> bool:
    """Waiting on a person: judged, and either a conflict or a drafted dispute ready to approve."""
    return f["state"] == "JUDGED" and (f.get("outcome") == "conflict" or bool(f.get("dispute_draft")))


def summary(store: Store) -> dict[str, Any]:
    rows = store.all()
    log = store.log()
    rec = [f for f in rows if f.get("outcome") == "recoverable"]
    by_state = {s: 0 for s in STATES}
    for f in rows:
        by_state[f["state"]] = by_state.get(f["state"], 0) + 1
    by_outcome = {o: sum(1 for f in rows if f.get("outcome") == o) for o in ("correct", "recoverable", "conflict")}
    return {
        "findings": len(rows),
        "by_state": by_state,
        "by_outcome": by_outcome,
        "dollars_found": round(sum(abs(f["gap"]) for f in rec), 2),
        "dollars_filed": round(sum(abs(f["gap"]) for f in rec if f["state"] in ("FILED", "RECOVERED")), 2),
        "to_human": by_outcome["conflict"],
        "awaiting_human": sum(1 for f in rows if needs_human(f)),
        "log_entries": len(log),
        "denied": sum(1 for e in log if not e["allowed"]),
        "activity": list(reversed(log[-15:])),
    }


def portal_entry(ref: str | None) -> dict[str, Any] | None:
    if not ref or not clicky.PORTAL.exists():
        return None
    for line in clicky.PORTAL.read_text().splitlines():
        if line.strip() and json.loads(line).get("portal_ref") == ref:
            return json.loads(line)
    return None


def detail(store: Store, finding_id: str) -> dict[str, Any]:
    f = store.get(finding_id)
    log = store.log(finding_id)
    decided = [e for e in log if e["allowed"] and e["role"] == "human" and e["action"] in HUMAN_ACTIONS]
    rule_id = f["reason"].split()[3].rstrip(":") if f.get("reason", "").startswith("Applied approved rule") else None
    rule = next((r for r in learner.approved_rules() if r["rule_id"] == rule_id), None) if rule_id else None
    return {
        "finding": f,
        "log": log,
        "decision": decided[-1] if decided else None,
        "denied": [e for e in log if not e["allowed"]],
        "rule": rule,
        "filed": portal_entry(f.get("portal_ref")),
        "needs_human": needs_human(f),
    }


def create_app(store: Store | None = None, runs_path: Path | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["STORE"] = store if store is not None else get_store()
    app.config["RUNS_PATH"] = runs_path or scoreboard.RUNS

    def st() -> Store:
        return app.config["STORE"]

    def body() -> dict[str, Any]:
        return request.get_json(silent=True) or {}

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_file(name: str):
        return send_from_directory(STATIC, name)

    @app.get("/api/summary")
    def api_summary():
        return jsonify(summary(st()))

    @app.get("/api/findings")
    def api_findings():
        rows = sorted(st().all(), key=lambda f: f["finding_id"])
        return jsonify([{**f, "needs_human": needs_human(f)} for f in rows])

    @app.get("/api/queue")
    def api_queue():
        # conflicts first, ordered by the attention budget (value of a human looking); deferred ones last
        def order(f):
            att = f.get("attention") or {}
            return f.get("outcome") != "conflict", att.get("status") == "deferred", att.get("rank", 0), -abs(f["gap"])
        return jsonify(sorted((f for f in st().all() if needs_human(f)), key=order))

    @app.get("/api/market")
    def api_market():
        """Judge balances (derived from the append-only ledger), recent bets / settlements, attention budget."""
        return jsonify(market.snapshot(st()))

    @app.get("/api/findings/<finding_id>")
    def api_detail(finding_id: str):
        try:
            return jsonify(detail(st(), finding_id))
        except KeyError:
            abort(404, f"no finding {finding_id}")

    @app.post("/api/findings/<finding_id>/<action>")
    def api_decide(finding_id: str, action: str):
        """Approve, reject, or recover (the platform paid). The gatekeeper decides whether this actor may;
        denials are logged and return 403. Allowed moves settle the judge market's bets (in the gatekeeper)."""
        if action not in DASHBOARD_ACTIONS:
            return jsonify(error=f"the dashboard only does {' / '.join(DASHBOARD_ACTIONS)}"), 400
        b = body()
        name = str(b.get("actor") or "").strip()
        if not name:
            return jsonify(error="say who you are: 'actor' is required"), 400
        actor = Actor(id=name, role=str(b.get("role") or "human"))
        reason = str(b.get("reason") or "").strip() or "via the dashboard"
        try:
            after = transition(st(), finding_id, action, actor, reason=reason)
        except KeyError:
            return jsonify(error=f"no finding {finding_id}"), 404
        except Denied as e:
            return jsonify(error=str(e), log_entry=st().log(finding_id)[-1]), 403
        learner.propose(st())  # every human decision is a precedent; refresh proposed rules
        return jsonify(finding=after, log_entry=st().log(finding_id)[-1])

    @app.post("/api/file-approved")
    def api_file():
        """Hand APPROVED disputes to Clicky; each filing is a gatekeeper transition."""
        return jsonify(filed=clicky.file_approved(st()))

    @app.get("/api/rules")
    def api_rules():
        return jsonify(proposed=learner._read(learner._proposed_path(st())), approved=learner.approved_rules())

    @app.post("/api/rules/<rule_id>/approve")
    def api_rule_approve(rule_id: str):
        name = str(body().get("actor") or "").strip()
        if not name:
            return jsonify(error="say who you are: 'actor' is required"), 400
        try:
            rule = learner.approve(rule_id, approver=Actor(id=name, role="human"), store=st())
        except (KeyError, LookupError, StopIteration):
            return jsonify(error=f"no proposed rule {rule_id}"), 404
        learner.propose(st())
        return jsonify(rule=rule)

    @app.get("/api/score")
    def api_score():
        return jsonify(scoreboard.score(st(), verbose=False, runs_path=app.config["RUNS_PATH"]))

    @app.get("/api/meta")
    def api_meta():
        return jsonify(states=STATES, store=type(st()).__name__,
                       transitions=[{"from": s, "action": a, "to": t, "roles": sorted(r)}
                                    for (s, a), (t, r) in TRANSITIONS.items()])

    @app.errorhandler(404)
    def not_found(e):
        return jsonify(error=getattr(e, "description", "not found")), 404

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    create_app().run(host="127.0.0.1", port=a.port, debug=a.debug)
