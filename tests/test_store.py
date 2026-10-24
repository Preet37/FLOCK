"""The Store protocol: JsonStore conforms, get_store() picks the backend, SpacetimeStore is a clear stub."""
import inspect

import pytest

from flock.models import Finding
from flock.store import JsonStore, SpacetimeStore, Store, get_store

METHODS = ["get", "all", "put_new", "commit", "log", "reset", "agent_event", "agents"]


def test_protocol_lists_the_six_methods_plus_the_agent_feed():
    assert sorted(m for m in vars(Store) if not m.startswith("_")) == sorted(METHODS)


def test_jsonstore_conforms(tmp_path):
    s = JsonStore(tmp_path)
    assert isinstance(s, Store)
    for m in METHODS:  # same parameter names as the protocol, so swapping backends is a drop-in
        want = list(inspect.signature(getattr(Store, m)).parameters)[1:]
        got = list(inspect.signature(getattr(JsonStore, m)).parameters)[1:]
        assert got == want, m


def test_jsonstore_behaves_like_the_protocol_says(tmp_path):
    s = JsonStore(tmp_path)
    f = Finding(order_ids=["O1"], channel="doordash", check_type="commission",
                expected=4, actual=6.5, gap=2.5, finding_id="F-S").to_dict()
    s.put_new(f)
    assert s.get("F-S")["gap"] == 2.5 and len(s.all()) == 1
    s.commit({**f, "state": "CHECKED"}, {"subject_id": "F-S", "allowed": True})
    s.commit(None, {"subject_id": "F-X", "allowed": False})  # denied attempt: log only
    assert s.get("F-S")["state"] == "CHECKED"
    assert [e["subject_id"] for e in s.log()] == ["F-S", "F-X"] and len(s.log("F-S")) == 1
    with pytest.raises(KeyError):
        s.get("nope")
    with pytest.raises(AssertionError):
        s.put_new({**f, "state": "JUDGED"})
    s.reset()
    assert s.all() == [] and s.log() == []


def test_log_skips_a_line_still_being_written(tmp_path):
    s = JsonStore(tmp_path)
    s.commit(None, {"subject_id": "F-1", "allowed": True})
    with s.log_path.open("a") as fh:
        fh.write('{"subject_id": "F-2", "allo')  # another process mid-write
    assert [e["subject_id"] for e in s.log()] == ["F-1"]


def test_get_store_defaults_to_json(monkeypatch, tmp_path):
    monkeypatch.delenv("FLOCK_STORE", raising=False)
    assert isinstance(get_store(tmp_path), JsonStore)
    monkeypatch.setenv("FLOCK_STORE", "json")
    assert isinstance(get_store(tmp_path), JsonStore)


def test_get_store_spacetime_is_a_clear_stub(monkeypatch):
    monkeypatch.setenv("FLOCK_STORE", "spacetime")
    with pytest.raises(NotImplementedError, match="audit_log"):
        get_store()
    with pytest.raises(NotImplementedError):
        SpacetimeStore()


def test_get_store_rejects_unknown(monkeypatch):
    monkeypatch.setenv("FLOCK_STORE", "postgres")
    with pytest.raises(ValueError, match="json"):
        get_store()
