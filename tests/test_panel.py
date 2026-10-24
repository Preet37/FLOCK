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


# --- debate round, escalation memo, audit sample -----------------------------

@pytest.fixture
def debate(monkeypatch, scripted):
    """First votes from `scripted`, then round 2 from a table (None = that judge can't debate)."""
    def use(first, second):
        scripted(*first)
        table = {j: (None if s is None else {"outcome": s[0], "confidence": s[1], "reason": f"{j} now says {s[0]}",
                                              "judge": j}) for j, s in zip(panel.JUDGES, second)}
        monkeypatch.setattr(panel, "revise", lambda judge, f, own, others: table[judge] and dict(table[judge]))
    return use


def test_no_debate_when_judges_agree(scripted, monkeypatch):
    scripted(("recoverable", 0.9), ("recoverable", 0.9), ("recoverable", 0.9))
    monkeypatch.setattr(panel, "revise", lambda *a: pytest.fail("no debate on a unanimous first round"))
    assert len(panel.decide(fake("F-0001"), rules=[])["votes"]) == 3


def test_debate_that_converges_is_decided_and_keeps_both_rounds(debate, monkeypatch):
    debate([("recoverable", 0.9), ("recoverable", 0.9), ("conflict", 0.6)],
           [("recoverable", 0.9), ("recoverable", 0.9), ("recoverable", 0.8)])
    monkeypatch.setattr(panel, "memo", lambda *a: pytest.fail("no memo when the panel settles it"))
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "recoverable" and d["confidence"] == pytest.approx(0.87)
    assert [(v["judge"], v["round"]) for v in d["votes"]] == [(j, 1) for j in panel.JUDGES] + [(j, 2) for j in panel.JUDGES]
    assert [v["revised"] for v in d["votes"][3:]] == [False, False, True]
    assert "judge.skeptic conflict -> recoverable" in d["reason"]


def test_judge_that_cannot_debate_keeps_its_first_vote(debate):
    debate([("recoverable", 0.9), ("correct", 0.9), ("recoverable", 0.9)],
           [("recoverable", 0.9), None, ("recoverable", 0.9)])
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "conflict"
    assert d["votes"][4] == {**d["votes"][1], "round": 2, "revised": False}


def test_split_after_debate_goes_to_a_human_with_a_memo(debate, monkeypatch):
    debate([("recoverable", 0.9), ("recoverable", 0.9), ("correct", 0.9)],
           [("recoverable", 0.9), ("recoverable", 0.9), ("correct", 0.9)])
    seen = {}

    def fake_memo(f, votes):
        seen["votes"] = votes
        return "Two judges cite the contract; the skeptic doubts it applies. A signed rate sheet settles it."
    monkeypatch.setattr(panel, "memo", fake_memo)
    monkeypatch.setattr(panel, "review", lambda *a: pytest.fail("the reviewer never breaks a tie"))
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "conflict" and "sending to a human" in d["reason"]
    assert d["review_memo"].startswith("Two judges cite the contract")
    assert [v["round"] for v in seen["votes"]] == [2, 2, 2]  # the memo sees the final positions


def test_low_confidence_unanimous_gets_no_memo(scripted, monkeypatch):
    scripted(("recoverable", 0.6), ("recoverable", 0.6), ("recoverable", 0.6))
    monkeypatch.setattr(panel, "memo", lambda *a: pytest.fail("memo is for splits"))
    assert "review_memo" not in panel.decide(fake("F-0001"), rules=[])


def test_offline_memo_and_review_are_no_ops():
    assert panel.memo(fake("F-0003"), []) is None
    assert panel.review(fake("F-0001"), "recoverable", []) is None


def test_audit_sample_is_deterministic_and_about_the_right_size(monkeypatch):
    ids = [f"F-{i:04d}" for i in range(2000)]
    picked = [i for i in ids if panel.in_audit_sample(i, 0.1)]
    assert 140 <= len(picked) <= 260
    assert picked == [i for i in ids if panel.in_audit_sample(i, 0.1)]
    assert not any(panel.in_audit_sample(i, 0.0) for i in ids) and all(panel.in_audit_sample(i, 1.0) for i in ids)
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "0.25")
    assert panel.audit_rate() == 0.25
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "lots")
    assert panel.audit_rate() == panel.DEFAULT_AUDIT_RATE


def reviewer_says(outcome):
    return lambda f, panel_outcome, votes: {"outcome": outcome, "confidence": 0.8, "reason": "Reviewer's view.",
                                            "judge": "reviewer", "round": "audit", "model": "anthropic/test"}


def test_audit_disagreement_sends_to_a_human(scripted, monkeypatch):
    scripted(("recoverable", 0.9), ("recoverable", 0.9), ("recoverable", 0.9))
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "1")
    monkeypatch.setattr(panel, "review", reviewer_says("correct"))
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "conflict"
    assert d["reason"].startswith("Audit sample: the reviewer (anthropic/test) disagrees with the panel's 'recoverable'")
    assert "Sending to a human" in d["reason"] and d["votes"][-1]["judge"] == "reviewer"


def test_audit_agreement_keeps_the_panels_call(scripted, monkeypatch):
    scripted(("correct", 0.9), ("correct", 0.9), ("correct", 0.9))
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "1")
    monkeypatch.setattr(panel, "review", reviewer_says("correct"))
    d = panel.decide(fake("F-0006"), rules=[])
    assert d["outcome"] == "correct" and d["reason"].endswith("Audit sample: the reviewer (anthropic/test) agrees.")
    assert len(d["votes"]) == 4


@pytest.mark.parametrize("fid,why", [("A-0002", "within tolerance"), ("A-0005", "weak-evidence guard made it a conflict")])
def test_audit_skips_calls_the_guardrails_made(scripted, monkeypatch, fid, why):
    scripted(("recoverable", 0.95), ("recoverable", 0.95), ("recoverable", 0.95))
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "1")
    monkeypatch.setattr(panel, "review", lambda *a: pytest.fail(why))
    panel.decide(fake(fid), rules=[])


def test_audit_skips_conflicts_and_unsampled_findings(scripted, monkeypatch):
    monkeypatch.setattr(panel, "review", lambda *a: pytest.fail("must not be reviewed"))
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "1")
    assert panel.decide(fake("F-0003"), rules=[])["outcome"] == "conflict"  # already going to a human
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "0")
    scripted(("recoverable", 0.9), ("recoverable", 0.9), ("recoverable", 0.9))
    assert panel.decide(fake("F-0001"), rules=[])["outcome"] == "recoverable"


def test_approved_rules_are_never_audited(monkeypatch):
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "1")
    monkeypatch.setattr(panel, "review", lambda *a: pytest.fail("a human-approved rule is not re-reviewed"))
    assert panel.decide(fake("F-0003"), rules=[RULE])["outcome"] == "correct"
