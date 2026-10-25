"""Local JSON API so a UI can watch and drive Flock. Stdlib only, no new dependencies. Full reference: docs/API.md.

  python -m flock.api                 # serves on http://127.0.0.1:8787
  python -m flock.api --port 9000

Read:
  GET  /api/health                    status, version, store backend, last run, counts (Railway health check)
  GET  /api/agents                    who the agents are, what each does, how often it acted
  GET  /api/agents/<agent_id>         one agent: activity timeline, subject, parent / children, case file, screens
  GET  /api/findings                  every finding with its risk score (?state= &outcome= &risk= &sort=risk)
  GET  /api/findings/<id>             one finding (with risk) plus its audit trail
  GET  /api/transactions              one row per payout per store (?channel= &store= &status= &limit= &offset=)
  GET  /api/payouts                   one row per lump sum (platform x payout): gross, deductions, net, expected net
                                      per contract, deposit, shortfall, owed, status (?channel= &status=)
  GET  /api/payouts/<payout_id>       one lump sum: waterfall, per-store contribution, the findings in it, what's owed
  GET  /api/stream?since=<cursor>     agent theater: ordered events (votes, bets, escalations, decisions, filings,
                                      rules) with a monotonically increasing cursor (&limit=)
  GET  /api/stream/sse?since=<cursor> the same as Server-Sent Events (&replay=1&speed=N streams the replay)
  GET  /api/stream/replay?run=last&speed=N   the last run's events with stage pacing (&since= &start=, or &all=1)
  GET  /api/metrics                   agent vs human hours, accuracy, cost, money saved (value, formula, sources)
  GET  /api/log                       the whole audit log, including denied attempts
  GET  /api/rules                     approved rules plus the Learner's proposals
  GET  /api/score                     scoreboard vs the run's answer key
  GET  /api/portal                    disputes Clicky filed in the sandbox portal
  GET  /api/progress                  what the pipeline is doing right now (stage, done/total)
  GET  /api/jobs                      background runs; GET /api/jobs/<id> for one (status, progress, output tail)
  GET  /api/screens/<file>            Clicky's screenshots (data/screens, data/demos/<id>/<file>), nothing else

Write (humans only; every write still goes through the gatekeeper):
  POST /api/findings/<id>/approve     {"actor": "thabhelo", "reason": "..."}  then Clicky files it
  POST /api/findings/<id>/reject      {"actor": "thabhelo", "reason": "..."}
  POST /api/findings/<id>/follow_up   the follow-up agent drafts a reminder for a FILED, unpaid claim (returns it)
  POST /api/findings/<id>/follow_up/approve   {"actor": "thabhelo", "reason": "...", "text": "edited (optional)"}
  POST /api/findings/<id>/follow_up/reject    {"actor": "thabhelo", "reason": "..."}
  POST /api/findings/<id>/follow_up/send      Clicky sends the approved follow-up to the sandbox portal (portal_ref)
  POST /api/rules/<id>/approve        {"actor": "thabhelo"}
  POST /api/run?wait=0                {"reset": true, "forget": false, "demo_approve": false}   fixtures, as a job
                                      (without ?wait=0: synchronous, for old callers; hosted, use the job)
  POST /api/run                       {"dataset": "demo", "seed": 2025, "profile": "ca_bagel_chain", "weeks": 2,
                                       "stores": 56}   generated data, background job (?wait=1 to block)

Hosting: set FLOCK_API_TOKEN and every POST must carry "Authorization: Bearer <token>" (reads stay open).
Set FLOCK_CORS_ORIGIN to the dashboard's origin to narrow CORS from "*". PORT and HOST are read from the env.
One run at a time: a second POST /api/run (or a decision while a run is going) gets 409. Errors are always JSON.
"""
from __future__ import annotations

import argparse
import contextlib
import hmac
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from flock import frontend, payouts, pipeline, theater, transactions
from flock.agents import clicky, learner
from flock.agents import follow_up as follow_ups
from flock.frontend import AGENTS, JUDGES  # noqa: F401  (the roster lives in flock/frontend.py now)
from flock.gatekeeper import Denied, transition
from flock.models import TRANSITIONS, Actor
from flock.progress import progress_path, read_progress
from flock.store import ROOT, JsonStore, Store, get_journal_store

sys.path.insert(0, str(ROOT))
from scripts.score import score  # noqa: E402

