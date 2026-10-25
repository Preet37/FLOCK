"""Vendor follow-ups over HTTP (flock/api.py and its dashboard mirror) and how rolled-up findings are shown: the token
is required, an agent can't approve, DRAFT -> APPROVED -> SENT is enforced one step at a time, every attempt is in the
audit log, and a finding covering hundreds of orders reads "N orders at S1, S2" instead of every id."""
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from flock import api, frontend, payouts, theater, transactions
from flock.gatekeeper import transition
from flock.models import Actor, Finding
from flock.store import JsonStore
from tests.test_api import TOKEN, Client

AGENT = Actor("checkers", "agent")
HUMAN = Actor("controller", "human")
STEPS = ("follow_up", "follow_up/approve", "follow_up/reject", "follow_up/send")


def filed_store(tmp_path):
    """F-FU: FILED in the sandbox portal, unpaid. F-NEW: only checked (no claim to follow up on)."""
    from flock.agents import clicky
    s = JsonStore(tmp_path / "state")
    s.put_new(Finding(order_ids=["ORD-1"], channel="doordash", check_type="commission", expected=8.0, actual=10.0,
                      gap=2.0, finding_id="F-FU",
                      evidence=[{"source_file": "doordash/doordash_payouts.csv", "row": 12, "what": "10.0"}]).to_dict())
    transition(s, "F-FU", "check", AGENT)
    transition(s, "F-FU", "judge", AGENT, patch={"outcome": "recoverable", "dispute_draft": "Please credit $2.00."})
    transition(s, "F-FU", "approve", HUMAN)
    assert len(clicky.file_approved(s, tmp_path / "portal.jsonl")) == 1
    s.put_new(Finding(order_ids=["ORD-2"], channel="doordash", check_type="fee", expected=1.0, actual=2.0, gap=1.0,
                      finding_id="F-NEW").to_dict())
    return s


@pytest.fixture
def server(tmp_path, monkeypatch):
    store = filed_store(tmp_path)
    a = api.Api(store=store, portal=tmp_path / "portal.jsonl")
    monkeypatch.setattr(api.Handler, "api", a)
    monkeypatch.setattr(api, "TOKEN", TOKEN)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Client(httpd.server_address[1]), a, tmp_path / "portal.jsonl"
    httpd.shutdown()
    httpd.server_close()


def test_follow_up_routes_need_the_token(server):
    client, a, _ = server
    for step in STEPS:
        assert client.post(f"/api/findings/F-FU/{step}", {"actor": "maja"}, auth=False)[0] == 401
        assert client.call("POST", f"/api/findings/F-FU/{step}", b"{}", {"Authorization": "Bearer nope"})[0] == 401
    assert "follow_up" not in a.store.get("F-FU")   # nothing happened


