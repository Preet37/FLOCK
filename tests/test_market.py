"""Judge market: bets settle against reality (through the gatekeeper), balances weight the panel, attention is a budget."""
import json

import pytest

from conftest import fake
from dashboard.app import create_app
from flock import market, pipeline
from flock.agents import panel
from flock.gatekeeper import GATEKEEPER, Denied, transition
from flock.models import Actor
from flock.store import JsonStore

AGENT = Actor("panel", "agent")
HUMAN = Actor("controller", "human")


def V(judge, outcome, conf):
    return {"judge": judge, "outcome": outcome, "confidence": conf, "reason": f"{judge} says {outcome}"}


VOTES = [V("judge.strict", "recoverable", 0.9), V("judge.operator", "recoverable", 0.6), V("judge.skeptic", "conflict", 0.7)]


@pytest.fixture
def store(tmp_path):
    return JsonStore(tmp_path / "state")


@pytest.fixture
def paths(store, tmp_path):
    return {"store": store, "rules_path": tmp_path / "rules.json", "portal": tmp_path / "portal" / "disputes.jsonl"}


def judged(store, fid="F-0001", votes=VOTES, outcome="conflict"):
    store.put_new(fake(fid))
    transition(store, fid, "check", AGENT)
    transition(store, fid, "judge", AGENT, patch={"outcome": outcome, "votes": votes, "confidence": 0.73})
    market.record_stakes(store, fid, votes, "RUN-test")
    return fid


def kinds(store, since_reset=True):
    return [e["kind"] for e in market.entries(store, since_reset)]


# --- scoring rule ------------------------------------------------------------

def test_brier_payout_is_proper_and_bounded():
    assert market.payout(1.0, "recoverable") == 10.0
    assert market.payout(0.9, "recoverable") == 9.6
    assert market.payout(0.5, "recoverable") == market.payout(0.5, "correct") == 0.0  # no lean, no payout
    assert market.payout(0.9, "correct") == -22.4
    assert market.payout(0.0, "recoverable") == -30.0
    # proper: if a judge truly believes q = 0.7, reporting 0.7 beats reporting 1.0 in expectation
    ev = lambda p, q=0.7: q * market.payout(p, "recoverable") + (1 - q) * market.payout(p, "correct")
    assert ev(0.7) > ev(1.0) and ev(0.7) > ev(0.5) and ev(0.7) > ev(0.9)


def test_forecast_maps_votes_to_p_recoverable():
    assert market.forecast(V("j", "recoverable", 0.8)) == 0.8
    assert market.forecast(V("j", "correct", 0.8)) == pytest.approx(0.2)
    assert market.forecast(V("j", "conflict", 0.95)) == 0.5


# --- stakes ------------------------------------------------------------------

def test_stakes_recorded_one_per_vote(store):
    judged(store)
    stakes = market.open_bets(store, "F-0001")
    assert [(s["judge"], s["outcome"], s["stake"]) for s in stakes] == [
        ("judge.strict", "recoverable", 9), ("judge.operator", "recoverable", 6), ("judge.skeptic", "conflict", 7)]
    s = stakes[0]
    assert (s["to_win"], s["at_risk"], s["run_id"]) == (9.6, 22.4, "RUN-test")
    assert market.balances(store) == {j: 100.0 for j in market.JUDGES}  # nothing moves until truth arrives


def test_pipeline_records_a_stake_per_vote_and_none_for_rule_settled(paths):
    store = pipeline.run(reset=True, forget=True, demo_approve=False, **paths)
    stakes = [e for e in market.entries(store) if e["kind"] == "stake"]
    assert len(stakes) == 6 * 3
    assert {(e["finding_id"], e["judge"]) for e in stakes} == {
        (f["finding_id"], v["judge"]) for f in store.all() for v in f["votes"]}


# --- settlement through the gatekeeper ------------------------------------------

def test_human_approve_settles_as_recoverable(store):
    fid = judged(store)
    transition(store, fid, "approve", HUMAN, reason="evidence checks out")
    s = {e["judge"]: e for e in market.entries(store) if e["kind"] == "settle"}
    assert {j: (e["truth"], e["source"], e["decided_by"], e["payout"], e["result"]) for j, e in s.items()} == {
        "judge.strict": ("recoverable", "approve", "controller", 9.6, "won"),
        "judge.operator": ("recoverable", "approve", "controller", 3.6, "won"),
        "judge.skeptic": ("recoverable", "approve", "controller", 0.0, "abstained"),
    }
    assert market.balances(store) == {"judge.strict": 109.6, "judge.operator": 103.6, "judge.skeptic": 100.0}
    assert market.open_bets(store) == []


