"""The live flock: which agents exist, what each is working on, and who spawned whom.

Every agent with a job writes status events to an append-only feed through the store
(store.agent_event(); JsonStore keeps it in <state dir>/agent_status.jsonl). The dashboard's Flock view polls
GET /api/agents and folds the events into a tree: worker per store -> judge per finding -> writer / investigator.

  from flock import agents_live
  flock = agents_live.Tracker(store, run_id)
  w = flock.spawn("worker", subject="S01")         # spawn event
  w.working("matching 4,210 rows")                  # working event
  j = flock.spawn("judge", parent=w, subject="F-1")
  j.done("recoverable, 3 of 3 agree")               # done event (or .failed("why"))
  flock.close()                                     # flushes pooled agents and the store's buffer

Event shape (one JSON line each; Ira's SpacetimeStore can keep the same fields as an agent_status table):
  seq, at, run, event (spawn|working|done|failed), agent_id, role, parent_id, subject, started, ended, summary
  plus count / done / failed on a pooled agent (below).

Roles, each with a real job (no agent without one):
  worker        one per store: matches that store's orders to payout lines and runs the checkers
  checker       the bank-deposit reconciler (deposits cross stores, so it runs once)
  judge         the panel's three votes on one finding
  investigator  spawned only for an unexplained finding (flock/agents/investigator.py); attaches a case file
  writer        drafts one dispute message
  clicky        files approved disputes in the sandbox portal
  learner       proposes rules from human decisions
  journal       drafts the journal entries

Year scale: the first SAMPLE agents of a role are tracked one by one; after that the role collapses into one
pooled agent ("judge *") whose count / done / failed go up, written at most once per flush. A year of a 56-store
chain (thousands of findings) stays a few hundred lines. Writes are buffered by the store (JsonStore inside
batch()); nothing here touches a finding, and a feed failure never breaks a run.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

ROLES = ("worker", "checker", "judge", "investigator", "writer", "clicky", "learner", "journal")
EVENTS = ("spawn", "working", "done", "failed")
STATE_NAME = "agent_status.jsonl"
SAMPLE = 150          # agents of one role tracked individually per run; the rest are pooled
POOL = "*"            # pooled agent id suffix: "judge*"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# store helpers: no-op on a backend without the feed ---------------------------------------------------------
def emit(store: Any, event: dict[str, Any]) -> None:
    fn = getattr(store, "agent_event", None)
    if fn is None:
        return
    try:
        fn(event)
    except Exception as e:  # the feed is for watching; it must never stop the work
        print(f"[agents] could not record {event.get('event')} for {event.get('agent_id')}: {e}", file=sys.stderr)


def events(store: Any, since: int | None = None) -> list[dict[str, Any]]:
    fn = getattr(store, "agents", None)
    return fn(since) if fn is not None else []


def flush(store: Any) -> None:
    fn = getattr(store, "flush_agents", None)
    if fn is not None:
        fn()


# the JSONL feed (JsonStore uses this) -------------------------------------------------------------------------
class Feed:
    """Append-only JSONL. Buffered while `buffered` is on (the store's batch()), written at most every `every`
    seconds; seq is the line number, so a reader's cursor is just "how many lines I have seen"."""

    def __init__(self, path, every: float = 0.5):
        self.path = path
        self.every = every
        self.buffered = False
        self._buf: list[str] = []
        self._lock = threading.Lock()
        self._flushed = time.monotonic()
        self._seq: int | None = None

    def _next_seq(self) -> int:
        if self._seq is None:
            self._seq = len(self._lines())
        self._seq += 1
        return self._seq - 1

    def add(self, event: dict[str, Any]) -> None:
        with self._lock:
            event = {"seq": self._next_seq(), **event}
            self._buf.append(json.dumps(event, default=str) + "\n")
            if not self.buffered or time.monotonic() - self._flushed >= self.every:
                self._flush()

    def _flush(self) -> None:
        if self._buf:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write("".join(self._buf))
            self._buf = []
        self._flushed = time.monotonic()

    def flush(self) -> None:
        with self._lock:
            self._flush()

    def _lines(self) -> list[str]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().split("\n")
        return [ln for ln in lines[:-1] if ln.strip()]   # last piece: "" or a line still being written

    def read(self, since: int | None = None) -> list[dict[str, Any]]:
        self.flush()
        rows = []
        for ln in self._lines()[since or 0:]:
            try:
                rows.append(json.loads(ln))
            except ValueError:
                continue
        return rows

    def reset(self) -> None:
        with self._lock:
            self._buf = []
            self._seq = None
            self.path.unlink(missing_ok=True)


# the tracker the pipeline uses --------------------------------------------------------------------------------
class Agent:
    """One tracked agent. done() / failed() end it; calling them twice does nothing."""

    def __init__(self, tracker: "Tracker", agent_id: str, role: str, parent_id: str | None, subject: Any):
        self.tracker, self.id, self.role, self.parent_id, self.subject = tracker, agent_id, role, parent_id, subject
        self.started = _now()
        self.ended: str | None = None

    def _send(self, event: str, summary: str = "", **extra: Any) -> None:
        self.tracker._send({"event": event, "agent_id": self.id, "role": self.role, "parent_id": self.parent_id,
                            "subject": self.subject, "started": self.started, "ended": self.ended,
                            "summary": summary[:300], **extra})

    def working(self, summary: str = "") -> "Agent":
        if self.ended is None:
            self._send("working", summary)
        return self

    def done(self, summary: str = "", **extra: Any) -> None:
        if self.ended is None:
            self.ended = _now()
            self._send("done", summary, **extra)

    def failed(self, summary: str = "") -> None:
        if self.ended is None:
            self.ended = _now()
            self._send("failed", summary)


class Pooled(Agent):
    """Stands in for one agent past the SAMPLE cap: counts into its role's pooled agent, writes nothing itself."""

    def __init__(self, pool: "_Pool", parent_id: str | None, subject: Any):
        self.pool, self.id, self.role, self.parent_id, self.subject = pool, pool.id, pool.role, parent_id, subject
        self.tracker = pool.tracker
        self.started, self.ended = pool.started, None

    def working(self, summary: str = "") -> "Agent":
        return self

    def done(self, summary: str = "", **extra: Any) -> None:
        if self.ended is None:
            self.ended = _now()
            self.pool.finish(ok=True, summary=summary)

    def failed(self, summary: str = "") -> None:
        if self.ended is None:
            self.ended = _now()
            self.pool.finish(ok=False, summary=summary)


class _Pool:
    def __init__(self, tracker: "Tracker", role: str):
        self.tracker, self.role, self.id = tracker, role, role + POOL
        self.started = _now()
        self.count = self.done = self.failed = 0
        self.last = ""
        self.dirty = False
        self.sent_spawn = False

    def add(self) -> None:
        self.count += 1
        self.dirty = True

    def finish(self, ok: bool, summary: str) -> None:
        with self.tracker._lock:
            if ok:
                self.done += 1
            else:
                self.failed += 1
            self.last = summary or self.last
            self.dirty = True
        self.tracker._maybe_pools()

    def event(self, final: bool = False) -> dict[str, Any]:
        finished = self.done + self.failed
        state = "spawn" if not self.sent_spawn else "done" if final or finished >= self.count else "working"
        self.sent_spawn = True
        return {"event": state, "agent_id": self.id, "role": self.role, "parent_id": None, "subject": None,
                "started": self.started, "ended": _now() if state == "done" else None, "pooled": True,
                "count": self.count, "done": self.done, "failed": self.failed,
                "summary": f"{finished:,} of {self.count:,} {self.role}s done past the first {SAMPLE} tracked"
                           + (f"; last: {self.last}" if self.last else "")}


class Tracker:
    """Spawns agents for one run and writes their events through the store. Thread-safe."""

    def __init__(self, store: Any, run: str = "", sample: int = SAMPLE, every: float = 0.5):
        self.store, self.run, self.sample, self.every = store, run, sample, every
        self.counts: dict[str, int] = {}
        self.owner: dict[str, str] = {}          # finding_id -> agent id that found it (worker / checker)
        self.judge_of: dict[str, str] = {}       # finding_id -> judge agent id
        self._pools: dict[str, _Pool] = {}
        self._lock = threading.Lock()
        self._pool_flushed = time.monotonic()

    def _send(self, event: dict[str, Any]) -> None:
        emit(self.store, {"at": _now(), "run": self.run, **event})

    def spawn(self, role: str, parent: Agent | str | None = None, subject: Any = None,
              agent_id: str | None = None, summary: str = "") -> Agent:
        parent_id = parent.id if isinstance(parent, Agent) else parent
        with self._lock:
            n = self.counts.get(role, 0) + 1
            self.counts[role] = n
            if n > self.sample:
                pool = self._pools.get(role) or self._pools.setdefault(role, _Pool(self, role))
                pool.add()
                agent: Agent = Pooled(pool, parent_id, subject)
            else:
                agent = Agent(self, agent_id or f"{role}:{subject if subject is not None else n}", role, parent_id,
                              subject)
        if isinstance(agent, Pooled):
            self._maybe_pools()
        else:
            agent._send("spawn", summary)
        return agent

    def _maybe_pools(self, force: bool = False, final: bool = False) -> None:
        with self._lock:
            if not force and time.monotonic() - self._pool_flushed < self.every:
                return
            self._pool_flushed = time.monotonic()
            out = [p.event(final) for p in self._pools.values() if p.dirty or final]
            for p in self._pools.values():
                p.dirty = False
        for e in out:
            self._send(e)

    def total(self, role: str | None = None) -> int:
        return self.counts.get(role, 0) if role else sum(self.counts.values())

    def flush(self) -> None:
        self._maybe_pools(force=True)
        flush(self.store)

    def close(self) -> None:
        self._maybe_pools(force=True, final=True)
        flush(self.store)


class NullTracker(Tracker):
    """Same calls, records nothing (code paths run without a pipeline, e.g. scan.detect in a test)."""

    def __init__(self) -> None:
        super().__init__(None)

    def _send(self, event: dict[str, Any]) -> None:
        return None


def ensure(tracker: Tracker | None) -> Tracker:
    return tracker if tracker is not None else NullTracker()


# reading the feed: fold events into the current flock ---------------------------------------------------------
STATE_OF = {"spawn": "spawned", "working": "working", "done": "done", "failed": "failed"}


def fold(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """agent_id -> latest state of that agent (fields merged across its events, oldest first)."""
    out: dict[str, dict[str, Any]] = {}
    for e in rows:
        a = out.setdefault(e["agent_id"], {"agent_id": e["agent_id"]})
        for k, v in e.items():
            if k in ("event", "seq"):
                continue
            if v is not None or k not in a:
                a[k] = v
        a["state"] = STATE_OF.get(e.get("event"), e.get("event"))
        a["seq"] = e.get("seq")
    return out


def snapshot(store: Any, since: int | None = None, active_limit: int = 40) -> dict[str, Any]:
    """What GET /api/agents returns: the latest run's flock, aggregated.

    cursor   pass back as ?since= to get only agents that changed after it
    agents   every agent of the latest run (since=None/0), or only the ones changed since the cursor
    counts   per role: total (incl. pooled), spawned / working / done / failed
    active   agents spawned or working right now (newest first, capped)
    reset    true when the run changed since the cursor (the client should drop what it has)
    """
    rows = events(store)
    cursor = (rows[-1]["seq"] + 1) if rows else 0
    run = rows[-1].get("run") if rows else None
    mine = [e for e in rows if e.get("run") == run]
    agents = fold(mine)
    first = mine[0].get("seq", 0) if mine else 0
    reset = not since or since > cursor or first >= since   # a new run (or a wiped feed) since the cursor
    if not reset:
        changed = {e["agent_id"] for e in mine if e.get("seq", -1) >= since}
        shown = [a for a in agents.values() if a["agent_id"] in changed]
    else:
        shown = list(agents.values())
    counts: dict[str, dict[str, int]] = {r: {"total": 0, "spawned": 0, "working": 0, "done": 0, "failed": 0}
                                         for r in ROLES}
    for a in agents.values():
        c = counts.setdefault(a.get("role") or "other", {"total": 0, "spawned": 0, "working": 0, "done": 0,
                                                            "failed": 0})
        if a.get("pooled"):
            n, d, f = a.get("count", 0), a.get("done", 0), a.get("failed", 0)
            c["total"] += n
            c["done"] += d
            c["failed"] += f
            c["working"] += max(0, n - d - f)
        else:
            c["total"] += 1
            c[a["state"]] = c.get(a["state"], 0) + 1
    active = sorted((a for a in agents.values() if a["state"] in ("spawned", "working")),
                    key=lambda a: -(a.get("seq") or 0))
    return {"run": run, "cursor": cursor, "reset": reset, "agents": shown,
            "counts": {r: c for r, c in counts.items() if c["total"] or r in ROLES},
            "total": sum(c["total"] for c in counts.values()),
            "active": active[:active_limit], "active_count": len(active)}
