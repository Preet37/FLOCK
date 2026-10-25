"""The deployed JSON API (flock/api.py) for the frontend: shapes, pagination, risk bounds, metrics that trace to their
inputs, path traversal, the token on POST, dataset-run caps, background jobs, and the dashboard's mirror."""
import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from flock import api, frontend, llm, transactions
from flock.agents import journal
from flock.store import JsonStore

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


class Client:
    """Raw http.client, so paths like /api/screens/../x reach the server un-normalized."""

    def __init__(self, port: int):
        self.port = port

    def call(self, method: str, path: str, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        c.request(method, path, body=data, headers=headers or {})
        r = c.getresponse()
        raw = r.read()
        ctype = r.getheader("Content-Type") or ""
        out = json.loads(raw) if ctype.startswith("application/json") and raw else raw
        c.close()
        return r.status, out, dict(r.getheaders())

    def get(self, path):
        code, out, _ = self.call("GET", path)
        return code, out

    def post(self, path, body=None, auth=True):
        code, out, _ = self.call("POST", path, body if body is not None else {}, AUTH if auth else
                                 {"Content-Type": "application/json"})
        return code, out


@pytest.fixture
def server(tmp_path, monkeypatch):
    store = JsonStore(tmp_path / "state")
    a = api.Api(store=store, portal=tmp_path / "portal.jsonl")
    monkeypatch.setattr(api.Handler, "api", a)
    monkeypatch.setattr(api, "TOKEN", TOKEN)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield Client(httpd.server_address[1]), a
    httpd.shutdown()
    httpd.server_close()


def fixtures_run(client, demo_approve=True):
    code, out = client.post("/api/run", {"reset": True, "demo_approve": demo_approve})
    assert code == 200, out
    return out


def wait_job(client, job_id, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        code, j = client.get(f"/api/jobs/{job_id}")
        assert code == 200
        if j["status"] != "running":
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")


@pytest.fixture
def dataset(server):
    client, a = server
    code, job = client.post("/api/run", {"dataset": "demo", "seed": 2025, "profile": "default", "stores": 3, "weeks": 2})
    assert code == 202 and job["status"] == "running" and job["poll"] == f"/api/jobs/{job['job_id']}"
    j = wait_job(client, job["job_id"])
    assert j["status"] == "done", j
    return client, a, j


# auth, CORS, errors -------------------------------------------------------------------------------------------------
def test_token_required_on_every_post(server):
    client, _ = server
    for path in ("/api/run", "/api/findings/F-0001/approve", "/api/rules/R-001/approve"):
        assert client.post(path, auth=False)[0] == 401
        code, _, _ = client.call("POST", path, b"{}", {"Authorization": "Bearer wrong"})
        assert code == 401
    assert client.get("/api/findings")[0] == 200  # reads stay open


def test_cors_and_options(server):
    client, _ = server
    code, _, headers = client.call("OPTIONS", "/api/run")
    assert code == 204 and headers["Access-Control-Allow-Origin"] == "*"
    assert "Authorization" in headers["Access-Control-Allow-Headers"]


def test_errors_are_json(server):
    client, _ = server
    assert client.get("/api/findings/F-NOPE") == (404, {"error": "not found: 'F-NOPE'"})
    assert client.get("/api/nope")[0] == 404
    code, out = client.get("/api/transactions?limit=lots")
    assert code == 400 and "limit" in out["error"]
    code, out, _ = client.call("POST", "/api/run", b"{not json", AUTH)
    assert code == 400 and "JSON" in out["error"]
    code, out = client.post("/api/run", {"dataset": "mystery"})
    assert code == 400 and "dataset" in out["error"]


def test_health(server):
    client, _ = server
    code, h = client.get("/api/health")
    assert code == 200 and h["status"] == "ok" and h["store"] == "JsonStore" and h["auth_required_for_post"]
    assert h["last_run"] is None and h["counts"]["findings"] == 0
    fixtures_run(client)
    h = client.get("/api/health")[1]
    assert h["last_run"]["source"] == "fixtures/fake_findings.json" and h["counts"]["findings"] == 6


# findings + risk ----------------------------------------------------------------------------------------------------
def test_findings_carry_risk(server):
    client, _ = server
    fixtures_run(client, demo_approve=False)
    rows = client.get("/api/findings")[1]
    assert len(rows) == 6
    for f in rows:
        assert 0 <= f["risk"] <= 100 and f["risk_label"] in ("low", "med", "high")
        assert set(f["risk_factors"]) == {"dollars", "uncertainty", "authority", "attention"}
        assert f["risk"] == frontend.risk(f)["risk"]
    by = {f["finding_id"]: f for f in rows}
    assert by["F-0005"]["risk_label"] == "low"                       # correct, $0 gap
    assert by["F-0003"]["risk"] > by["F-0001"]["risk"]               # a conflict outranks a clear $2.50 recoverable
    ranked = client.get("/api/findings?sort=risk")[1]
    assert [f["risk"] for f in ranked] == sorted((f["risk"] for f in ranked), reverse=True)
    assert all(f["outcome"] == "conflict" for f in client.get("/api/findings?outcome=conflict")[1])
    one = client.get("/api/findings/F-0003")[1]
    assert one["finding"]["risk"] == by["F-0003"]["risk"] and one["log"] and "45*D" in one["risk_formula"]


def test_risk_formula_bounds():
    assert frontend.risk({"gap": 0, "confidence": 1.0, "outcome": "correct"}) == {
        "risk": 0, "risk_label": "low", "risk_factors": {"dollars": 0, "uncertainty": 0, "authority": 0, "attention": 0}}
    worst = frontend.risk({"gap": -1e6, "confidence": 0.0, "outcome": "conflict", "attention": {"value": 1e9}})
    assert worst["risk"] == 100 and worst["risk_label"] == "high"
    for gap in (0, 0.01, 1, 10, 99, 1000, -50):
        for conf in (None, 0, 0.3, 0.9, 1, 1.7, -2):
            for outcome in ("correct", "recoverable", "conflict", None):
                r = frontend.risk({"gap": gap, "confidence": conf, "outcome": outcome})
                assert 0 <= r["risk"] <= 100
                assert all(0 <= v <= 1 for v in r["risk_factors"].values())
    gaps = [frontend.risk({"gap": g, "confidence": 0.5, "outcome": "recoverable"})["risk"] for g in (1, 10, 50, 100)]
    assert gaps == sorted(gaps)  # more money, more risk
    weak = frontend.risk({"gap": 5, "confidence": 0.9, "outcome": "recoverable",
                          "evidence": [{"authority": "unauth_email"}]})
    assert weak["risk_factors"]["authority"] == 0.5
    # the documented formula, by hand: 45*D + 25*U + 20*A + 10*V
    import math
    f = {"gap": 20, "confidence": 0.6, "outcome": "conflict", "attention": {"value": 5}}
    d = math.log1p(20) / math.log1p(frontend.RISK_GAP_CAP)
    assert frontend.risk(f)["risk"] == round(45 * d + 25 * 0.4 + 20 * 1 + 10 * 5 / frontend.RISK_ATTENTION_CAP)


# transactions -------------------------------------------------------------------------------------------------------
def test_transactions_empty_after_fixtures(server):
    client, _ = server
    fixtures_run(client)
    t = client.get("/api/transactions")[1]
    assert t["total"] == 0 and t["rows"] == [] and "no payout tables" in t["note"]


def test_transactions_shape_and_pagination(dataset):
    client, a, _ = dataset
    t = client.get("/api/transactions?limit=5")[1]
    assert t["total"] > 5 and len(t["rows"]) == 5 and t["limit"] == 5 and t["offset"] == 0
    row = next(r for r in t["rows"] if r["kind"] == "payout")
    for k in ("txn_id", "channel", "platform", "store_id", "payout_id", "payout_date", "gross", "deductions", "net",
              "deposit", "deposit_gap", "deposit_status", "finding_ids", "source", "status", "flags"):
        assert k in row
    assert round(row["gross"] - row["deductions"]["total"], 2) == pytest.approx(row["net"], abs=0.02)
    assert row["source"]["files"][0]["file"] and row["source"]["lines"] >= 1
    nxt = client.get("/api/transactions?limit=5&offset=5")[1]
    assert not {r["txn_id"] for r in t["rows"]} & {r["txn_id"] for r in nxt["rows"]}
    every = client.get(f"/api/transactions?limit={transactions.MAX_LIMIT}")[1]
    assert len(every["rows"]) == min(t["total"], transactions.MAX_LIMIT)
    s = every["summary"]
    assert s["rows"] == t["total"] and sum(s["by_status"].values()) == s["rows"]
    assert client.get("/api/transactions?limit=100000")[1]["limit"] == transactions.MAX_LIMIT
    dd = client.get("/api/transactions?channel=doordash&limit=500")[1]
    assert dd["total"] and all(r["channel"] == "doordash" for r in dd["rows"])
    assert dd["summary"]["rows"] == s["rows"] and dd["filtered_summary"]["rows"] == dd["total"]
    st = client.get("/api/transactions?store=S01&limit=500")[1]
    assert st["total"] and all(r["store_id"] == "S01" for r in st["rows"])
    # every finding-bearing payout links real finding ids; status filter returns rows flagged with it
    ids = {f["finding_id"] for f in a.store.all()}
    assert all(set(r["finding_ids"]) <= ids for r in every["rows"])
    for status, n in s["by_status"].items():
        if n:
            got = client.get(f"/api/transactions?status={status}&limit=500")[1]
            assert got["total"] >= n and all(status in r["flags"] for r in got["rows"])
    assert s["by_status"]["sold_never_paid"] >= 1  # the generator plants sold-never-paid orders
    assert t["data"]["store_weeks"] == 6


def test_transactions_build_by_hand(tmp_path):
    lines = [
        {"payout_line_id": "L1", "channel": "doordash", "store_id": "S1", "order_id": "O1", "gross": "10", "commission": "2",
         "net": "8", "payout_id": "P1", "payout_date": "2026-01-07", "source_file": "dd.csv", "source_row": "2"},
        {"payout_line_id": "L2", "channel": "doordash", "store_id": "S2", "order_id": "O2", "gross": "20", "commission": "4",
         "net": "16", "payout_id": "P1", "payout_date": "2026-01-07", "source_file": "dd.csv", "source_row": "3"},
        {"payout_line_id": "L3", "channel": "grubhub", "store_id": "S1", "order_id": "O3", "gross": "5", "net": "5",
         "payout_id": "G1", "payout_date": "2026-01-07", "source_file": "gh.csv", "source_row": "2"},
    ]
    orders = [{"order_id": o, "store_id": s, "channel": c, "placed_at": d, "subtotal": v, "source_file": "pos.csv",
               "source_row": str(i)} for i, (o, s, c, d, v) in enumerate([
                   ("O1", "S1", "doordash", "2026-01-01T10:00", "10"), ("O2", "S2", "doordash", "2026-01-02T10:00", "20"),
                   ("O3", "S1", "grubhub", "2026-01-03T10:00", "5"), ("O4", "S2", "ubereats", "2026-01-12T10:00", "7")], 2)]
    deposits = [{"channel": "doordash", "payout_ids": ["P1"], "deposit_amount": 21.0, "match_confidence": 0.7,
                 "deposit": {"deposit_id": "B1", "amount": 21.0, "deposit_date": "2026-01-08"}},
                {"channel": "grubhub", "payout_ids": ["G1"], "deposit": None, "deposit_amount": 0.0}]
    findings = [
        {"finding_id": "F-D", "check_type": "deposit", "channel": "doordash", "order_ids": ["P1"], "gap": 3.0,
         "outcome": "recoverable", "state": "FILED", "expected": 24, "actual": 21},
        {"finding_id": "F-C", "check_type": "commission", "channel": "doordash", "order_ids": ["O2"], "gap": 1.0,
         "outcome": "conflict", "state": "JUDGED", "expected": 3, "actual": 4},
        {"finding_id": "F-N", "check_type": "missing_order", "channel": "ubereats", "order_ids": ["O4"], "gap": 7.0,
         "outcome": "recoverable", "state": "JUDGED", "expected": 7.0, "actual": 0.0},
    ]
    doc = transactions.build({"payout_lines": lines, "orders": orders}, deposits, findings, "real data (test)")
    rows = {r["txn_id"]: r for r in doc["rows"]}
    s1, s2 = rows["doordash:P1:S1"], rows["doordash:P1:S2"]
    assert s1["deposit"]["store_share"] + s2["deposit"]["store_share"] == 21.0  # the deposit, split exactly by net
    assert (s1["deposit"]["store_share"], s2["deposit"]["store_share"]) == (7.0, 14.0)
    assert s1["deposit_status"] == s2["deposit_status"] == "short" and s2["deposit_gap"] == -2.0
    assert s1["finding_ids"] == ["F-D"] and s2["finding_ids"] == ["F-C", "F-D"]
    assert rows["grubhub:G1:S1"]["deposit_status"] == "missing_deposit"
    assert rows["order:O4"]["kind"] == "order" and rows["order:O4"]["gross"] == 7.0
    assert doc["data"] == {"stores": 2, "days": 12, "weeks": 2, "store_weeks": 4, "orders": 4, "payout_lines": 3,
                           "deposits": 1, "period_start": "2026-01-01", "period_end": "2026-01-12"}
    page = transactions.query(doc, findings, limit=50)
    st = {r["txn_id"]: r for r in page["rows"]}
    assert st["doordash:P1:S2"]["status"] == "short"
    assert st["doordash:P1:S2"]["flags"] == ["short", "disputed", "needs_review"]
    assert st["grubhub:G1:S1"]["status"] == "missing_deposit"
    assert st["order:O4"]["status"] == "sold_never_paid"
    findings[0]["state"] = "REJECTED"  # status follows the findings' current state, not the run's
    assert transactions.query(doc, findings)["rows"][0]["txn_id"] and \
        {r["txn_id"]: r for r in transactions.query(doc, findings)["rows"]}["doordash:P1:S1"]["flags"] == ["short"]


# metrics ------------------------------------------------------------------------------------------------------------
def test_metrics_trace_to_inputs(dataset):
    client, a, job = dataset
    m = client.get("/api/metrics")[1]
    cards = {c["id"]: c for c in m["cards"]}
    assert list(cards) == ["agent_time", "accuracy", "cost", "money_saved"]
    for c in cards.values():
        assert {"value", "unit", "formula", "inputs", "sources"} <= set(c) and c["formula"] and c["sources"]
    run = json.loads((a.dir / "last_run.json").read_text())

    t = cards["agent_time"]
    assert t["inputs"]["agent_seconds"] == run["seconds"]
    assert t["value"]["agent_hours"] == round(run["seconds"] / 3600, 6)
    assert t["inputs"]["store_weeks"] == run["data"]["store_weeks"] == 3 * 2
    assert t["inputs"]["human_baseline"] == "INDUSTRY ESTIMATE" and t["inputs"]["minutes_per_store_week"] == 60.0
    assert t["value"]["human_hours"] == 6 * 60 / 60
    src = next(s for s in t["sources"] if s["what"] == "minutes_per_store_week")
    assert src["label"] == "INDUSTRY ESTIMATE" and src["source"].startswith("https://lunchbox.io") and "§7" in src["cited_in"]

    acc = cards["accuracy"]
    score = client.get("/api/score")[1]
    assert (acc["value"]["caught"], acc["value"]["total"]) == (score["caught"], score["total"]) == (
        job["result"]["score"]["caught"], job["result"]["score"]["total"])
    assert acc["value"]["accuracy"] == round(score["caught"] / score["total"], 4)
    assert acc["inputs"]["answer_key"].endswith("state/answer_key.csv") or "answer_key.csv" in acc["inputs"]["answer_key"]

    cost = cards["cost"]
    assert cost["value"]["model_calls"] == run["llm"]["calls"] == 0 and cost["value"]["usd"] == 0.0

    money = cards["money_saved"]
    open_ = [f for f in a.store.all() if journal.is_open(f)]
    assert money["value"]["usd"] == round(sum(abs(f["gap"]) for f in open_), 2)
    parts = sum(money["value"][k] for k in ("recovered", "filed", "approved", "pending"))
    assert round(parts, 2) == money["value"]["usd"]
    assert money["inputs"]["projection_multiplier"] == 26.0  # 52 weeks / 2 weeks
    assert money["value"]["annualized_projection"] == round(money["value"]["usd"] * 26, 2)
    jes = api.get_journal_store(a.store).all()
    assert jes  # small runs draft journal entries
    booked = sum(ln["debit"] for je in jes for ln in je["lines"] if ln.get("finding_id") and ln["account_type"] == "asset")
    assert money["value"]["due_from_platform_booked"] == round(booked, 2)
    assert any(s["label"] == "PROJECTION" for s in money["sources"])


def test_metrics_after_fixtures_say_what_is_missing(server):
    client, _ = server
    fixtures_run(client)
    cards = {c["id"]: c for c in client.get("/api/metrics")[1]["cards"]}
    assert cards["agent_time"]["value"]["human_hours"] is None and "fixtures" in cards["agent_time"]["notes"][0]
    assert cards["accuracy"]["value"]["caught"] == 6 and cards["accuracy"]["value"]["total"] == 6
    assert cards["money_saved"]["value"]["annualized_projection"] is None


def test_human_baseline_prefers_a_measured_benchmark(tmp_path, monkeypatch):
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"current": {"human": {"human_minutes_per_store_week": 95, "label": "A teammate",
                                                         "accuracy": 0.8, "caught": 8, "total": 10},
                                               "rules": {"label": "Rules only", "accuracy": 1.0}}}))
    b = frontend.human_baseline(results)
    assert b["label"] == "MEASURED" and b["minutes_per_store_week"] == 95
    monkeypatch.setenv("FLOCK_HUMAN_MINUTES_PER_STORE_WEEK", "30")
    assert frontend.human_baseline(tmp_path / "none.json")["minutes_per_store_week"] == 30.0
    monkeypatch.delenv("FLOCK_HUMAN_MINUTES_PER_STORE_WEEK")
    assert frontend.human_baseline(tmp_path / "none.json")["label"] == "INDUSTRY ESTIMATE"
    store = JsonStore(tmp_path / "s")
    card = frontend.accuracy_card(store, results)
    assert card["benchmark"]["human"]["accuracy"] == 0.8 and card["benchmark"]["rules"]["accuracy"] == 1.0


