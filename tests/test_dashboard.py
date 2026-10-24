"""Dashboard endpoints. The dashboard never writes a finding itself: decisions go through the gatekeeper."""
import json

import pytest

from dashboard.app import create_app
from flock.agents import clicky, learner
from flock.gatekeeper import transition
from flock.models import Actor, Finding
from flock.store import JsonStore

AGENT = Actor("checkers", "agent")


def seed(store, fid, outcome, gap, draft=None, votes=None):
    store.put_new(Finding(order_ids=[f"ORD-{fid}"], channel="ubereats", check_type="promo", expected=0, actual=gap,
                          gap=gap, finding_id=fid,
                          evidence=[{"source_file": "data/raw/emails/rep.txt", "quote": "Uber covers it",
                                     "authority": "unauth_email"}]).to_dict())
    transition(store, fid, "check", AGENT)
    transition(store, fid, "judge", AGENT, patch={"outcome": outcome, "confidence": 0.67, "reason": "test",
                                                  "votes": votes or []})
    if draft:
        transition(store, fid, "draft", AGENT, patch={"dispute_draft": draft})


@pytest.fixture
def store(tmp_path, monkeypatch):
    # keep learner / clicky files out of the real repo
    monkeypatch.setattr(learner, "RULES", tmp_path / "rules.json")
    monkeypatch.setattr(learner, "PROPOSED", tmp_path / "proposed_rules.json")
    monkeypatch.setattr(clicky, "PORTAL", tmp_path / "portal" / "disputes.jsonl")
    s = JsonStore(tmp_path / "state")
    vote = [{"judge": "judge.strict", "outcome": "conflict", "confidence": 0.7, "reason": "no authority"}]
    seed(s, "F-C1", "conflict", 6.0, votes=vote)
    seed(s, "F-C2", "conflict", 4.5, votes=vote)
    seed(s, "F-R", "recoverable", 2.5, draft="Hello support")
    seed(s, "F-OK", "correct", 0.0)
    return s


@pytest.fixture
def client(store, tmp_path):
    runs = tmp_path / "runs.jsonl"
    runs.write_text(json.dumps({"to_human": 2, "learned_rules": 0}) + "\n" + json.dumps({"to_human": 0, "learned_rules": 1}) + "\n")
    return create_app(store, runs_path=runs).test_client()


def test_index_and_static(client):
    assert b"Flock" in client.get("/").data
    for f in ("app.js", "style.css"):
        assert client.get(f"/static/{f}").status_code == 200


def test_summary_counts(client):
    s = client.get("/api/summary").get_json()
    assert s["findings"] == 4 and s["by_state"]["JUDGED"] == 4
    assert s["to_human"] == 2 and s["awaiting_human"] == 3
    assert s["dollars_found"] == 2.5 and s["denied"] == 0
    assert s["activity"][0]["subject_id"] == "F-OK"  # latest first


def test_findings_and_queue(client):
    assert [f["finding_id"] for f in client.get("/api/findings").get_json()] == ["F-C1", "F-C2", "F-OK", "F-R"]
    assert [f["finding_id"] for f in client.get("/api/queue").get_json()] == ["F-C1", "F-C2", "F-R"]


def test_detail(client):
    d = client.get("/api/findings/F-R").get_json()
    assert d["finding"]["dispute_draft"] == "Hello support"
    assert [e["action"] for e in d["log"]] == ["check", "judge", "draft"]
    assert d["needs_human"] and d["decision"] is None and d["denied"] == []
    assert client.get("/api/findings/F-NOPE").status_code == 404


def test_approve_goes_through_gatekeeper_and_is_logged(client, store):
    before = len(store.log())
    r = client.post("/api/findings/F-R/approve", json={"actor": "ira", "reason": "evidence checks out"})
    assert r.status_code == 200 and r.get_json()["finding"]["state"] == "APPROVED"
    e = store.log("F-R")[-1]
    assert (e["actor"], e["role"], e["action"], e["allowed"], e["before"], e["after"], e["reason"]) == \
           ("ira", "human", "approve", True, "JUDGED", "APPROVED", "evidence checks out")
    assert len(store.log()) == before + 1
    d = client.get("/api/findings/F-R").get_json()
    assert d["decision"]["actor"] == "ira" and not d["needs_human"]


def test_agent_actor_is_denied_and_denial_logged(client, store):
    r = client.post("/api/findings/F-C1/approve", json={"actor": "panel", "role": "agent"})
    assert r.status_code == 403
    assert store.get("F-C1")["state"] == "JUDGED"
    e = store.log("F-C1")[-1]
    assert e["allowed"] is False and e["role"] == "agent" and e["reason"].startswith("DENIED")
    d = client.get("/api/findings/F-C1").get_json()
    assert len(d["denied"]) == 1
    assert client.get("/api/summary").get_json()["denied"] == 1


def test_bad_requests(client, store):
    assert client.post("/api/findings/F-R/approve", json={}).status_code == 400  # who are you?
    assert client.post("/api/findings/F-R/file", json={"actor": "ira"}).status_code == 400  # not a dashboard action
    assert client.post("/api/findings/F-NOPE/approve", json={"actor": "ira"}).status_code == 404
    assert client.post("/api/findings/F-OK/reject", json={"actor": "ira"}).status_code == 200  # any JUDGED item
    client.post("/api/findings/F-OK/reject", json={"actor": "ira"})  # already REJECTED: illegal move
    assert store.log("F-OK")[-1]["allowed"] is False


def test_file_approved_uses_gatekeeper(client, store):
    client.post("/api/findings/F-R/approve", json={"actor": "ira"})
    r = client.post("/api/file-approved").get_json()
    assert len(r["filed"]) == 1
    e = store.log("F-R")[-1]
    assert (e["actor"], e["role"], e["after"]) == ("gatekeeper", "gatekeeper", "FILED")
    d = client.get("/api/findings/F-R").get_json()
    assert d["filed"]["portal_ref"] == r["filed"][0]


def test_rejecting_conflicts_proposes_a_rule_and_approving_it(client):
    for fid in ("F-C1", "F-C2"):
        client.post(f"/api/findings/{fid}/reject", json={"actor": "ira", "reason": "rep had no authority"})
    proposed = client.get("/api/rules").get_json()["proposed"]
    assert len(proposed) == 1 and proposed[0]["evidence_count"] == 2
    r = client.post(f"/api/rules/{proposed[0]['rule_id']}/approve", json={"actor": "ira"})
    assert r.status_code == 200 and r.get_json()["rule"]["approved_by"] == "ira"
    rules = client.get("/api/rules").get_json()
    assert len(rules["approved"]) == 1 and rules["proposed"] == []
    assert client.post("/api/rules/R-999/approve", json={"actor": "ira"}).status_code == 404


def test_score(client):
    s = client.get("/api/score").get_json()
    assert {"caught", "total", "missed", "false_alarms", "recovered", "to_human", "traps", "runs"} <= set(s)
    assert [r["label"] for r in s["runs"]] == ["Week 1", "Week 2"]
    assert [r["to_human"] for r in s["runs"]] == [2, 0]


def test_meta(client):
    m = client.get("/api/meta").get_json()
    assert m["store"] == "JsonStore" and "APPROVED" in m["states"]
