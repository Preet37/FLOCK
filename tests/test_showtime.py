"""The 3-minute demo: the demo_month scenario (incidents planted, the big error's dollars computed from the data),
per-lump-sum rollup, and python -m flock.showtime prepare / status / restore / reset at small scale in a temp dir."""
import csv
import hashlib
import json
from datetime import date

import pytest

from flock import rollup, showtime
from flock.agents import collector, learner
from flock.demo import generator
from flock.gatekeeper import transition
from flock.models import Actor, money
from flock.store import ROOT, JsonStore

SEED = 11


@pytest.fixture(scope="module")
def world():
    return generator.build_scenario(SEED, "demo_month", "2026-09", n_stores=6)


def test_demo_month_is_one_calendar_month_with_every_incident(world):
    sc = world["scenario"]
    assert (sc["period_start"], sc["period_end"]) == ("2026-09-01", "2026-09-30")
    assert {o["day"] for o in world["orders"]} == {date(2026, 9, d) for d in range(1, 31)}
    assert set(sc["orders_by_channel"]) == {"doordash", "ubereats", "grubhub", "direct_app"}
    assert sc["deposits"] == 20 and sc["payouts"] == 20              # 4 channels x 5 weekly lump sums
    planted = {i["incident"]: i for i in sc["incidents"]}
    assert set(planted) == {k for k, v in generator.DEMO_PLAN.items() if v}
    assert 5 <= len(planted) <= 10
    assert all(i["rows"] >= 1 for i in planted.values())
    assert {r["trap_no"] for r in world["key"]} == {3, 6, 7, 8, 10, 15, 16}
    assert planted["legit_error"]["expected_outcome"] == "correct" and planted["legit_error"]["amount"] == 0
    assert planted["ue_unauth"]["expected_outcome"] == "conflict"
    assert len({r["order_id"] for r in world["key"]}) == len(world["key"])   # one incident per order


def test_big_error_dollars_come_from_the_data(world):
    big = world["scenario"]["big_error"]
    dd = world["contracts"]["doordash"]
    assert (dd["commission"], dd["amendment"]["commission"]) == (0.25, 0.20)
    assert dd["amendment"]["effective_from"] == date(2026, 9, 1) and len(big["stores"]) == 3
    orders = [o for o in world["orders"] if o["channel"] == "doordash" and o["store_id"] in big["stores"]]
    assert len(orders) == big["orders"]
    recomputed = money(sum(money(o["subtotal"] * 0.25) - money(o["subtotal"] * 0.20) for o in orders))
    assert big["overcharge"] == recomputed > 0
    assert money(sum(big["by_payout"].values())) == big["overcharge"]
    key = money(sum(float(r["amount"]) for r in world["key"] if r["trap_no"] == 7))
    assert key == big["overcharge"]
    other = [o for o in world["orders"] if o["channel"] == "doordash" and o["store_id"] not in big["stores"]]
    assert all(o["actual"]["commission"] == o["truth"]["commission"] for o in other)


def test_lump_sums_add_up(world):
    sc = world["scenario"]
    assert money(sum(x["deposited"] for x in sc["lump_sums"])) == sc["deposited"]
    assert money(sum(x["shortfall"] for x in sc["lump_sums"])) == sc["shortfall"]
    owed = money(sum(i["amount"] for i in sc["incidents"] if i["expected_outcome"] == "recoverable"))
    assert sc["shortfall"] == owed         # rep promises without authority are not money owed
    never_paid = next(i for i in sc["incidents"] if i["incident"] == "sold_never_paid")
    assert money(sum(x["unpaid_net"] for x in sc["lump_sums"])) == never_paid["amount"]


def test_deterministic_per_seed_and_files(tmp_path, world):
    again = generator.build_scenario(SEED, "demo_month", "2026-09", n_stores=6)
    assert again["key"] == world["key"] and again["scenario"] == world["scenario"]
    other = generator.build_scenario(SEED + 1, "demo_month", "2026-09", n_stores=6)
    assert other["key"] != world["key"]
    a, b = tmp_path / "a", tmp_path / "b"
    generator.generate(SEED, 4, out=a, scenario="demo_month", month="2026-09")
    generator.generate(SEED, 4, out=b, scenario="demo_month", month="2026-09")
    for p in sorted(a.rglob("*")):
        if p.is_file():
            assert p.read_bytes() == (b / p.relative_to(a)).read_bytes(), p.name
    assert json.loads((a / "scenario.json").read_text())["big_error"]["overcharge"] > 0
    collector.collect(a, tmp_path / "raw")
    assert not list((tmp_path / "raw").rglob("scenario.json"))        # the grader's copy is never collected
    assert not list((tmp_path / "raw").rglob("answer_key.csv"))


