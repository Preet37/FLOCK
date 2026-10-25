"""Dashboard (owner: Ira). A small Flask server + one HTML page on 127.0.0.1.

  python -m dashboard.app              # http://127.0.0.1:5050
  python -m dashboard.app --port 8000

Reads through the Store protocol (flock.store.get_store, so FLOCK_STORE picks the backend).
It never writes a finding itself: approve / reject call flock.gatekeeper.transition() with the
person's Actor, and filing goes through Clicky, which also uses the gatekeeper.
Journal entries work the same way: approve / reject are gatekeeper transitions (machine=JOURNAL) with the
person's Actor, and "export" has the gatekeeper post every approved entry to a CSV in data/exports/.
The Flock view polls GET /api/agents?since=<cursor> (flock/agents_live.py folds the store's agent feed): only agents
that changed since the cursor come back, and past a few hundred per role they are pooled, so it stays light.
The Clicky view shows the workflow Clicky learned from a human's demonstration (flock/workflow.py): plain-English
steps with screenshots, which finding field feeds each, its state, and the recording. Approving it is a gatekeeper
transition (machine=workflow.WORKFLOW) with the person's Actor; only a human may.
The Lump sums view reads GET /api/payouts (flock/payouts.py): each platform deposit as a waterfall from gross to what hit
the bank, actual vs the deal's figure per deduction, with the findings inside it. The Theater view polls
GET /api/stream?since=<cursor> (flock/theater.py), or plays GET /api/stream/replay for the stage.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, request, send_from_directory
from flask.json.provider import DefaultJSONProvider

from flock import agents_live, frontend, llm, market, payouts, portal, theater, transactions, workflow
from flock.agents import clicky, journal, learner, panel
from flock.gatekeeper import Denied, transition
from flock.models import JE_TRANSITIONS, JOURNAL, TRANSITIONS, Actor
from flock.progress import progress_path, read_progress
from flock.pipeline import AIRGAP_NAME
from flock.store import ROOT, STATE_DIR, JournalStore, Store, get_journal_store, get_store

STATIC = Path(__file__).resolve().parent / "static"
STATES = ["NEW", "CHECKED", "JUDGED", "APPROVED", "REJECTED", "FILED", "RECOVERED"]
HUMAN_ACTIONS = ("approve", "reject")
DASHBOARD_ACTIONS = HUMAN_ACTIONS + ("recover",)  # recover = a person marks a filed dispute as paid
JE_STATES = ["DRAFT", "APPROVED", "REJECTED", "POSTED"]
DATA = ROOT / "data"


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
        "finding": frontend.with_risk(f),
        "log": log,
        "decision": decided[-1] if decided else None,
        "denied": [e for e in log if not e["allowed"]],
        "rule": rule,
        "filed": portal_entry(f.get("portal_ref")),
        "needs_human": needs_human(f),
        "agents": finding_agents(store, finding_id),
    }


def finding_agents(store: Store, finding_id: str) -> list[dict[str, Any]]:
    """The latest run's agents that worked on this finding (judge, investigator, writer), oldest first."""
    rows = agents_live.events(store)
    run = rows[-1].get("run") if rows else None
    mine = [e for e in rows if e.get("run") == run and e.get("subject") == finding_id]
    return sorted(agents_live.fold(mine).values(), key=lambda a: a.get("seq") or 0)


def je_brief(je: dict[str, Any]) -> dict[str, Any]:
    """A journal entry for the list view: everything but the per-line source rows."""
    disputed = sum(ln["debit"] for ln in je["lines"] if ln.get("finding_id") and ln["account_type"] == "asset")
    return {**{k: v for k, v in je.items() if k != "lines"}, "line_count": len(je["lines"]),
            "disputed": round(disputed, 2)}


