"""Agent theater: one ordered feed of what the agents do, for GET /api/stream (and its replay). Read-only.

Built from what the run already writes, never from a second log:
  agent_status.jsonl   agents spawned / working / done (flock/agents_live.py)
  audit_log.jsonl      every gatekeeper move: checks, investigator case files, the panel's votes (inside the judge move),
                       drafts, human decisions, Clicky's filings, rule approvals, denied attempts
  market_ledger.jsonl  bets staked and settled, with each judge's new balance (flock/market.py)
  proposed_rules.json  what the Learner proposes

Event shape (every event): cursor (monotonically increasing integer), at, type, lane (agents | judges | market | human |
learn), actor, text (one human-readable sentence), plus structured fields per type:
  run_started       run
  agent_spawned / agent_working / agent_done / agent_failed    role, agent_id, subject, summary
  workers           count, stores, findings, rows (worker noise, aggregated)
  checked           count, gap_total, by_type (the checkers' findings, aggregated)
  case_file         finding_id, trigger, why, facts, next_step (an investigator's case file)
  vote              finding_id, judge, outcome, confidence, model, round, reason, weight
  revision          finding_id, judge, from, to, confidence, changed (the debate round)
  second_opinion    finding_id, judge (reviewer), outcome, confidence, model (audit sample)
  verdict           finding_id, outcome, confidence, gap, check_type, channel, reason
  rule_applied      finding_id, rule_id, outcome (an approved rule settled it; no panel, no bets)
  memo              finding_id, memo (the reviewer's note for the human on a split)
  escalation        finding_id, rank, budget, value, status (queued | deferred), gap
  bet_staked        finding_id, judge, outcome, confidence, stake, to_win, at_risk, balances
  bet_settled       finding_id, judge, payout, result, truth, balance, balances
  dispute_drafted   finding_id, gap, channel
  human_decision    finding_id, decision (approve | reject | recover), human, reason
  filed             finding_id, portal_ref
  follow_up         finding_id, action (follow_up | approve_follow_up | reject_follow_up | send_follow_up), state,
                    claim_ref, sent_ref (a vendor follow-up on a filed, unpaid claim)
  denied            finding_id, attempted, by, role, reason (the gatekeeper refused a move)
  rule_proposed     rule_id, description, evidence_count, precedents
  rule_approved     rule_id, human, description
  journal           count (journal entries drafted)
  *_bulk            judged_bulk, bets_bulk, drafted_bulk, decided_bulk, settled_bulk, filed_bulk, cases_bulk, ranked:
                    counts past the per-run caps below, so a year of data stays a few hundred events

Year scale (and a watchable stage): a finding is told in full (three votes, revisions, verdict, memo, escalation, bets,
decision, settlements) when it is the first of its kind (check type x outcome), one of the first HIGHLIGHT, one of the
first FULL_CONFLICTS conflicts, or it had a debate or a memo (capped at CONFLICT_CAP). Every other escalation (up to
CONFLICT_CAP) still pops into the human lane with its attention rank, and its human decision is shown. The rest are
counted into one bulk event per kind per batch. Workers, checks, ranks and journal entries are always aggregated. Files
are tailed by byte offset, so a poll reads only what is new.

Ordering: sources are merged by the second they happened in, then by pipeline stage (the audit log keeps whole seconds),
and bets are anchored to the move that caused them: stakes right after their finding's verdict, settlements right after
the human decision. Once an event has a cursor it never moves; a late line gets a later cursor.

Replay (GET /api/stream/replay): the same events rebuilt from the files, each given a delay by type (a vote takes longer
to read than a worker spawning), divided by speed, clamped to [MIN_GAP_MS, MAX_GAP_MS] / speed, and compressed so the
whole show fits in MAX_TOTAL_MS / speed (the floor gives way only when even the floor would not fit: then evenly
spaced). A stage-safe theater without re-running the pipeline.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from flock import agents_live, frontend, market
from flock.agents import learner

HIGHLIGHT = 6            # findings per run told in full whatever they are
FULL_CONFLICTS = 6       # conflicts told in full past the first of each kind (the rest: escalation only)
CONFLICT_CAP = 60        # per run: debates / memos / first-of-a-kind told in full; escalations shown
CASE_CAP = 8             # investigator case files told one by one per run
CAP = 40                 # case files / drafts / decisions / filings / denials told one by one per run
WORKERS_ONE_BY_ONE = 8   # a batch with more worker "done" events than this is one aggregated event
MAX_LIMIT = 1000

LANE = {"run_started": "agents", "agent_spawned": "agents", "agent_working": "agents", "agent_done": "agents",
        "agent_failed": "agents", "workers": "agents", "checked": "agents", "case_file": "agents", "cases_bulk": "agents",
        "ranked": "agents", "dispute_drafted": "agents", "drafted_bulk": "agents", "journal": "agents",
        "vote": "judges", "revision": "judges", "second_opinion": "judges", "verdict": "judges", "memo": "judges",
        "judged_bulk": "judges", "rule_applied": "learn",
        "bet_staked": "market", "bet_settled": "market", "bets_bulk": "market", "settled_bulk": "market",
        "escalation": "human", "human_decision": "human", "decided_bulk": "human", "denied": "human",
        "filed": "agents", "filed_bulk": "agents", "rule_proposed": "learn", "rule_approved": "learn", "other": "agents",
        "follow_up": "agents"}

# pipeline stage of each kind of line, to order lines that share a second (the audit log keeps whole seconds)
PHASE = {"run": -1, "agent:worker": 0, "agent:checker": 0, "check": 1, "investigate:CHECKED": 2, "judge": 3,
         "investigate:JUDGED": 4, "rank": 5, "draft": 6, "decide": 7, "agent:clicky:start": 7.5, "file": 8,
         "agent:clicky:done": 8.5, "recover": 8.6, "journal": 9, "agent:journal": 9, "agent:journal:done": 9.5,
         "agent:learner": 10, "approve_rule": 11, "other": 12}
FLUSH_ORDER = ["workers", "workers_done", "checked", "cases_bulk", "judged_bulk", "bets_bulk", "ranked",
               "drafted_bulk", "decided_bulk", "settled_bulk", "filed_bulk", "journal"]

# replay pacing: ms before each event at speed 1
PACE_MS = {"run_started": 600, "agent_spawned": 250, "agent_working": 300, "agent_done": 350, "agent_failed": 600,
           "workers": 700, "checked": 1000, "case_file": 1300, "cases_bulk": 700, "vote": 500, "revision": 800,
           "second_opinion": 900, "verdict": 800, "rule_applied": 1200, "memo": 1100, "escalation": 1400,
           "bet_staked": 220, "bets_bulk": 600, "judged_bulk": 1000, "ranked": 500, "dispute_drafted": 500,
           "drafted_bulk": 600, "human_decision": 1400, "decided_bulk": 900, "bet_settled": 300,
           "settled_bulk": 700, "filed": 800, "filed_bulk": 700, "denied": 1200, "journal": 800,
           "rule_proposed": 1800, "rule_approved": 1500}
DEFAULT_PACE_MS = 400
MIN_GAP_MS = 60
MAX_GAP_MS = 3000
MAX_TOTAL_MS = 150_000   # a whole replay at speed 1 fits in 2.5 minutes of a 3-minute demo
SPEEDS = (0.25, 20.0)


def _judge(j: Any) -> str:
    return str(j or "").replace("judge.", "")


def _usd(x: Any) -> str:
    try:
        v = float(x or 0)
    except (TypeError, ValueError):
        return str(x)
    return ("-$" if v < 0 else "$") + f"{abs(v):,.2f}"


def _pct(c: Any) -> str:
    try:
        return f"{float(c):.0%}"
    except (TypeError, ValueError):
        return "?"


def _covers(f: dict[str, Any] | None) -> str:
    """' (457 orders at S28, S38, S41)' for a rolled-up finding (flock/rollup.py), '' otherwise: never every id."""
    if not f or not f.get("rollup"):
        return ""
    from flock.frontend import orders_label   # late import: flock.frontend imports this module's neighbours
    return f" ({orders_label(f)})"


def _after(diff: dict[str, Any], key: str) -> Any:
    """The new value of an audit diff field ([old, new]), or None."""
    v = (diff or {}).get(key)
    return v[1] if isinstance(v, list) and len(v) == 2 else None


# sources ---------------------------------------------------------------------------------------------------------
class _Tail:
    """A JSONL file read from a byte offset. replaced() when the file was truncated or re-created (a new run)."""

    def __init__(self, path: Path):
        self.path, self.offset = Path(path), 0
        self.head: bytes | None = None
        self.inode: tuple | None = None

    def _first(self) -> bytes | None:
        try:
            with self.path.open("rb") as fh:
                return fh.read(4096).split(b"\n", 1)[0]
        except OSError:
            return None

    def replaced(self) -> bool:
        if not self.offset:
            return False
        try:
            st = self.path.stat()
        except OSError:
            return True
        return st.st_size < self.offset or (st.st_ino, st.st_dev) != self.inode or self._first() != self.head

    def read(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("rb") as fh:
                st = os.fstat(fh.fileno())
                fh.seek(self.offset)
                data = fh.read()
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []                       # nothing, or a line still being written
        if not self.offset:
            self.head, self.inode = data.split(b"\n", 1)[0], (st.st_ino, st.st_dev)
        self.offset += end + 1
        rows = []
        for line in data[:end].split(b"\n"):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue                # a damaged line is skipped, never fatal
        return rows

    def rewind(self) -> None:
        self.offset, self.head, self.inode = 0, None, None


class _List:
    """The same for a store without files (e.g. SpacetimeStore): the whole list, sliced by count."""

    def __init__(self, fn: Callable[[], list[dict[str, Any]]]):
        self.fn, self.offset, self.head = fn, 0, None

    def replaced(self) -> bool:
        if not self.offset:
            return False
        rows = self.fn()
        return len(rows) < self.offset or (bool(rows) and json.dumps(rows[0], default=str) != self.head)

    def read(self) -> list[dict[str, Any]]:
        rows = self.fn()
        if rows and not self.offset:
            self.head = json.dumps(rows[0], default=str)
        new, self.offset = rows[self.offset:], len(rows)
        return new

    def rewind(self) -> None:
        self.offset, self.head = 0, None


def _sources(store: Any) -> tuple[Any, Any, _Tail, Path]:
    log_path = getattr(store, "log_path", None)
    feed = getattr(getattr(store, "feed", None), "path", None)
    audit = _Tail(log_path) if log_path else _List(store.log)
    agents = _Tail(feed) if feed else _List(lambda: agents_live.events(store))
    return audit, agents, _Tail(market.ledger_path(store)), learner._proposed_path(store)


# the feed --------------------------------------------------------------------------------------------------------
class Theater:
    """The feed for one store. poll() turns new lines into events; read(since) pages them. Thread-safe."""

    def __init__(self, store: Any, highlight: int = HIGHLIGHT, conflict_cap: int = CONFLICT_CAP, cap: int = CAP):
        self.store, self.highlight, self.conflict_cap, self.cap = store, highlight, conflict_cap, cap
        self.audit, self.agents, self.ledger, self.proposed = _sources(store)
        self.lock = threading.Lock()
        self.cursor = 0                     # last cursor handed out; never goes down while this process lives
        self.events: list[dict[str, Any]] = []
        self.first = 1                      # first cursor of the current run's events
        self._epoch()

    # state of one run --------------------------------------------------------------------------------------------
    def _epoch(self) -> None:
        self.run: str | None = None
        self.n: dict[str, int] = {}         # events told one by one, per kind
        self.highlighted: set[str] = set()  # findings shown one by one in the human lane
        self.full: set[str] = set()         # findings told in full: votes, verdict, bets, settlements
        self.combos: set[tuple] = set()     # (check_type, outcome) already told in full this run
        self.judged: dict[str, str] = {}    # finding_id -> "one" | "bulk": where its stakes go
        self.decided: dict[str, str] = {}   # finding_id -> "one" | "bulk": where its settlements go
        self.stakes: dict[str, list[dict[str, Any]]] = {}
        self.settles: dict[str, list[dict[str, Any]]] = {}
        self.bets: set[str] = set()
        self.bal = {j: market.START for j in market.JUDGES}
        self.rules_seen: set[tuple] = set()
        self.learner_seen = False
        try:
            self.proposed_m0 = Path(self.proposed).stat().st_mtime
        except (OSError, TypeError):
            self.proposed_m0 = 0.0
        self.agg: dict[str, dict[str, Any]] = {}
        self.phase: float | None = None
        self._findings: dict[str, dict[str, Any]] | None = None

    def _reset(self) -> None:
        for src in (self.audit, self.agents, self.ledger):
            src.rewind()
        self.events = []
        self.first = self.cursor + 1
        self._epoch()

    def _rebuild_ledger(self) -> None:
        """Re-read the whole ledger for the current run: balances before it, its stakes and settlements pending until
        their move is told. Called when the run changes (stakes are filtered by run id)."""
        self.ledger.rewind()
        self.bets, self.stakes, self.settles = set(), {}, {}
        self.bal = {j: market.START for j in market.JUDGES}
        self._ledger(self.ledger.read())

    def _ledger(self, rows: list[dict[str, Any]]) -> None:
        for e in rows:
            k = e.get("kind")
            if k == "reset":
                self.bal = {j: market.START for j in market.JUDGES}
            elif k == "stake" and (self.run is None or e.get("run_id") in (self.run, "", None)):
                self.bets.add(e["bet_id"])
                self.stakes.setdefault(e["finding_id"], []).append(e)
            elif k == "settle":
                if e.get("bet_id") in self.bets:
                    self.settles.setdefault(e["finding_id"], []).append(e)
                else:                       # an earlier run's bet: only its effect on the balance carries over
                    self.bal[e["judge"]] = e.get("balance", self.bal.get(e["judge"], market.START))

    def findings(self) -> dict[str, dict[str, Any]]:
        if self._findings is None:
            try:
                self._findings = {f["finding_id"]: f for f in self.store.all()}
            except Exception:  # a reader never breaks on the store
                self._findings = {}
        return self._findings

    # polling -----------------------------------------------------------------------------------------------------
    def poll(self) -> None:
        with self.lock:
            if self.audit.replaced() or self.agents.replaced():
                self._reset()
            self._findings = None
            agent_rows = self.agents.read()
            run = next((e.get("run") for e in reversed(agent_rows) if e.get("run")), None)
            if run and run != self.run:
                if self.run is not None:    # a run without --reset: a new act on the same stage
                    self.events, self.first = [], self.cursor + 1
                    self._epoch()
                self.run = run
                self._rebuild_ledger()
                self._emit({"type": "run_started", "at": next((e["at"] for e in agent_rows if e.get("run") == run),
                                                              _now()),
                            "actor": "pipeline", "run": run, "text": f"Run {run} started: the flock is waking up"})
            else:
                self._ledger(self.ledger.read())
            audit_rows = self.audit.read()
            self._batch(agent_rows, audit_rows)
            self._rules()

    def _batch(self, agent_rows: list[dict[str, Any]], audit_rows: list[dict[str, Any]]) -> None:
        items = []
        workers_done = sum(1 for e in agent_rows if e.get("role") == "worker" and e.get("event") == "done")
        for i, e in enumerate(agent_rows):
            role = e.get("role")
            if role in ("judge", "investigator", "writer") or str(e.get("agent_id", "")).endswith(agents_live.POOL):
                if e.get("event") != "failed":
                    continue                # the audit log tells their work (votes, case files, drafts)
            if role == "clicky":
                ph = PHASE["agent:clicky:done" if e.get("event") == "done" else "agent:clicky:start"]
            else:
                ph = PHASE.get(f"agent:{role}:{e.get('event')}", PHASE.get(f"agent:{role}", PHASE["other"]))
            items.append((str(e.get("at", ""))[:19], ph, 0, i, "agent", e))
        for i, r in enumerate(audit_rows):
            a = r.get("action")
            if r.get("subject_type") == "journal_entry" or (a == "draft" and r.get("actor") == "journal"):
                ph = PHASE["journal"]
            elif a == "investigate":
                ph = PHASE["investigate:" + ("JUDGED" if r.get("before") == "JUDGED" else "CHECKED")]
            elif a in ("approve", "reject") and r.get("subject_type") != "rule":
                ph = PHASE["decide"]
            else:
                ph = PHASE.get(a, PHASE["other"])
            items.append((str(r.get("at", ""))[:19], ph, 1, i, "audit", r))
        items.sort(key=lambda t: t[:4])
        for at, ph, _, _, src, row in items:
            if self.phase is not None and ph != self.phase:
                self._flush()
            self.phase = ph
            if src == "agent":
                self._agent(row, workers_done)
            else:
                self._audit(row)
        self._flush()
        for fid in list(self.stakes):       # bets whose verdict was told in an earlier poll
            if fid in self.judged:
                self._stakes(fid)
        for fid in list(self.settles):
            if fid in self.decided:
                self._settles(fid)
        self._flush()

    # one line -> events --------------------------------------------------------------------------------------
    def _emit(self, ev: dict[str, Any]) -> dict[str, Any]:
        self.cursor += 1
        ev = {"cursor": self.cursor, "at": ev.get("at") or _now(), "type": ev["type"],
              "lane": LANE.get(ev["type"], "agents"), **{k: v for k, v in ev.items() if k not in ("at", "type")}}
        self.events.append(ev)
        return ev

    def _one(self, kind: str, limit: int) -> bool:
        """True while this kind is still told one by one this run (and counts it)."""
        if self.n.get(kind, 0) >= limit:
            return False
        self.n[kind] = self.n.get(kind, 0) + 1
        return True

    def _add(self, kind: str, at: str, **counts: Any) -> dict[str, Any]:
        a = self.agg.setdefault(kind, {"at": at, "count": 0})
        a["count"] += 1
        for k, v in counts.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                a[k] = a.get(k, 0) + v
            elif isinstance(v, str):
                a.setdefault(k, {})
                a[k][v] = a[k].get(v, 0) + 1
            elif isinstance(v, list):
                a.setdefault(k, [])
                a[k].extend(x for x in v if x not in a[k])
        return a

    def _agent(self, e: dict[str, Any], workers_done: int) -> None:
        role, ev, at = e.get("role"), e.get("event"), e.get("at")
        base = {"at": at, "actor": e.get("agent_id"), "role": role, "agent_id": e.get("agent_id"),
                "subject": e.get("subject"), "summary": e.get("summary") or ""}
        if ev == "failed":
            self._emit({**base, "type": "agent_failed", "text": f"{e.get('agent_id')} failed: {e.get('summary')}"})
            return
        if role == "worker":
            if ev in ("spawn", "working"):
                if ev == "spawn":
                    self._add("workers", at, stores=[e.get("subject")])
            elif workers_done > WORKERS_ONE_BY_ONE:
                self._add("workers_done", at, findings=e.get("findings") or 0, rows=e.get("rows") or 0,
                          stores=[e.get("subject")])
            else:
                a = self.agg.pop("workers", None)   # "N workers spawned" before the first one reports done
                if a:
                    self._emit(_bulk("workers", a, self._balances()))
                self._emit({**base, "type": "agent_done", "findings": e.get("findings"),
                            "text": f"Worker {e.get('subject')} done: {e.get('summary')}"})
            return
        if role == "learner":
            self.learner_seen = True
        word = {"spawn": "agent_spawned", "working": "agent_working", "done": "agent_done"}.get(ev)
        if not word:
            return
        name = {"checker": "Deposit checker", "clicky": "Clicky", "journal": "Journal drafter",
                "learner": "Learner"}.get(role, str(role).capitalize())
        text = {"agent_spawned": f"{name} spawned" + (f" for {e.get('subject')}" if e.get("subject") else ""),
                "agent_working": f"{name}: {e.get('summary') or 'working'}",
                "agent_done": f"{name} done: {e.get('summary') or 'finished'}"}[word]
        self._emit({**base, "type": word, "text": text})

    def _audit(self, r: dict[str, Any]) -> None:
        a, fid, diff, at = r.get("action"), r.get("subject_id"), r.get("diff") or {}, r.get("at")
        f = self.findings().get(fid) or {}
        if not r.get("allowed", True):
            if self._one("denied", self.cap):
                self._emit({"type": "denied", "at": at, "actor": "gatekeeper", "finding_id": fid, "attempted": a,
                            "by": r.get("actor"), "role": r.get("role"), "reason": r.get("reason"),
                            "text": f"Gatekeeper refused: {r.get('actor')} ({r.get('role')}) tried to {a} {fid}"})
            return
        if r.get("subject_type") == "journal_entry" or (a == "draft" and r.get("actor") == "journal"):
            self._add("journal", at)
            return
        if a == "check":
            self._add("checked", at, gap_total=abs(float(f.get("gap") or 0)), by_type=f.get("check_type") or "?")
            return
        if a == "investigate":
            cf = _after(diff, "case_file") or f.get("case_file") or {}
            if self._one("case_file", CASE_CAP):
                self._emit({"type": "case_file", "at": at, "actor": f"investigator:{fid}", "finding_id": fid,
                            "trigger": cf.get("trigger"), "why": cf.get("why"), "facts": (cf.get("facts") or [])[:3],
                            "next_step": cf.get("next_step"),
                            "text": f"Investigator opened a case file on {fid}: {cf.get('why') or 'unexplained gap'}."
                                    + (f" Next: {cf['next_step']}" if cf.get("next_step") else "")})
            else:
                self._add("cases_bulk", at)
            return
        if a == "judge":
            self._judge(r, f)
            return
        if a == "rank":
            self._add("ranked", at)
            return
        if a == "draft":
            gap = f.get("gap")
            if self._one("draft", self.cap) or fid in self.highlighted:
                self._emit({"type": "dispute_drafted", "at": at, "actor": r.get("actor"), "finding_id": fid,
                            "gap": gap, "channel": f.get("channel"),
                            "text": f"Writer drafted a {_usd(abs(float(gap or 0)))} dispute to {f.get('channel')} "
                                    f"for {fid}{_covers(f)}; it waits for a human"})
            else:
                self._add("drafted_bulk", at, gap_total=abs(float(gap or 0)))
            return
        if r.get("subject_type") == "rule" or a == "approve_rule":
            self._emit({"type": "rule_approved", "at": at, "actor": r.get("actor"), "rule_id": fid,
                        "human": r.get("actor"), "description": r.get("reason"),
                        "text": f"{r.get('actor')} approved rule {fid}: {r.get('reason')}. The panel applies it "
                                "next run"})
            return
        if a in ("approve", "reject", "recover"):
            one = fid in self.highlighted or self._one("decide", self.cap)
            self.decided[fid] = "one" if fid in self.full or (one and fid not in self.judged) else "bulk"
            self.learner_seen = self.learner_seen or a in ("approve", "reject")
            if one:
                verb = {"approve": "approved", "reject": "rejected", "recover": "marked recovered"}[a]
                self._emit({"type": "human_decision", "at": at, "actor": r.get("actor"), "finding_id": fid,
                            "decision": a, "human": r.get("actor"), "role": r.get("role"), "reason": r.get("reason"),
                            "gap": f.get("gap"),
                            "text": f"{r.get('actor')} {verb} {fid}" + (f": \"{r['reason']}\"" if r.get("reason")
                                                                        else "")})
            else:
                self._add("decided_bulk", at, decision=a)
            self._settles(fid)
            return
        if a == "file":
            ref = _after(diff, "portal_ref") or f.get("portal_ref")
            if fid in self.highlighted or self._one("file", self.cap):
                self._emit({"type": "filed", "at": at, "actor": "clicky", "finding_id": fid, "portal_ref": ref,
                            "gap": f.get("gap"),
                            "text": f"Clicky filed {fid}{_covers(f)} in the sandbox portal: {ref}"})
            else:
                self._add("filed_bulk", at, refs=[ref] if ref else [])
            return
        if a in FOLLOW_UP_TEXT and r.get("allowed", True):
            fu = _after(diff, "follow_up") or f.get("follow_up") or {}
            self._emit({"type": "follow_up", "at": at, "actor": r.get("actor"), "finding_id": fid, "action": a,
                        "state": fu.get("state"), "claim_ref": fu.get("claim_ref"), "sent_ref": fu.get("sent_ref"),
                        "gap": f.get("gap"),
                        "text": FOLLOW_UP_TEXT[a].format(actor=r.get("actor"), fid=fid, covers=_covers(f),
                                                         claim=fu.get("claim_ref"), sent=fu.get("sent_ref"),
                                                         amount=_usd(fu.get("amount")))})
            return
        if self._one("other", self.cap):
            self._emit({"type": "other", "at": at, "actor": r.get("actor"), "finding_id": fid, "action": a,
                        "text": f"{r.get('actor')} {a} {fid}: {r.get('before')} -> {r.get('after')}"})

    def _judge(self, r: dict[str, Any], f: dict[str, Any]) -> None:
        fid, diff, at = r.get("subject_id"), r.get("diff") or {}, r.get("at")
        votes = _after(diff, "votes")
        votes = votes if votes is not None else f.get("votes") or []
        outcome = _after(diff, "outcome") or f.get("outcome")
        conf = _after(diff, "confidence") if "confidence" in diff else f.get("confidence")
        reason = _after(diff, "reason") or f.get("reason") or ""
        memo = _after(diff, "review_memo")
        att = _after(diff, "attention") or (f.get("attention") if outcome == "conflict" else None)
        gap, ct, ch = f.get("gap"), f.get("check_type"), f.get("channel")
        debated = any(v.get("round") == 2 for v in votes)
        if reason.startswith("Applied approved rule"):
            self.judged[fid] = "one"
            if self._one("rule_applied", self.cap):
                rid = reason.split("Applied approved rule ", 1)[1].split(":", 1)[0]
                self._emit({"type": "rule_applied", "at": at, "actor": "panel", "finding_id": fid, "rule_id": rid,
                            "outcome": outcome, "gap": gap, "check_type": ct, "channel": ch,
                            "text": f"Learned rule {rid} settled {fid} ({ch} {ct}) as {outcome}: no panel, no human"})
            else:
                self._add("judged_bulk", at, outcome=outcome or "?", by_rule=1)
            return
        combo = (ct, outcome)
        one = bool(((debated or memo) and self._one("debate", self.conflict_cap))
                   or (combo not in self.combos and self._one("combo", self.conflict_cap))
                   or self._one("judged", self.highlight)
                   or (outcome == "conflict" and self._one("conflict", FULL_CONFLICTS)))
        self.combos.add(combo)
        self.judged[fid] = "one" if one else "bulk"
        if not one:
            if att and self._one("escalation", self.conflict_cap):   # still shown in the human lane, briefly
                self.highlighted.add(fid)
                self._escalation(fid, at, att, gap, ct, ch)
            else:
                self._add("judged_bulk", at, outcome=outcome or "?", gap_total=abs(float(gap or 0)))
            self._stakes(fid)
            return
        self.full.add(fid)
        self.highlighted.add(fid)
        what = f"{ch} {str(ct or '').replace('_', ' ')} {_usd(gap)}"
        for v in votes:
            rd = v.get("round")
            model = v.get("model") or ("offline rules" if not v.get("fallback") else "offline fallback")
            common = {"at": at, "actor": v.get("judge"), "finding_id": fid, "judge": _judge(v.get("judge")),
                      "outcome": v.get("outcome"), "confidence": v.get("confidence"), "model": model,
                      "reason": v.get("reason"), "weight": v.get("weight"), "round": rd}
            if rd in (None, 1):
                self._emit({**common, "type": "vote",
                            "text": f"{_judge(v.get('judge'))} votes {v.get('outcome')} ({_pct(v.get('confidence'))}) "
                                    f"on {fid}, {what}"})
            elif rd == 2:
                first = next((x for x in votes if x.get("judge") == v.get("judge") and x.get("round") == 1), {})
                changed = first.get("outcome") != v.get("outcome")
                self._emit({**common, "type": "revision", "from": first.get("outcome"), "to": v.get("outcome"),
                            "changed": changed,
                            "text": (f"{_judge(v.get('judge'))} changed its vote after the debate: "
                                     f"{first.get('outcome')} -> {v.get('outcome')} ({_pct(v.get('confidence'))})")
                            if changed else f"{_judge(v.get('judge'))} held {v.get('outcome')} after the debate"})
            elif rd == "audit":
                self._emit({**common, "type": "second_opinion",
                            "text": f"Reviewer ({model}) gives a second opinion on {fid}: {v.get('outcome')}"})
        agree = sum(1 for v in market.final_votes(votes) if v.get("outcome") == outcome)
        self._emit({"type": "verdict", "at": at, "actor": "panel", "finding_id": fid, "outcome": outcome,
                    "confidence": conf, "gap": gap, "check_type": ct, "channel": ch, "reason": reason[:300],
                    "agree": agree,
                    "text": (f"Panel: {fid} is {outcome}, {agree} of 3 agree ({_pct(conf)})" if outcome != "conflict"
                             else f"Panel split on {fid}: sending it to a human")})
        if memo:
            self._emit({"type": "memo", "at": at, "actor": "reviewer", "finding_id": fid, "memo": memo,
                        "text": f"Reviewer wrote a memo for the human on {fid}"})
        if att:
            self._escalation(fid, at, att, gap, ct, ch, f)
        self._stakes(fid)

    def _escalation(self, fid: str, at: Any, att: dict[str, Any], gap: Any, ct: Any, ch: Any,
                    f: dict[str, Any] | None = None) -> None:
        q = att.get("status") != "deferred"
        self._emit({"type": "escalation", "at": at, "actor": "panel", "finding_id": fid, "rank": att.get("rank"),
                    "budget": att.get("budget"), "value": att.get("value"), "status": att.get("status"),
                    "gap": gap, "check_type": ct, "channel": ch,
                    "text": (f"{fid} ({ch} {str(ct or '').replace('_', ' ')} {_usd(gap)}){_covers(f)} escalated "
                             "to a human: "
                             f"attention #{att.get('rank')} of {att.get('budget')} (worth {_usd(att.get('value'))} of "
                             "a look)") if q else
                    f"{fid} is over this week's attention budget: deferred, still waiting for a human"})

    def _stakes(self, fid: str) -> None:
        rows = self.stakes.pop(fid, [])
        if not rows:
            return
        if self.judged.get(fid) == "one":
            for b in rows:
                odds = (f"wins up to +{b.get('to_win')}, risks {b.get('at_risk')}" if b.get("to_win") or
                        b.get("at_risk") else "no lean, pays 0 either way")
                self._emit({"type": "bet_staked", "at": b.get("at"), "actor": b["judge"], "finding_id": fid,
                            "judge": _judge(b["judge"]), "outcome": b.get("outcome"), "confidence": b.get("confidence"),
                            "stake": b.get("stake"), "to_win": b.get("to_win"), "at_risk": b.get("at_risk"),
                            "bet_id": b.get("bet_id"), "balances": self._balances(),
                            "text": f"{_judge(b['judge'])} stakes {b.get('stake')} on {b.get('outcome')} ({fid}): "
                                    + odds})
        else:
            for b in rows:
                self._add("bets_bulk", b.get("at"), stake=b.get("stake") or 0)

    def _settles(self, fid: str) -> None:
        rows = self.settles.pop(fid, [])
        for s in rows:
            self.bal[s["judge"]] = s.get("balance", self.bal.get(s["judge"]))
            if self.decided.get(fid) == "one":
                sign = "+" if (s.get("payout") or 0) > 0 else ""
                self._emit({"type": "bet_settled", "at": s.get("at"), "actor": s["judge"], "finding_id": fid,
                            "judge": _judge(s["judge"]), "payout": s.get("payout"), "result": s.get("result"),
                            "truth": s.get("truth"), "called": s.get("called"), "balance": s.get("balance"),
                            "bet_id": s.get("bet_id"), "balances": self._balances(),
                            "text": f"{_judge(s['judge'])} {s.get('result')} {sign}{s.get('payout')} on {fid} "
                                    f"(truth: {s.get('truth')}); balance {s.get('balance')}"})
            else:
                a = self._add("settled_bulk", s.get("at"), payout=s.get("payout") or 0)
                a.setdefault("by_judge", {})
                a["by_judge"][_judge(s["judge"])] = round(a["by_judge"].get(_judge(s["judge"]), 0)
                                                          + (s.get("payout") or 0), 2)

    def _balances(self) -> dict[str, float]:
        return {_judge(j): b for j, b in self.bal.items()}

    def _flush(self) -> None:
        for kind in FLUSH_ORDER:
            a = self.agg.pop(kind, None)
            if a:
                self._emit(_bulk(kind, a, self._balances()))

    def _rules(self) -> None:
        if not self.proposed:
            return
        try:
            m = Path(self.proposed).stat().st_mtime
        except OSError:
            return
        if not (self.learner_seen or m > self.proposed_m0):
            return
        for r in learner._read(self.proposed):
            key = (r.get("rule_id"), r.get("description"))
            if key in self.rules_seen:
                continue
            self.rules_seen.add(key)
            self._emit({"type": "rule_proposed", "at": datetime.fromtimestamp(m, timezone.utc).isoformat(
                timespec="seconds"), "actor": "learner", "rule_id": r.get("rule_id"),
                "description": r.get("description"), "evidence_count": r.get("evidence_count"),
                "precedents": (r.get("precedents") or [])[:12], "outcome": r.get("outcome"),
                "text": f"Learner proposes {r.get('rule_id')}: {r.get('description')} "
                        f"(learned from {r.get('evidence_count')} human decisions)"})

    # reading -----------------------------------------------------------------------------------------------------
    def read(self, since: int = 0, limit: int = 200) -> dict[str, Any]:
        """Events after cursor `since`. reset=true when the client's cursor is from an earlier run (or an earlier
        server process): drop what you have and take these, from the start of the current run."""
        self.poll()
        limit = max(1, min(int(limit), MAX_LIMIT))
        with self.lock:
            reset = since <= 0 or since < self.first or since > self.cursor   # the cursor is not in this run
            start = self.first - 1 if reset else since
            picked = [e for e in self.events if e["cursor"] > start][:limit]
            last = picked[-1]["cursor"] if picked else (self.cursor if not reset else self.first - 1)
            more = bool(picked) and picked[-1]["cursor"] < self.cursor
            return {"run": self.run, "cursor": last, "first_cursor": self.first, "latest": self.cursor,
                    "reset": reset, "more": more, "events": picked, "balances": self._balances(),
                    "count": len(self.events), "poll_ms": 1000}


FOLLOW_UP_TEXT = {
    "follow_up": "Follow-up agent drafted a {amount} reminder on unpaid claim {claim} ({fid}){covers}; "
                 "it waits for a human",
    "approve_follow_up": "{actor} approved the follow-up on claim {claim} ({fid})",
    "reject_follow_up": "{actor} rejected the follow-up on claim {claim} ({fid}): not sent",
    "send_follow_up": "Clicky sent the follow-up on claim {claim} ({fid}) to the sandbox portal: {sent}",
}


def _bulk(kind: str, a: dict[str, Any], balances: dict[str, float]) -> dict[str, Any]:
    n = a["count"]
    s = "s" if n != 1 else ""
    if kind == "workers":
        st = a.get("stores", [])
        return {"type": "workers", "at": a["at"], "actor": "workers", "count": n, "stores": st[:60],
                "text": f"{n} worker{s} spawned, one per store ({_span(st)}): matching orders to payouts"}
    if kind == "workers_done":
        return {"type": "workers", "at": a["at"], "actor": "workers", "count": n, "stores": a.get("stores", [])[:60],
                "findings": a.get("findings", 0), "rows": a.get("rows", 0),
                "text": f"{n} worker{s} done: {a.get('rows', 0):,} rows read, {a.get('findings', 0):,} findings"}
    if kind == "checked":
        by = a.get("by_type", {})
        top = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:4])
        return {"type": "checked", "at": a["at"], "actor": "checkers", "count": n, "by_type": by,
                "gap_total": round(a.get("gap_total", 0), 2),
                "text": f"Checkers logged {n} finding{s} ({top}): {_usd(a.get('gap_total', 0))} in gaps, computed in "
                        "code from the deal terms"}
    if kind == "cases_bulk":
        return {"type": "cases_bulk", "at": a["at"], "actor": "investigators", "count": n,
                "text": f"{n} more case file{s} opened by investigators"}
    if kind == "judged_bulk":
        by = a.get("outcome", {})
        parts = ", ".join(f"{v} {k}" for k, v in sorted(by.items()))
        return {"type": "judged_bulk", "at": a["at"], "actor": "panel", "count": n, "by_outcome": by,
                "by_rule": a.get("by_rule", 0), "gap_total": round(a.get("gap_total", 0), 2),
                "text": f"Judges decided {n} more finding{s}: {parts}"
                        + (f" ({a['by_rule']} by learned rules)" if a.get("by_rule") else "")}
    if kind == "bets_bulk":
        return {"type": "bets_bulk", "at": a["at"], "actor": "market", "count": n, "stake": a.get("stake", 0),
                "balances": balances, "text": f"{n} more bet{s} staked ({a.get('stake', 0)} credits in all)"}
    if kind == "ranked":
        return {"type": "ranked", "at": a["at"], "actor": "ranker", "count": n,
                "text": f"Ranker scored {n} recoverable finding{s}: gap x win probability - effort"}
    if kind == "drafted_bulk":
        return {"type": "drafted_bulk", "at": a["at"], "actor": "dispute_writer", "count": n,
                "gap_total": round(a.get("gap_total", 0), 2),
                "text": f"Writers drafted {n} more dispute{s} ({_usd(a.get('gap_total', 0))})"}
    if kind == "decided_bulk":
        by = a.get("decision", {})
        return {"type": "decided_bulk", "at": a["at"], "actor": "human", "count": n, "by_decision": by,
                "text": f"Humans decided {n} more: " + ", ".join(f"{v} {k}" for k, v in sorted(by.items()))}
    if kind == "settled_bulk":
        return {"type": "settled_bulk", "at": a["at"], "actor": "market", "count": n,
                "payout": round(a.get("payout", 0), 2), "by_judge": a.get("by_judge", {}), "balances": balances,
                "text": f"{n} more bet{s} settled: " + ", ".join(f"{j} {'+' if v > 0 else ''}{v}"
                                                                for j, v in sorted(a.get("by_judge", {}).items()))}
    if kind == "filed_bulk":
        return {"type": "filed_bulk", "at": a["at"], "actor": "clicky", "count": n, "refs": a.get("refs", [])[:20],
                "text": f"Clicky filed {n} more dispute{s} in the sandbox portal"}
    return {"type": "journal", "at": a["at"], "actor": "journal", "count": n,
            "text": f"Journal drafter wrote {n} journal entr{'ies' if n != 1 else 'y'}, waiting for a human to approve"}


def _span(stores: list[Any]) -> str:
    st = sorted(str(s) for s in stores if s is not None)
    if not st:
        return "no stores"
    return st[0] if len(st) == 1 else f"{st[0]}..{st[-1]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# one theater per store, shared by every client of this process --------------------------------------------------
_theaters: dict[str, Theater] = {}
_reg = threading.Lock()


def for_store(store: Any) -> Theater:
    key = str(frontend.state_dir(store).resolve()) + f":{type(store).__name__}:{id(store)}"
    with _reg:
        t = _theaters.get(key)
        if t is None or t.store is not store:
            t = _theaters[key] = Theater(store)
        return t


# replay ------------------------------------------------------------------------------------------------------------
def schedule(events: list[dict[str, Any]], speed: float = 1.0) -> list[dict[str, Any]]:
    """Each event with delay_ms (wait before showing it) and t_ms (when, from the start), paced by type, bounded."""
    speed = min(max(float(speed), SPEEDS[0]), SPEEDS[1])
    lo, hi = MIN_GAP_MS / speed, MAX_GAP_MS / speed
    delays = [min(hi, max(lo, PACE_MS.get(e["type"], DEFAULT_PACE_MS) / speed)) if i else 0.0
              for i, e in enumerate(events)]
    total, gaps = sum(delays), max(0, len(delays) - 1)
    cap = MAX_TOTAL_MS / speed
    if total > cap and lo * gaps >= cap:  # too many events even at the floor: evenly spaced, the total still fits
        delays = [cap / gaps if i else 0.0 for i in range(len(delays))]
    elif total > cap:                     # a long run: compress toward the floor, keep the rhythm
        k = (cap - lo * gaps) / (total - lo * gaps)
        delays = [lo + (d - lo) * k if i else 0.0 for i, d in enumerate(delays)]
    out, t = [], 0.0
    for i, (e, d) in enumerate(zip(events, delays), 1):
        t += d
        out.append({**e, "cursor": i, "live_cursor": e["cursor"], "delay_ms": round(d), "t_ms": round(t)})
    return out


_replays: dict[str, tuple[tuple, list[dict[str, Any]]]] = {}


def replay_events(store: Any) -> list[dict[str, Any]]:
    """The last run's events, rebuilt from the files by a fresh theater (cached until a file changes)."""
    audit, agents, ledger, _ = _sources(store)
    sig = tuple(_size(s) for s in (audit, agents, ledger))
    key = str(frontend.state_dir(store).resolve())
    hit = _replays.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    t = Theater(store)
    t.poll()
    _replays[key] = (sig, t.events)
    return t.events