def test_human_reject_settles_as_correct(store):
    fid = judged(store, "F-0003")  # conflict on a non-binding rep email; the human accepts the charge
    transition(store, fid, "reject", HUMAN, reason="rep had no authority")
    assert market.balances(store) == {"judge.strict": 77.6, "judge.operator": 95.6, "judge.skeptic": 100.0}
    assert {e["truth"] for e in market.entries(store) if e["kind"] == "settle"} == {"correct"}


def test_recovered_settles_open_bets_and_is_idempotent(store):
    store.put_new(fake("F-0002"))
    transition(store, "F-0002", "check", AGENT)
    transition(store, "F-0002", "judge", AGENT, patch={"outcome": "recoverable", "votes": VOTES})
    transition(store, "F-0002", "approve", HUMAN)          # approved before any bet existed
    market.record_stakes(store, "F-0002", VOTES)           # bets still open when the money arrives
    transition(store, "F-0002", "file", GATEKEEPER)
    transition(store, "F-0002", "recover", HUMAN, reason="DoorDash paid")
    settles = [e for e in market.entries(store) if e["kind"] == "settle"]
    assert len(settles) == 3 and {(e["truth"], e["source"]) for e in settles} == {("recoverable", "recover")}
    assert market.settle(store, "F-0002", "recover") == []  # nothing left open: never pays twice


def test_recover_after_approve_does_not_pay_twice(store):
    fid = judged(store)
    transition(store, fid, "approve", HUMAN)
    transition(store, fid, "file", GATEKEEPER)
    transition(store, fid, "recover", GATEKEEPER)
    assert len([e for e in market.entries(store) if e["kind"] == "settle"]) == 3


def test_denied_moves_do_not_settle_and_settle_needs_a_logged_truth(store):
    fid = judged(store)
    with pytest.raises(Denied):
        transition(store, fid, "approve", AGENT)  # an agent can't create ground truth
    assert "settle" not in kinds(store)
    with pytest.raises(PermissionError):
        market.settle(store, fid, "approve")    # no allowed human approve in the audit log
    with pytest.raises(ValueError):
        market.settle(store, fid, "judge")
    assert len(market.open_bets(store)) == 3


def test_a_judge_never_loses_more_than_it_has(store):
    bets = [V("judge.strict", "recoverable", 1.0)]
    for i in range(5):
        fid = f"F-000{i + 1}"
        judged(store, fid, votes=bets)
        transition(store, fid, "reject", HUMAN)
    assert market.balances(store)["judge.strict"] == 0.0
    assert market.weights(store)["judge.strict"] == 0.0


# --- weights in the panel ------------------------------------------------------------

@pytest.fixture
def scripted(monkeypatch):
    def use(*spec):
        table = {j: V(j, o, c) for j, (o, c) in zip(panel.JUDGES, spec)}
        monkeypatch.setattr(panel, "vote", lambda judge, f: dict(table[judge]))
    return use


EQUAL = {j: 1 / 3 for j in panel.JUDGES}
TRUST_SKEPTIC = {"judge.strict": 0.1, "judge.operator": 0.1, "judge.skeptic": 0.8}
TRUST_STRICT = {"judge.strict": 0.8, "judge.operator": 0.1, "judge.skeptic": 0.1}


def test_equal_weights_match_the_unweighted_panel(scripted):
    scripted(("recoverable", 0.75), ("recoverable", 0.75), ("recoverable", 0.75))
    assert panel.decide(fake("F-0001"), rules=[], weights=EQUAL)["outcome"] == "recoverable"
    scripted(("recoverable", 0.9), ("recoverable", 0.9), ("recoverable", 0.44))
    assert panel.decide(fake("F-0001"), rules=[], weights=EQUAL)["outcome"] == "conflict"


def test_weights_move_a_unanimous_call_across_the_threshold(scripted):
    scripted(("recoverable", 0.9), ("recoverable", 0.9), ("recoverable", 0.5))   # plain mean 0.767: decided
    assert panel.decide(fake("F-0001"), rules=[])["outcome"] == "recoverable"
    d = panel.decide(fake("F-0001"), rules=[], weights=TRUST_SKEPTIC)          # weighted 0.58: ask a human
    assert d["outcome"] == "conflict" and d["confidence"] == 0.58
    assert [v["weight"] for v in d["votes"]] == [0.1, 0.1, 0.8]
    scripted(("recoverable", 0.9), ("recoverable", 0.9), ("recoverable", 0.4))   # plain mean 0.733: a human
    assert panel.decide(fake("F-0001"), rules=[])["outcome"] == "conflict"
    d = panel.decide(fake("F-0001"), rules=[], weights=TRUST_STRICT)
    assert d["outcome"] == "recoverable" and "balance-weighted" in d["reason"]


