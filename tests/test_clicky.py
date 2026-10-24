"""Clicky: file mode files every APPROVED finding through the gatekeeper and puts the portal ref in the audit log.
Browser mode falls back to file mode when Playwright/browser is missing. Real browser run: FLOCK_TEST_BROWSER=1."""
import json
import os
import threading

import pytest

from flock.agents import clicky
from flock.gatekeeper import transition
from flock.models import Actor, Finding
from flock.store import JsonStore

AGENT = Actor("checkers", "agent")
HUMAN = Actor("controller", "human")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(clicky, "PORTAL", tmp_path / "portal" / "disputes.jsonl")
    s = JsonStore(tmp_path / "state")
    for fid, approve in (("F-A", True), ("F-B", False)):
        s.put_new(Finding(order_ids=["ORD-1"], channel="doordash", check_type="commission", expected=4,
                          actual=6.5, gap=2.5, finding_id=fid,
                          evidence=[{"source_file": "x.csv", "row": 1}]).to_dict())
        transition(s, fid, "check", AGENT)
        transition(s, fid, "judge", AGENT, patch={"outcome": "recoverable"})
        transition(s, fid, "draft", AGENT, patch={"dispute_draft": "Please refund 2.50."})
        if approve:
            transition(s, fid, "approve", HUMAN)
    return s


def check_filed(store, ref, via):
    f = store.get("F-A")
    assert f["state"] == "FILED" and f["portal_ref"] == ref
    assert store.get("F-B")["state"] == "JUDGED"  # not approved, not filed
    last = store.log("F-A")[-1]
    assert last["actor"] == "gatekeeper" and last["action"] == "file" and last["allowed"]
    assert ref in last["reason"] and last["diff"]["portal_ref"] == [None, ref]
    assert last["evidence"][-1] == {**last["evidence"][-1], "source": "sandbox_portal", "portal_ref": ref, "via": via}
    assert last["evidence"][0] == {"source_file": "x.csv", "row": 1}


def test_file_mode_is_default(store, monkeypatch):
    monkeypatch.delenv("FLOCK_CLICKY_MODE", raising=False)
    refs = clicky.file_approved(store)
    assert len(refs) == 1 and refs[0].startswith("SBX-")
    check_filed(store, refs[0], "file")
    row = json.loads(clicky.PORTAL.read_text())
    assert row["portal_ref"] == refs[0] and row["amount"] == 2.5 and row["merchant_ref"] == "F-A"
    assert row["message"] == "Please refund 2.50." and row["category"] == "commission"


def test_nothing_approved_files_nothing(store):
    clicky.file_approved(store, mode="file")
    assert clicky.file_approved(store, mode="file") == []


def test_browser_mode_falls_back_to_file(store, monkeypatch):
    class Broken:
        def __enter__(self):
            raise ImportError("No module named 'playwright'")

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(clicky, "BrowserFiler", Broken)
    refs = clicky.file_approved(store, mode="browser")
    check_filed(store, refs[0], "file")


def test_refuses_non_local_portal():
    with pytest.raises(ValueError):
        clicky.BrowserFiler(url="https://merchant.example.com")


@pytest.mark.skipif(os.environ.get("FLOCK_TEST_BROWSER") != "1", reason="set FLOCK_TEST_BROWSER=1 to drive a real browser")
def test_browser_mode_end_to_end(store, tmp_path, monkeypatch):
    from werkzeug.serving import make_server

    from flock.portal import create_app

    server = make_server("127.0.0.1", 0, create_app(tmp_path / "live"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(clicky, "PORTAL_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        refs = clicky.file_approved(store, mode="browser")
    finally:
        server.shutdown()
    check_filed(store, refs[0], "browser")
    rows = [json.loads(x) for x in (tmp_path / "live" / "disputes.jsonl").read_text().splitlines()]
    assert rows[0]["portal_ref"] == refs[0] and rows[0]["via"] == "portal" and rows[0]["order_ids"] == ["ORD-1"]
    assert not clicky.PORTAL.exists()  # really went through the browser, not the fallback
