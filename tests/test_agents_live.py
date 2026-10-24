"""The live flock: agent status events, investigators that spawn only when a finding is unexplained, case files that
go through the gatekeeper and never move an outcome or a dollar, the agents endpoint, and year-scale pooling."""
import json
import threading
import time

import pytest

from dashboard.app import create_app
from flock import agents_live, pipeline
from flock.agents import investigator
from flock.gatekeeper import Denied, transition
from flock.models import Actor, Finding
from flock.store import JsonStore, Store

AGENT = Actor("checkers", "agent")
HUMAN = Actor("controller", "human")
MONEY = ("expected", "actual", "gap", "outcome", "confidence", "state", "votes", "reason", "evidence")


@pytest.fixture
def paths(tmp_path):
    return {"store": JsonStore(tmp_path / "state"), "rules_path": tmp_path / "rules.json",
            "portal": tmp_path / "portal" / "disputes.jsonl", "runs_path": tmp_path / "runs.jsonl"}


def finding(fid, check_type="deposit", gap=40.0, evidence=1, **kw):
    ev = [{"source_file": f"data/raw/x{i}.csv", "row": i, "what": "a record"} for i in range(evidence)]
    return Finding(order_ids=[f"P-{fid}"], channel="ubereats", check_type=check_type, expected=gap, actual=0.0,
                   gap=gap, evidence=ev, finding_id=fid, **kw).to_dict()


# the feed -------------------------------------------------------------------------------------------------------
def test_events_are_written_with_a_cursor(tmp_path):
    s = JsonStore(tmp_path)
    t = agents_live.Tracker(s, "RUN-1")
    w = t.spawn("worker", subject="S01", agent_id="worker:S01").working("matching")
    j = t.spawn("judge", parent=w, subject="F-1", agent_id="judge:F-1")
    j.done("recoverable")
    w.done("10 orders, 1 finding", findings=1)
    w.done("twice")  # a second done is ignored
    rows = s.agents()
    assert [(e["agent_id"], e["event"]) for e in rows] == [
        ("worker:S01", "spawn"), ("worker:S01", "working"), ("judge:F-1", "spawn"), ("judge:F-1", "done"),
        ("worker:S01", "done")]
    assert [e["seq"] for e in rows] == [0, 1, 2, 3, 4] and {e["run"] for e in rows} == {"RUN-1"}
    assert rows[2]["parent_id"] == "worker:S01" and rows[-1]["findings"] == 1 and rows[-1]["ended"]
    assert (tmp_path / "agent_status.jsonl").exists()
    assert [e["seq"] for e in s.agents(3)] == [3, 4]
    folded = agents_live.fold(rows)
    assert folded["worker:S01"]["state"] == "done" and folded["judge:F-1"]["summary"] == "recoverable"
    s.reset()
    assert s.agents() == []


def test_writes_are_batched_inside_store_batch(tmp_path):
    s = JsonStore(tmp_path)
    path = tmp_path / "agent_status.jsonl"
    with s.batch(flush_every=60):
        t = agents_live.Tracker(s, "RUN-1")
        for i in range(50):
            t.spawn("worker", subject=f"S{i}").done("ok")
        assert not path.exists()                            # 100 events wait in memory, nothing written yet
        assert len(s.agents()) == 100                       # a reader in this process still sees everything
    assert len(path.read_text().splitlines()) == 100


def test_protocol_default_is_a_no_op():
    class Minimal:  # a backend with only the six original methods
        pass

    class Explicit(Store):
        pass

    agents_live.emit(Minimal(), {"event": "spawn", "agent_id": "x"})
    assert agents_live.events(Minimal()) == [] and agents_live.snapshot(Minimal())["total"] == 0
    assert Explicit().agent_event({"agent_id": "x"}) is None and Explicit().agents() == []
    t = agents_live.Tracker(Minimal(), "RUN-1")
    t.spawn("worker", subject="S01").done("fine")
    t.close()