def test_follow_up_order_is_enforced_and_logged(server):
    client, a, portal = server
    s = a.store
    # nothing to approve or send yet: refused (409) and on the record
    code, out = client.post("/api/findings/F-FU/follow_up/approve", {"actor": "maja"})
    assert code == 409 and "DRAFT" in out["error"]
    code, out = client.post("/api/findings/F-FU/follow_up/send")
    assert code == 409
    assert [(e["action"], e["allowed"]) for e in s.log("F-FU")[-2:]] == [("approve_follow_up", False),
                                                                       ("send_follow_up", False)]
    # the agent drafts
    code, out = client.post("/api/findings/F-FU/follow_up", {"actor": "maja"})
    assert code == 200, out
    fu = out["follow_up"]
    assert fu["state"] == "DRAFT" and fu["amount"] == 2.0 and fu["claim_ref"] == s.get("F-FU")["portal_ref"]
    assert "remains unpaid" in fu["draft"] and out["log_entry"]["actor"] == "follow_up"
    assert client.post("/api/findings/F-FU/follow_up")[0] == 409          # one draft at a time
    assert client.post("/api/findings/F-FU/follow_up/send")[0] == 409     # a draft is never sent
    # an agent may not approve, even through the human route
    code, out = client.post("/api/findings/F-FU/follow_up/approve", {"actor": "follow_up"})
    assert code == 409
    e = s.log("F-FU")[-1]
    assert (e["actor"], e["role"], e["action"], e["allowed"]) == ("follow_up", "agent", "approve_follow_up", False)
    # a person approves, then the gatekeeper sends via Clicky
    code, out = client.post("/api/findings/F-FU/follow_up/approve", {"actor": "maja", "reason": "chase it"})
    assert code == 200 and out["follow_up"]["state"] == "APPROVED" and out["follow_up"]["approved_by"] == "maja"
    assert out["log_entry"]["reason"] == "chase it"
    assert client.post("/api/findings/F-FU/follow_up/reject", {"actor": "maja"})[0] == 409   # past DRAFT
    code, out = client.post("/api/findings/F-FU/follow_up/send")
    assert code == 200 and out["portal_ref"] and out["follow_up"]["state"] == "SENT"
    assert out["follow_up"]["sent_ref"] == out["portal_ref"] and out["log_entry"]["actor"] == "gatekeeper"
    rows = [json.loads(x) for x in portal.read_text().splitlines()]
    assert rows[-1]["kind"] == "follow_up" and rows[-1]["portal_ref"] == out["portal_ref"]
    assert client.post("/api/findings/F-FU/follow_up/send")[0] == 409     # sent once
    allowed = [(e["action"], e["actor"]) for e in s.log("F-FU") if e["allowed"]][-3:]
    assert allowed == [("follow_up", "follow_up"), ("approve_follow_up", "maja"), ("send_follow_up", "gatekeeper")]
    # reads: the detail carries the follow-up, the list flags it
    d = client.get("/api/findings/F-FU")[1]
    assert d["follow_up"]["state"] == "SENT" and d["finding"]["follow_up"]["sent_ref"] == out["portal_ref"]
    items = {f["finding_id"]: f for f in client.get("/api/findings?state=FILED")[1]}
    assert items["F-FU"]["has_follow_up"] is True
    assert {f["finding_id"]: f for f in client.get("/api/findings")[1]}["F-NEW"]["has_follow_up"] is False
    # a reminder can be drafted again after one was sent
    assert client.post("/api/findings/F-FU/follow_up")[1]["follow_up"]["state"] == "DRAFT"


def test_reject_and_bad_requests(server):
    client, a, _ = server
    assert client.post("/api/findings/F-FU/follow_up")[0] == 200
    code, out = client.post("/api/findings/F-FU/follow_up/reject", {"actor": "maja", "reason": "they paid by phone"})
    assert code == 200 and out["follow_up"]["state"] == "REJECTED" and out["follow_up"]["rejected_by"] == "maja"
    assert client.post("/api/findings/F-FU/follow_up/send")[0] == 409
    assert client.post("/api/findings/F-NEW/follow_up")[0] == 409          # not filed: nothing to follow up
    assert a.store.log("F-NEW")[-1]["allowed"] is False
    assert client.post("/api/findings/F-NOPE/follow_up")[0] == 404
    assert client.post("/api/findings/F-FU/follow_up/explode")[0] == 400


def test_follow_up_is_409_during_a_run(server):
    client, a, _ = server
    a.running = "JOB-busy"
    try:
        for step in STEPS:
            code, out = client.post(f"/api/findings/F-FU/{step}", {"actor": "maja"})
            assert code == 409 and out["running"] == "JOB-busy"
    finally:
        a.running = None
    assert "follow_up" not in a.store.get("F-FU")