def test_cost_card_prices_reported_tokens(tmp_path, monkeypatch):
    llm.reset_calls()
    llm._count("anthropic")
    llm._note_tokens("claude-sonnet-5-5-20260101", 1_000_000, 100_000)
    with llm._stats_lock:
        llm._credits["agent-x"] += 12.5
    run = {"findings": 10, "llm": llm.call_stats()}
    store = JsonStore(tmp_path / "s")
    c = frontend.cost_card(store, run)
    tok = next(p for p in c["inputs"]["parts"] if p["what"].startswith("tokens"))
    assert tok["usd"] == 2.0 + 1.0  # 1M in x $2 + 0.1M out x $10
    assert c["value"]["usd"] == 3.0 and c["value"]["complete"] is False and c["value"]["credits"] == 12.5
    assert c["value"]["display"] == "$3.00" and c["value"]["basis"] == "estimated from tokens at list price"
    assert any("FLOCK_CREAO_USD_PER_CREDIT" in u for u in c["unknown"])
    monkeypatch.setenv("FLOCK_CREAO_USD_PER_CREDIT", "0.02")
    c = frontend.cost_card(store, run)
    assert c["value"]["usd"] == 3.25 and c["value"]["usd_per_1000_findings"] == 325.0
    assert run["llm"]["by_provider"] == {"anthropic": 1}
    # CREAO /v2 bills USD: reported tokens x the console's per-model price
    llm._count("creao")
    llm._creao_note_usage("agent-y", {"models": [{"model": "anthropic/claude-sonnet-5-5", "input_tokens": 1_000_000,
                                                  "output_tokens": 0}]})
    run = {"findings": 10, "llm": llm.call_stats()}
    c = frontend.cost_card(store, run)
    assert c["value"]["usd"] == round(3.0 + 0.25 + 2.60, 6)
    creao = next(p for p in c["inputs"]["parts"] if p["what"] == "CREAO hosted agents")
    assert creao["calls"] == 1 and creao["input_tokens"] == 1_000_000