def test_pipeline_runs_on_a_store_without_the_feed(paths):
    class SixMethods:
        def __init__(self, inner):
            self.inner, self.dir = inner, inner.dir

        def __getattr__(self, name):
            if name in ("get", "all", "put_new", "commit", "log", "reset"):
                return getattr(self.inner, name)
            raise AttributeError(name)

    store = pipeline.run(reset=True, forget=True, demo_approve=True,
                         **{**paths, "store": SixMethods(paths["store"])})
    assert len(store.all()) == 6 and paths["store"].agents() == []


def test_year_scale_pools_instead_of_writing_thousands(tmp_path):
    s = JsonStore(tmp_path)
    with s.batch():
        t = agents_live.Tracker(s, "RUN-Y", sample=20)
        workers = [t.spawn("worker", subject=f"S{i:02d}").working() for i in range(56)]
        for i in range(5000):
            t.spawn("judge", parent=workers[i % 56], subject=f"F-{i}").done("recoverable")
        for w in workers:
            w.done("ok")
        t.close()
    lines = (tmp_path / "agent_status.jsonl").read_text().splitlines()
    assert len(lines) < 20 * 3 + 20 * 2 + 100            # sampled workers + judges + a handful of pool updates
    snap = agents_live.snapshot(s)
    assert snap["counts"]["judge"] == {"total": 5000, "spawned": 0, "working": 0, "done": 5000, "failed": 0}
    assert snap["counts"]["worker"]["total"] == 56 and snap["counts"]["worker"]["done"] == 56
    pools = [a for a in snap["agents"] if a.get("pooled")]
    assert {a["agent_id"] for a in pools} == {"judge*", "worker*"} and snap["total"] == 5056
    assert len(json.dumps(snap)) < 60_000                 # what the dashboard downloads stays small


# investigator triggers ----------------------------------------------------------------------------------------
def test_triggers_fire_only_on_unexplained_findings():
    assert investigator.detection_trigger(finding("A", "deposit", evidence=1)) == "deposit_gap_unsupported"
    assert investigator.detection_trigger(finding("B", "deposit", evidence=2)) is None      # two sources
    assert investigator.detection_trigger(finding("C", "deposit", gap=0.03, evidence=1)) is None  # rounding
    assert investigator.detection_trigger(finding("D", "missing_order", evidence=1)) == "missing_order_unsupported"
    assert investigator.detection_trigger(finding("E", "missing_order", evidence=3)) is None
    assert investigator.detection_trigger(finding("F", "commission", evidence=1)) is None   # the panel's job
    conflict = {**finding("G", "promo", evidence=2), "outcome": "conflict"}
    assert investigator.conflict_trigger(conflict) == "conflict_no_memo"
    assert investigator.conflict_trigger({**conflict, "review_memo": "each side..."}) is None
    assert investigator.conflict_trigger({**conflict, "case_file": {"trigger": "x"}}) is None
    assert investigator.conflict_trigger({**conflict, "outcome": "recoverable"}) is None


def test_fixture_run_investigates_only_the_two_conflicts(paths):
    store = pipeline.run(reset=True, forget=True, demo_approve=True, **paths)
    got = {f["finding_id"]: f["case_file"]["trigger"] for f in store.all() if f.get("case_file")}
    assert got == {"F-0003": "conflict_no_memo", "F-0004": "conflict_no_memo"}
    for fid in got:
        [entry] = [e for e in store.log(fid) if e["action"] == "investigate"]
        assert (entry["actor"], entry["role"], entry["allowed"]) == ("investigator", "agent", True)
        assert entry["before"] == entry["after"] == "JUDGED" and list(entry["diff"]) == ["case_file"]
    cf = store.get("F-0003")["case_file"]
    assert cf["summary_by"] == "template" and cf["next_step"] and cf["facts"]
    assert {c["authority"] for c in cf["claims"]} == {"contract", "unauth_email"}   # non-binding claims included
    snap = agents_live.snapshot(store)
    inv = [a for a in snap["agents"] if a["role"] == "investigator"]
    assert {a["subject"] for a in inv} == {"F-0003", "F-0004"} and {a["state"] for a in inv} == {"done"}
    assert all(a["parent_id"] == f"judge:{a['subject']}" for a in inv)
    judges = [a for a in snap["agents"] if a["role"] == "judge"]
    assert len(judges) == 6 and all(a["parent_id"] == "worker:fixtures" for a in judges)
    assert {r: c["total"] for r, c in snap["counts"].items() if c["total"]} == {
        "worker": 1, "judge": 6, "investigator": 2, "writer": 3, "clicky": 1, "learner": 1}
    assert snap["active_count"] == 0