def test_dashboard_mirror(tmp_path, monkeypatch):
    from dashboard.app import create_app
    from flock.agents import clicky
    monkeypatch.setattr(clicky, "PORTAL", tmp_path / "portal.jsonl")
    s = filed_store(tmp_path)
    d = create_app(s, runs_path=tmp_path / "runs.jsonl").test_client()
    assert d.post("/api/findings/F-FU/follow_up/send").status_code == 403
    r = d.post("/api/findings/F-FU/follow_up")
    assert r.status_code == 200 and r.get_json()["follow_up"]["state"] == "DRAFT"
    assert d.post("/api/findings/F-FU/follow_up/approve", json={}).status_code == 400      # say who you are
    r = d.post("/api/findings/F-FU/follow_up/approve", json={"actor": "bot", "role": "agent"})
    assert r.status_code == 403 and r.get_json()["log_entry"]["allowed"] is False
    assert d.post("/api/findings/F-FU/follow_up/approve", json={"actor": "maja"}).get_json()["follow_up"]["state"] \
        == "APPROVED"
    r = d.post("/api/findings/F-FU/follow_up/send").get_json()
    assert r["follow_up"]["state"] == "SENT" and r["portal_ref"] == r["follow_up"]["sent_ref"]
    assert d.get("/api/findings/F-FU").get_json()["follow_up"]["state"] == "SENT"
    assert {f["finding_id"]: f["has_follow_up"] for f in d.get("/api/findings").get_json()} == {"F-FU": True,
                                                                                             "F-NEW": False}
    assert d.post("/api/findings/F-FU/follow_up/explode").status_code == 400


# rolled-up findings: shown as "N orders at S1, S2", counted once per lump sum -----------------------------------------
def rolled(n=457, stores=("S28", "S38", "S41")):
    ids = [f"ORD-{i:05d}" for i in range(n)]
    f = Finding(order_ids=ids, channel="doordash", check_type="commission", expected=n * 2.0, actual=n * 2.5,
                gap=n * 0.5, finding_id="F-ROLL", reason=f"{n} DoorDash orders at 3 stores").to_dict()
    f["rollup"] = {"payout_id": "P1", "orders": n, "stores": list(stores), "gross": n * 10.0,
                   "parts": {i: 0.5 for i in ids}}
    return f


def test_orders_label_and_list_item():
    f = rolled()
    assert frontend.orders_label(f) == "457 orders at S28, S38, S41"
    assert frontend.orders_label({"order_ids": ["A", "B"]}) == "A, B"
    assert frontend.orders_label({"order_ids": list("ABCDEFG")}) == "A, B, C, D, E and 2 more"
    item = frontend.list_item(f)
    assert item["orders_label"] == "457 orders at S28, S38, S41" and item["order_count"] == 457
    assert len(item["order_ids"]) == frontend.LIST_IDS and item["order_ids_truncated"] is True
    assert "parts" not in item["rollup"] and item["rollup"]["orders"] == 457
    assert len(f["order_ids"]) == 457 and "parts" in f["rollup"]          # the stored finding is untouched
    assert frontend.list_item({"finding_id": "F", "order_ids": ["A"], "gap": 1})["order_ids"] == ["A"]


def test_api_list_trims_a_rollup_and_the_detail_keeps_every_id(tmp_path):
    s = JsonStore(tmp_path / "state")
    s.put_new(rolled())
    a = api.Api(store=s, portal=tmp_path / "portal.jsonl")
    item = a.findings()[0]
    assert item["orders_label"] == "457 orders at S28, S38, S41" and len(item["order_ids"]) == 10
    d = a.finding("F-ROLL")
    assert len(d["finding"]["order_ids"]) == 457 and len(d["finding"]["rollup"]["parts"]) == 457
    assert d["finding"]["orders_label"] == "457 orders at S28, S38, S41" and d["follow_up"] is None


def test_theater_shows_follow_up_steps(server):
    client, a, _ = server
    client.post("/api/findings/F-FU/follow_up")
    client.post("/api/findings/F-FU/follow_up/approve", {"actor": "maja"})
    client.post("/api/findings/F-FU/follow_up/send")
    evs = [e for e in theater.for_store(a.store).read(since=0, limit=500)["events"] if e["type"] == "follow_up"]
    assert [e["action"] for e in evs] == ["follow_up", "approve_follow_up", "send_follow_up"]
    assert "unpaid claim" in evs[0]["text"] and "maja approved" in evs[1]["text"]
    assert evs[2]["sent_ref"] in evs[2]["text"]