def _size(src: Any) -> Any:
    if isinstance(src, _Tail):
        try:
            st = src.path.stat()
            return st.st_size, st.st_mtime
        except OSError:
            return None
    return len(src.fn())


def replay(store: Any, speed: float = 1.0, since: int = 0, start_ms: float | None = None, now_ms: float | None = None,
           everything: bool = False, limit: int = 200) -> dict[str, Any]:
    """GET /api/stream/replay. everything: the whole paced schedule at once (the client plays it). Otherwise the events
    due by now since `start_ms` (the first call returns it; pass it back with since=<cursor>), like the live stream."""
    events = schedule(replay_events(store), speed)
    duration = events[-1]["t_ms"] if events else 0
    sp = min(max(float(speed), SPEEDS[0]), SPEEDS[1])
    base = {"mode": "replay", "run": next((e.get("run") for e in events if e.get("run")), None), "speed": sp,
            "duration_ms": duration, "count": len(events), "pace_ms": PACE_MS,
            "bounds": {"min_gap_ms": round(MIN_GAP_MS / sp), "max_gap_ms": round(MAX_GAP_MS / sp),
                       "max_total_ms": round(MAX_TOTAL_MS / sp)}}
    if everything:
        return {**base, "events": events, "cursor": len(events), "done": True}
    now = now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000
    start = start_ms if start_ms is not None else now
    elapsed = now - start
    due = [e for e in events if e["cursor"] > since and e["t_ms"] <= elapsed][:max(1, min(int(limit), MAX_LIMIT))]
    cursor = due[-1]["cursor"] if due else max(0, since)
    nxt = next((e for e in events if e["cursor"] > cursor), None)
    return {**base, "start": round(start), "elapsed_ms": round(elapsed), "cursor": cursor, "events": due,
            "done": nxt is None, "next_in_ms": max(0, round(nxt["t_ms"] - elapsed)) if nxt else None}
