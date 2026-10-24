"""Panel: when does the system decide on its own, and when does it bring a human in?"""
import threading

import pytest

from conftest import ADVERSARIAL, fake
from flock.agents import learner, panel


def votes(*spec):
    """spec: (outcome, confidence) per judge, in JUDGES order."""
    return {j: {"outcome": o, "confidence": c, "reason": f"{j} says {o}", "judge": j} for j, (o, c) in zip(panel.JUDGES, spec)}


@pytest.fixture
def scripted(monkeypatch):
    def use(*spec):
        table = votes(*spec)
        monkeypatch.setattr(panel, "vote", lambda judge, f: dict(table[judge]))
    return use


# --- agree vs escalate -------------------------------------------------------

def test_unanimous_high_confidence_is_decided_offline():
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "recoverable"
    assert d["confidence"] >= panel.THRESHOLD
    assert [v["judge"] for v in d["votes"]] == list(panel.JUDGES)
    assert d["reason"].startswith("All 3 judges agree")


@pytest.mark.parametrize("outcome", ["recoverable", "correct"])
def test_unanimous_high_confidence_is_decided(scripted, outcome):
    scripted((outcome, 0.9), (outcome, 0.8), (outcome, 0.85))
    f = fake("F-0006")
    d = panel.decide(f, rules=[])
    assert d["outcome"] == outcome and d["confidence"] == 0.85


@pytest.mark.parametrize("spec", [
    [("recoverable", 0.95), ("recoverable", 0.95), ("correct", 0.95)],
    [("recoverable", 0.95), ("conflict", 0.95), ("recoverable", 0.95)],
    [("correct", 0.9), ("recoverable", 0.9), ("conflict", 0.9)],
])
def test_any_disagreement_goes_to_a_human(scripted, spec):
    scripted(*spec)
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "conflict"
    assert "sending to a human" in d["reason"]
    assert len(d["votes"]) == 3  # every vote is kept for the audit trail


def test_unanimous_but_low_confidence_goes_to_a_human(scripted):
    scripted(("recoverable", 0.7), ("recoverable", 0.7), ("recoverable", 0.7))
    assert panel.decide(fake("F-0001"), rules=[])["outcome"] == "conflict"


def test_average_just_under_threshold_is_not_rounded_up(scripted):
    # mean 0.7467 rounds to 0.75 for display; it must still count as below the 0.75 bar
    scripted(("recoverable", 0.9), ("recoverable", 0.9), ("recoverable", 0.44))
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "conflict"


def test_average_exactly_at_threshold_is_decided(scripted):
    scripted(("recoverable", 0.75), ("recoverable", 0.75), ("recoverable", 0.75))
    assert panel.decide(fake("F-0001"), rules=[])["outcome"] == "recoverable"


def test_fixture_conflicts_go_to_a_human_offline():
    for fid in ("F-0003", "F-0004"):
        assert panel.decide(fake(fid), rules=[])["outcome"] == "conflict"


def test_decide_never_sets_state():
    d = panel.decide(fake("F-0001"), rules=[])
    assert "state" not in d and set(d) == {"outcome", "confidence", "votes", "reason"}


# --- approved rules ----------------------------------------------------------

RULE = {"rule_id": "R-001", "channel": "ubereats", "check_type": "promo", "authority": ["contract", "unauth_email"],
        "outcome": "correct", "description": "ubereats promo backed by contract, unauth_email: accept, do not dispute",
        "approved_by": "controller"}


def test_approved_rule_short_circuits_the_panel(monkeypatch):
    monkeypatch.setattr(panel, "vote", lambda *a: pytest.fail("panel must not vote when a rule applies"))
    d = panel.decide(fake("F-0003"), rules=[RULE])
    assert d == {"outcome": "correct", "confidence": 1.0, "votes": [],
                 "reason": f"Applied approved rule R-001: {RULE['description']}"}