def source_row(file: str, row: int) -> dict[str, Any] | None:
    """One raw row (header + values) from a CSV under data/, so a journal line's source can be opened."""
    path = (ROOT / file).resolve()
    if not path.is_relative_to(DATA.resolve()) or not path.is_file() or path.suffix.lower() != ".csv":
        return None
    with path.open(newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows or not (2 <= row <= len(rows)):
        return None
    return {"file": file, "row": row, "header": rows[0], "values": rows[row - 1]}


def airgap_status(store: Store) -> dict[str, Any]:
    """Air-gap evidence: the last pipeline run's model calls by host (data/state/airgap.json) and, for the
    dashboard's own environment, where each role would send its calls right now."""
    last = llm.read_airgap_report(Path(getattr(store, "dir", None) or STATE_DIR) / AIRGAP_NAME)
    roles = [(j, llm.route("judge", i)) for i, j in enumerate(panel.JUDGES)]
    roles += [(role, llm.route(role)) for role in ("reviewer", "writer", "mapper")]
    routes = []
    for role, r in roles:
        h = llm.host(r[0])
        routes.append({"role": role, "route": llm.label(r), "host": h,
                       "on_this_network": bool(h) and llm.on_this_network(h), "offline_reason": llm.offline_reason(r)})
    line = last["line"] if last else "no pipeline run recorded yet"
    return {"enforced": llm.airgap(), "last_run": last, "line": line, "routes": routes}


DEMO_FILE = re.compile(r"step-\d{2,3}\.png|video\.webm|trace\.zip")


def clicky_status(ws: workflow.WorkflowStore) -> dict[str, Any]:
    """The learned workflow for the Clicky view: steps in plain English, what feeds each, its state, the recording."""
    wf = ws.current()
    replay, stop = clicky.learned_status(ws)
    demo = None
    if wf and wf.get("learned_from"):
        d = workflow.demo_dir(wf["learned_from"])
        base = f"/api/clicky/demos/{wf['learned_from']}/"
        demo = {"demo_id": wf["learned_from"], "exists": d.exists(),
                "video": base + "video.webm" if (d / "video.webm").exists() else None,
                "trace": base + "trace.zip" if (d / "trace.zip").exists() else None,
                "trace_path": str(d / "trace.zip"), "base": base}
    history = [{k: w.get(k) for k in ("workflow_id", "version", "state", "learned_from", "learned_at", "portal_layout",
                                      "approved_by", "stale_reason")}
               for w in sorted(ws.all(), key=lambda w: w.get("version", 0), reverse=True)]
    return {
        "workflow": wf, "demo": demo, "history": history, "log": ws.log()[-40:],
        "uses": "learned" if replay else ("stopped" if stop else "hard-coded"), "stop": stop,
        "needs_demo": bool(wf and wf["state"] == "STALE"), "needs_approval": bool(wf and wf["state"] == "DRAFT"),
        "mode": os.environ.get("FLOCK_CLICKY_MODE", "file"), "layout": portal.layout_name(),
        "missing_required": workflow.missing_required(wf) if wf else [],
    }


class StrictJSON(DefaultJSONProvider):
    """jsonify that never writes NaN / Infinity (JSON.parse rejects them): non-finite floats become null."""

    def dumps(self, obj: Any, **kw: Any) -> str:
        try:
            return super().dumps(obj, allow_nan=False, **kw)
        except ValueError:
            return super().dumps(frontend.finite(obj), allow_nan=False, **kw)


def create_app(store: Store | None = None, runs_path: Path | None = None, journal_store: JournalStore | None = None,
               exports_dir: Path | None = None, workflow_store: workflow.WorkflowStore | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.json = StrictJSON(app)
    app.config["STORE"] = store if store is not None else get_store()
    app.config["RUNS_PATH"] = runs_path or scoreboard.runs_for(app.config["STORE"])  # a temp store's own history
    app.config["JOURNAL"] = journal_store if journal_store is not None else get_journal_store(app.config["STORE"])
    app.config["EXPORTS"] = exports_dir or journal.EXPORTS
    app.config["WORKFLOWS"] = workflow_store if workflow_store is not None else workflow.get_workflow_store(
        app.config["STORE"])

    def st() -> Store:
        return app.config["STORE"]

    def wfs() -> workflow.WorkflowStore:
        return app.config["WORKFLOWS"]

    def js() -> JournalStore:
        return app.config["JOURNAL"]

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
        return jsonify([{**frontend.with_risk(f), "needs_human": needs_human(f)} for f in rows])

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
        return jsonify(filed=clicky.file_approved(st(), wstore=wfs()))

    # Clicky's learned workflow -------------------------------------------------------
    @app.get("/api/clicky")
    def api_clicky():
        return jsonify(clicky_status(wfs()))

    @app.post("/api/clicky/<action>")
    def api_clicky_decide(action: str):
        """A person approves (or rejects) the learned workflow. The gatekeeper refuses anyone but a human, logs it
        with the workflow's hash, and refuses steps that changed since Clicky learned them."""
        if action not in HUMAN_ACTIONS:
            return jsonify(error=f"the dashboard only does {' / '.join(HUMAN_ACTIONS)}"), 400
        b = body()
        name = str(b.get("actor") or "").strip()
        if not name:
            return jsonify(error="say who you are: 'actor' is required"), 400
        actor = Actor(id=name, role=str(b.get("role") or "human"))
        wf = wfs().current()
        if wf is None:
            return jsonify(error="no learned workflow yet: python -m flock.agents.clicky record"), 404
        reason = str(b.get("reason") or "").strip() or "via the dashboard"
        try:
            if action == "approve":
                after = workflow.approve(wfs(), actor, wf["workflow_id"], reason=reason)
            else:
                after = transition(wfs(), wf["workflow_id"], "reject", actor, reason=reason,
                                   evidence=[{"workflow_hash": wf.get("hash")}], machine=workflow.WORKFLOW)
        except Denied as e:
            return jsonify(error=str(e), log_entry=wfs().log(wf["workflow_id"])[-1]), 403
        return jsonify(workflow=after, log_entry=wfs().log(wf["workflow_id"])[-1])

    @app.get("/api/clicky/demos/<demo_id>/<name>")
    def api_clicky_demo_file(demo_id: str, name: str):
        """A recording's screenshots, video and trace (data/demos/<demo_id>/), nothing else."""
        if not DEMO_FILE.fullmatch(name):
            abort(404, "not a recording file")
        try:
            d = workflow.demo_dir(demo_id)
        except ValueError:
            abort(404, "bad demo id")
        if not (d / name).is_file():
            abort(404, "no such recording file")
        return send_from_directory(d, name)

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

    @app.get("/api/progress")
    def api_progress():
        """What the pipeline / formatter / live demo is doing right now (flock.progress state file)."""
        return jsonify(read_progress(progress_path(st())))

    @app.get("/api/airgap")
    def api_airgap():
        """'air-gapped: N model calls, 0 left this network' for the last run, plus each role's route now."""
        return jsonify(airgap_status(st()))

    @app.get("/api/agents")
    def api_agents():
        """The live flock: counts per role, active agents, and agents changed since ?since=<cursor>."""
        try:
            since = int(request.args.get("since") or 0)
        except ValueError:
            return jsonify(error="since must be a number"), 400
        return jsonify(agents_live.snapshot(st(), since))

    # frontend read models (same as flock/api.py; see docs/API.md) ---------------------------------------
    @app.get("/api/transactions")
    def api_transactions():
        """One row per payout per store from the last real-data run (transactions.json), paged."""
        a = request.args
        try:
            limit, offset = int(a.get("limit") or 50), int(a.get("offset") or 0)
        except ValueError:
            return jsonify(error="limit and offset must be whole numbers"), 400
        return jsonify(transactions.query(transactions.load(frontend.state_dir(st())), st().all(),
                                          channel=a.get("channel") or None, store=a.get("store") or None,
                                          status=a.get("status") or None, limit=limit, offset=offset))

    @app.get("/api/payouts")
    def api_payouts():
        """One row per lump sum (platform x payout): expected net per contract vs what hit the bank, and what's owed."""
        return jsonify(payouts.listing(transactions.load(frontend.state_dir(st())), st().all(),
                                       channel=request.args.get("channel") or None,
                                       status=request.args.get("status") or None))

    @app.get("/api/payouts/<path:key>")
    def api_payout(key: str):
        try:
            return jsonify(payouts.detail(transactions.load(frontend.state_dir(st())), st().all(), key))
        except KeyError:
            abort(404, f"no payout {key}")

    @app.get("/api/stream")
    def api_stream():
        """The agent theater: events after ?since=<cursor> (flock/theater.py)."""
        try:
            since, limit = int(request.args.get("since") or 0), int(request.args.get("limit") or 200)
        except ValueError:
            return jsonify(error="since and limit must be whole numbers"), 400
        return jsonify(theater.for_store(st()).read(since=since, limit=limit))

    @app.get("/api/stream/replay")
    def api_stream_replay():
        """The last run's theater with stage pacing (?speed=N, &since= &start=, or &all=1)."""
        a = request.args
        if (a.get("run") or "last") != "last":
            return jsonify(error="run must be 'last'"), 400
        try:
            speed = float(a.get("speed") or 1)
            since = int(a.get("since") or 0)
            start = float(a["start"]) if a.get("start") else None
        except ValueError:
            return jsonify(error="speed and start must be numbers, since a whole number"), 400
        return jsonify(theater.replay(st(), speed=speed, since=since, start_ms=start,
                                      everything=a.get("all") in ("1", "true", "yes")))

    @app.get("/api/metrics")
    def api_metrics():
        """Agent vs human hours, accuracy, cost, money saved: each with value, formula, inputs, sources."""
        return jsonify(frontend.metrics(st(), js()))

    @app.get("/api/agents/<agent_id>")
    def api_agent(agent_id: str):
        try:
            return jsonify(frontend.agent_detail(st(), agent_id))
        except KeyError:
            abort(404, f"no agent {agent_id}")

    @app.get("/api/screens/<path:rest>")
    def api_screen(rest: str):
        """Clicky's screenshots only (data/screens, data/demos/<id>/<file>); traversal refused."""
        path = frontend.screen_file([p for p in rest.split("/") if p])
        if path is None:
            abort(404, "no such screenshot")
        return send_from_directory(path.parent, path.name)

    @app.get("/api/meta")
    def api_meta():
        gap = airgap_status(st())
        return jsonify(states=STATES, store=type(st()).__name__,
                       airgap={"enforced": gap["enforced"], "line": gap["line"]},
                       transitions=[{"from": s, "action": a, "to": t, "roles": sorted(r)}
                                    for (s, a), (t, r) in TRANSITIONS.items()],
                       journal_transitions=[{"from": s, "action": a, "to": t, "roles": sorted(r)}
                                            for (s, a), (t, r) in JE_TRANSITIONS.items()])

    # journal entries ----------------------------------------------------------------
    @app.get("/api/journal")
    def api_journal():
        rows = sorted(js().all(), key=lambda j: (j["store_id"], j.get("week") or "", j["channel"], j["payout_id"]))
        return jsonify(entries=[je_brief(j) for j in rows],
                       by_state={s: sum(1 for j in rows if j["state"] == s) for s in JE_STATES},
                       unbalanced=sum(1 for j in rows if not j["balanced"]),
                       debits=round(sum(j["total_debit"] for j in rows), 2),
                       credits=round(sum(j["total_credit"] for j in rows), 2))

    @app.get("/api/journal/<je_id>")
    def api_journal_detail(je_id: str):
        try:
            je = js().get(je_id)
        except KeyError:
            abort(404, f"no journal entry {je_id}")
        log = js().log(je_id)
        return jsonify(entry=je, log=log, denied=[e for e in log if not e["allowed"]])

    @app.post("/api/journal/export")
    def api_journal_export():
        """The gatekeeper posts every APPROVED entry: one CSV import file, each entry APPROVED -> POSTED."""
        return jsonify(journal.export_approved(js(), app.config["EXPORTS"]))

    @app.post("/api/journal/<je_id>/<action>")
    def api_journal_decide(je_id: str, action: str):
        """Approve or reject a drafted entry. Same rule as findings: the gatekeeper decides; denials are logged."""
        if action not in HUMAN_ACTIONS:
            return jsonify(error=f"the dashboard only does {' / '.join(HUMAN_ACTIONS)}"), 400
        b = body()
        name = str(b.get("actor") or "").strip()
        if not name:
            return jsonify(error="say who you are: 'actor' is required"), 400
        actor = Actor(id=name, role=str(b.get("role") or "human"))
        reason = str(b.get("reason") or "").strip() or "via the dashboard"
        try:
            after = transition(js(), je_id, action, actor, reason=reason, evidence=[], machine=JOURNAL)
        except KeyError:
            return jsonify(error=f"no journal entry {je_id}"), 404
        except Denied as e:
            return jsonify(error=str(e), log_entry=js().log(je_id)[-1]), 403
        return jsonify(entry=je_brief(after), log_entry=js().log(je_id)[-1])

    @app.get("/api/source")
    def api_source():
        """One raw CSV row under data/ (a journal line's source link)."""
        try:
            row = int(request.args.get("row", ""))
        except ValueError:
            return jsonify(error="row must be a number"), 400
        found = source_row(str(request.args.get("file", "")), row)
        if found is None:
            abort(404, "no such CSV row under data/")
        return jsonify(found)

    @app.errorhandler(404)
    def not_found(e):
        return jsonify(error=getattr(e, "description", "not found")), 404

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))  # 0.0.0.0 when hosted (Railway)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5050")))
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    create_app().run(host=a.host, port=a.port, debug=a.debug)
