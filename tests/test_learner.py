"""Learner: proposes rules from repeated human decisions. Never approves them itself."""
import json

import pytest

from conftest import fake
from flock.agents import learner, panel
from flock.gatekeeper import Denied, transition
from flock.models import Actor
from flock.store import JsonStore

AGENT = Actor("checkers", "agent")
HUMAN = Actor("controller", "human")


@pytest.fixture
def store(tmp_path):
    return JsonStore(tmp_path / "state")


@pytest.fixture
def rules_path(tmp_path):
    return tmp_path / "rules.json"


def judged(store, fid, base="F-0003", **over):
    """Put a finding through check + a real panel vote, so it carries votes like the pipeline's do."""
    f = {**fake(base), "finding_id": fid, **over}
    store.put_new(f)
    transition(store, fid, "check", AGENT)
    transition(store, fid, "judge", panel.ACTOR, patch=panel.decide(store.get(fid), rules=[]))
    return store.get(fid)


def human(store, fid, action):
    transition(store, fid, action, HUMAN, reason="test decision")


def test_one_decision_is_not_enough(store, rules_path):
    judged(store, "C1")
    human(store, "C1", "reject")
    assert learner.MIN_EVIDENCE == 2
    assert learner.propose(store, rules_path) == []


def test_proposes_at_min_evidence(store, rules_path):
    for fid in ("C1", "C2"):
        assert judged(store, fid)["outcome"] == "conflict"
        human(store, fid, "reject")
    [r] = learner.propose(store, rules_path)
    assert r["rule_id"] == "R-001" and r["evidence_count"] == 2 and r["precedents"] == ["C1", "C2"]
    assert (r["channel"], r["check_type"], r["authority"], r["outcome"]) == ("ubereats", "promo", ["contract", "unauth_email"], "correct")
    assert "approved_by" not in r


def test_human_approvals_propose_an_always_dispute_rule(store, rules_path):
    for fid in ("C1", "C2", "C3"):
        judged(store, fid)
        human(store, fid, "approve")
    [r] = learner.propose(store, rules_path)
    assert r["outcome"] == "recoverable" and r["evidence_count"] == 3 and "always dispute" in r["description"]


def test_propose_never_writes_rules_or_auto_approves(store, rules_path):
    for fid in ("C1", "C2", "C3", "C4"):
        judged(store, fid)
        human(store, fid, "reject")
    learner.propose(store, rules_path)
    assert not rules_path.exists() and not learner.RULES.exists()
    assert learner.approved_rules(rules_path) == []
    assert (store.dir / "proposed_rules.json").exists()
    # and the panel still escalates: a proposal changes nothing until a human approves it
    assert panel.decide(fake("F-0003"), rules=learner.approved_rules(rules_path))["outcome"] == "conflict"


def test_humans_disagreeing_with_each_other_yields_no_rule(store, rules_path):
    for fid in ("C1", "C2", "C3", "C4"):
        judged(store, fid)
    for fid in ("C1", "C2"):
        human(store, fid, "reject")
    for fid in ("C3", "C4"):
        human(store, fid, "approve")
    assert learner.propose(store, rules_path) == []


def test_only_learns_from_items_the_panel_sent_to_a_human(store, rules_path):
    for fid in ("D1", "D2", "D3"):  # doordash commission, contract-backed: panel decides recoverable itself
        assert judged(store, fid, base="F-0001")["outcome"] == "recoverable"
        human(store, fid, "approve")
    assert learner.propose(store, rules_path) == []


def test_agent_decisions_and_denied_attempts_do_not_count(store, rules_path):
    for fid in ("C1", "C2"):
        judged(store, fid)
        with pytest.raises(Denied):
            transition(store, fid, "reject", AGENT)
    assert [e["allowed"] for e in store.log() if e["action"] == "reject"] == [False, False]
    assert learner.propose(store, rules_path) == []


def test_one_finding_counts_once_even_if_logged_twice(store, rules_path):
    judged(store, "C1")
    human(store, "C1", "reject")
    dup = store.log("C1")[-1]
    store.commit(None, dup)  # e.g. the same finding id re-run without --reset
    assert learner.propose(store, rules_path) == []


def test_groups_split_by_channel_check_and_authority(store, rules_path):
    judged(store, "C1")
    judged(store, "C2", channel="doordash")
    judged(store, "C3", evidence=fake("F-0003")["evidence"][:1] + [{"source_file": "x.txt", "quote": "q", "authority": "call_note"}])
    for fid in ("C1", "C2", "C3"):
        human(store, fid, "reject")
    assert learner.propose(store, rules_path) == []


def test_approve_writes_rules_json(store, rules_path):
    for fid in ("C1", "C2"):
        judged(store, fid)
        human(store, fid, "reject")
    learner.propose(store, rules_path)
    rule = learner.approve("R-001", HUMAN, store=store, rules_path=rules_path)
    saved = json.loads(rules_path.read_text())
    assert saved == [rule] and rule["approved_by"] == "controller" and rule["approved_at"]
    assert learner.approved_rules(rules_path) == [rule]
    entry = store.log("R-001")[-1]
    assert (entry["action"], entry["role"], entry["actor"]) == ("approve_rule", "human", "controller")


def test_approved_rule_is_not_proposed_again(store, rules_path):
    for fid in ("C1", "C2"):
        judged(store, fid)
        human(store, fid, "reject")
    learner.propose(store, rules_path)
    learner.approve("R-001", HUMAN, store=store, rules_path=rules_path)
    assert learner.propose(store, rules_path) == []


@pytest.mark.parametrize("who", [Actor("panel", "agent"), Actor("gatekeeper", "gatekeeper"), "human"])
def test_only_a_human_actor_may_approve_a_rule(store, rules_path, who):
    for fid in ("C1", "C2"):
        judged(store, fid)
        human(store, fid, "reject")
    learner.propose(store, rules_path)
    with pytest.raises(PermissionError):
        learner.approve("R-001", who, store=store, rules_path=rules_path)
    assert not rules_path.exists()


def test_approving_unknown_rule_fails_clearly(store, rules_path):
    learner.propose(store, rules_path)
    with pytest.raises(KeyError, match="no proposed rule R-009"):
        learner.approve("R-009", HUMAN, store=store, rules_path=rules_path)


def test_approving_twice_does_not_duplicate(store, rules_path):
    for fid in ("C1", "C2"):
        judged(store, fid)
        human(store, fid, "reject")
    learner.propose(store, rules_path)
    a = learner.approve("R-001", HUMAN, store=store, rules_path=rules_path)
    b = learner.approve("R-001", HUMAN, store=store, rules_path=rules_path)
    assert a == b and len(json.loads(rules_path.read_text())) == 1


def test_matching_rule_ignores_unapproved_entries(rules_path):
    rules_path.write_text(json.dumps([{"rule_id": "R-001", "channel": "ubereats", "check_type": "promo",
                                       "authority": ["contract", "unauth_email"], "outcome": "correct"}]))
    assert learner.matching_rule(fake("F-0003"), learner.approved_rules(rules_path)) is None
