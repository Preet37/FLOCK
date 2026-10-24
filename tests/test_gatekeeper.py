"""The rules judges will poke at: agents can't approve, nobody skips states, denials are logged, log is append-only."""
import pytest

from flock.gatekeeper import GATEKEEPER, Denied, transition
from flock.models import Actor, Finding
from flock.store import JsonStore

AGENT = Actor("checkers", "agent")
HUMAN = Actor("controller", "human")


@pytest.fixture
def store(tmp_path):
    s = JsonStore(tmp_path)
    s.put_new(Finding(order_ids=["O1"], channel="doordash", check_type="commission",
                      expected=4, actual=6.5, gap=2.5, finding_id="F-T").to_dict())
    return s


def walk_to_judged(store):
    transition(store, "F-T", "check", AGENT)
    transition(store, "F-T", "judge", AGENT, patch={"outcome": "recoverable"})


def test_happy_path(store):
    walk_to_judged(store)
    transition(store, "F-T", "approve", HUMAN)
    transition(store, "F-T", "file", GATEKEEPER)
    assert store.get("F-T")["state"] == "FILED"
    assert [e["after"] for e in store.log("F-T")] == ["CHECKED", "JUDGED", "APPROVED", "FILED"]


def test_agent_cannot_approve_and_denial_is_logged(store):
    walk_to_judged(store)
    with pytest.raises(Denied):
        transition(store, "F-T", "approve", AGENT)
    last = store.log("F-T")[-1]
    assert last["allowed"] is False and last["action"] == "approve"
    assert store.get("F-T")["state"] == "JUDGED"


def test_cannot_skip_states(store):
    with pytest.raises(Denied):
        transition(store, "F-T", "approve", HUMAN)  # still NEW


def test_human_cannot_file(store):
    walk_to_judged(store)
    transition(store, "F-T", "approve", HUMAN)
    with pytest.raises(Denied):
        transition(store, "F-T", "file", HUMAN)


def test_patch_cannot_set_state(store):
    transition(store, "F-T", "check", AGENT, patch={"state": "APPROVED"})
    assert store.get("F-T")["state"] == "CHECKED"


def test_log_records_diff(store):
    walk_to_judged(store)
    e = store.log("F-T")[-1]
    assert e["diff"]["outcome"] == [None, "recoverable"]
    assert e["diff"]["state"] == ["CHECKED", "JUDGED"]