def test_case_files_never_change_outcome_or_money(paths, tmp_path, monkeypatch):
    with_inv = pipeline.run(reset=True, forget=True, demo_approve=True, **paths)
    a = {f["finding_id"]: {k: f.get(k) for k in MONEY} for f in with_inv.all()}
    other = {**paths, "store": JsonStore(tmp_path / "state2"), "rules_path": tmp_path / "r2.json",
             "portal": tmp_path / "p2.jsonl", "runs_path": tmp_path / "runs2.jsonl"}
    monkeypatch.setattr(investigator, "run", lambda *a, **k: [])
    without = pipeline.run(reset=True, forget=True, demo_approve=True, **other)
    b = {f["finding_id"]: {k: f.get(k) for k in MONEY} for f in without.all()}
    assert a == b


def test_gatekeeper_keeps_investigate_to_the_case_file(tmp_path):
    s = JsonStore(tmp_path)
    s.put_new(finding("F-T", "deposit"))
    with pytest.raises(Denied):
        transition(s, "F-T", "investigate", investigator.ACTOR, patch={"case_file": {}})  # NEW: not allowed yet
    transition(s, "F-T", "check", AGENT)
    with pytest.raises(Denied):
        transition(s, "F-T", "investigate", investigator.ACTOR, patch={"case_file": {}, "gap": 0.0})
    with pytest.raises(Denied):
        transition(s, "F-T", "investigate", HUMAN, patch={"case_file": {}})
    transition(s, "F-T", "investigate", investigator.ACTOR, patch={"case_file": {"summary": "ok"}})
    transition(s, "F-T", "judge", AGENT, patch={"outcome": "conflict"})
    transition(s, "F-T", "approve", HUMAN)
    with pytest.raises(Denied):
        transition(s, "F-T", "investigate", investigator.ACTOR, patch={"case_file": {}})  # decided: too late
    f = s.get("F-T")
    assert f["gap"] == 40.0 and f["case_file"] == {"summary": "ok"} and f["state"] == "APPROVED"
    denied = [e for e in s.log("F-T") if not e["allowed"]]
    assert len(denied) == 4 and "may only write case_file, not gap" in denied[1]["reason"]


def test_investigators_are_capped(tmp_path, monkeypatch):
    s = JsonStore(tmp_path)
    jobs = []
    for i in range(8):
        s.put_new(finding(f"F-{i}", "deposit"))
        transition(s, f"F-{i}", "check", AGENT)
        jobs.append((s.get(f"F-{i}"), "deposit_gap_unsupported", "checker:deposits"))
    live, peak, lock = [0], [0], threading.Lock()
    real = investigator.build

    def slow(*a, **k):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.02)
        with lock:
            live[0] -= 1
        return real(*a, **k)

    monkeypatch.setattr(investigator, "build", slow)
    t = agents_live.Tracker(s, "RUN-C")
    done = investigator.run(s, jobs, investigator.Casebook(), t, limit=2)
    assert sorted(done) == [f"F-{i}" for i in range(8)] and peak[0] <= 2
    assert all(s.get(f"F-{i}")["case_file"]["trigger"] == "deposit_gap_unsupported" for i in range(8))