@pytest.mark.parametrize("w", [TRUST_STRICT, TRUST_SKEPTIC, {"judge.strict": 1.0, "judge.operator": 0, "judge.skeptic": 0}])
def test_weights_never_override_disagreement_or_guardrails(scripted, w):
    scripted(("recoverable", 0.99), ("recoverable", 0.99), ("correct", 0.2))
    assert panel.decide(fake("F-0001"), rules=[], weights=w)["outcome"] == "conflict"   # still not unanimous
    scripted(("recoverable", 0.99), ("recoverable", 0.99), ("recoverable", 0.99))
    assert panel.decide({**fake("F-0001"), "gap": 0.03}, rules=[], weights=w)["outcome"] == "correct"  # tolerance
    assert panel.decide(fake("A-0005"), rules=[], weights=w)["outcome"] == "conflict"   # weak evidence
    rule = {"rule_id": "R-001", "channel": "ubereats", "check_type": "promo", "authority": ["contract", "unauth_email"],
            "outcome": "correct", "description": "accept", "approved_by": "controller"}
    assert panel.decide(fake("F-0003"), rules=[rule], weights=w)["votes"] == []        # rules still come first


# --- attention budget --------------------------------------------------------------------

def esc(fid, gap, votes):
    return {"finding_id": fid, "gap": gap, "votes": votes}


def test_attention_orders_by_gap_times_uncertainty():
    split = [V("judge.strict", "recoverable", 0.9), V("judge.operator", "correct", 0.9), V("judge.skeptic", "conflict", 0.8)]
    lean = [V("judge.strict", "recoverable", 0.9), V("judge.operator", "recoverable", 0.9), V("judge.skeptic", "conflict", 0.8)]
    # a $100 gap the panel mostly agrees on is worth less of a human's time than a $60 gap where it's split
    rows = [esc("small-split", 60.0, split), esc("big-lean", 100.0, lean), esc("big-split", 400.0, split)]
    a = market.allocate_attention(rows, tokens=2)
    assert a["big-split"]["value"] == 400.0 and a["small-split"]["value"] == 60.0
    assert a["big-lean"]["value"] == pytest.approx(100 * (1 - (2 * (0.9 + 0.9 + 0.5) / 3 - 1)), abs=0.01)
    assert [fid for fid, x in sorted(a.items(), key=lambda kv: kv[1]["rank"])] == ["big-split", "small-split", "big-lean"]
    assert {fid: x["status"] for fid, x in a.items()} == {"big-split": "queued", "small-split": "queued", "big-lean": "deferred"}


def test_budget_comes_from_env(monkeypatch):
    monkeypatch.setenv("FLOCK_ATTENTION_BUDGET", "3")
    assert market.budget() == 3
    monkeypatch.delenv("FLOCK_ATTENTION_BUDGET")
    assert market.budget() == market.DEFAULT_BUDGET == 10


def test_over_budget_escalations_are_deferred_never_decided(paths):
    store = pipeline.run(reset=True, forget=True, demo_approve=True, attention_budget=1, **paths)
    f3, f4 = store.get("F-0003"), store.get("F-0004")
    assert f3["attention"]["status"] == "queued" and f3["attention"]["rank"] == 1   # $6.00 outranks $4.50
    assert f4["attention"]["status"] == "deferred"
    assert f3["state"] == "REJECTED"                                   # the human spent the one token
    assert (f4["state"], f4["outcome"]) == ("JUDGED", "conflict")      # still waiting for a human
    # check + judge (+ the investigator's case file, an annotation) only: nothing decided it
    assert [(e["role"], e["action"]) for e in store.log("F-0004")] == [
        ("agent", "check"), ("agent", "judge"), ("agent", "investigate")]
    assert len(market.open_bets(store, "F-0004")) == 3


def test_zero_budget_still_sends_everything_to_a_human(paths):
    store = pipeline.run(reset=True, forget=True, demo_approve=True, attention_budget=0, **paths)
    for fid in ("F-0003", "F-0004"):
        f = store.get(fid)
        assert (f["state"], f["outcome"], f["attention"]["status"]) == ("JUDGED", "conflict", "deferred")
    a = market.attention_summary(store.all(), store.log())
    assert (a["budget"], a["used"], a["remaining"], len(a["deferred"])) == (0, 0, 0, 2)


def test_default_budget_leaves_the_quickstart_alone(paths):
    store = pipeline.run(reset=True, forget=True, demo_approve=True, **paths)
    assert {store.get(f)["attention"]["status"] for f in ("F-0003", "F-0004")} == {"queued"}
    assert {store.get(f)["state"] for f in ("F-0003", "F-0004")} == {"REJECTED"}


# --- persistence, --reset, --forget, append-only --------------------------------------------

