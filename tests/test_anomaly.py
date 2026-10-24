"""Anomaly Detector: learns a store x channel's normal fee line from earlier weeks and flags a later jump no rule covers."""
import csv
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flock.agents import anomaly, collector, deal_keeper, formatter, matcher, panel  # noqa: E402
from flock.models import money  # noqa: E402
from generator.generate import TRAPS, generate  # noqa: E402

START = datetime(2026, 8, 3, 9, 0)


def story(i: int, day: int, gross: float, fee: float, store="S01", channel="direct_app", commission=0.0, lag_days=8):
    placed = START + timedelta(days=day, minutes=i)
    return {"kind": "order", "order_id": f"ORD-{i}", "store_id": store, "channel": channel,
            "placed_at": placed.isoformat(),
            "payout_lines": [{"gross": gross, "other_fees": fee, "commission": commission,
                              "payout_date": (placed + timedelta(days=lag_days)).date().isoformat(),
                              "source_file": "data/raw/direct_app/direct_app_settlements.csv", "source_row": i + 2}]}


def processor(gross: float, pct=0.029, fixed=0.30) -> float:
    return money(gross * pct + fixed)


def weeks(n_weeks: int, per_day: int = 4, creep_week: int | None = None, bump_pct=0.01, bump_fixed=0.10):
    out, i = [], 0
    for d in range(7 * n_weeks):
        for _ in range(per_day):
            i += 1
            gross = money(12 + (i * 7) % 23)
            fee = processor(gross)
            if creep_week is not None and d // 7 == creep_week:
                fee = money(fee + gross * bump_pct + bump_fixed)
            out.append(story(i, d, gross, fee))
    return out


no_terms = lambda s: {}


def test_fee_creep_in_week_two_is_one_finding_with_the_right_dollars():
    stories = weeks(2, creep_week=1)
    found = anomaly.detect(stories, no_terms)
    assert len(found) == 1
    f = found[0]
    assert f.check_type == "anomaly" and f.channel == "direct_app"
    week2 = {s["order_id"] for s in stories if datetime.fromisoformat(s["placed_at"]) >= START + timedelta(days=7)}
    assert set(f.order_ids) == week2, "every week-2 order carries the jump"
    extra = sum(money(s["payout_lines"][0]["gross"] * 0.01 + 0.10) for s in stories if s["order_id"] in week2)
    assert f.gap == pytest.approx(extra, abs=0.05 * len(week2))
    assert f.gap > 0 and f.actual > f.expected
    assert len(f.evidence) >= 2 and all("source_file" in e and "row" in e for e in f.evidence)
    assert "no rule caught it" in f.reason.lower() or "no contract term" in f.reason.lower()


def test_steady_fees_raise_nothing():
    assert anomaly.detect(weeks(3), no_terms) == []


def test_a_deal_term_hands_the_metric_to_the_checkers():
    stories = weeks(2, creep_week=1)
    with_fee_term = lambda s: {"fee": {"value": 0.0, "authority": "contract", "quote": "x", "source_file": "c.json"}}
    assert anomaly.detect(stories, with_fee_term) == [], "a fee term exists: check_fee owns it, no duplicate finding"


def test_needs_history_before_it_judges():
    assert anomaly.detect(weeks(1, creep_week=0), no_terms) == [], "nothing to compare the first week against"


def test_flagged_week_stays_out_of_the_baseline():
    stories = weeks(3, creep_week=1) + []
    # week 3 keeps the higher rate: it must be flagged again, against the week-1 baseline
    for s in stories:
        if datetime.fromisoformat(s["placed_at"]) >= START + timedelta(days=14):
            g = s["payout_lines"][0]["gross"]
            s["payout_lines"][0]["other_fees"] = money(processor(g) + g * 0.01 + 0.10)
    found = anomaly.detect(stories, no_terms)
    assert len(found) == 2 and all(f.gap > 0 for f in found)


def test_panel_offline_calls_the_jump_recoverable(monkeypatch):
    monkeypatch.setenv("FLOCK_OFFLINE", "1")
    f = anomaly.detect(weeks(2, creep_week=1), no_terms)[0].to_dict()
    patch = panel.decide(f, rules=[], weights=None)
    assert patch["outcome"] == "recoverable", patch["reason"]


# ---- through the generator: trap 18 is planted, formatted, matched and caught; nothing else trips the detector ------
@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    base = tmp_path_factory.mktemp("anomaly_gen")
    out, raw, std = base / "out", base / "raw", base / "std"
    mappings = base / "mappings.json"
    shutil.copy(ROOT / "mappings.json", mappings)
    world = generate(seed=11, stores=3, days=14, out=out, profile="default")
    collector.collect(out, raw)
    tables = formatter.format_all(raw, std, mappings, confirm=True, interactive=False)
    stories, _ = matcher.match(tables)
    terms = deal_keeper.TermIndex(deal_keeper.extract_terms(raw / "contracts", raw / "emails"))
    return world, stories, anomaly.detect(stories, terms)


def test_trap_18_exists_and_is_caught(generated):
    world, stories, found = generated
    assert TRAPS[18] == ("processing_fee_creep", "recoverable")
    planted = {r["order_id"] for r in world["key"] if r["trap_no"] == 18}
    assert planted, "the generator planted processing fee creep"
    flagged = {o for f in found for o in f.order_ids}
    assert planted <= flagged, f"missed {sorted(planted - flagged)[:5]}"
    extra = sum(float(r["amount"]) for r in world["key"] if r["trap_no"] == 18)
    assert sum(f.gap for f in found if f.channel == "direct_app") == pytest.approx(extra, abs=0.05 * len(planted))


def test_no_other_trap_trips_the_detector(generated):
    world, stories, found = generated
    planted = {r["order_id"] for r in world["key"] if r["trap_no"] == 18}
    innocent = {o for f in found for o in f.order_ids} - planted
    assert not innocent, f"flagged orders that were not creep: {sorted(innocent)[:5]}"
    assert {f.channel for f in found} == {"direct_app"}
