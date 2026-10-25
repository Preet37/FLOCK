"""Vendor follow-up on a filed claim the platform never paid: the agent drafts, only a human approves, only the
gatekeeper sends (Clicky types it into the sandbox portal), and every attempt, allowed or denied, is logged."""
import json
from datetime import date, timedelta

import pytest

from flock.agents import clicky, follow_up
from flock.gatekeeper import GATEKEEPER, Denied, transition
from flock.models import Actor, Finding
from flock.store import JsonStore

AGENT = Actor("checkers", "agent")
HUMAN = Actor("controller", "human")


@pytest.fixture
def store(tmp_path):
    s = JsonStore(tmp_path / "state")
    f = Finding(order_ids=["ORD-1", "ORD-2"], channel="doordash", check_type="commission", expected=8.0, actual=10.0,
                gap=2.0, finding_id="F-FU",
                evidence=[{"source_file": "doordash/doordash_payouts.csv", "row": 12, "what": "commission 10.0"},
                          {"source_file": "contracts/doordash_amendment_1.json", "quote": "20% of the food subtotal",
                           "authority": "amendment"}]).to_dict()
    s.put_new(f)
    transition(s, "F-FU", "check", AGENT)
    transition(s, "F-FU", "judge", AGENT, patch={"outcome": "recoverable", "dispute_draft": "Please credit $2.00."})
    transition(s, "F-FU", "approve", HUMAN)
    return s


def file_it(store, portal):
    refs = clicky.file_approved(store, portal)
    assert len(refs) == 1
    return refs[0]


def test_full_flow_draft_approve_send(store, tmp_path):
    portal = tmp_path / "portal.jsonl"
    ref = file_it(store, portal)
    assert follow_up.due(store, days=14) == []                 # filed today: not due yet
    later = date.today() + timedelta(days=20)
    assert [f["finding_id"] for f in follow_up.due(store, days=14, today=later)] == ["F-FU"]

    assert follow_up.draft(store, today=later) == ["F-FU"]
    fu = store.get("F-FU")["follow_up"]
    assert fu["state"] == "DRAFT" and fu["claim_ref"] == ref and fu["amount"] == 2.0 and fu["platform"] == "DoorDash"
    assert f"We filed claim {ref} on {date.today().isoformat()} for $2.00" in fu["draft"]
    assert "remains unpaid" in fu["draft"] and "Please credit $2.00" in fu["draft"]
    assert "doordash_amendment_1.json" in fu["evidence_summary"] and "—" not in fu["draft"]
    assert store.get("F-FU")["state"] == "FILED"               # an annotation: the claim itself doesn't move

    assert clicky.send_follow_ups(store, portal) == []         # a draft is never sent
    follow_up.approve(store, "F-FU", HUMAN)
    assert store.get("F-FU")["follow_up"]["state"] == "APPROVED"
    sent = clicky.send_follow_ups(store, portal)
    assert len(sent) == 1
    fu = store.get("F-FU")["follow_up"]
    assert fu["state"] == "SENT" and fu["sent_ref"] == sent[0] and fu["approved_by"] == "controller"
    rows = [json.loads(x) for x in portal.read_text().splitlines()]
    assert rows[-1]["kind"] == "follow_up" and rows[-1]["follows"] == ref and rows[-1]["amount"] == 2.0
    moves = [(e["action"], e["actor"], e["allowed"]) for e in store.log("F-FU")][-3:]
    assert moves == [("follow_up", "follow_up", True), ("approve_follow_up", "controller", True),
                     ("send_follow_up", "gatekeeper", True)]