PORTAL = ROOT / "data" / "sandbox_portal" / "disputes.jsonl"
TOKEN = os.environ.get("FLOCK_API_TOKEN", "")
CORS_ORIGIN = os.environ.get("FLOCK_CORS_ORIGIN", "*")
MAX_STORES = int(os.environ.get("FLOCK_HOSTED_MAX_STORES", "56"))   # caps for POST /api/run {"dataset": "demo"}
MAX_WEEKS = int(os.environ.get("FLOCK_HOSTED_MAX_WEEKS", "8"))
SSE_POLL, SSE_HEARTBEAT, SSE_MAX = 0.5, 15.0, 600.0   # seconds, for GET /api/stream/sse
OUTPUT_LINES = 400       # a job keeps this many output lines; GET /api/jobs/<id> shows the tail
TAIL = 40


class BadRequest(ValueError):
    """The request is wrong (400), as opposed to a bug (500)."""


class Busy(RuntimeError):
    """A run is already going (409)."""


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().split("\n")[:-1]:   # the last piece is "" or a line still being written
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


class ThreadStdout(io.TextIOBase):
    """sys.stdout for a threaded server: writes go to the calling thread's capture buffer if it has one, else to the
    real stdout. Installed once (install()); a capture never swaps sys.stdout, so concurrent requests and the
    background job each keep their own output and server logs keep reaching the terminal. Threads a job starts
    itself (e.g. the judge pool) have no buffer of their own, so their prints go to the real stdout."""

    def __init__(self, real: Any):
        self.real, self._local = real, threading.local()

    def _target(self) -> Any:
        return getattr(self._local, "buf", None) or self.real

    def write(self, s: str) -> int:
        return self._target().write(s)

    def flush(self) -> None:
        t = self._target()
        if hasattr(t, "flush"):
            t.flush()

    def isatty(self) -> bool:
        t = self._target()
        return bool(getattr(t, "isatty", lambda: False)())

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self.real, "encoding", "utf-8")

    @contextlib.contextmanager
    def capture(self, buf: Any):
        """Route this thread's prints to buf for the duration (nests; restores the previous buffer)."""
        prev = getattr(self._local, "buf", None)
        self._local.buf = buf
        try:
            yield buf
        finally:
            self._local.buf = prev


_stdout_lock = threading.Lock()


def install_stdout() -> ThreadStdout:
    """Put the ThreadStdout proxy on sys.stdout (idempotent). serve() calls it at startup; captures call it too, in
    case sys.stdout was replaced since (pytest's capture does that), keeping the replacement as the fallback."""
    with _stdout_lock:
        cur = sys.stdout
        if isinstance(cur, ThreadStdout):
            return cur
        proxy = ThreadStdout(cur)
        sys.stdout = proxy
        return proxy


def _captured(fn, *a, **kw) -> tuple[Any, str]:
    buf = io.StringIO()
    with install_stdout().capture(buf):
        out = fn(*a, **kw)
    return out, buf.getvalue()


def dumps(payload: Any) -> bytes:
    """Strict JSON for a response: allow_nan=False, and any NaN / inf is sent as null instead of failing."""
    try:
        return json.dumps(payload, default=str, allow_nan=False).encode()
    except ValueError:
        return json.dumps(frontend.finite(payload), default=str, allow_nan=False).encode()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _version() -> str | None:
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_SHA")
    if sha:
        return sha[:12]
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                             text=True, timeout=2)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


VERSION = _version()


class _Lines(io.TextIOBase):
    """stdout for a background job: keeps the last OUTPUT_LINES lines so GET /api/jobs/<id> can show them live."""

    def __init__(self, job: dict[str, Any]):
        self.job, self.partial = job, ""

    def write(self, s: str) -> int:
        text = self.partial + s
        *done, self.partial = text.split("\n")
        if done:
            out = self.job["output"]
            out.extend(done)
            del out[:-OUTPUT_LINES]
        return len(s)


def _int(q: dict[str, list[str]], name: str, default: int) -> int:
    v = (q.get(name) or [""])[0].strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        raise BadRequest(f"{name} must be a whole number") from None


def _float(q: dict[str, list[str]], name: str, default: float | None) -> float | None:
    v = (q.get(name) or [""])[0].strip()
    if not v:
        return default
    try:
        x = float(v)
    except ValueError:
        raise BadRequest(f"{name} must be a number") from None
    if x != x or x in (float("inf"), float("-inf")):
        raise BadRequest(f"{name} must be a finite number")
    return x