# agents + screens ---------------------------------------------------------------------------------------------------
def test_agent_detail(server):
    client, _ = server
    fixtures_run(client)
    code, c = client.get("/api/agents/clicky")
    assert code == 200 and c["kind"] == "roster" and c["has_screen"] and "Only Clicky" in c["screen_note"]
    assert c["screens"] == {"filings": [], "demonstration": None, "latest": None}
    assert c["actions"] == 3 and any(x["action"] == "file" for x in c["activity"])
    p = client.get("/api/agents/panel")[1]
    assert not p["has_screen"] and p["screens"] is None and p["children_total"] == 6
    j = client.get("/api/agents/judge:F-0003")[1]
    assert j["kind"] == "live" and j["subject"] == "F-0003" and j["case_file"]["url"] == "/api/findings/F-0003"
    assert j["parent"]["agent_id"] == "worker:fixtures" and any(x["kind"] == "audit" for x in j["activity"])
    w = client.get("/api/agents/worker:fixtures")[1]
    assert {x["agent_id"] for x in w["children"]} >= {"judge:F-0001"}
    assert client.get("/api/agents/nobody")[0] == 404
    roster = client.get("/api/agents")[1]["agents"]
    assert {"clicky", "investigator", "journal"} <= {r["actor"] for r in roster}