def test_theater_text_names_the_count_not_every_id():
    f = rolled()
    assert theater._covers(f) == " (457 orders at S28, S38, S41)" and theater._covers({"order_ids": ["A"]}) == ""


def lumpsum(findings):
    """Two stores in one DoorDash payout: commission 15 + 10, the deal says 12 + 8."""
    lines = [{"payout_line_id": f"L{i}", "channel": "doordash", "store_id": st, "order_id": f"O{i}", "gross": g,
              "commission": c, "net": n, "payout_id": "P1", "payout_date": "2026-01-07",
              "placed_at": f"2026-01-0{i}T10:00"}
             for i, st, g, c, n in ((1, "S1", "60", "15", "45"), (2, "S2", "40", "10", "30"))]
    deps = [{"channel": "doordash", "payout_ids": ["P1"], "deposit_amount": 75.0, "match_confidence": 1.0,
             "deposit": {"deposit_id": "B1", "amount": 75.0, "deposit_date": "2026-01-08"}}]
    return transactions.build({"payout_lines": lines, "orders": []}, deps, findings, "test")


@pytest.mark.parametrize("by_store", [True, False])
def test_payouts_count_a_rollup_once_and_split_it_by_store(by_store):
    f = {"finding_id": "F-R", "check_type": "commission", "channel": "doordash", "order_ids": ["O1", "O2"],
         "expected": 20.0, "actual": 25.0, "gap": 5.0, "outcome": "recoverable", "state": "FILED", "evidence": [],
         "rollup": {"payout_id": "P1", "orders": 2, "stores": ["S1", "S2"], "gross": 100.0,
                    "parts": {"O1": 3.0, "O2": 2.0}}}
    if by_store:
        f["rollup"]["by_store"] = {"S1": 3.0, "S2": 2.0}
    doc = lumpsum([f])
    listed = payouts.listing(doc, [f])["payouts"][0]
    assert listed["findings"] == 1 and listed["open_gaps"] == 5.0 and listed["expected_net"] == 80.0
    p = payouts.detail(doc, [f], "P1")
    line = next(ln for ln in p["lines"] if ln["key"] == "commission")
    assert line["gap"] == 5.0 and line["findings"] == 1 and line["expected"] == 20.0     # one payout, one gap
    assert [s["open_gaps"] for s in p["stores_detail"]] == ([3.0, 2.0] if by_store else [5.0, 0.0])
    assert sum(s["open_gaps"] for s in p["stores_detail"]) == p["open_gaps"] == 5.0
    (b,) = p["findings_detail"]
    assert b["store_id"] == "S1, S2" and b["orders"] == 2 and b["orders_label"] == "2 orders at S1, S2" and b["rollup"]
    step = next(x for x in p["waterfall"] if x["key"] == "commission")
    assert step["gap"] == 5.0 and step["finding_ids"] == ["F-R"]


def test_rollup_records_each_stores_share():
    from flock import rollup
    index = {"O1": {"payout_id": "P1", "store_id": "S1", "gross": 60.0},
             "O2": {"payout_id": "P1", "store_id": "S2", "gross": 40.0},
             "O3": {"payout_id": "P1", "store_id": "S1", "gross": 10.0}}
    fs = [Finding(order_ids=[o], channel="doordash", check_type="commission", expected=1.0, actual=1.0 + g, gap=g,
                  finding_id=f"F-{o}").to_dict() for o, g in (("O1", 3.0), ("O2", 2.0), ("O3", 0.5))]
    (r,) = rollup.rollup(fs, index)
    assert r["rollup"]["by_store"] == {"S1": 3.5, "S2": 2.0} and r["gap"] == 5.5