def test_balances_persist_across_runs_and_forget_resets(paths):
    store = pipeline.run(reset=True, forget=True, demo_approve=True, **paths)
    week1 = market.balances(store)
    assert week1 == {"judge.strict": 128.8, "judge.operator": 120.0, "judge.skeptic": 125.6}
    store = pipeline.run(reset=True, demo_approve=True, **paths)        # week 2: no --forget
    week2 = market.balances(store)
    assert all(week2[j] > week1[j] for j in week1)                     # carried over and kept growing
    store = pipeline.run(reset=True, forget=True, **paths)              # true day one again
    assert market.balances(store) == {j: 100.0 for j in market.JUDGES}
    assert "reset" in kinds(store, since_reset=False)


def test_week2_votes_carry_week1_weights(paths):
    pipeline.run(reset=True, forget=True, demo_approve=True, **paths)
    w1 = market.weights(paths["store"])
    store = pipeline.run(reset=True, **paths)
    votes = store.get("F-0001")["votes"]
    assert [v["weight"] for v in votes] == [round(w1[j], 4) for j in panel.JUDGES]
    assert votes[0]["weight"] > 1 / 3 > votes[1]["weight"]   # strict earned trust, operator lost some


def test_reset_voids_open_bets_without_paying(paths):
    store = pipeline.run(reset=True, forget=True, demo_approve=True, **paths)
    before = market.balances(store)
    assert len(market.open_bets(store)) == 3                           # F-0005: no ground truth ever arrived
    store = pipeline.run(reset=True, **paths)
    voids = [e for e in market.entries(store) if e["kind"] == "void"]
    assert {e["finding_id"] for e in voids} == {"F-0005"}
    assert market.balances(store) == before


def test_ledger_is_append_only(paths):
    path = market.ledger_path(paths["store"])
    seen = ""
    for kw in ({"forget": True, "demo_approve": True}, {"demo_approve": True}, {"forget": True}):
        pipeline.run(reset=True, **kw, **paths)
        now = path.read_text()
        assert now.startswith(seen) and len(now) > len(seen)           # earlier lines never change
        seen = now
    assert all(json.loads(line)["kind"] in ("stake", "settle", "void", "reset") for line in seen.splitlines())


# --- dashboard ---------------------------------------------------------------------------------

def test_market_endpoint(paths, tmp_path):
    store = pipeline.run(reset=True, forget=True, demo_approve=True, attention_budget=1, **paths)
    client = create_app(store, runs_path=tmp_path / "runs.jsonl").test_client()
    m = client.get("/api/market").get_json()
    assert [r["judge"] for r in m["leaderboard"]] == ["judge.strict", "judge.skeptic", "judge.operator"]
    top = m["leaderboard"][0]
    assert top["history"][0] == 100.0 and top["history"][-1] == top["balance"] and top["won"] == 3
    assert sum(r["weight"] for r in m["leaderboard"]) == pytest.approx(1.0, abs=1e-3)
    assert m["scoring"] == "brier" and m["settled"] == 12 and m["open_bets"] == 6
    assert {e["kind"] for e in m["recent"]} <= {"stake", "settle", "void"}
    a = m["attention"]
    assert (a["budget"], a["used"], a["remaining"], a["escalations"], a["decided"]) == (1, 1, 0, 2, 1)
    assert [d["finding_id"] for d in a["deferred"]] == ["F-0004"]
    queue = [f["finding_id"] for f in client.get("/api/queue").get_json()]
    assert queue == ["F-0004"]  # the deferred conflict is still in the human queue, visible


def test_dashboard_recover_goes_through_gatekeeper_and_settles(paths, tmp_path):
    store = pipeline.run(reset=True, forget=True, **paths)
    client = create_app(store, runs_path=tmp_path / "runs.jsonl").test_client()
    assert client.post("/api/findings/F-0001/recover", json={"actor": "ira"}).status_code == 403  # not FILED yet
    assert client.post("/api/findings/F-0001/approve", json={"actor": "ira"}).status_code == 200
    settled = client.get("/api/market").get_json()["settled"]
    assert settled == 3
    client.post("/api/file-approved")
    r = client.post("/api/findings/F-0001/recover", json={"actor": "ira", "reason": "DoorDash paid"})
    assert r.status_code == 200 and r.get_json()["finding"]["state"] == "RECOVERED"
    assert client.get("/api/market").get_json()["settled"] == 3  # already settled at approve: no double pay


def test_only_final_round_judge_votes_bet():
    votes = [{"judge": "judge.strict", "outcome": "recoverable", "confidence": 0.8, "round": 1},
             {"judge": "judge.skeptic", "outcome": "conflict", "confidence": 0.7, "round": 1},
             {"judge": "judge.strict", "outcome": "recoverable", "confidence": 0.8, "round": 2},
             {"judge": "judge.skeptic", "outcome": "recoverable", "confidence": 0.75, "round": 2},
             {"judge": "reviewer", "outcome": "recoverable", "confidence": 0.9, "round": "audit"}]
    final = market.final_votes(votes)
    assert [(v["judge"], v["round"]) for v in final] == [("judge.strict", 2), ("judge.skeptic", 2)]
