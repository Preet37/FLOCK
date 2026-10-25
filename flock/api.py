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
  GET  /api/market                    judge market: balances, weights, won/lost, recent bets and settlements
  GET  /api/connectors                connector batches received (Slack/email claims, sender, authority, binding)
  GET  /api/showtime                  the showtime cheat sheet (json + markdown) if one was prepared here
  GET  /api/live                      the last live month run (POST /api/run {"dataset": "live"}) summary
  GET  /api/approvals                 findings waiting for a human decision, biggest first (the approvals inbox)
  GET  /api/benchmarks                GX10 speed, held-out benchmark, LoRA training (docs/gx10_results.json)
  GET  /api/filings/<id>/screen       the portal confirmation page Clicky saw when it filed <id> (text/html)
  GET  /api/outbox                    vendor emails sent or queued (flock/mailer.py)
  GET  /api/clicky/queue              approved findings waiting for Clicky (the live agent screen's queue)
  GET  /api/clicky/plan/<id>          what Clicky will type into the sandbox portal for one finding
  POST /api/clicky/file/<id>          the sandbox portal's Submit on the agent screen: {fields} -> filed (gatekeeper)
  GET  /api/me                        the logged-in user for a session token (401 without one)
  GET  /                              the web UI (frontend/dashboard.html); /frontend/<file> and /<file> serve frontend/

Write (humans only; every write still goes through the gatekeeper):
  POST /api/findings/<id>/approve     {"actor": "thabhelo", "reason": "..."}  then Clicky files it
                                      ("role": "agent", or an agent's id as actor: the gatekeeper refuses, logged)
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
  POST /api/run                       {"dataset": "live"}  a fresh month (new seed) generated and processed now
  POST /api/login                     {"username", "password"} -> {token, user, expires_at} (no token needed)
  POST /api/logout                    ends the session in the Authorization header

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

try:   # agent C's modules (vendor email, hosted filing screens, client login); the API works without them
    from flock import mailer
except ImportError:  # pragma: no cover
    mailer = None
try:
    from flock import filing_screens
except ImportError:  # pragma: no cover
    filing_screens = None
try:
    from flock import auth
except ImportError:  # pragma: no cover
    auth = None
try:
    from flock import evidence
except ImportError:  # pragma: no cover
    evidence = None
try:
    from flock import slack_pull
except ImportError:  # pragma: no cover
    slack_pull = None

sys.path.insert(0, str(ROOT))
try:   # Clicky's live screen (frontend/agent-screen.html files through the sandbox portal page)
    from flock import clicky_screen
except Exception:  # pragma: no cover
    clicky_screen = None
from scripts.score import score  # noqa: E402

PORTAL = ROOT / "data" / "sandbox_portal" / "disputes.jsonl"
FRONTEND_DIR = ROOT / "frontend"
UI_ENTRY = "dashboard.html"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".ico": "image/x-icon",
                ".json": "application/json", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
TOKEN = os.environ.get("FLOCK_API_TOKEN", "")
CORS_ORIGIN = os.environ.get("FLOCK_CORS_ORIGIN", "*")
MAX_STORES = int(os.environ.get("FLOCK_HOSTED_MAX_STORES", "56"))   # caps for POST /api/run {"dataset": "demo"}
MAX_WEEKS = int(os.environ.get("FLOCK_HOSTED_MAX_WEEKS", "8"))
LIVE_JUDGE_BUDGET = int(os.environ.get("FLOCK_LIVE_RUN_JUDGE_BUDGET", "6"))   # live models on a live run's riskiest N
BENCHMARKS_JSON = ROOT / "docs" / "gx10_results.json"
SSE_POLL, SSE_HEARTBEAT, SSE_MAX = 0.5, 15.0, 600.0   # seconds, for GET /api/stream/sse
OUTPUT_LINES = 400       # a job keeps this many output lines; GET /api/jobs/<id> shows the tail
TAIL = 40


@contextlib.contextmanager
def capped_judge_budget(cap: int | None = None):
    """FLOCK_LIVE_JUDGE_BUDGET at most `cap` (FLOCK_LIVE_RUN_JUDGE_BUDGET, default 6) for one hosted run: live models
    on the riskiest few findings, offline rules on the rest, so a run on stage finishes in about a minute."""
    cap = LIVE_JUDGE_BUDGET if cap is None else cap
    old = os.environ.get("FLOCK_LIVE_JUDGE_BUDGET")
    cur = int(old) if old and old.strip().isdigit() else None
    os.environ["FLOCK_LIVE_JUDGE_BUDGET"] = str(min(cur, cap) if cur is not None else cap)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("FLOCK_LIVE_JUDGE_BUDGET", None)
        else:
            os.environ["FLOCK_LIVE_JUDGE_BUDGET"] = old


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


def static_file(parts: list[str], root: Path | None = None) -> Path | None:
    """The web UI: / -> frontend/dashboard.html, /frontend/<file> and /<file> -> frontend/<file>. Only files inside
    frontend/ with a known type; dotfiles, '..' and anything resolving outside the folder are refused (None)."""
    base = (root or FRONTEND_DIR).resolve()
    rel = parts[1:] if parts[:1] == ["frontend"] else parts
    rel = [unquote(p) for p in rel] or [UI_ENTRY]
    if any(p in ("", ".", "..") or p.startswith(".") or "\\" in p or "\x00" in p for p in rel):
        return None
    path = base.joinpath(*rel)
    if path.is_dir():
        path = path / UI_ENTRY
    try:
        path = path.resolve()
        path.relative_to(base)
    except (OSError, ValueError):
        return None
    if path.suffix.lower() not in STATIC_TYPES or not path.is_file():
        return None
    return path


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
        self.snapshot: dict[str, Any] | None = None   # the last completed run's reads, served while a run goes

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
        status = self.agent_status()
        roster = [{**a, "actions": counts.get(a["actor"], 0), "last_at": max(filter(None, (last.get(a["actor"]),
                   status.get(a["actor"], {}).get("at"))), default=None), **self._status_of(a, counts, status),
                   **provider_of(a["actor"])}
                  for a in AGENTS]
        humans = sorted({e["actor"] for e in log if e["role"] == "human"})
        return {"agents": roster, "judges": JUDGES, "humans": humans,
                "transitions": [{"from": s, "action": a, "to": t, "roles": sorted(r)} for (s, a), (t, r) in TRANSITIONS.items()]}

    SNAPSHOT_READS = ("metrics", "score", "payouts", "approvals", "findings", "showtime")

    def take_snapshot(self) -> dict[str, Any]:
        """What the dashboard reads, as of the last completed run. A run resets the store and fills it as it goes;
        until it finishes, these reads come from here (each tagged with running_job) so no card shows half a run."""
        return {"metrics": self.metrics(), "score": self.scoreboard(), "payouts": self.payouts({}),
                "approvals": self.approvals(), "findings": self.findings({}), "showtime": self.showtime_sheet(),
                "at": _now()}

    def read(self, name: str, fresh, q: dict[str, list[str]] | None = None) -> Any:
        """fresh() normally; the last completed run's snapshot while a job runs (no query string)."""
        snap = self.snapshot
        if self.running and snap is not None and name in snap and not q:
            out = snap[name]
            if isinstance(out, dict):
                return {**out, "running_job": self.running, "as_of": snap["at"], "stale_while_running": True}
            return out
        out = fresh()
        if isinstance(out, dict):
            out = {**out, "running_job": self.running}
        return out

    def agent_status(self) -> dict[str, dict[str, Any]]:
        """Latest feed event per role in the latest run (stage:<actor> events plus the tracker's roles)."""
        from flock import agents_live
        rows = agents_live.events(self.store)
        run = next((e.get("run") for e in reversed(rows) if e.get("run")), None)
        out: dict[str, dict[str, Any]] = {}
        for e in rows:
            if run and e.get("run") not in (run, None):
                continue
            role = e.get("role")
            if not role:
                continue
            cur = out.setdefault(role, {"spawned": 0, "done": 0})
            cur["spawned"] += e.get("event") == "spawn"
            cur["done"] += e.get("event") == "done"
            if e.get("event") in ("working", "done", "failed") or "event" not in cur:
                cur.update(event=e.get("event"), summary=e.get("summary"), at=e.get("at"),
                           count=e.get("count", cur.get("count")))
        return out

    @staticmethod
    def _status_of(a: dict[str, Any], counts: dict[str, int], status: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """status: working | done | idle, with one line saying what it did (the Agent Chain card)."""
        mine = [status[r] for r in [a["actor"], *a.get("live", [])] if r in status]
        if any(m.get("event") in ("working", "spawn") for m in mine):
            st = "working"
        elif mine or counts.get(a["actor"]):
            st = "done"
        else:
            st = "idle"
        best = max(mine, key=lambda m: m.get("at") or "", default={})
        text = best.get("summary")
        if not text and counts.get(a["actor"]):
            text = f"{counts[a['actor']]:,} audit-log entries"
        if not text:
            text = {"follow_up": "standing by: drafts a reminder when a filed claim goes unpaid",
                    "gatekeeper": "standing by: checks every move"}.get(a["actor"], "idle: nothing to do this run")
        return {"status": st, "status_text": text, "work_count": best.get("count")}

    def approvals(self) -> dict[str, Any]:
        """The approvals inbox: every finding that waits on a person (a drafted dispute to approve, or a conflict the
        panel won't decide), biggest first."""
        items = []
        for f in self.store.all():
            if not _needs_human(f):
                continue
            g = frontend.with_risk(f)
            roll = f.get("rollup") or {}
            items.append({"finding_id": f["finding_id"], "kind": "conflict" if f.get("outcome") == "conflict"
                          else "dispute", "channel": f["channel"],
                          "platform": PLATFORM_NAMES.get(f["channel"], f["channel"]), "check_type": f["check_type"],
                          "gap": f["gap"], "amount": abs(f["gap"]), "expected": f.get("expected"),
                          "actual": f.get("actual"), "payout_id": roll.get("payout_id"), "stores": roll.get("stores"),
                          "orders": len(f.get("order_ids") or []), "outcome": f.get("outcome"),
                          "confidence": f.get("confidence"), "verdict": (f.get("reason") or "")[:240],
                          "risk": g.get("risk"), "state": f["state"], "has_draft": bool(f.get("dispute_draft")),
                          "memo": (f.get("review_memo") or {}).get("memo") if isinstance(f.get("review_memo"), dict)
                          else f.get("review_memo")})
        items.sort(key=lambda x: (-x["amount"], x["finding_id"]))
        return {"count": len(items), "total_usd": round(sum(x["amount"] for x in items), 2),
                "disputes": sum(1 for x in items if x["kind"] == "dispute"),
                "conflicts": sum(1 for x in items if x["kind"] == "conflict"), "items": items}

    def benchmarks(self) -> dict[str, Any]:
        """GX10 + training + held-out benchmark (docs/gx10_results.json, from docs/gx10_results.md), plus the latest
        local harness results if scripts/benchmark.py ran on this box."""
        doc = json.loads(BENCHMARKS_JSON.read_text()) if BENCHMARKS_JSON.exists() else {}
        harness = (frontend.read_json(frontend.RESULTS_JSON) or {}).get("current")
        return {**doc, "harness": harness, "team": team_row(), "headline": headline(doc, harness),
                "models": {"judges_default": "flock-judge-v1 x3 on the GX10 (scripts/gx10.sh env)",
                           "hosted": "CREAO hosted agents (Claude Haiku 4.5 inside) on Railway"}}

    def live_sheet(self) -> dict[str, Any]:
        from flock import showtime
        p = self.dir / showtime.LIVE_JSON
        return {"json": json.loads(p.read_text()) if p.exists() else None}

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

    def market(self) -> dict[str, Any]:
        """Judge balances (from the append-only ledger), recent bets / settlements, attention budget (same as the
        dashboard's /api/market)."""
        from flock import market
        return market.snapshot(self.store)

    def rules(self) -> dict[str, Any]:
        """Approved rules plus the Learner's proposals, and a "Flock learns" list: each rule's text, what it was learned
        from (the findings humans decided, who approved it, when) and how often it settled a finding, month by month
        (runs.jsonl since the last reset, the current month from the findings now)."""
        approved = learner.approved_rules()
        proposed = learner._read(learner._proposed_path(self.store))
        runs = [json.loads(x) for x in self.runs_path.read_text().splitlines() if x.strip()] \
            if self.runs_path.exists() else []
        now = pipeline.rule_counts(self.store.all())
        months = [(r.get("month") or r.get("period") or f"run {i + 1}", r.get("by_rule_ids") or {})
                  for i, r in enumerate(runs)]
        if months:
            months[-1] = (months[-1][0], now)
        out = []
        for r, status in [(r, "approved") for r in approved] + [(r, "proposed") for r in proposed]:
            rid = r["rule_id"]
            by_month = [{"month": m, "applied": c.get(rid, 0)} for m, c in months] if status == "approved" else []
            out.append({"id": rid, "text": r.get("description"), "status": status, "outcome": r.get("outcome"),
                        "channel": r.get("channel"), "check_type": r.get("check_type"),
                        "authority": r.get("authority"),
                        "learned_from": {"findings": r.get("precedents") or [], "count": r.get("evidence_count"),
                                         "approver": r.get("approved_by"), "date": r.get("approved_at")},
                        "applied_count": sum(x["applied"] for x in by_month), "applied_by_month": by_month})
        return {"approved": approved, "proposed": proposed, "rules": out}

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

    def decide(self, fid: str, action: str, body: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
        self._not_running()
        with self.write_lock:
            actor = _human(body, user)
            after = transition(self.store, fid, action, actor,
                               reason=str(body.get("reason") or f"{action}d from the dashboard"))
            entry = self.store.log(fid)[-1]   # the human's decision, before Clicky's filing is logged
            # file_via "screen": leave it APPROVED; Clicky files it on the live agent screen (frontend/agent-screen.html)
            on_screen = body.get("file_via") == "screen" and clicky_screen is not None
            filed = clicky.file_approved(self.store, self.portal) if action == "approve" and not on_screen else []
            proposed = learner.propose(self.store)
            f = self.store.get(fid)
            screen = f"/api/filings/{fid}/screen" if (filing_screens and f.get("portal_ref")
                                                       and filing_screens.render(fid, self.dir)) else None
            return {"finding": frontend.with_risk(f), "filed": filed, "proposed": proposed,
                    "was": after["state"], "log_entry": entry, "portal_ref": f.get("portal_ref"), "screen": screen,
                    "agent_screen": f"/agent-screen.html?finding={fid}" if on_screen and action == "approve" else None}

    def follow_up(self, fid: str, step: str, body: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
        """Vendor follow-up on a FILED, unpaid claim: draft (the follow_up agent), approve / reject (a human), send
        (the gatekeeper, via Clicky). Every step is a gatekeeper annotation; refusals are logged and raise Denied."""
        self._not_running()
        with self.write_lock:
            ref, mail = None, None
            if step == "draft":
                follow_ups.draft(self.store, [fid])
            elif step == "send":
                ref = follow_ups.send(self.store, fid, self.portal)
                if mailer is not None:   # the gatekeeper recorded the send; now the email goes to the vendor
                    mail = mailer.send_follow_up(self.store.get(fid), state_dir=self.dir)
            else:
                actor = _human(body, user)
                reason = str(body.get("reason") or "")
                if step == "approve":
                    follow_ups.approve(self.store, fid, actor, text=body.get("text") or None,
                                       reason=reason or "Follow-up approved from the dashboard")
                else:
                    follow_ups.reject(self.store, fid, actor, reason=reason or "Follow-up rejected from the dashboard")
            f = self.store.get(fid)
            return {"finding": frontend.with_risk(f), "follow_up": f.get("follow_up"), "portal_ref": ref,
                    "email": mail, "log_entry": self.store.log(fid)[-1]}

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
        if dataset == "showtime":
            params = self.showtime_params(body)
            if wait:
                return 200, self._exclusive(lambda: self._showtime(params))
            return 202, self.start("showtime", params, lambda: self._showtime(params))
        if dataset == "live":
            params = self.live_params(body)
            if wait:
                return 200, self._exclusive(lambda: self._live(params))
            return 202, self.start("live", params, lambda: self._live(params))
        if dataset != "demo":
            raise BadRequest(f"unknown dataset {dataset!r}: use 'fixtures' (default), 'demo', 'showtime' or 'live'")
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

    @staticmethod
    def showtime_params(body: dict[str, Any]) -> dict[str, Any]:
        """Validate a hosted showtime prepare (prior months + the demo month). Raises BadRequest."""
        def whole(name: str, default: int, lo: int, hi: int) -> int:
            v = body.get(name, default)
            if isinstance(v, bool) or not str(v).strip().isdigit():
                raise BadRequest(f"{name} must be a whole number")
            v = int(v)
            if not lo <= v <= hi:
                raise BadRequest(f"{name} must be between {lo} and {hi} on this server (got {v})")
            return v
        return {"months": whole("months", 4, 1, 4), "seed": whole("seed", 2025, 0, 2**31 - 1),
                "stores": whole("stores", MAX_STORES, 1, MAX_STORES)}

    def _showtime(self, p: dict[str, Any]) -> dict[str, Any]:
        """flock.showtime prepare into this server's state: the prior months (learning curve, judge balances, learned
        rules) and the demo month, stopped before the headline finding is approved. Cheat sheet: GET /api/showtime."""
        from flock import showtime
        with capped_judge_budget():   # the history months are offline; the month on stage gets the live judges
            showtime.prepare(months=p["months"], seed=p["seed"], stores=p["stores"], store=self.store,
                             portal=self.portal)
        return {"params": p, "showtime": self.showtime_sheet().get("json"), "findings": len(self.store.all())}

    @staticmethod
    def live_params(body: dict[str, Any]) -> dict[str, Any]:
        """A live month: a fresh seed unless one is given, the hosted store cap."""
        out: dict[str, Any] = {"seed": None, "stores": MAX_STORES}
        for name, hi in (("seed", 2**31 - 1), ("stores", MAX_STORES)):
            v = body.get(name)
            if v is None:
                continue
            if isinstance(v, bool) or not str(v).strip().isdigit() or not 1 <= int(v) <= hi:
                raise BadRequest(f"{name} must be a whole number between 1 and {hi}")
            out[name] = int(v)
        return out

    def _live(self, p: dict[str, Any]) -> dict[str, Any]:
        """flock.showtime.live into this server's state: the generator makes a brand-new month, the pipeline runs on
        it now with live judges on its riskiest few (FLOCK_LIVE_RUN_JUDGE_BUDGET, default 6) and offline rules on the
        rest. Nothing is approved: every drafted dispute lands in GET /api/approvals."""
        from flock import showtime
        with capped_judge_budget():
            info = showtime.live(seed=p["seed"], stores=p["stores"], store=self.store, portal=self.portal)
        m = info["months"][0]
        return {"params": {**p, "seed": info["seed"]}, "month": info["month_name"], "seed": info["seed"],
                "orders": m["orders"], "deposits": m["deposits"], "findings": m["findings"], "to_human": m["to_human"],
                "caught": m["caught"], "total": m["total"], "false_alarms": m["false_alarms"],
                "shortfall": info["scenario"]["shortfall"], "deposited": info["scenario"]["deposited"],
                "headline": info["headline"], "approvals": self.approvals()["count"], "seconds": info["seconds"]}

    def connector(self, body: Any) -> dict[str, Any]:
        """POST /api/connectors: one connector batch (a CREAO agent watching Slack/Gmail). BadRequest on a bad payload."""
        from flock import agents_live, connectors
        try:
            rec = connectors.receive(body, self.dir)
            rec.pop("payload", None)
        except connectors.PayloadError as e:
            raise BadRequest("; ".join(e.problems))
        agents_live.emit(self.store, {"event": "done", "agent_id": f"connector:{rec['connector']}",
                                      "role": "connector", "subject": rec["source"], "at": rec["at"],
                                      "summary": f"{rec['items']} {rec['source']} item(s) from {rec['connector']}: "
                                                 f"{len(rec['claims'])} deal claim(s)"})
        return rec

    def showtime_sheet(self) -> dict[str, Any]:
        """The last showtime cheat sheet in this server's state (json + markdown), or an empty note."""
        from flock import showtime
        jp, mp, lp = self.dir / showtime.JSON_NAME, self.dir / showtime.MD_NAME, self.dir / showtime.LIVE_JSON
        newest = max((p for p in (jp, lp) if p.exists()), key=lambda p: p.stat().st_mtime, default=None)
        if newest is None:
            return {"json": None, "markdown": None, "note": "no showtime prepared here: POST /api/run {\"dataset\": \"showtime\"}"}
        info = json.loads(newest.read_text())
        try:
            info = showtime.refresh(self.store, info)
        except Exception as e:   # the recorded sheet is still worth showing
            info["refresh_error"] = f"{type(e).__name__}: {e}"
        return {"json": info, "markdown": mp.read_text() if mp.exists() and newest == jp else None,
                "source": newest.name}

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
        try:
            self.snapshot = self.take_snapshot()
        except Exception:   # a snapshot is a convenience; never block a run on it
            self.snapshot = None
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
                self.snapshot = None
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


def _human(body: dict[str, Any], user: dict[str, Any] | None = None) -> Actor:
    """The person deciding. A logged-in session names the person itself (the browser's actor is ignored). An agent's own id is not a person, and "role": "agent" says so explicitly (the UI's "Try as
    an agent"): either goes to the gatekeeper as role agent, which refuses it and logs the attempt."""
    if user:
        return Actor(id=str(user.get("name") or user.get("username")), role="human")
    name = str(body.get("actor") or "human")
    agent = name in AGENT_IDS or str(body.get("role") or "").strip().lower() == "agent"
    return Actor(id=name, role="agent" if agent else "human")


STAFF_ACCOUNTANT_SALARY = 70_000   # FLOCK_CONTEXT.md: AppleOne posting, staff accountant incl. delivery reconciliation
HOURS_PER_YEAR = 2080              # 40 h x 52 weeks: salary -> hourly (salary only, no benefits or overhead)
STORES = 56


def team_row() -> dict[str, Any]:
    """Today's finance team, sourced numbers only: ~1 h per location per week (Lunchbox, an industry estimate) and a
    $70,000 staff accountant salary (a posted LA role that includes delivery reconciliation). Accuracy: not measured."""
    hours_month = round(STORES * 1.0 * 52 / 12, 1)
    rate = round(STAFF_ACCOUNTANT_SALARY / HOURS_PER_YEAR, 2)
    return {"who": "today's finance team (staff accountant + disputes clerk)", "stores": STORES,
            "hours_per_store_week": 1.0, "hours_per_month": hours_month,
            "hourly_usd": rate, "cost_per_month_usd": round(hours_month * rate, 2),
            "accuracy": None, "accuracy_note": "not measured",
            "sources": [{"what": "~1 h per location per week", "label": "INDUSTRY ESTIMATE",
                         "source": "https://lunchbox.io/third-party-disputes-the-silent-restaurant-revenue-killer"},
                        {"what": "$70,000/yr staff accountant (delivery platform reconciliation in the role)",
                         "source": "https://www.appleone.com/CareerSearch/JobDetail/Staff-Accountant-1776310WR",
                         "note": "hourly = salary / 2,080 h; salary only, no benefits"}]}


def headline(doc: dict[str, Any], harness: dict[str, Any] | None) -> dict[str, Any]:
    """Three columns for the pitch: our tuned local judge, a frontier model as run here, today's finance team. Only
    measured or sourced numbers; a contestant that was not run says so."""
    bench = doc.get("benchmark") or {}
    rows = {r.get("kind"): r for r in bench.get("rows") or []}
    total = bench.get("total")
    tuned = rows.get("tuned") or {}
    out = {"tuned": {"name": tuned.get("contestant"), "where": "GX10, on our network",
                     "caught": tuned.get("caught"), "total": total,
                     "accuracy": round(tuned["caught"] / total, 4) if tuned.get("caught") is not None and total else None,
                     "false_alarms": tuned.get("false_alarms"), "p50_s": tuned.get("p50_s"),
                     "usd_per_1000": tuned.get("usd_per_1000"), "run": tuned.get("run")}}
    fr = (harness or {}).get("creao") or doc.get("frontier") or {}
    if fr and fr.get("status", "ok") == "ok" and fr.get("total"):
        out["frontier"] = {"name": fr.get("label") or "CREAO hosted agents (Claude Haiku 4.5)", "where": "CREAO cloud",
                           "caught": fr.get("caught"), "total": fr.get("total"),
                           "accuracy": fr.get("accuracy") if fr.get("accuracy") is not None else
                           round(fr["caught"] / fr["total"], 4),
                           "false_alarms": fr.get("false_alarms"), "p50_s": fr.get("p50_s"),
                           "usd_per_1000": fr.get("cost_per_1000"), "seeds": fr.get("seeds"), "run": fr.get("run_id")}
    else:
        out["frontier"] = {"name": "Claude via CREAO", "status": "not run on the held-out seeds yet",
                           "note": "Sonnet/Opus not run: no Anthropic key"}
    t = team_row()
    out["team"] = {"name": t["who"], "hours_per_month": t["hours_per_month"],
                   "cost_per_month_usd": t["cost_per_month_usd"], "accuracy": None, "accuracy_note": "not measured",
                   "sources": t["sources"]}
    return out


# roster agent -> the model role it calls (flock/llm.py route); agents not listed are plain code by design: the money
# math (formatter, matcher, checkers), the gatekeeper's door, the ranker's formula, the journal's double entry.
AGENT_MODEL_ROLE = {"panel": "judge", "dispute_writer": "writer", "investigator": "writer", "deal_keeper": "reader",
                    "follow_up": "writer", "learner": "writer"}
CODE_NOTE = {"formatter": "known layouts are code; an unseen layout asks the mapper model once, then a human confirms",
             "checkers": "plain Python does every dollar: no model computes money"}


def provider_label(r: tuple[str, str]) -> str:
    """creao | gx10-tuned | gx10 | anthropic | groq | nvidia | code (offline rules / template)."""
    from flock import llm
    if not llm.available(r):
        return "code"
    p, m = r
    if p in ("creao", "creaoapp"):
        return "creao"
    if p == "local":
        return "gx10-tuned" if "flock-judge" in (m or "") else "gx10"
    return p


def model_name(r: tuple[str, str]) -> str:
    """A readable model for the badge: a CREAO route names an agent id, so show the model inside it."""
    if provider_label(r) == "creao":
        return os.environ.get("FLOCK_CREAO_LLM") or "claude-haiku-4-5 (CREAO hosted agent)"
    return r[1] if provider_label(r) != "code" else "offline rules"


def provider_of(actor: str) -> dict[str, Any]:
    """What runs this agent right now: provider + model (the UI's badge). Code-only agents say "code"."""
    from flock import llm
    role = AGENT_MODEL_ROLE.get(actor)
    if role is None:
        return {"provider": "code", "model": None, "provider_note": CODE_NOTE.get(actor, "deterministic code by design")}
    if role == "judge":
        routes = [llm.route("judge", i) for i in range(3)]
        labels = [provider_label(r) for r in routes]
        models = [model_name(r) for r in routes]
        return {"provider": labels[0] if len(set(labels)) == 1 else "mixed", "model": ", ".join(dict.fromkeys(models)),
                "providers": labels, "provider_note": "three judges; outside the live budget they use offline rules"}
    r = llm.route(role)
    lab = provider_label(r)
    return {"provider": lab, "model": model_name(r) if lab != "code" else "template",
            "provider_note": "falls back to the template / offline rules if the model is down or slow"}


PLATFORM_NAMES = {"doordash": "DoorDash", "ubereats": "Uber Eats", "grubhub": "Grubhub", "direct_app": "Direct app"}


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

    def _send_static(self, path: Path) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES[path.suffix.lower()])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)
        ms = (time.monotonic() - getattr(self, "_t0", time.monotonic())) * 1000
        sys.stderr.write(f"api  {self.command} {self.path}  200  {ms:.0f}ms\n")

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
            if parts == ["api", "showtime"]:
                return self._send(200, a.read("showtime", a.showtime_sheet))
            if parts == ["api", "live"]:
                return self._send(200, a.live_sheet())
            if parts == ["api", "approvals"]:
                return self._send(200, a.read("approvals", a.approvals))
            if parts == ["api", "evidence", "index"] and evidence is not None:
                return self._send(200, evidence.index(a.store.all()))
            if parts == ["api", "evidence"] and evidence is not None:
                try:
                    return self._send(200, evidence.resolve(_arg(q, "ref") or "", evidence.roots(a.dir)))
                except evidence.EvidenceError as e:
                    return self._send(400, {"error": str(e)})
            if parts == ["api", "clicky", "queue"] and clicky_screen is not None:
                return self._send(200, {"queue": clicky_screen.queue(a.store)})
            if len(parts) == 4 and parts[:3] == ["api", "clicky", "plan"] and clicky_screen is not None:
                return self._send(200, clicky_screen.plan(a.store, parts[3]))
            if parts == ["api", "benchmarks"]:
                return self._send(200, a.benchmarks())
            if parts == ["api", "outbox"]:
                return self._send(200, {"messages": mailer.outbox(a.dir) if mailer else [],
                                        "backend": mailer.backend() if mailer else "none"})
            if len(parts) == 4 and parts[:2] == ["api", "filings"] and parts[3] == "screen":
                page = filing_screens.render(parts[2], a.dir) if filing_screens else None
                if page is None:
                    return self._send(404, {"error": f"no filing screen for {parts[2]} (Clicky hasn't filed it here)"})
                data = page.encode()
                self._headers(200, "text/html; charset=utf-8", len(data))
                self.wfile.write(data)
                return None
            if parts == ["api", "me"]:
                u = auth.user_for(self._bearer(), a.dir) if auth else None
                return self._send(200, {"user": u}) if u else self._send(401, {"error": "not logged in"})
            if parts == ["api", "connectors"]:
                from flock import connectors
                return self._send(200, {"received": connectors.received(a.dir)})
            if parts == ["api", "agents"]:
                return self._send(200, a.agents())
            if len(parts) == 3 and parts[:2] == ["api", "agents"]:
                return self._send(200, a.agent(parts[2]))
            if parts == ["api", "findings"]:
                return self._send(200, a.read("findings", lambda: a.findings(q), q))
            if len(parts) == 3 and parts[:2] == ["api", "findings"]:
                return self._send(200, a.finding(parts[2]))
            if parts == ["api", "transactions"]:
                return self._send(200, a.transactions(q))
            if parts == ["api", "payouts"]:
                return self._send(200, a.read("payouts", lambda: a.payouts(q), q))
            if len(parts) == 3 and parts[:2] == ["api", "payouts"]:
                return self._send(200, a.payout(unquote(parts[2])))
            if parts == ["api", "stream"]:
                return self._send(200, a.stream(q))
            if parts == ["api", "stream", "replay"]:
                return self._send(200, a.replay(q))
            if parts == ["api", "stream", "sse"]:
                return self._sse(q)
            if parts == ["api", "metrics"]:
                return self._send(200, a.read("metrics", a.metrics))
            if parts == ["api", "log"]:
                return self._send(200, a.store.log())
            if parts == ["api", "rules"]:
                return self._send(200, a.rules())
            if parts == ["api", "market"]:
                return self._send(200, a.market())
            if parts == ["api", "score"]:
                return self._send(200, a.read("score", a.scoreboard))
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
            if parts[:1] != ["api"]:
                path = static_file(parts)
                if path is None:
                    return self._send(404, {"error": "no such page (the UI is at /)"})
                return self._send_static(path)
            if parts == ["api"]:
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

    def _bearer(self) -> str | None:
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.startswith("Bearer ") else None

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        got = self.headers.get("Authorization", "").encode()
        return hmac.compare_digest(got, f"Bearer {TOKEN}".encode())   # constant time

    def _session(self) -> dict[str, Any] | None:
        """The logged-in user behind the bearer token (flock/auth.py), or None."""
        return auth.user_for(self._bearer(), self.api.dir) if auth else None

    def do_POST(self) -> None:  # noqa: N802
        self._t0 = time.monotonic()
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = parse_qs(url.query)
        a = self.api
        if parts in (["api", "login"], ["api", "logout"]):
            return self._auth_route(parts[1])
        user = self._session()
        if user is not None:
            if not auth.can_approve(user):
                return self._send(403, {"error": f"{user.get('name')} can view but not approve (role {user.get('role')})"})
        elif not self._authorized():
            return self._send(401, {"error": "Log in to approve (or send the API token)."})
        try:
            body = self._body()
            if parts == ["api", "connectors"]:
                with a.write_lock:
                    return self._send(200, a.connector(body))
            if parts == ["api", "connectors", "slack", "pull"]:
                if slack_pull is None:
                    return self._send(404, {"error": "Slack pull is not available on this server"})
                with a.write_lock:
                    return self._send(200, slack_pull.pull(a.dir))
            if len(parts) == 4 and parts[:3] == ["api", "clicky", "file"] and clicky_screen is not None:
                a._not_running()
                with a.write_lock:
                    try:
                        return self._send(200, clicky_screen.file(a.store, parts[3], body.get("fields"), a.portal))
                    except clicky_screen.Mismatch as e:
                        raise BadRequest(str(e)) from None
            if len(parts) == 4 and parts[:2] == ["api", "findings"] and parts[3] in ("approve", "reject"):
                return self._send(200, a.decide(parts[2], parts[3], body, user))
            if len(parts) in (4, 5) and parts[:2] == ["api", "findings"] and parts[3] == "follow_up":
                step = parts[4] if len(parts) == 5 else "draft"
                if step not in ("draft", "approve", "reject", "send"):
                    raise BadRequest("follow_up steps: POST .../follow_up (draft), /approve, /reject, /send")
                return self._send(200, a.follow_up(parts[2], step, body, user))
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
        except Denied as e:   # the gatekeeper refused (and logged it): send the refusal entry so the UI can show it
            out: dict[str, Any] = {"error": str(e), "denied": True}
            if len(parts) >= 3 and parts[:2] == ["api", "findings"]:
                with contextlib.suppress(Exception):
                    out["log_entry"] = a.store.log(parts[2])[-1]
            self._send(409, out)
        except (KeyError, StopIteration, LookupError) as e:
            self._send(404, {"error": f"not found: {e}"})
        except Exception as e:  # keep the server alive, tell the UI what broke
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _auth_route(self, which: str) -> None:
        if auth is None:
            return self._send(404, {"error": "login is not available on this server"})
        try:
            if which == "logout":
                return self._send(200, {"logged_out": auth.logout(self._bearer(), self.api.dir)})
            body = self._body()
            return self._send(200, auth.login(str(body.get("username") or ""), str(body.get("password") or ""),
                                              self.api.dir))
        except BadRequest as e:
            return self._send(400, {"error": str(e)})
        except auth.RateLimited as e:
            return self._send(429, {"error": str(e)})
        except auth.LoginError as e:
            return self._send(401, {"error": str(e)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return None   # _headers writes one line per request with status + ms


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    install_stdout()   # per-thread stdout capture for jobs and the scoreboard; everything else reaches the terminal
    print(f"Flock API on http://{host}:{port}/api  UI on http://{host}:{port}/  (ctrl-c to stop)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    a = ap.parse_args()
    serve(a.host, a.port)