def test_agent_cannot_approve_or_send_and_denials_are_logged(store, tmp_path):
    file_it(store, tmp_path / "portal.jsonl")
    follow_up.draft(store, ["F-FU"])
    with pytest.raises(Denied):
        follow_up.approve(store, "F-FU", Actor("follow_up", "agent"))
    last = store.log("F-FU")[-1]
    assert last["allowed"] is False and last["action"] == "approve_follow_up" and last["reason"].startswith("DENIED")
    fu = store.get("F-FU")["follow_up"]
    assert fu["state"] == "DRAFT"
    with pytest.raises(Denied):   # a human can't send it either: only the gatekeeper sends
        transition(store, "F-FU", "send_follow_up", HUMAN, patch={"follow_up": {**fu, "state": "SENT", "sent_ref": "X"}})
    with pytest.raises(Denied):   # nor can the gatekeeper send a draft nobody approved
        transition(store, "F-FU", "send_follow_up", GATEKEEPER,
                   patch={"follow_up": {**fu, "state": "SENT", "sent_ref": "X"}})
    assert store.get("F-FU")["follow_up"]["state"] == "DRAFT"
    assert sum(1 for e in store.log("F-FU") if not e["allowed"]) == 3


def test_patch_rules(store, tmp_path):
    file_it(store, tmp_path / "portal.jsonl")
    good = follow_up.compose(store, store.get("F-FU"))
    with pytest.raises(Denied, match="amount"):          # the draft's amount must be the code-computed |gap|
        transition(store, "F-FU", "follow_up", follow_up.ACTOR, patch={"follow_up": {**good, "amount": 99.0}})
    with pytest.raises(Denied, match="DRAFT"):           # an agent can't draft straight into APPROVED
        transition(store, "F-FU", "follow_up", follow_up.ACTOR, patch={"follow_up": {**good, "state": "APPROVED"}})
    with pytest.raises(Denied, match="only write"):      # nor touch any other field
        transition(store, "F-FU", "follow_up", follow_up.ACTOR, patch={"follow_up": good, "gap": 99.0})
    follow_up.draft(store, ["F-FU"])
    fu = store.get("F-FU")["follow_up"]
    with pytest.raises(Denied, match="may not change amount"):   # a human edits wording, never the money
        transition(store, "F-FU", "approve_follow_up", HUMAN, patch={"follow_up": {**fu, "state": "APPROVED",
                                                                                   "amount": 50.0}})
    follow_up.approve(store, "F-FU", HUMAN, text="Edited by a person. Please credit $2.00 for claim.")
    assert store.get("F-FU")["follow_up"]["draft"].startswith("Edited by a person")


def test_reject_then_redraft(store, tmp_path):
    file_it(store, tmp_path / "portal.jsonl")
    follow_up.draft(store, ["F-FU"])
    follow_up.reject(store, "F-FU", HUMAN, reason="They already said yes by phone")
    assert store.get("F-FU")["follow_up"]["state"] == "REJECTED"
    assert clicky.send_follow_ups(store, tmp_path / "portal.jsonl") == []
    later = date.today() + timedelta(days=30)
    assert follow_up.due(store, days=14, today=later) == []      # a human said no: not redrafted on its own
    follow_up.draft(store, ["F-FU"])                              # but a person can ask for a new one
    assert store.get("F-FU")["follow_up"]["state"] == "DRAFT"


def test_only_filed_claims_get_follow_ups(store):
    f = follow_up.compose(store, store.get("F-FU"))                 # APPROVED, not filed yet
    with pytest.raises(Denied):
        transition(store, "F-FU", "follow_up", follow_up.ACTOR, patch={"follow_up": f})
    assert "follow_up" not in store.get("F-FU")


def test_recovered_claims_are_not_followed_up(store, tmp_path):
    file_it(store, tmp_path / "portal.jsonl")
    transition(store, "F-FU", "recover", GATEKEEPER, reason="platform paid")
    assert follow_up.unpaid(store) == []
    assert follow_up.due(store, days=0) == []


def test_cli_show_and_approve(store, tmp_path, monkeypatch, capsys):
    file_it(store, tmp_path / "portal.jsonl")
    follow_up.draft(store, ["F-FU"])
    monkeypatch.setattr(follow_up, "get_store", lambda: store)
    monkeypatch.setattr(clicky, "PORTAL", tmp_path / "portal.jsonl")
    follow_up.main([])
    assert "follow-up: DRAFT" in capsys.readouterr().out
    follow_up.main(["approve", "F-FU", "--actor", "preet", "--send"])
    out = capsys.readouterr().out
    assert "approved by preet" in out and "Clicky sent 1 follow-up" in out
    assert store.get("F-FU")["follow_up"]["state"] == "SENT"
