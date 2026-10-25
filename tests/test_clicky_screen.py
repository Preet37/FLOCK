"""Clicky's live agent screen: approve with file_via=screen leaves the finding APPROVED, the agent screen gets the plan,
the sandbox portal's Submit files it only if the typed form matches the approved finding, and the gatekeeper still
refuses anything that is not APPROVED."""
import threading
from http.server import ThreadingHTTPServer

import pytest

from flock import api
from flock.gatekeeper import transition
from flock.models import Actor, Finding
from flock.store import JsonStore

from tests.test_api import Client, TOKEN


def drafted(store, fid="F-A", gap=549.33):
    store.put_new(Finding(order_ids=["DD-PO-20260901", "ORD-1"], channel="doordash", check_type="commission",
                          expected=2196.93, actual=2746.26, gap=gap, finding_id=fid,
                          evidence=[{"source_file": "data/showtime/2026-09/raw/doordash/doordash_payouts.csv", "row": 28,
                                     "what": "commission 25%"}]).to_dict())
    transition(store, fid, "check", Actor("checkers", "agent"))
    transition(store, fid, "judge", Actor("checkers", "agent"), patch={"outcome": "recoverable"})
    transition(store, fid, "draft", Actor("checkers", "agent"), patch={"dispute_draft": "Charged 25%, contract says 20%."})


@pytest.fixture
def server(tmp_path, monkeypatch):
    store = JsonStore(tmp_path / "state")
    a = api.Api(store=store, portal=tmp_path / "portal.jsonl")
    monkeypatch.setattr(api.Handler, "api", a)
    monkeypatch.setattr(api, "TOKEN", TOKEN)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Client(httpd.server_address[1]), store
    httpd.shutdown()
    httpd.server_close()


def test_approve_for_the_screen_then_file_through_the_portal(server):
    client, store = server
    drafted(store)
    code, out = client.post("/api/findings/F-A/approve", {"actor": "preet", "file_via": "screen"})
    assert code == 200 and out["filed"] == [] and out["agent_screen"] == "/agent-screen.html?finding=F-A"
    assert store.get("F-A")["state"] == "APPROVED"

    code, q = client.get("/api/clicky/queue")
    assert code == 200 and [i["finding_id"] for i in q["queue"]] == ["F-A"]
    code, plan = client.get("/api/clicky/plan/F-A")
    assert code == 200 and plan["fileable"] and plan["fields"]["amount"] == "549.33"
    assert plan["fields"]["payout_id"] == "DD-PO-20260901" and plan["evidence"][0]["name"] == "doordash_payouts.csv"

    typed = {k: plan["fields"][k] for k in ("channel", "category", "merchant_ref")}
    code, err = client.post("/api/clicky/file/F-A", {"fields": {**typed, "amount": "594.33"}})
    assert code == 400 and "amount" in err["error"] and store.get("F-A")["state"] == "APPROVED"

    code, done = client.post("/api/clicky/file/F-A", {"fields": {**typed, "amount": "$549.33"}})
    assert code == 200 and done["portal_ref"].startswith("SBX-") and done["finding"]["state"] == "FILED"
    last = store.log("F-A")[-1]
    assert last["action"] == "file" and last["allowed"] and "screen" in last["reason"]
    assert client.get("/api/filings/F-A/screen")[0] == 200
    assert client.get("/api/clicky/plan/F-A")[1]["fileable"] is False   # filed: the screen replays it

    code, again = client.post("/api/clicky/file/F-A", {"fields": {**typed, "amount": "549.33"}})
    assert code == 409 and again["denied"]                               # the gatekeeper refuses a second filing


def test_not_approved_is_refused_and_logged(server):
    client, store = server
    drafted(store, "F-B", gap=12.30)
    plan = client.get("/api/clicky/plan/F-B")[1]
    assert plan["fileable"] is False
    typed = {k: plan["fields"][k] for k in ("channel", "category", "merchant_ref", "amount")}
    code, out = client.post("/api/clicky/file/F-B", {"fields": typed})
    assert code == 409 and out["denied"] and store.get("F-B")["state"] != "FILED"
    assert store.log("F-B")[-1]["allowed"] is False
    assert client.post("/api/clicky/file/F-B", {"fields": typed}, auth=False)[0] == 401


def test_plain_approve_still_files_server_side(server):
    client, store = server
    drafted(store, "F-C")
    code, out = client.post("/api/findings/F-C/approve", {"actor": "preet"})
    assert code == 200 and len(out["filed"]) == 1 and out["agent_screen"] is None
    assert store.get("F-C")["state"] == "FILED"


def test_pages_are_served(server):
    client, _ = server
    for page, marker in (("/portal.html", b"SANDBOX MERCHANT PORTAL"), ("/agent-screen.html", b"agent-screen.js"),
                         ("/agent-screen.js", b"clickyFile")):
        code, body = client.get(page)
        assert code == 200 and marker in body