def test_prior_months_have_no_amendment_and_a_learning_schedule():
    w = generator.build_scenario(SEED, "prior_month", "2026-06", n_stores=6, plan=0)
    assert "amendment" not in w["contracts"]["doordash"] and w["scenario"]["big_error"] is None
    assert {r["trap_no"] for r in w["key"]} >= {4, 13, 15}
    repeats = [max((k for k, v in p.items() if k in ("gh_unauth", "no_pos", "refund_stuck") and v >= 2), default=None)
               for p in generator.PRIOR_PLANS]
    assert repeats[:3] == ["gh_unauth", "no_pos", "refund_stuck"]   # a new pattern repeats (and is learned) each month
    assert all(p["ue_unauth"] == 1 for p in generator.PRIOR_PLANS)   # this one never repeats: always a human's call
    assert generator.shift_month("2026-01", -1) == "2025-12" and generator.month_span("2024-02")[1] == 29


def _f(oid, gap, quote="20% of the food subtotal", check="commission", fid=None):
    return {"finding_id": fid or f"F-{oid}", "order_ids": [oid], "channel": "doordash", "check_type": check,
            "expected": 1.0, "actual": 1.0 + gap, "gap": gap, "reason": "r", "state": "NEW",
            "evidence": [{"source_file": "doordash/p.csv", "row": int(oid[1:]), "what": "x"},
                         {"source_file": "contracts/a.json", "quote": quote, "authority": "amendment"}]}


def test_rollup_groups_one_term_per_payout():
    index = {"O1": {"payout_id": "P1", "store_id": "S01", "gross": 10.0},
             "O2": {"payout_id": "P1", "store_id": "S02", "gross": 20.0},
             "O3": {"payout_id": "P2", "store_id": "S01", "gross": 10.0},
             "O4": {"payout_id": "P1", "store_id": "S01", "gross": 10.0},
             "O5": {"payout_id": "P1", "store_id": "S01", "gross": 10.0}}
    found = [_f("O1", 0.5), _f("O2", 1.0), _f("O3", 0.5), _f("O4", 0.25, quote="another deal"),
             _f("O5", 3.0, check="error_charge")]
    out = rollup.rollup(found, index)
    assert len(out) == 4
    g = out[0]
    assert g["order_ids"] == ["O1", "O2"] and g["gap"] == 1.5 and g["actual"] == money(1.5 + 2.0)
    assert g["rollup"]["payout_id"] == "P1" and g["rollup"]["stores"] == ["S01", "S02"]
    assert money(sum(g["rollup"]["parts"].values())) == g["gap"]
    assert g["evidence"][0]["rows"] == [1, 2] and g["evidence"][-1]["authority"] == "amendment"
    assert [f["finding_id"] for f in out[1:]] == ["F-O3", "F-O4", "F-O5"]   # other payout, other claim, error charge


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    """One small prepare (5 stores, 2 prior months) shared by the showtime tests."""
    mp = pytest.MonkeyPatch()
    base = tmp_path_factory.mktemp("show")
    mp.setenv("FLOCK_OFFLINE", "1")
    mp.setattr(learner, "RULES", base / "rules.json")
    before = {p: _digest(ROOT / p) for p in ("data/answer_key.csv", "mappings.json")}
    store = JsonStore(base / "state")
    kw = dict(store=store, rules_path=base / "rules.json", portal=base / "portal.jsonl", workdir=base / "work")
    info = showtime.prepare(months=2, seed=7, stores=5, quiet=True, **kw)
    yield {"info": info, "store": store, "kw": kw, "base": base, "before": before}
    mp.undo()


def test_prepare_leaves_the_headline_for_the_presenter(prepared):
    info, store = prepared["info"], prepared["store"]
    h = info["headline"]
    f = store.get(h["finding_id"])
    assert f["state"] == "JUDGED" and f["outcome"] == "recoverable" and f["dispute_draft"]
    assert f["check_type"] == "commission" and f["channel"] == "doordash" and f.get("rollup")
    others = [x for x in store.all() if x["outcome"] == "recoverable" and x["finding_id"] != h["finding_id"]
              and x.get("dispute_draft")]
    assert others and all(x["state"] == "FILED" for x in others)
    assert all(x["state"] == "JUDGED" for x in store.all() if x["outcome"] == "conflict")   # a human's call, live
    lump = info["lump_sum"]
    assert lump["payout_id"] == f["rollup"]["payout_id"] and lump["arrived"] > 0
    assert lump["short"] >= f["gap"] and lump["expected"] == money(lump["arrived"] + lump["short"])
    assert f["finding_id"] in {x["finding_id"] for x in lump["findings"]}
    key = lump["answer_key"]
    assert key["deposited"] == lump["arrived"]
    assert lump["short"] == money(key["shortfall"] - key["unpaid_net"])     # Flock's lump sum = the answer key's