def test_screens_served_and_traversal_refused(server, tmp_path):
    client, _ = server
    frontend.SCREENS_DIR.mkdir(parents=True)
    (frontend.SCREENS_DIR / "filing-SBX-1.png").write_bytes(b"\x89PNG fake")
    (frontend.SCREENS_DIR / "notes.txt").write_text("not an image")
    (frontend.SCREENS_DIR.parent / "secret.png").write_bytes(b"secret")
    demo = frontend.workflow.DEMOS_DIR / "demo-1"
    demo.mkdir(parents=True)
    (demo / "step-01.png").write_bytes(b"\x89PNG step")
    code, body, headers = client.call("GET", "/api/screens/filing-SBX-1.png")
    assert code == 200 and body == b"\x89PNG fake" and headers["Content-Type"] == "image/png"
    assert client.call("GET", "/api/screens/demos/demo-1/step-01.png")[1] == b"\x89PNG step"
    for bad in ("/api/screens/../secret.png", "/api/screens/..%2fsecret.png", "/api/screens/%2e%2e%2fsecret.png",
                "/api/screens/notes.txt", "/api/screens/demos/../../secret.png", "/api/screens/demos/demo-1/../../x.png",
                "/api/screens/.hidden.png", "/api/screens/a/b", "/api/screens/demos/demo-1/missing.png",
                "/api/screens/..", "/api/screens/demos/..%2f..%2fsecret.png/x.png"):
        code, out, _ = client.call("GET", bad)
        assert code == 404 and isinstance(out, dict), bad
    shots = client.get("/api/agents/clicky")[1]["screens"]
    assert shots["latest"] == "/api/screens/filing-SBX-1.png" and shots["demonstration"]["demo_id"] == "demo-1"


