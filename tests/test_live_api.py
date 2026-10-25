"""Live demo endpoints: a fresh month on demand, the approvals inbox, the agent chain's statuses, benchmarks, the
session login that names the approver, filing screens and the outbox."""
import json

import pytest

from flock import api, showtime
from flock.agents import learner

from test_api import TOKEN, server, wait_job  # noqa: F401  (fixtures)


@pytest.fixture
def live(server, tmp_path, monkeypatch):  # noqa: F811
    client, a = server
    monkeypatch.setenv("FLOCK_OFFLINE", "1")
    monkeypatch.setattr(showtime, "WORKDIR", tmp_path / "showtime")
    monkeypatch.setattr(learner, "RULES", tmp_path / "rules.json")
    code, job = client.post("/api/run", {"dataset": "live", "stores": 5})
    assert code == 202, job
    j = wait_job(client, job["job_id"], timeout=240)
    assert j["status"] == "done", j.get("error")
    return client, a, j["result"]


def test_live_run_generates_a_fresh_month_and_waits_for_a_human(live):
    client, a, r = live
    assert r["seed"] and r["orders"] > 0 and r["findings"] > 0
    assert r["caught"] == r["total"] and r["false_alarms"] == 0
    assert r["shortfall"] > 0.02 * r["deposited"], "planted leakage is material (~2.5% of deposits)"
    rows = a.store.all()
    assert not [f for f in rows if f["state"] in ("APPROVED", "FILED")], "nothing is approved or filed for the human"
    code, inbox = client.get("/api/approvals")
    assert code == 200 and inbox["count"] == r["approvals"] > 0 and inbox["disputes"] >= 1
    amounts = [x["amount"] for x in inbox["items"]]
    assert amounts == sorted(amounts, reverse=True)
    top = inbox["items"][0]
    assert {"finding_id", "platform", "check_type", "amount", "payout_id", "verdict", "kind"} <= set(top)
    code, sheet = client.get("/api/live")
    assert code == 200 and sheet["json"]["seed"] == r["seed"] and sheet["json"]["live"] is True


def test_every_stage_reports_a_status(live):
    client, _, _ = live
    code, out = client.get("/api/agents")
    assert code == 200
    by = {x["actor"]: x for x in out["agents"]}
    for actor in ("collector", "formatter", "matcher", "deal_keeper", "checkers", "anomaly", "investigator", "panel",
                  "ranker", "dispute_writer", "clicky", "learner"):
        assert by[actor]["status"] == "done", (actor, by[actor])
        assert by[actor]["status_text"] and "idle" not in by[actor]["status_text"]
    assert "wait for a human" in by["clicky"]["status_text"]


def test_metrics_cards_read_cleanly(live):
    client, _, _ = live
    code, m = client.get("/api/metrics")
    cards = {c["id"]: c for c in m["cards"]}
    t = cards["agent_time"]["value"]
    assert t["agent_display"].endswith(("s", "min", "h")) and t["human_display"].startswith("~")
    assert "Lunchbox" in t["basis"] and "ASSUMPTION" not in json.dumps(m)
    assert cards["cost"]["value"]["usd"] == 0.0 and cards["cost"]["value"]["display"] == "$0.00"
    money = cards["money_saved"]["value"]
    assert money["monthly_usd"] > 0 and money["annual_display"].startswith("~$")


def test_benchmarks_come_from_the_gx10_results():
    b = api.Api.benchmarks(api.Api.__new__(api.Api))
    rows = {r["contestant"]: r for r in b["benchmark"]["rows"]}
    tuned = rows["Qwen2.5-7B, LoRA-tuned (flock-judge-v1)"]
    assert (tuned["caught"], tuned["false_alarms"], b["benchmark"]["total"]) == (916, 0, 916)
    assert b["speed"]["headline"] == {"mac_p50_s": 88.4, "gx10_p50_s": 26.9, "speedup": 3.3}
    assert b["training"]["model"] == "flock-judge-v1" and b["training"]["base"] == "Qwen/Qwen2.5-7B-Instruct"


auth = pytest.importorskip("flock.auth")


@pytest.fixture
def users(monkeypatch):
    rows = [{"username": "noahs-finance", "password_hash": auth.hash_password("pw-one", iterations=1000),
             "name": "Preet Karia", "role": "reviewer", "org": "Ringtide"},
            {"username": "viewer", "password_hash": auth.hash_password("pw-two", iterations=1000),
             "name": "Vi Ewer", "role": "viewer", "org": "Ringtide"}]
    monkeypatch.setenv("FLOCK_USERS", json.dumps(rows))
    auth._fails.clear()