def test_prepare_shows_a_learning_curve(prepared):
    info, store, base = prepared["info"], prepared["store"], prepared["base"]
    hand_offs = [m["to_human"] for m in info["months"]]
    assert len(hand_offs) == 3 and hand_offs[0] > hand_offs[-1] >= 1
    assert all(a >= b for a, b in zip(hand_offs, hand_offs[1:]))
    assert all(m["caught"] == m["total"] and m["false_alarms"] == 0 for m in info["months"])
    runs = [json.loads(x) for x in (base / "state" / "runs.jsonl").read_text().splitlines()]
    assert [r["to_human"] for r in runs] == hand_offs
    from flock.demo import scoreboard
    assert [r["label"] for r in scoreboard.history(base / "state" / "runs.jsonl")] == [m["name"] for m in info["months"]]
    assert len(learner.approved_rules(base / "rules.json")) >= 2
    from flock import market
    assert any(b != market.START for b in market.balances(store).values())


def test_prepare_leaves_an_unpaid_claim_with_a_follow_up_draft(prepared):
    info, store, base = prepared["info"], prepared["store"], prepared["base"]
    fu = info["follow_up"]
    f = store.get(fu["finding_id"])
    assert f["state"] == "FILED" and f["follow_up"]["state"] == "DRAFT"
    assert f["follow_up"]["claim_ref"] == f["portal_ref"] and f"We filed claim {f['portal_ref']}" in f["follow_up"]["draft"]
    portal = [json.loads(x) for x in (base / "portal.jsonl").read_text().splitlines()]
    assert f["portal_ref"] in {r["portal_ref"] for r in portal}
    assert any("Carried over from the" in e["reason"] for e in store.log(f["finding_id"]))
    assert f["order_ids"][0] not in {r["order_id"] for r in csv.DictReader((base / "state" / "answer_key.csv").open())}


def test_cheat_sheet_and_committed_files(prepared):
    info, base = prepared["info"], prepared["base"]
    md = (base / "state" / "showtime.md").read_text()
    assert info["headline"]["finding_id"] in md and info["lump_sum"]["payout_id"] in md
    assert "Exact clicks" in md and "Human queue" in md and info["follow_up"]["claim_ref"] in md
    assert json.loads((base / "state" / "showtime.json").read_text())["headline"] == info["headline"]
    assert {p: _digest(ROOT / p) for p in prepared["before"]} == prepared["before"]


def test_status_restore_reset(prepared):
    store, kw, info = prepared["store"], prepared["kw"], prepared["info"]
    hid = info["headline"]["finding_id"]
    assert "ready for the presenter" in showtime.status(store, kw["rules_path"])
    transition(store, hid, "approve", Actor("rehearsal", "human"), reason="rehearsal")
    assert "already decided" in showtime.status(store, kw["rules_path"])
    showtime.restore(store, kw["rules_path"], kw["portal"], kw["workdir"])
    assert store.get(hid)["state"] == "JUDGED"
    assert "ready for the presenter" in showtime.status(store, kw["rules_path"])
    r = showtime.approve_headline("presenter", store, kw["portal"])            # the terminal fallback
    assert r["state"] == "FILED" and r["portal_ref"] in r["filed"]
    assert [e["actor"] for e in store.log(hid) if e["action"] == "approve"] == ["presenter"]
    showtime.restore(store, kw["rules_path"], kw["portal"], kw["workdir"])
    showtime.reset(store, kw["rules_path"], kw["portal"], quiet=True)
    assert "No showtime loaded" in showtime.status(store, kw["rules_path"])
    assert len(store.all()) == 6 and not kw["rules_path"].exists()              # the Quickstart's fixtures, week 1
    assert sum(1 for f in store.all() if f["outcome"] == "conflict") == 2
    showtime.restore(store, kw["rules_path"], kw["portal"], kw["workdir"])     # and the show comes back
    assert store.get(hid)["state"] == "JUDGED"


def test_prepare_with_live_judges_respects_the_budget(tmp_path, monkeypatch):
    """With a live judge configured, the pipeline's FLOCK_LIVE_JUDGE_BUDGET picks which findings reach it; the rest use
    offline rules. Here 'live' is simulated (no network in tests); the budget and the cheat sheet must still hold."""
    from flock import pipeline
    monkeypatch.setattr(pipeline, "live_judges", lambda: True)
    monkeypatch.setenv("FLOCK_LIVE_JUDGE_BUDGET", "2")
    store = JsonStore(tmp_path / "state")
    info = showtime.prepare(months=1, seed=3, stores=4, store=store, rules_path=tmp_path / "rules.json",
                            portal=tmp_path / "portal.jsonl", workdir=tmp_path / "work", quiet=True)
    for m in info["months"]:
        assert m["judging"]["live"] and m["judging"]["budget"] == 2 and m["judging"]["used"] <= 2
    assert any(f.get("live_budget") == "outside" for f in store.all())   # outside the budget, and marked so
    assert info["headline"] and store.get(info["headline"]["finding_id"])["state"] == "JUDGED"
    assert ", live judges on" in showtime.judging(info["months"][-1])