# runs, jobs, concurrency --------------------------------------------------------------------------------------------
def test_dataset_caps():
    p = api.Api.dataset_params({"dataset": "demo"})
    assert p["profile"] == "ca_bagel_chain" and p["stores"] == 56 and p["weeks"] == 2 and p["seed"] == 2025
    for bad in ({"stores": 57}, {"stores": 0}, {"weeks": 9}, {"weeks": 0}, {"seed": "x"}, {"seed": -1},
                {"profile": "../etc"}, {"stores": True}, {"weeks": 2.5}):
        with pytest.raises(api.BadRequest):
            api.Api.dataset_params({"dataset": "demo", **bad})
    assert api.Api.dataset_params({"stores": "12", "weeks": 8})["stores"] == 12


def test_dataset_caps_over_http(server):
    client, a = server
    code, out = client.post("/api/run", {"dataset": "demo", "stores": 500, "weeks": 52})
    assert code == 400 and "between 1 and" in out["error"] and not a.jobs


def test_dataset_job_keeps_the_answer_key(dataset):
    client, a, job = dataset
    assert job["result"]["auto_confirmed_unseen_layout"] is True
    assert (a.dir / "answer_key.csv").exists() and job["output_tail"]
    assert job["result"]["score"]["caught"] == job["result"]["score"]["total"]
    jobs = client.get("/api/jobs")[1]
    assert jobs["running"] is None and jobs["jobs"][0]["job_id"] == job["job_id"]
    # the fixtures run afterwards puts the committed key back (pipeline --reset removes the kept one)
    fixtures_run(client)
    assert not (a.dir / "answer_key.csv").exists()