def test_approved_rule_read_from_rules_file(tmp_path):
    import json
    learner.RULES.write_text(json.dumps([RULE]))
    assert panel.decide(fake("F-0004"))["reason"].startswith("Applied approved rule R-001")


def test_rule_without_approval_never_applies():
    unapproved = {k: v for k, v in RULE.items() if k != "approved_by"}
    import json
    learner.RULES.write_text(json.dumps([unapproved]))
    assert panel.decide(fake("F-0003"))["outcome"] == "conflict"


@pytest.mark.parametrize("change", [{"channel": "doordash"}, {"check_type": "commission"}, {"authority": ["contract"]}])
def test_rule_must_match_channel_check_and_authority(change):
    assert panel.decide(fake("F-0003"), rules=[{**RULE, **change}])["outcome"] == "conflict"


# --- code guardrails ---------------------------------------------------------

@pytest.mark.parametrize("gap", [0.0, 0.01, 0.04, 0.05, 0.05000000000000071, -0.03])
def test_gap_within_tolerance_is_correct(gap):
    f = {**fake("F-0001"), "gap": gap}
    assert panel.decide(f, rules=[])["outcome"] == "correct"


def test_gap_just_over_tolerance_is_not_ignored():
    f = {**fake("F-0001"), "gap": 0.06}
    assert panel.decide(f, rules=[])["outcome"] != "correct"


def test_tolerance_guard_overrides_judges_who_want_to_dispute_pennies(scripted):
    scripted(("recoverable", 0.95), ("recoverable", 0.95), ("recoverable", 0.95))
    d = panel.decide({**fake("F-0001"), "gap": 0.03}, rules=[])
    assert d["outcome"] == "correct" and "tolerance" in d["reason"]


def test_weak_evidence_guard_overrides_unanimous_recoverable(scripted):
    scripted(("recoverable", 0.95), ("recoverable", 0.95), ("recoverable", 0.95))
    d = panel.decide(fake("A-0005"), rules=[])
    assert d["outcome"] == "conflict" and "No deal document" in d["reason"]


@pytest.mark.parametrize("fid,weak", [("F-0001", False), ("F-0002", False), ("F-0006", False),
                                       ("A-0005", True), ("A-0006", True)])
def test_weak_evidence(fid, weak):
    assert panel.weak_evidence(fake(fid)) is weak


# --- adversarial fixtures ----------------------------------------------------

EXPECTED = {
    "A-0001": "correct",      # legit error charge: must NOT be disputed
    "A-0002": "correct",      # penny rounding (float gap 0.05000000000000071) within tolerance
    "A-0003": "conflict",     # only an unauthorized rep email supports the claim
    "A-0004": "recoverable",  # amendment outranks an authorized email (CONTRACT.md authority ranking); human still approves
    "A-0005": "conflict",     # $412.50 gap, one statement line, no deal document
    "A-0006": "conflict",     # $1,850 deposit gap, single source
}


def test_every_adversarial_fixture_has_an_expectation():
    assert {f["finding_id"] for f in ADVERSARIAL} == set(EXPECTED)


@pytest.mark.parametrize("fid", sorted(EXPECTED))
def test_adversarial_findings_settle_or_escalate_sensibly(fid):
    d = panel.decide(fake(fid), rules=[])
    assert d["outcome"] == EXPECTED[fid], d["reason"]


# --- parallel judges ---------------------------------------------------------

def test_judges_run_in_parallel(monkeypatch):
    barrier = threading.Barrier(len(panel.JUDGES), timeout=5)  # sequential judges would deadlock here

    def slow_vote(judge, f):
        barrier.wait()
        return {"outcome": "recoverable", "confidence": 0.9, "reason": "ok", "judge": judge}

    monkeypatch.setattr(panel, "vote", slow_vote)
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "recoverable"
    assert [v["judge"] for v in d["votes"]] == list(panel.JUDGES)  # order is stable