def test_login_then_approve_names_the_session_user(server, users):  # noqa: F811
    client, a = server
    code, out = client.post("/api/run", {"reset": True, "demo_approve": False})
    assert code == 200
    fid = next(f["finding_id"] for f in a.store.all() if f["state"] == "JUDGED" and f.get("dispute_draft"))
    code, bad = client.post("/api/login", {"username": "noahs-finance", "password": "nope"}, auth=False)
    assert code == 401
    code, s = client.post("/api/login", {"username": "noahs-finance", "password": "pw-one"}, auth=False)
    assert code == 200 and s["user"]["name"] == "Preet Karia"
    hdr = {"Authorization": f"Bearer {s['token']}", "Content-Type": "application/json"}
    code, me, _ = client.call("GET", "/api/me", headers=hdr)
    assert code == 200 and me["user"]["username"] == "noahs-finance"
    code, r, _ = client.call("POST", f"/api/findings/{fid}/approve", {"actor": "someone-else", "reason": "ok"}, hdr)
    assert code == 200, r
    assert r["log_entry"]["actor"] == "Preet Karia" and r["log_entry"]["role"] == "human"
    assert r["finding"]["state"] == "FILED" and r["portal_ref"]
    if api.filing_screens is not None:
        assert r["screen"] == f"/api/filings/{fid}/screen"
        code, page, h = client.call("GET", r["screen"])
        assert code == 200 and h["Content-Type"].startswith("text/html") and r["portal_ref"].encode() in page
    code, v = client.post("/api/login", {"username": "viewer", "password": "pw-two"}, auth=False)
    vh = {"Authorization": f"Bearer {v['token']}", "Content-Type": "application/json"}
    other = next(f["finding_id"] for f in a.store.all() if f["state"] == "JUDGED" and f.get("outcome") == "conflict")
    code, _, _ = client.call("POST", f"/api/findings/{other}/reject", {"reason": "x"}, vh)
    assert code == 403
    code, _, _ = client.call("POST", "/api/logout", None, hdr)
    assert code == 200
    code, _, _ = client.call("GET", "/api/me", headers=hdr)
    assert code == 401


def test_outbox_and_missing_screen(server):  # noqa: F811
    client, _ = server
    code, out = client.get("/api/outbox")
    assert code == 200 and out["messages"] == []
    code, out = client.get("/api/filings/F-nope/screen")
    assert code == 404


def test_money_history_and_measured_seconds(live):
    client, a, r = live
    code, m = client.get("/api/metrics")
    money = m["money"]
    assert money["ytd"]["months"] == 1 and money["month"]["found"] > 0 and money["month"]["recovered"] == 0
    [row] = money["ytd"]["by_month"]
    assert row["agent_seconds"] > 0 and row["human_hours_est"] > row["agent_hours"] and row["live"] is True
    assert money["annualized_run_rate"] == round(money["ytd"]["found"] * 12, 2)
    code, s = client.get("/api/score")
    assert s["runs"][-1]["seconds"] > 0 and s["runs"][-1]["month"]


def test_short_deposit_is_caught_by_the_deposit_check(live):
    """The deposit reconciler finds the short Uber Eats deposit; offline the panel calls it recoverable (on the hosted
    CREAO run one judge's split sent the $71 version to a human: a judging call, not a missed detection)."""
    _, a, _ = live
    dep = [f for f in a.store.all() if f["check_type"] == "deposit" and f["channel"] == "ubereats"]
    assert dep and max(abs(f["gap"]) for f in dep) > 10_000 and all(f["outcome"] == "recoverable" for f in dep)


def test_evidence_hooks(server):  # noqa: F811
    client, _ = server
    if api.evidence is None:
        pytest.skip("no flock.evidence")
    code, out = client.get("/api/evidence/index")
    assert code == 200
    code, out = client.get("/api/evidence?ref=../../etc/passwd:1")
    assert code == 400 and "error" in out


def test_reads_serve_the_last_completed_run_while_a_job_runs(server):  # noqa: F811
    client, a = server
    assert client.post("/api/run", {"reset": True, "demo_approve": False})[0] == 200
    code, before = client.get("/api/approvals")
    a.snapshot, a.running = a.take_snapshot(), "JOB-test"
    try:
        a.store.reset()                                   # what a new run does first
        code, during = client.get("/api/approvals")
        assert during["count"] == before["count"] > 0 and during["running_job"] == "JOB-test"
        assert during["stale_while_running"] is True
        code, m = client.get("/api/metrics")
        assert m["running_job"] == "JOB-test" and m["cards"]
        code, rows = client.get("/api/findings")
        assert len(rows) == len(a.snapshot["findings"]) > 0
    finally:
        a.running = a.snapshot = None
    code, after = client.get("/api/approvals")
    assert after["count"] == 0 and after["running_job"] is None


def test_live_panels_finish_after_the_fast_ones(monkeypatch):
    from flock import pipeline
    from flock.agents import panel
    todo = [{"finding_id": f"F-{i}", "gap": 1.0} for i in range(5)]
    monkeypatch.setattr(panel, "decide", lambda f, rules=None, weights=None, live=True: {"reason": f["finding_id"],
                                                                                          "live": live})
    plan = {"live": True, "chosen": {"F-0", "F-3"}, "rule_ids": set(), "concurrency": 3}
    got = [f["finding_id"] for f, _ in pipeline.judge_all(todo, [], {}, plan)]
    assert got[:3] == ["F-1", "F-2", "F-4"] and set(got[3:]) == {"F-0", "F-3"}