def test_one_run_at_a_time(server):
    client, a = server
    a.run_lock.acquire()
    try:
        code, out = client.post("/api/run", {"dataset": "demo", "stores": 1, "weeks": 1})
        assert code == 409
        assert client.post("/api/run", {})[0] == 409
    finally:
        a.run_lock.release()
    fixtures_run(client, demo_approve=False)
    a.running = "JOB-busy"
    try:
        code, out = client.post("/api/findings/F-0001/approve", {"actor": "maja"})
        assert code == 409 and out["running"] == "JOB-busy"
    finally:
        a.running = None


def test_fixtures_run_can_go_to_the_background(server):
    client, _ = server
    code, job = client.post("/api/run?wait=0", {"reset": True})
    assert code == 202
    j = wait_job(client, job["job_id"])
    assert j["status"] == "done" and len(j["result"]["findings"]) == 6


def test_concurrent_decisions_cannot_double_approve(server):
    client, a = server
    fixtures_run(client, demo_approve=False)
    results = []

    def go():
        results.append(client.post("/api/findings/F-0001/approve", {"actor": "maja", "reason": "race"}))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(c for c, _ in results)
    assert codes == [200, 409, 409, 409]
    approvals = [e for e in a.store.log("F-0001") if e["action"] == "approve" and e["allowed"]]
    assert len(approvals) == 1 and a.store.get("F-0001")["state"] == "FILED"
    ok = next(out for c, out in results if c == 200)
    assert ok["log_entry"]["actor"] == "maja" and ok["finding"]["risk_label"]


def test_reads_skip_damaged_and_partial_jsonl_lines(tmp_path):
    store = JsonStore(tmp_path / "s")
    good = {"subject_id": "F-1", "actor": "x", "allowed": True}
    store.log_path.write_text(json.dumps(good) + "\n{broken\n" + json.dumps(good) + "\n" + '{"subject_id": "F-')
    assert store.log() == [good, good]
    p = tmp_path / "portal.jsonl"
    p.write_text('{"a": 1}\n{"b"')
    assert api._jsonl(p) == [{"a": 1}]