def test_investigator_reads_the_std_tables():
    lines = [{"payout_line_id": f"L{i}", "channel": "ubereats", "store_id": "S01", "order_id": f"O{i}",
              "platform_order_id": f"P{i}", "gross": "20", "net": "15", "commission": "5", "payout_id": "PO-1",
              "payout_date": "2026-09-15", "source_file": "data/raw/ue.csv", "source_row": str(i + 2)} for i in range(4)]
    deposits = [{"deposit_id": "D-1", "channel": "ubereats", "amount": "59.00", "deposit_date": "2026-09-17",
                 "memo": "UBER", "source_file": "data/raw/bank.csv", "source_row": "5"},
                {"deposit_id": "D-2", "channel": "doordash", "amount": "60.00", "deposit_date": "2026-09-17",
                 "memo": "", "source_file": "data/raw/bank.csv", "source_row": "6"}]
    terms = [{"term_id": "T-1", "channel": "ubereats", "store_id": "all", "term_type": "commission", "value": 0.25,
              "authority": "contract", "quote": "25%", "source_file": "c.json", "effective_from": "2026-01-01"},
             {"term_id": "T-2", "channel": "ubereats", "store_id": "S01", "term_type": "commission", "value": 0.15,
              "authority": "call_note", "quote": "15% for you", "source_file": "n.txt", "effective_from": "2026-06-01"},
             {"term_id": "T-3", "channel": "doordash", "store_id": "all", "term_type": "commission", "value": 0.2,
              "authority": "contract", "quote": "20%", "source_file": "d.json", "effective_from": "2026-01-01"}]
    book = investigator.Casebook({"payout_lines": lines, "bank_deposits": deposits}, terms)
    dep = {**finding("F-D", "deposit"), "order_ids": ["PO-1"]}
    com = {**finding("F-C", "commission", evidence=2), "order_ids": ["O1"], "outcome": "conflict"}
    book.prepare([dep], [dep, com])
    cf = investigator.build(dep, "deposit_gap_unsupported", book, [dep, com])
    assert cf["store_id"] == "S01" and cf["payouts"][0]["lines"] == 4 and cf["payouts"][0]["net"] == 60.0
    assert [d["deposit_id"] for d in cf["nearby_deposits"]] == ["D-1"] and cf["nearby_deposits"][0]["off_by"] == -1.0
    book.prepare([com], [dep, com])     # already read for every kind in the run: no second pass
    cf = investigator.build(com, "conflict_no_memo", book, [dep, com])
    assert [c["term_id"] for c in cf["claims"]] == ["T-1", "T-2"] and cf["claims"][1]["binding"] is False
    assert [ln["payout_line_id"] for ln in cf["payouts"][0]["other_lines"]] == ["L0", "L2", "L3"]
    assert "call_note" in cf["next_step"].replace(" ", "_")


# the endpoint -----------------------------------------------------------------------------------------------------
def test_agents_endpoint_and_finding_page(paths):
    store = pipeline.run(reset=True, forget=True, demo_approve=True, **paths)
    client = create_app(store, runs_path=paths["runs_path"]).test_client()
    full = client.get("/api/agents").get_json()
    assert full["reset"] and full["total"] == 14 and full["counts"]["investigator"]["done"] == 2
    assert len(full["agents"]) == 14 and full["active"] == []
    again = client.get(f"/api/agents?since={full['cursor']}").get_json()
    assert again["agents"] == [] and not again["reset"] and again["cursor"] == full["cursor"]
    assert client.get("/api/agents?since=x").status_code == 400
    d = client.get("/api/findings/F-0003").get_json()
    assert d["finding"]["case_file"]["trigger"] == "conflict_no_memo"
    assert [a["role"] for a in d["agents"]] == ["judge", "investigator"]
    # a new run: the client's old cursor gets a reset and the new run's agents
    store2 = pipeline.run(demo_approve=True, **paths)
    nxt = create_app(store2, runs_path=paths["runs_path"]).test_client().get(
        f"/api/agents?since={full['cursor']}").get_json()
    assert nxt["reset"] and nxt["run"] != full["run"]