def _arg(q: dict[str, list[str]], name: str) -> str | None:
    v = (q.get(name) or [""])[0].strip()
    return v or None


class Api:
    def __init__(self, store: Store | None = None, portal: Path | None = None, runs_path: Path | None = None) -> None:
        self.store = store if store is not None else JsonStore()
        self.dir = frontend.state_dir(self.store)
        self.portal = portal
        self.runs_path = runs_path or self.dir / "runs.jsonl"
        self.jobs: dict[str, dict[str, Any]] = {}
        self.run_lock = threading.Lock()     # one run at a time (a run resets the store)
        self.write_lock = threading.RLock()  # human decisions and runs never interleave
        self.running: str | None = None

    # reads -----------------------------------------------------------
    def health(self) -> dict[str, Any]:
        run = frontend.last_run(self.store)
        rows = self.store.all()
        doc = transactions.load(self.dir)
        return {"status": "ok", "version": VERSION, "store": type(self.store).__name__, "state_dir": self.dir.name,
                "at": _now(), "auth_required_for_post": bool(TOKEN),
                "last_run": {k: run.get(k) for k in ("run_id", "label", "source", "finished_at", "seconds")}
                if run else None,
                "counts": {"findings": len(rows), "needs_human": sum(1 for f in rows if _needs_human(f)),
                           "log_entries": len(self.store.log()),
                           "transactions": len(doc["rows"]) if doc else 0},
                "running_job": self.running}

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

    def agent(self, agent_id: str) -> dict[str, Any]:
        return frontend.agent_detail(self.store, agent_id)

    def findings(self, q: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
        q = q or {}
        rows = [{**frontend.list_item(f), "needs_human": _needs_human(f)} for f in self.store.all()]
        for name in ("state", "outcome", "channel", "check_type"):
            want = _arg(q, name)
            if want:
                rows = [f for f in rows if f.get(name) == want]
        want = _arg(q, "risk")
        if want:
            rows = [f for f in rows if f["risk_label"] == want]
        if _arg(q, "sort") == "risk":
            return sorted(rows, key=lambda f: (-f["risk"], f["finding_id"]))
        return sorted(rows, key=default_order)

    def finding(self, fid: str) -> dict[str, Any]:
        f = self.store.get(fid)
        return {"finding": {**frontend.with_risk(f), "needs_human": _needs_human(f),
                            "orders_label": frontend.orders_label(f)},
                "follow_up": f.get("follow_up"), "log": self.store.log(fid), "risk_formula": frontend.RISK_FORMULA}

    def transactions(self, q: dict[str, list[str]]) -> dict[str, Any]:
        return transactions.query(transactions.load(self.dir), self.store.all(), channel=_arg(q, "channel"),
                                  store=_arg(q, "store"), status=_arg(q, "status"), limit=_int(q, "limit", 50),
                                  offset=_int(q, "offset", 0))

    def payouts(self, q: dict[str, list[str]]) -> dict[str, Any]:
        return payouts.listing(transactions.load(self.dir), self.store.all(), channel=_arg(q, "channel"),
                               status=_arg(q, "status"))

    def payout(self, key: str) -> dict[str, Any]:
        return payouts.detail(transactions.load(self.dir), self.store.all(), key)

    def stream(self, q: dict[str, list[str]]) -> dict[str, Any]:
        return theater.for_store(self.store).read(since=_int(q, "since", 0), limit=_int(q, "limit", 200))

    def replay(self, q: dict[str, list[str]], now_ms: float | None = None) -> dict[str, Any]:
        run = _arg(q, "run") or "last"
        if run != "last":
            raise BadRequest("run must be 'last' (the theater replays the run in this server's state)")
        return theater.replay(self.store, speed=_float(q, "speed", 1.0), since=_int(q, "since", 0),
                              start_ms=_float(q, "start", None), now_ms=now_ms,
                              everything=_arg(q, "all") in ("1", "true", "yes"), limit=_int(q, "limit", 200))

    def metrics(self) -> dict[str, Any]:
        return frontend.metrics(self.store, get_journal_store(self.store))

    def rules(self) -> dict[str, Any]:
        return {"approved": learner.approved_rules(), "proposed": learner._read(learner._proposed_path(self.store))}

    def scoreboard(self) -> dict[str, Any]:
        result, text = _captured(score, self.store, runs_path=self.runs_path)
        return {**result, "text": text.strip()}

    def job(self, job_id: str) -> dict[str, Any]:
        j = self.jobs[job_id]
        out = {k: v for k, v in j.items() if k != "output"}
        out["output_tail"] = j["output"][-TAIL:]
        if j["status"] == "running":
            out["progress"] = read_progress(progress_path(self.store))
        return out

    # writes ----------------------------------------------------------
    def _not_running(self) -> None:
        if self.running:
            raise Busy(f"a run is in progress ({self.running}); try again when GET /api/jobs/{self.running} is done")

    def decide(self, fid: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        self._not_running()
        with self.write_lock:
            actor = Actor(id=str(body.get("actor") or "human"), role="human")
            after = transition(self.store, fid, action, actor,
                               reason=str(body.get("reason") or f"{action}d from the dashboard"))
            entry = self.store.log(fid)[-1]   # the human's decision, before Clicky's filing is logged
            filed = clicky.file_approved(self.store, self.portal) if action == "approve" else []
            proposed = learner.propose(self.store)
            return {"finding": frontend.with_risk(self.store.get(fid)), "filed": filed, "proposed": proposed,
                    "was": after["state"], "log_entry": entry}

    def follow_up(self, fid: str, step: str, body: dict[str, Any]) -> dict[str, Any]:
        """Vendor follow-up on a FILED, unpaid claim: draft (the follow_up agent), approve / reject (a human), send
        (the gatekeeper, via Clicky). Every step is a gatekeeper annotation; refusals are logged and raise Denied."""
        self._not_running()
        with self.write_lock:
            ref = None
            if step == "draft":
                follow_ups.draft(self.store, [fid])
            elif step == "send":
                ref = follow_ups.send(self.store, fid, self.portal)
            else:
                actor = _human(body)
                reason = str(body.get("reason") or "")
                if step == "approve":
                    follow_ups.approve(self.store, fid, actor, text=body.get("text") or None,
                                       reason=reason or "Follow-up approved from the dashboard")
                else:
                    follow_ups.reject(self.store, fid, actor, reason=reason or "Follow-up rejected from the dashboard")
            f = self.store.get(fid)
            return {"finding": frontend.with_risk(f), "follow_up": f.get("follow_up"), "portal_ref": ref,
                    "log_entry": self.store.log(fid)[-1]}

    def approve_rule(self, rid: str, body: dict[str, Any]) -> dict[str, Any]:
        self._not_running()
        with self.write_lock:
            return learner.approve(rid, approver=Actor(id=str(body.get("actor") or "human"), role="human"),
                                   store=self.store)

    def run(self, body: dict[str, Any], wait: bool | None = None) -> tuple[int, dict[str, Any]]:
        """Fixtures (default): synchronous, as before. dataset=demo: a background job unless wait. (status, payload)."""
        dataset = body.get("dataset") or "fixtures"
        if dataset == "fixtures":
            if wait is False:
                return 202, self.start("fixtures", {k: body.get(k) for k in ("reset", "forget", "demo_approve")},
                                       lambda: self._fixtures(body))
            return 200, self._exclusive(lambda: self._fixtures(body))
        if dataset != "demo":
            raise BadRequest(f"unknown dataset {dataset!r}: use 'fixtures' (default) or 'demo'")
        params = self.dataset_params(body)
        if wait:
            return 200, self._exclusive(lambda: self._dataset(params))
        return 202, self.start("demo", params, lambda: self._dataset(params))

    def _fixtures(self, body: dict[str, Any]) -> dict[str, Any]:
        _, text = _captured(pipeline.run, reset=bool(body.get("reset", True)), store=self.store, portal=self.portal,
                            demo_approve=bool(body.get("demo_approve", False)), forget=bool(body.get("forget", False)),
                            runs_path=self.runs_path)
        return {"output": text.strip(), "findings": self.findings()}

    @staticmethod
    def dataset_params(body: dict[str, Any]) -> dict[str, Any]:
        """Validate a dataset run against the hosted caps. Raises BadRequest."""
        from flock import demo
        profiles = demo.generator.list_profiles()

        def whole(name: str, default: int, lo: int, hi: int) -> int:
            v = body.get(name, default)
            if isinstance(v, bool) or not isinstance(v, (int, str)) or not str(v).strip().lstrip("-").isdigit():
                raise BadRequest(f"{name} must be a whole number")
            v = int(v)
            if not lo <= v <= hi:
                raise BadRequest(f"{name} must be between {lo} and {hi} on this server (got {v})")
            return v

        profile = str(body.get("profile") or "ca_bagel_chain")
        if profile not in profiles:
            raise BadRequest(f"unknown profile {profile!r}: one of {', '.join(profiles)}")
        prof = demo.generator.load_profile(profile)
        return {"seed": whole("seed", 2025, 0, 2**31 - 1), "profile": profile,
                "weeks": whole("weeks", 2, 1, MAX_WEEKS),
                "stores": whole("stores", min(prof["default_stores"], MAX_STORES), 1, MAX_STORES),
                "demo_approve": bool(body.get("demo_approve", True)), "forget": bool(body.get("forget", False))}

    def _dataset(self, p: dict[str, Any]) -> dict[str, Any]:
        """flock.demo's generate -> collect -> format -> pipeline into this server's state, kept (keep=True), with the
        generated answer key left next to the state so /api/score and /api/metrics use it. The unseen layout is
        auto-confirmed (confirm=True): there is no person at a terminal to answer y/N on the hosted box."""
        from flock import demo
        r = demo.run_demo(p["seed"], p["profile"], weeks=p["weeks"], stores=p["stores"], keep=True, confirm=True,
                          interactive=False, approve=p["demo_approve"], forget=p["forget"], store=self.store,
                          portal=self.portal)
        return {"params": p, "auto_confirmed_unseen_layout": True,
                "score": {k: r.get(k) for k in ("caught", "total", "missed", "false_alarms", "recovered", "to_human",
                                                "orders", "seconds")},
                "findings": len(self.store.all()),
                "transactions": (transactions.load(self.dir) or {}).get("data")}

    def _exclusive(self, fn) -> dict[str, Any]:
        if not self.run_lock.acquire(blocking=False):
            raise Busy(f"a run is in progress ({self.running or 'synchronous'})")
        try:
            self.running = self.running or "sync"
            with self.write_lock:
                return fn()
        finally:
            self.running = None
            self.run_lock.release()

    def start(self, kind: str, params: dict[str, Any], fn) -> dict[str, Any]:
        """Run fn in a background thread as a job. Busy if one is going."""
        if not self.run_lock.acquire(blocking=False):
            raise Busy(f"a run is in progress ({self.running}); poll GET /api/jobs/{self.running}")
        job_id = "JOB-" + uuid.uuid4().hex[:8]
        job = {"job_id": job_id, "kind": kind, "params": params, "status": "running", "created_at": _now(),
               "started_at": _now(), "finished_at": None, "seconds": None, "result": None, "error": None,
               "output": [], "poll": f"/api/jobs/{job_id}"}
        self.jobs[job_id] = job
        self.running = job_id

        def work() -> None:
            t0 = time.monotonic()
            try:
                with self.write_lock, install_stdout().capture(_Lines(job)):
                    job["result"] = fn()
                job["status"] = "done"
            except Exception as e:  # the job reports it; the server stays up
                job["status"], job["error"] = "failed", f"{type(e).__name__}: {e}"
            finally:
                job["finished_at"], job["seconds"] = _now(), round(time.monotonic() - t0, 2)
                self.running = None
                self.run_lock.release()

        threading.Thread(target=work, name=job_id, daemon=True).start()
        return {k: v for k, v in job.items() if k != "output"}


# default /api/findings order: what waits on a person first, then by how far along it is, riskiest first in each group
STATE_PRIORITY = {"JUDGED": 1, "APPROVED": 2, "FILED": 3, "CHECKED": 4, "NEW": 5, "RECOVERED": 6, "REJECTED": 7}


def default_order(f: dict[str, Any]) -> tuple:
    """needs_human first, then state (JUDGED, APPROVED, FILED, CHECKED, NEW, RECOVERED, REJECTED), then risk
    (highest first), then finding_id so ties are stable."""
    return (not f.get("needs_human"), STATE_PRIORITY.get(f.get("state"), 9), -f.get("risk", 0), f["finding_id"])


AGENT_IDS = {a["actor"] for a in AGENTS if a["role"] != "human"}


def _human(body: dict[str, Any]) -> Actor:
    """The person deciding. An agent's own id is not a person: it goes to the gatekeeper as role agent (refused, logged)."""
    name = str(body.get("actor") or "human")
    return Actor(id=name, role="agent" if name in AGENT_IDS else "human")


def _needs_human(f: dict[str, Any]) -> bool:
    return f["state"] == "JUDGED" and (f.get("outcome") == "conflict" or bool(f.get("dispute_draft")))


class Handler(BaseHTTPRequestHandler):
    api = Api()

    def _headers(self, code: int, ctype: str, length: int) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        ms = (time.monotonic() - getattr(self, "_t0", time.monotonic())) * 1000
        sys.stderr.write(f"api  {self.command} {self.path}  {code}  {ms:.0f}ms\n")

    def _send(self, code: int, payload: Any) -> None:
        data = dumps(payload)
        self._headers(code, "application/json", len(data))
        self.wfile.write(data)

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        self._headers(200, frontend.SCREEN_TYPES[path.suffix.lower()], len(data))
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise BadRequest("the body must be JSON") from None
        if not isinstance(body, dict):
            raise BadRequest("the body must be a JSON object")
        return body

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._t0 = time.monotonic()
        self._headers(204, "application/json", 0)   # a 204 has no body

    def do_GET(self) -> None:  # noqa: N802
        self._t0 = time.monotonic()
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = parse_qs(url.query)
        a = self.api
        try:
            if parts == ["api", "health"]:
                return self._send(200, a.health())
            if parts == ["api", "agents"]:
                return self._send(200, a.agents())
            if len(parts) == 3 and parts[:2] == ["api", "agents"]:
                return self._send(200, a.agent(parts[2]))
            if parts == ["api", "findings"]:
                return self._send(200, a.findings(q))
            if len(parts) == 3 and parts[:2] == ["api", "findings"]:
                return self._send(200, a.finding(parts[2]))
            if parts == ["api", "transactions"]:
                return self._send(200, a.transactions(q))
            if parts == ["api", "payouts"]:
                return self._send(200, a.payouts(q))
            if len(parts) == 3 and parts[:2] == ["api", "payouts"]:
                return self._send(200, a.payout(unquote(parts[2])))
            if parts == ["api", "stream"]:
                return self._send(200, a.stream(q))
            if parts == ["api", "stream", "replay"]:
                return self._send(200, a.replay(q))
            if parts == ["api", "stream", "sse"]:
                return self._sse(q)
            if parts == ["api", "metrics"]:
                return self._send(200, a.metrics())
            if parts == ["api", "log"]:
                return self._send(200, a.store.log())
            if parts == ["api", "rules"]:
                return self._send(200, a.rules())
            if parts == ["api", "score"]:
                return self._send(200, a.scoreboard())
            if parts == ["api", "portal"]:
                return self._send(200, _jsonl(Path(a.portal or PORTAL)))
            if parts == ["api", "progress"]:
                return self._send(200, read_progress(progress_path(a.store)))
            if parts == ["api", "jobs"]:
                return self._send(200, {"running": a.running, "jobs": [a.job(j) for j in reversed(list(a.jobs))]})
            if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                return self._send(200, a.job(parts[2]))
            if len(parts) >= 3 and parts[:2] == ["api", "screens"]:
                path = frontend.screen_file(parts[2:])
                if path is None:
                    return self._send(404, {"error": "no such screenshot (only data/screens and data/demos files)"})
                return self._send_file(path)
            if parts in (["api"], []):
                return self._send(200, {"ok": True, "docs": "docs/API.md",
                                        "routes": [l.strip() for l in __doc__.splitlines() if l.strip().startswith(("GET", "POST"))]})
            self._send(404, {"error": "no such route"})
        except BadRequest as e:
            self._send(400, {"error": str(e)})
        except KeyError as e:
            self._send(404, {"error": f"not found: {e}"})
        except Exception as e:  # never an HTML traceback: tell the UI what broke
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _sse(self, q: dict[str, list[str]]) -> None:
        """GET /api/stream/sse: the theater as Server-Sent Events (id = cursor, event = type, data = the event). Live by
        default: checks the files every SSE_POLL seconds, a comment every SSE_HEARTBEAT seconds keeps proxies open, and
        it closes after SSE_MAX seconds (EventSource reconnects with Last-Event-ID). &replay=1 plays the last run at
        &speed=N, then sends "done" and closes. A proxy that buffers defeats SSE: then poll GET /api/stream."""
        a = self.api
        replay = _arg(q, "replay") in ("1", "true", "yes")
        since = _int(q, "since", 0) or _int({"h": [self.headers.get("Last-Event-ID") or ""]}, "h", 0)
        speed = _float(q, "speed", 1.0)
        self.send_response(200)
        for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-cache"), ("Connection", "close"),
                     ("X-Accel-Buffering", "no"), ("Access-Control-Allow-Origin", CORS_ORIGIN), ("Vary", "Origin")):
            self.send_header(k, v)
        self.end_headers()
        sys.stderr.write(f"api  GET {self.path}  200  sse\n")
        self.close_connection = True

        def send(name: str, payload: Any, cursor: int | None = None) -> None:
            head = (f"id: {cursor}\n" if cursor is not None else "") + f"event: {name}\ndata: "
            self.wfile.write(head.encode() + dumps(payload) + b"\n\n")
            self.wfile.flush()

        try:
            if replay:
                t0 = time.monotonic()
                for ev in a.replay({"all": ["1"], "speed": [str(speed)]})["events"]:
                    if ev["cursor"] <= since:
                        continue
                    wait = ev["t_ms"] / 1000 - (time.monotonic() - t0)
                    if wait > 0:
                        time.sleep(wait)
                    send(ev["type"], ev, ev["cursor"])
                send("done", {"mode": "replay"})
                return
            end, beat = time.monotonic() + SSE_MAX, time.monotonic()
            while time.monotonic() < end:
                page = a.stream({"since": [str(since)], "limit": ["500"]})
                if page["reset"] and page["events"]:
                    send("reset", {"run": page["run"], "first_cursor": page["first_cursor"]})
                for ev in page["events"]:
                    send(ev["type"], ev, ev["cursor"])
                since = page["cursor"]
                if time.monotonic() - beat > SSE_HEARTBEAT:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    beat = time.monotonic()
                if not page["more"]:
                    time.sleep(SSE_POLL)
        except (BrokenPipeError, ConnectionResetError):
            return   # the viewer left

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        got = self.headers.get("Authorization", "").encode()
        return hmac.compare_digest(got, f"Bearer {TOKEN}".encode())   # constant time

    def do_POST(self) -> None:  # noqa: N802
        self._t0 = time.monotonic()
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = parse_qs(url.query)
        a = self.api
        if not self._authorized():
            return self._send(401, {"error": "This backend needs a token. Add it in the dashboard (Authorization: Bearer ...)."})
        try:
            body = self._body()
            if len(parts) == 4 and parts[:2] == ["api", "findings"] and parts[3] in ("approve", "reject"):
                return self._send(200, a.decide(parts[2], parts[3], body))
            if len(parts) in (4, 5) and parts[:2] == ["api", "findings"] and parts[3] == "follow_up":
                step = parts[4] if len(parts) == 5 else "draft"
                if step not in ("draft", "approve", "reject", "send"):
                    raise BadRequest("follow_up steps: POST .../follow_up (draft), /approve, /reject, /send")
                return self._send(200, a.follow_up(parts[2], step, body))
            if len(parts) == 4 and parts[:2] == ["api", "rules"] and parts[3] == "approve":
                return self._send(200, a.approve_rule(parts[2], body))
            if parts == ["api", "run"]:
                w = _arg(q, "wait")
                code, payload = a.run(body, wait=None if w is None else w not in ("0", "false", "no"))
                return self._send(code, payload)
            self._send(404, {"error": "no such route"})
        except BadRequest as e:
            self._send(400, {"error": str(e)})
        except Busy as e:
            self._send(409, {"error": str(e), "running": a.running})
        except Denied as e:
            self._send(409, {"error": str(e)})
        except (KeyError, StopIteration, LookupError) as e:
            self._send(404, {"error": f"not found: {e}"})
        except Exception as e:  # keep the server alive, tell the UI what broke
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt: str, *args: Any) -> None:
        return None   # _headers writes one line per request with status + ms


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    install_stdout()   # per-thread stdout capture for jobs and the scoreboard; everything else reaches the terminal
    print(f"Flock API on http://{host}:{port}/api  (ctrl-c to stop)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    a = ap.parse_args()
    serve(a.host, a.port)