# the dashboard mirrors the cheap ones -------------------------------------------------------------------------------
def test_dashboard_mirror(server, tmp_path):
    from dashboard.app import create_app
    client, a = server
    fixtures_run(client)
    d = create_app(a.store, runs_path=a.runs_path).test_client()
    assert [c["id"] for c in d.get("/api/metrics").get_json()["cards"]] == ["agent_time", "accuracy", "cost",
                                                                          "money_saved"]
    assert d.get("/api/transactions").get_json()["total"] == 0
    assert d.get("/api/transactions?limit=x").status_code == 400
    assert "risk" in d.get("/api/findings").get_json()[0]
    assert "risk" in d.get("/api/findings/F-0001").get_json()["finding"]
    assert d.get("/api/agents/clicky").get_json()["has_screen"] is True
    assert d.get("/api/agents/nobody").status_code == 404
    assert d.get("/api/screens/../secret.png").status_code == 404


# QA fixes A2, A3, A5, A6, A7 and the dataset source label ------------------------------------------------------------
def test_stdout_capture_is_per_thread(server, capsys):
    """A3: two captures printing at the same time each get only their own lines; a background job's output stays in
    the job; a print from any other thread (server logs) still reaches the real stdout."""
    import sys
    _, a = server
    go = threading.Barrier(3, timeout=10)
    out = {}

    def chatty(name):
        go.wait()
        for i in range(200):
            print(f"{name} line {i}")
            if i % 20 == 0:
                time.sleep(0.001)
        return name

    def capture(name):
        out[name] = api._captured(chatty, name)

    def job():
        go.wait()
        for i in range(200):
            print(f"job line {i}")
            if i % 20 == 0:
                time.sleep(0.001)
        return "ok"

    started = a.start("fixtures", {}, job)
    threads = [threading.Thread(target=capture, args=(n,)) for n in ("left", "right")]
    for t in threads:
        t.start()
    print("server log while capturing")
    for t in threads:
        t.join()
    end = time.monotonic() + 10
    while a.jobs[started["job_id"]]["status"] == "running" and time.monotonic() < end:
        time.sleep(0.01)
    for name in ("left", "right"):
        result, text = out[name]
        assert result == name
        assert text.splitlines() == [f"{name} line {i}" for i in range(200)]
    assert a.jobs[started["job_id"]]["output"] == [f"job line {i}" for i in range(200)]
    print("server log after")
    real = capsys.readouterr().out
    assert "server log while capturing" in real and "server log after" in real
    assert "left line" not in real and "right line" not in real and "job line" not in real
    assert isinstance(sys.stdout, api.ThreadStdout)   # installed once; never swapped back and forth


def test_scoreboard_text_is_captured_per_request(server):
    client, _ = server
    fixtures_run(client)
    code, s = client.get("/api/score")
    assert code == 200 and s["text"].startswith("caught 6/6")


def test_responses_are_strict_json_even_with_nan(server, monkeypatch, tmp_path):
    """A2: NaN / inf never reach the wire (JSON.parse rejects them); they are sent as null."""
    assert api.dumps({"a": float("nan"), "b": [1.5, float("inf")], "c": {"d": -float("inf")}}) == \
        b'{"a": null, "b": [1.5, null], "c": {"d": null}}'
    client, a = server
    monkeypatch.setattr(a, "health", lambda: {"status": "ok", "gap": float("nan")})
    c = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    c.request("GET", "/api/health")
    raw = c.getresponse().read()
    c.close()

    def refuse(name):
        raise AssertionError(f"non-standard JSON constant {name}")

    assert json.loads(raw, parse_constant=refuse) == {"status": "ok", "gap": None}
    from dashboard.app import create_app
    d = create_app(JsonStore(tmp_path / "dash"), runs_path=tmp_path / "runs.jsonl")
    with d.app_context():
        assert json.loads(d.json.dumps({"x": float("nan")}), parse_constant=refuse) == {"x": None}


def test_options_204_has_no_body(server):
    """A5: a 204 must not carry a body."""
    client, _ = server
    c = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    c.request("OPTIONS", "/api/run")
    r = c.getresponse()
    assert r.status == 204 and r.getheader("Content-Length") == "0" and r.read() == b""
    c.close()


