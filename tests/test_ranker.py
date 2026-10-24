"""Ranker: spend the limited dispute budget on the best bets, never on losers."""
import pytest

from flock.agents import ranker


def rec(fid, channel="doordash", gap=10.0, check_type="commission", outcome="recoverable"):
    f = {"finding_id": fid, "channel": channel, "check_type": check_type, "gap": gap, "outcome": outcome}
    f["rank_score"] = ranker.score(f)
    return f


def test_score_is_gap_times_win_probability_minus_effort():
    assert ranker.score({"gap": 2.5, "check_type": "commission"}) == 1.25          # 2.5 * 0.9 - 1
    assert ranker.score({"gap": 8.4, "check_type": "error_charge"}) == 5.30         # 8.4 * 0.75 - 1
    assert ranker.score({"gap": 10, "check_type": "something_new"}) == 4.00         # unknown type: 0.5


def test_score_uses_the_size_of_the_gap():
    assert ranker.score({"gap": -10, "check_type": "promo"}) == ranker.score({"gap": 10, "check_type": "promo"})


def test_small_gaps_score_negative():
    assert ranker.score({"gap": 0.5, "check_type": "commission"}) < 0


def test_orders_by_score_best_first():
    fs = [rec("a", gap=3), rec("b", gap=50), rec("c", gap=12, check_type="deposit"), rec("d", gap=12)]
    assert [f["finding_id"] for f in ranker.within_budget(fs)] == ["b", "d", "c", "a"]


def test_respects_dispute_limit_per_channel(monkeypatch):
    monkeypatch.setattr(ranker, "DISPUTE_LIMIT", {"doordash": 2, "grubhub": 1, "ubereats": 5})
    fs = ([rec(f"dd{i}", "doordash", gap=10 + i) for i in range(5)]
          + [rec(f"gh{i}", "grubhub", gap=20 + i) for i in range(3)]
          + [rec("ue0", "ubereats", gap=4)])
    got = [f["finding_id"] for f in ranker.within_budget(fs)]
    assert got == ["gh2", "dd4", "dd3", "ue0"]  # top 2 doordash, top 1 grubhub, by score
    per = {ch: sum(f["channel"] == ch for f in ranker.within_budget(fs)) for ch in ("doordash", "grubhub")}
    assert per == {"doordash": 2, "grubhub": 1}


def test_limit_of_zero_files_nothing_on_that_channel(monkeypatch):
    monkeypatch.setattr(ranker, "DISPUTE_LIMIT", {**ranker.DISPUTE_LIMIT, "doordash": 0})
    assert ranker.within_budget([rec("a", "doordash", gap=100)]) == []


@pytest.mark.parametrize("gap", [0.5, 1.0, 1.11])
def test_negative_or_zero_score_items_are_not_filed(gap):
    f = rec("x", gap=gap)
    assert f["rank_score"] <= 0
    assert [x["finding_id"] for x in ranker.within_budget([f, rec("good", gap=10)])] == ["good"]


def test_unscored_items_are_not_filed():
    assert ranker.within_budget([{"finding_id": "x", "channel": "doordash", "outcome": "recoverable", "rank_score": None}]) == []


def test_unknown_channel_gets_no_budget():
    assert ranker.within_budget([rec("x", channel="newapp", gap=100)]) == []


@pytest.mark.parametrize("outcome", ["correct", "conflict", None])
def test_only_recoverable_findings_are_ranked_in(outcome):
    assert ranker.within_budget([rec("x", gap=100, outcome=outcome)]) == []


def test_does_not_mutate_input():
    fs = [rec("a", gap=3), rec("b", gap=50)]
    before = [dict(f) for f in fs]
    ranker.within_budget(fs)
    assert fs == before
