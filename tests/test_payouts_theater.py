"""Lump sums (flock/payouts.py, GET /api/payouts) and the agent theater (flock/theater.py, GET /api/stream): the payout
math reconciles to the cent, the theater's cursor only goes up, replay pacing stays inside its bounds, and the API and
dashboard routes return the documented shapes."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from dashboard.app import create_app
from flock import api, payouts, pipeline, theater, transactions
from flock.agents import learner
from flock.store import JsonStore

ALL = ("missing_deposit", "short_deposit", "owed", "needs_review", "recovered", "matched")


def cents(x):
    return round(float(x) * 100)


# a real generated run, once for the module ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def run(tmp_path_factory):
    from flock import demo
    tmp = tmp_path_factory.mktemp("lumpsum")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FLOCK_OFFLINE", "1")
        mp.setattr(learner, "RULES", tmp / "rules.json")
        store = JsonStore(tmp / "state")
        demo.run_demo(2025, "ca_bagel_chain", weeks=2, stores=4, keep=True, confirm=True, interactive=False,
                      approve=True, forget=True, store=store, portal=tmp / "portal.jsonl", workdir=tmp / "ws")
    return store, tmp


def check_reconciles(p):
    """Every identity the docs promise, in cents."""
    lines = {ln["key"]: ln for ln in p["lines"]} if "lines" in p else None
    ded = sum(cents(v) for v in p["deductions"].values())
    assert ded == cents(p["deductions_total"])
    assert cents(p["gross"]) - ded + cents(p["adjustments"]) == cents(p["net"])          # the statement adds up
    assert cents(p["shortfall"]) == cents(p["expected_net"]) - cents(p["received"])     # shortfall = expected - received
    assert cents(p["owed"]) == cents(p["open_gaps"]) + cents(p["deposit_owed"])         # owed = open gaps + deposit
    assert cents(p["deposit_shortfall"]) == cents(p["net"]) - cents(p["received"])
    if lines:
        expected_ded = sum(cents(ln["expected"]) for k, ln in lines.items() if k != "other")
        assert cents(p["expected_net"]) == cents(p["gross"]) - expected_ded + cents(p["adjustments"]) + \
            cents(lines["other"]["expected"])
        for k, ln in lines.items():
            if k != "other":
                assert cents(ln["expected"]) == cents(ln["actual"]) - cents(ln["gap"])
                assert cents(ln["gap"]) == cents(ln["owed"]) + cents(ln["recovered"])
        assert cents(p["open_gaps"]) == sum(cents(ln["owed"]) for ln in lines.values())
        assert cents(p["owed_breakdown"]["total"]) == cents(p["owed"])
        wf = {s["key"]: s for s in p["waterfall"]}
        assert wf["gross"]["amount"] == p["gross"] and wf["statement_net"]["amount"] == p["net"]
        assert wf["expected_net"]["running_expected"] == p["expected_net"] and wf["shortfall"]["amount"] == p["shortfall"]
        last_deduction = [s for s in p["waterfall"] if s["kind"] in ("deduction", "adjustment")][-1]
        assert cents(last_deduction["running"]) == cents(p["net"])                     # the bars land on net


def test_payout_math_reconciles_on_a_generated_run(run):
    store, _ = run
    doc = transactions.load(store.dir)
    assert doc["payouts"], "the pipeline saves one record per lump sum"
    listing = payouts.listing(doc, store.all())
    assert listing["total"] == len(doc["payouts"]) > 0
    rows = [r for r in doc["rows"] if r["kind"] == "payout"]
    for p in listing["payouts"]:
        check_reconciles(p)
        mine = [r for r in rows if r["channel"] == p["channel"] and r["payout_id"] == p["payout_id"]]
        assert len(mine) == p["stores"]                                                # sum of parts = the lump sum
        assert sum(cents(r["gross"]) for r in mine) == cents(p["gross"])
        assert sum(cents(r["net"]) for r in mine) == cents(p["net"])
        assert sum(r["orders"] for r in mine) == p["orders"]
        assert p["status"] in ALL and p["status"] in p["flags"]
        d = payouts.detail(doc, store.all(), p["payout_key"])
        check_reconciles(d)
        assert sum(cents(s["gross"]) for s in d["stores_detail"]) == cents(d["gross"])
        assert sum(cents(s["received"]) for s in d["stores_detail"]) == cents(d["received"])
        assert {f["finding_id"] for f in d["findings_detail"]} == set(p_ids(doc, p))
        for f in d["findings_detail"]:
            assert {"finding_id", "gap", "outcome", "state", "risk", "risk_label", "counts_as"} <= set(f)
            assert 0 <= f["risk"] <= 100
    s = listing["summary"]
    for k in ("gross", "net", "expected_net", "received", "shortfall", "owed"):          # totals = sum of rows
        assert cents(s[k]) == sum(cents(p[k]) for p in listing["payouts"])
    assert payouts.detail(doc, store.all(), listing["payouts"][0]["payout_id"])["payout_key"] == \
        listing["payouts"][0]["payout_key"]                                            # bare payout_id works too
    with pytest.raises(KeyError):
        payouts.detail(doc, store.all(), "nope:nope")


def p_ids(doc, p):
    return next(x for x in doc["payouts"] if x["payout_key"] == p["payout_key"])["finding_ids"]


def test_generated_run_finds_the_short_deposit_and_the_deal_terms(run):
    store, _ = run
    listing = payouts.listing(transactions.load(store.dir), store.all())
    assert listing["summary"]["owed"] > 0 and listing["summary"]["by_status"]["short_deposit"] >= 1
    short = next(p for p in listing["payouts"] if p["status"] == "short_deposit")
    assert short["deposit_owed"] == short["deposit_shortfall"] > 0
    d = payouts.detail(transactions.load(store.dir), store.all(), short["payout_key"])
    com = next(ln for ln in d["lines"] if ln["key"] == "commission")
    assert com["terms"] and com["terms"][0]["authority"] and "commission rate" in com["basis"]
    assert not str(com["terms"][0]["source_file"]).startswith("/")                     # a readable path, not a temp dir


def lumpsum(findings, deposit=True, terms=True, net_override=None):
    """A two-store lump sum by hand: gross 100, commission 25 (deal says 20), fees 2, deposit 70 or none."""
    lines = [
        {"payout_line_id": "L1", "channel": "doordash", "store_id": "S1", "order_id": "O1", "gross": "60",
         "commission": "15", "other_fees": "1", "net": "44", "payout_id": "P1", "payout_date": "2026-01-07",
         "placed_at": "2026-01-01T10:00"},
        {"payout_line_id": "L2", "channel": "doordash", "store_id": "S2", "order_id": "O2", "gross": "40",
         "commission": "10", "other_fees": "1", "net": net_override or "29", "payout_id": "P1",
         "payout_date": "2026-01-07", "placed_at": "2026-01-02T10:00"},
    ]
    deps = [{"channel": "doordash", "payout_ids": ["P1"], "deposit_amount": 70.0, "match_confidence": 1.0,
             "deposit": {"deposit_id": "B1", "amount": 70.0, "deposit_date": "2026-01-08"}}] if deposit else \
        [{"channel": "doordash", "payout_ids": ["P1"], "deposit": None, "deposit_amount": 0.0}]
    term = {"term_id": "T1", "channel": "doordash", "store_id": "all", "term_type": "commission", "value": 0.2,
            "authority": "contract", "source_file": "/tmp/x/raw/contracts/dd.pdf", "quote": "20% of subtotal",
            "effective_from": "2025-01-01"}
    doc = transactions.build({"payout_lines": lines, "orders": []}, deps, findings, "test",
                             terms=[term] if terms else None)
    return doc


def finding(fid, ct, oid, gap, outcome, state):
    return {"finding_id": fid, "check_type": ct, "channel": "doordash", "order_ids": [oid], "gap": gap,
            "expected": 0, "actual": gap, "outcome": outcome, "state": state, "evidence": []}


def test_payout_math_by_hand_follows_finding_state():
    fs = [finding("F-1", "commission", "O1", 3.0, "recoverable", "FILED"),     # owed
          finding("F-2", "commission", "O2", 2.0, "conflict", "JUDGED"),       # in question, not counted
          finding("F-3", "fee", "O2", 1.0, "recoverable", "RECOVERED"),        # counted, already paid back
          {**finding("F-4", "deposit", "P1", 3.0, "recoverable", "FILED"), "order_ids": ["P1"]}]
    doc = lumpsum(fs)
    by = {f["finding_id"]: f for f in fs}
    p = payouts.detail(doc, fs, "P1")
    check_reconciles(p)
    lines = {ln["key"]: ln for ln in p["lines"]}
    assert (p["gross"], p["deductions_total"], p["net"], p["received"]) == (100.0, 27.0, 73.0, 70.0)
    assert lines["commission"]["expected"] == 22.0 and lines["commission"]["in_question"] == 2.0
    assert lines["other_fees"]["expected"] == 1.0 and lines["other_fees"]["recovered"] == 1.0
    assert p["expected_net"] == 77.0 and p["shortfall"] == 7.0                       # 73 + 3 + 1; 77 - 70
    assert (p["open_gaps"], p["deposit_owed"], p["owed"]) == (3.0, 3.0, 6.0)          # recovered 1 is not owed
    assert p["status"] == "short_deposit" and set(p["flags"]) == {"short_deposit", "owed", "needs_review"}
    assert lines["commission"]["computable"] and lines["other_fees"]["computable"] is False   # no fee term
    assert "no deal document sets a fee" in lines["other_fees"]["note"]
    assert not p["expected_net_computable"] and p["not_computable"] == ["other_fees"]

    by["F-2"]["state"] = "APPROVED"            # a human sided with us: now counted
    by["F-4"]["state"] = "RECOVERED"           # the platform paid the deposit gap
    by["F-1"]["state"] = "RECOVERED"
    by["F-3"]["state"] = "RECOVERED"
    p = payouts.detail(doc, fs, "doordash:P1")
    check_reconciles(p)
    assert p["expected_net"] == 79.0 and p["owed"] == 2.0 and p["deposit_owed"] == 0.0
    assert p["owed_breakdown"]["deposit_closed_by"] == ["F-4"]
    by["F-2"]["state"] = "REJECTED"            # the contract figure stands
    p = payouts.detail(doc, fs, "P1")
    assert p["owed"] == 0.0 and p["status"] == "recovered"


def test_missing_deposit_and_adjustments_and_no_terms():
    doc = lumpsum([], deposit=False, terms=False, net_override="28.50")
    p = payouts.detail(doc, [], "P1")
    check_reconciles(p)
    assert p["status"] == "missing_deposit" and p["received"] == 0.0
    assert p["adjustments"] == -0.5 and p["owed"] == p["net"] == p["deposit_shortfall"]
    assert all(not ln["computable"] for ln in p["lines"] if ln["actual"] and ln["key"] != "other")
    assert payouts.listing(None, [])["payouts"] == [] and "no pipeline run" in payouts.listing(None, [])["note"]
    old = {k: v for k, v in doc.items() if k != "payouts"}       # a transactions.json from before payouts existed
    assert payouts.listing(old, [])["payouts"][0]["payout_key"] == "doordash:P1"


# the theater ----------------------------------------------------------------------------------------------------
def fixtures_run(store, tmp, demo_approve=True, reset=True):
    pipeline.run(reset=reset, store=store, portal=tmp / "portal.jsonl", rules_path=tmp / "rules.json",
                 runs_path=tmp / "runs.jsonl", demo_approve=demo_approve)


def test_stream_cursor_is_monotonic_and_pages(tmp_path):
    store = JsonStore(tmp_path / "state")
    fixtures_run(store, tmp_path)
    t = theater.Theater(store)
    seen, since = [], 0
    while True:
        page = t.read(since, limit=7)
        seen += [e["cursor"] for e in page["events"]]
        if not page["more"]:
            break
        assert not page["reset"] or since == 0
        since = page["cursor"]
    assert seen == sorted(seen) and len(seen) == len(set(seen)) and seen[0] == 1
    types = [e["type"] for e in t.events]
    for want in ("run_started", "vote", "verdict", "bet_staked", "escalation", "human_decision", "bet_settled",
                 "filed", "case_file", "rule_proposed", "agent_done"):
        assert want in types, want
    assert all(e["text"] and e["lane"] in ("agents", "judges", "market", "human", "learn") for e in t.events)
    v = next(e for e in t.events if e["type"] == "vote")
    assert {"judge", "outcome", "confidence", "model", "finding_id"} <= set(v)
    esc = next(e for e in t.events if e["type"] == "escalation")
    assert esc["rank"] >= 1 and esc["status"] in ("queued", "deferred")
    st = next(e for e in t.events if e["type"] == "bet_settled")
    assert {"payout", "balance", "balances"} <= set(st)
    # a stake comes after its verdict; a settlement after its human decision
    idx = {(e["type"], e.get("finding_id"), e.get("judge")): i for i, e in enumerate(t.events)}
    for (typ, fid, _), i in idx.items():
        if typ == "bet_staked":
            assert idx[("verdict", fid, None)] < i
        if typ == "bet_settled":
            assert idx[("human_decision", fid, None)] < i

    last = t.cursor
    page = t.read(last)
    assert page["events"] == [] and not page["reset"] and page["cursor"] == last     # nothing new: same cursor
    fixtures_run(store, tmp_path)                                                    # a new run resets the files
    page = t.read(last)
    assert page["reset"] and page["events"][0]["cursor"] > last and page["first_cursor"] > last
    assert page["events"][0]["type"] == "run_started"
    assert t.read(10 ** 6)["reset"]                       # a cursor from a later process: start over, still JSON


def test_stream_appends_live_decisions(tmp_path):
    store = JsonStore(tmp_path / "state")
    fixtures_run(store, tmp_path, demo_approve=False)
    a = api.Api(store=store, portal=tmp_path / "portal.jsonl")
    first = a.stream({})
    fid = next(f["finding_id"] for f in store.all() if f["state"] == "JUDGED" and f.get("dispute_draft"))
    a.decide(fid, "approve", {"actor": "thabhelo", "reason": "checks out"})
    nxt = a.stream({"since": [str(first["cursor"])]})
    assert not nxt["reset"] and all(e["cursor"] > first["cursor"] for e in nxt["events"])
    kinds = [e["type"] for e in nxt["events"]]
    assert kinds[0] == "human_decision" and "bet_settled" in kinds and "filed" in kinds
    d = next(e for e in nxt["events"] if e["type"] == "human_decision")
    assert d["human"] == "thabhelo" and d["finding_id"] == fid


def test_year_scale_theater_stays_light(tmp_path):
    """Thousands of judged findings become a few hundred events: the bulk events count the rest."""
    store = JsonStore(tmp_path / "state")
    rows, at = [], "2026-10-25T07:00:00+00:00"
    for i in range(3000):
        fid = f"F-{i:05d}"
        outcome = "conflict" if i % 25 == 0 else "recoverable"
        votes = [{"judge": j, "outcome": outcome, "confidence": 0.9, "reason": "r"} for j in
                 ("judge.strict", "judge.operator", "judge.skeptic")]
        rows.append({"at": at, "actor": "checkers", "role": "agent", "action": "check", "subject_id": fid,
                     "subject_type": "finding", "allowed": True, "before": "NEW", "after": "CHECKED", "diff": {}})
        rows.append({"at": at, "actor": "panel", "role": "agent", "action": "judge", "subject_id": fid,
                     "subject_type": "finding", "allowed": True, "before": "CHECKED", "after": "JUDGED",
                     "diff": {"outcome": [None, outcome], "votes": [[], votes], "reason": [None, "x"],
                              **({"attention": [None, {"rank": i // 25 + 1, "budget": 10, "value": 1.0,
                                                       "status": "queued"}]} if outcome == "conflict" else {})}})
    store.log_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    t = theater.Theater(store)
    page = t.read(0, limit=1000)
    assert len(t.events) < 600, len(t.events)
    bulk = next(e for e in t.events if e["type"] == "judged_bulk")
    told = sum(1 for e in t.events if e["type"] == "verdict")
    esc = sum(1 for e in t.events if e["type"] == "escalation")
    assert told + bulk["count"] + (esc - sum(1 for e in t.events if e["type"] == "verdict"
                                             and e["outcome"] == "conflict")) == 3000
    assert next(e for e in t.events if e["type"] == "checked")["count"] == 3000
    told_conflicts = sum(1 for e in t.events if e["type"] == "verdict" and e["outcome"] == "conflict")
    assert told_conflicts == 1 + theater.FULL_CONFLICTS                    # first of its kind + the first few
    assert esc == theater.CONFLICT_CAP + told_conflicts and page["cursor"] == len(t.events)


def PACE(e):
    return theater.PACE_MS.get(e["type"], theater.DEFAULT_PACE_MS)


def test_replay_pacing_is_bounded():
    events = [{"cursor": i, "type": t, "lane": "judges", "text": "x"} for i, t in
              enumerate(["run_started", "vote", "bet_staked", "escalation", "rule_proposed", "nope"] * 2000, 1)]
    for speed in (0.1, 1, 2, 8, 100):
        s = theater.schedule(events, speed)
        sp = min(max(speed, theater.SPEEDS[0]), theater.SPEEDS[1])
        assert s[0]["delay_ms"] == 0 and [e["cursor"] for e in s] == list(range(1, len(events) + 1))
        floor = min(theater.MIN_GAP_MS / sp, theater.MAX_TOTAL_MS / sp / (len(events) - 1))  # floor gives way only
        assert all(floor - 1 <= e["delay_ms"] <= theater.MAX_GAP_MS / sp + 1 for e in s[1:])  # when it can't fit
        assert s[-1]["t_ms"] <= theater.MAX_TOTAL_MS / sp + 1                            # the whole show fits
        assert all(a["t_ms"] <= b["t_ms"] for a, b in zip(s, s[1:]))
    mid = theater.schedule(events[:400], 1)                     # long but fits above the floor: compressed, floor kept
    assert sum(PACE(e) for e in events[1:400]) > theater.MAX_TOTAL_MS >= mid[-1]["t_ms"] - 1
    assert all(e["delay_ms"] >= theater.MIN_GAP_MS - 1 for e in mid[1:])
    short = theater.schedule(events[:4], 1)
    assert [e["delay_ms"] for e in short] == [0, theater.PACE_MS["vote"], theater.PACE_MS["bet_staked"],
                                              theater.PACE_MS["escalation"]]


def test_replay_releases_events_on_schedule(tmp_path):
    store = JsonStore(tmp_path / "state")
    fixtures_run(store, tmp_path)
    full = theater.replay(store, speed=2, everything=True)
    assert full["count"] == len(full["events"]) > 10 and full["done"]
    first = theater.replay(store, speed=2, now_ms=1_000_000)
    assert first["start"] == 1_000_000 and [e["cursor"] for e in first["events"]] == [1]   # only t=0 is due
    mid = full["events"][len(full["events"]) // 2]
    page = theater.replay(store, speed=2, since=1, start_ms=1_000_000, now_ms=1_000_000 + mid["t_ms"])
    assert page["events"][0]["cursor"] == 2 and page["events"][-1]["t_ms"] <= mid["t_ms"]
    assert page["cursor"] == page["events"][-1]["cursor"] and page["next_in_ms"] >= 0 and not page["done"]
    end = theater.replay(store, speed=2, since=page["cursor"], start_ms=1_000_000,
                         now_ms=1_000_000 + full["duration_ms"])
    assert end["done"] and end["cursor"] == full["count"]


# the endpoints --------------------------------------------------------------------------------------------------
@pytest.fixture
def server(run, monkeypatch):
    store, tmp = run
    a = api.Api(store=store, portal=tmp / "portal.jsonl")
    monkeypatch.setattr(api.Handler, "api", a)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def get(port, path, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, (body if raw else json.loads(body))


def test_api_shapes(server):
    code, out = get(server, "/api/payouts")
    assert code == 200 and {"payouts", "total", "summary", "unpaid_orders", "formula", "source"} <= set(out)
    p = out["payouts"][0]
    assert {"payout_id", "payout_key", "platform", "deposit_amount", "deposit_date", "received", "stores", "orders",
            "gross", "deductions", "net", "expected_net", "shortfall", "owed", "findings", "status"} <= set(p)
    code, one = get(server, f"/api/payouts/{p['payout_id']}")
    assert code == 200 and {"waterfall", "lines", "stores_detail", "findings_detail", "owed_breakdown"} <= set(one)
    code, one = get(server, "/api/payouts/" + p["payout_key"].replace(":", "%3A"))
    assert code == 200 and one["payout_key"] == p["payout_key"]
    assert get(server, "/api/payouts?status=matched")[1]["total"] == out["summary"]["by_status"]["matched"]
    assert get(server, "/api/payouts/NOPE")[0] == 404
    code, s = get(server, "/api/stream?since=0&limit=5")
    assert code == 200 and len(s["events"]) == 5 and s["more"] and s["reset"]
    assert {"run", "cursor", "events", "balances", "reset", "first_cursor", "latest"} <= set(s)
    code, s2 = get(server, f"/api/stream?since={s['cursor']}&limit=5")
    assert s2["events"][0]["cursor"] == s["cursor"] + 1 and not s2["reset"]
    code, r = get(server, "/api/stream/replay?run=last&speed=4&all=1")
    assert code == 200 and r["mode"] == "replay" and r["events"] and r["bounds"]["max_gap_ms"] == 750
    assert get(server, "/api/stream/replay?run=RUN-x")[0] == 400
    assert get(server, "/api/stream/replay?speed=fast")[0] == 400
    assert get(server, "/api/stream?since=abc")[0] == 400
    routes = get(server, "/api")[1]["routes"]
    assert any("/api/payouts" in r for r in routes) and any("/api/stream" in r for r in routes)


def test_sse_replay_streams_events(server, monkeypatch):
    monkeypatch.setattr(theater, "MAX_TOTAL_MS", 400)          # keep the test quick
    code, body = get(server, "/api/stream/sse?replay=1&speed=20", raw=True)
    text = body.decode()
    assert code == 200 and "event: run_started" in text and text.rstrip().endswith("data: {\"mode\": \"replay\"}")
    first = next(ln for ln in text.splitlines() if ln.startswith("data: {\"cursor\""))
    assert json.loads(first[6:])["cursor"] == 1


def test_dashboard_routes(run):
    store, tmp = run
    c = create_app(store, runs_path=tmp / "runs.jsonl").test_client()
    html = c.get("/").data
    assert b"#/payouts" in html and b"#/theater" in html
    js = c.get("/static/app.js").data
    assert b"renderPayouts" in js and b"renderTheater" in js and b"/api/stream/replay" in js
    d = c.get("/api/payouts").get_json()
    assert d["payouts"] and d["summary"]["payouts"] == d["total"]
    key = d["payouts"][0]["payout_key"]
    one = c.get(f"/api/payouts/{key}").get_json()
    assert one["payout_key"] == key and one["waterfall"]
    assert c.get("/api/payouts/nope").status_code == 404
    s = c.get("/api/stream?since=0").get_json()
    assert s["events"] and s["events"][0]["cursor"] >= 1
    assert c.get("/api/stream?since=x").status_code == 400
    r = c.get("/api/stream/replay?speed=8&all=1").get_json()
    assert r["count"] == len(r["events"]) and r["speed"] == 8
    assert c.get("/api/stream/replay?run=other").status_code == 400


def test_fixtures_run_has_no_lump_sums_but_says_why(tmp_path):
    store = JsonStore(tmp_path / "state")
    fixtures_run(store, tmp_path)
    out = payouts.listing(transactions.load(Path(store.dir)), store.all())
    assert out["payouts"] == [] and "no transactions" in out["note"]