def test_token_compare_is_constant_time(server, monkeypatch):
    """A6: the token is checked with hmac.compare_digest."""
    client, _ = server
    seen = []
    real = api.hmac.compare_digest
    monkeypatch.setattr(api.hmac, "compare_digest", lambda x, y: seen.append((x, y)) or real(x, y))
    assert client.post("/api/run", {"dataset": "mystery"})[0] == 400
    assert client.call("POST", "/api/run", b"{}", {"Authorization": "Bearer x"})[0] == 401
    want = f"Bearer {TOKEN}".encode()
    assert seen == [(want, want), (b"Bearer x", want)]


def test_findings_default_order_is_meaningful_and_stable(server):
    """A6: needs_human first, then state priority, then risk (highest first), then finding_id."""
    client, _ = server
    fixtures_run(client, demo_approve=False)
    rows = client.get("/api/findings")[1]
    keys = [(not f["needs_human"], api.STATE_PRIORITY[f["state"]], -f["risk"], f["finding_id"]) for f in rows]
    assert keys == sorted(keys)
    assert rows[0]["needs_human"] and not rows[-1]["needs_human"]
    assert [f["finding_id"] for f in client.get("/api/findings")[1]] == [f["finding_id"] for f in rows]  # stable


def test_dashboard_run_history_follows_a_temp_store(tmp_path):
    """A7: create_app(<temp store>) without runs_path must not read or write the repo's data/state/runs.jsonl."""
    from dashboard.app import create_app
    from flock.store import STATE_DIR
    d = create_app(JsonStore(tmp_path / "state"))
    assert d.config["RUNS_PATH"] == tmp_path / "state" / "runs.jsonl"
    assert create_app(JsonStore(STATE_DIR)).config["RUNS_PATH"].resolve() == (STATE_DIR / "runs.jsonl").resolve()


def test_dataset_run_source_is_a_readable_label(dataset):
    client, a, job = dataset
    src = client.get("/api/health")[1]["last_run"]["source"]
    assert src == "generated: default seed 2025, 3 stores x 2 weeks"
    from flock import demo
    assert demo.source_label("ca_bagel_chain", 7, 1, 10) == "generated: ca_bagel_chain seed 7, 1 store x 10 days"


# the web UI (frontend/) served at /, the judge market, an agent trying to decide --------------------------------------
def test_ui_is_served_at_root_with_types_and_no_traversal(server):
    client, _ = server
    code, body, h = client.call("GET", "/")
    assert code == 200 and h["Content-Type"].startswith("text/html") and b"dashboard.js" in body
    for path, ctype in (("/dashboard.js", "text/javascript"), ("/frontend/dashboard.js", "text/javascript"),
                        ("/styles.css", "text/css"), ("/index.html", "text/html"), ("/frontend/", "text/html"),
                        ("/vendor/lucide-0.453.0.min.js", "text/javascript")):
        code, _, h = client.call("GET", path)
        assert code == 200 and h["Content-Type"].startswith(ctype), path
    for bad in ("/../flock/api.py", "/frontend/../flock/api.py", "/frontend/%2e%2e/flock/api.py", "/%2e%2e/README.md",
                "/vendor/..%2f..%2fflock%2fapi.py", "/.env", "/README.md", "/frontend/README.md", "/nope.html"):
        code, out, _ = client.call("GET", bad)
        assert code == 404 and isinstance(out, dict), bad
    assert api.static_file(["frontend", "..", "flock", "api.py"]) is None
    code, out = client.get("/api")
    assert code == 200 and out["ok"] and any("/api/market" in r for r in out["routes"])


def test_market_snapshot(server):
    client, _ = server
    fixtures_run(client)
    code, m = client.get("/api/market")
    assert code == 200 and {r["judge"] for r in m["leaderboard"]} == {"judge.strict", "judge.operator", "judge.skeptic"}
    assert {"balance", "weight", "won", "lost", "history"} <= set(m["leaderboard"][0]) and "rule" in m


def test_an_agent_cannot_approve_and_the_refusal_comes_back(server):
    client, a = server
    fixtures_run(client, demo_approve=False)
    fid = next(f["finding_id"] for f in a.findings() if f["state"] == "JUDGED")
    for body in ({"actor": "dispute_writer"}, {"actor": "someone", "role": "agent"}):
        code, out = client.post(f"/api/findings/{fid}/approve", body)
        assert code == 409 and out["denied"] is True, out
        e = out["log_entry"]
        assert (e["actor"], e["role"], e["action"], e["allowed"]) == (body["actor"], "agent", "approve", False)
    assert a.store.get(fid)["state"] == "JUDGED"
    code, out = client.post(f"/api/findings/{fid}/approve", {"actor": "maja", "reason": "checks out"})
    assert code == 200 and out["finding"]["state"] in ("APPROVED", "FILED")
