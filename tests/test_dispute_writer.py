"""Dispute Writer offline template: every evidence item cited, no em dashes, no internal reasoning leaked."""
import pytest

from conftest import ADVERSARIAL, FAKE, fake
from flock.agents import dispute_writer

ALL_IDS = [f["finding_id"] for f in FAKE + ADVERSARIAL]


@pytest.mark.parametrize("fid", ALL_IDS)
def test_offline_draft_cites_every_evidence_item(fid):
    f = fake(fid)
    text = dispute_writer.draft(f)
    for e in f["evidence"]:
        assert e["source_file"] in text
        if e.get("quote"):
            assert e["quote"] in text
        if e.get("row") is not None:
            assert f"row {e['row']}" in text


@pytest.mark.parametrize("fid", ALL_IDS)
def test_offline_draft_has_no_em_dashes(fid):
    assert "—" not in dispute_writer.draft(fake(fid))


def test_em_dashes_in_evidence_are_removed():
    f = fake("F-0001")
    f["evidence"][1]["quote"] = "Marketplace commission — 20% of subtotal"
    f["order_ids"] = ["ORD—1001"]
    text = dispute_writer.draft(f)
    assert "—" not in text and "Marketplace commission, 20% of subtotal" in text


def test_offline_draft_states_the_numbers():
    text = dispute_writer.draft(fake("F-0006"))
    assert "Grubhub" in text and "ORD-1006" in text
    assert "$7.00" in text and "$14.00" in text


def test_negative_gap_is_stated_as_a_positive_difference():
    text = dispute_writer.draft({**fake("F-0001"), "gap": -2.5})
    assert "difference of $2.50" in text


def test_panel_reasoning_never_reaches_the_platform():
    f = {**fake("F-0001"), "reason": "All 3 judges agree (83% avg confidence). judge.skeptic: recoverable (70%)"}
    text = dispute_writer.draft(f)
    assert "judge" not in text.lower() and "confidence" not in text


def test_evidence_without_row_or_quote_still_cited():
    f = {**fake("F-0001"), "evidence": [{"source_file": "data/raw/bank/oct.csv"},
                                        {"source_file": "data/raw/bank/nov.csv", "what": "deposit 120.00"}]}
    text = dispute_writer.draft(f)
    assert "1. data/raw/bank/oct.csv" in text and "2. data/raw/bank/nov.csv (deposit 120.00)" in text
    assert "None" not in text


def test_unknown_channel_uses_the_channel_name():
    assert dispute_writer.draft({**fake("F-0001"), "channel": "newapp"}).startswith("Hello newapp merchant support")
