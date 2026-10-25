"""Generator -> Collector -> Formatter: deterministic, traceable, and cleaning never deletes a disagreement."""
import csv
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from flock.agents import collector, formatter
from flock.store import ROOT
from generator.generate import SOURCE_TRAPS, TRAPS, generate


def read(path):
    with Path(path).open(newline="") as fh:
        return list(csv.DictReader(fh))


def header(path):
    with Path(path).open(newline="") as fh:
        return next(csv.reader(fh))


def tree_bytes(d: Path) -> dict:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """One full run: 3 stores x 2 weeks, all 17 traps, collected and formatted into a temp folder."""
    base = tmp_path_factory.mktemp("flock")
    out, raw, std = base / "out", base / "raw", base / "std"
    maps = base / "mappings.json"
    shutil.copy(ROOT / "mappings.json", maps)
    w = generate(seed=11, stores=3, days=14, out=out)
    collector.collect(out, raw)
    formatter.format_all(raw, std, maps, confirm=True)
    return {"w": w, "out": out, "raw": raw, "std": std, "maps": maps, "key": read(out / "answer_key.csv")}


# generator ----------------------------------------------------------------------------------------------------

def test_same_seed_same_bytes(tmp_path):
    generate(seed=5, stores=2, days=8, out=tmp_path / "a")
    generate(seed=5, stores=2, days=8, out=tmp_path / "b")
    generate(seed=6, stores=2, days=8, out=tmp_path / "c")
    a, b, c = (tree_bytes(tmp_path / x) for x in "abc")
    assert a == b                      # includes grubhub_statement.xlsx
    assert a["pos_orders.csv"] != c["pos_orders.csv"]


def test_mini_milestone(tmp_path):
    w = generate(seed=42, stores=1, days=1, traps=[3, 7, 16], out=tmp_path)
    key = read(tmp_path / "answer_key.csv")
    assert {r["trap_type"] for r in key} == {"sold_never_paid", "commission_wrong_tier", "legit_error_charge"}
    assert {s["store_id"] for s in w["stores"]} == {"S01"}


def test_every_trap_planted_and_key_format(world):
    assert header(world["out"] / "answer_key.csv") == header(ROOT / "data" / "answer_key.csv")
    assert {r["trap_type"] for r in world["key"]} == {t for no, (t, _) in TRAPS.items() if no not in SOURCE_TRAPS}
    for r in world["key"]:
        assert r["expected_outcome"] in ("correct", "recoverable", "conflict")
        float(r["amount"])


def test_does_not_touch_demo_answer_key(world):
    assert read(ROOT / "data" / "answer_key.csv")[0]["order_id"] == "ORD-1001"


# collector ----------------------------------------------------------------------------------------------------

def test_collector_untouched_idempotent_and_blind_to_answers(world):
    raw, out = world["raw"], world["out"]
    assert collector.collect(out, raw) == []          # second run: nothing new
    rows = collector.read_manifest(raw)
    assert len(rows) == len({r["sha256"] for r in rows})
    assert not list(raw.rglob("answer_key.csv"))
    for r in rows:
        assert collector.sha256(raw / r["file"]) == r["sha256"]
    assert (raw / "bank" / "bank_deposits.csv").exists()
    assert (raw / "contracts" / "doordash_contract.json").exists()
    assert (raw / "grubhub" / "grubhub_statement.xlsx").read_bytes() == (out / "grubhub_statement.xlsx").read_bytes()


# formatter ----------------------------------------------------------------------------------------------------

def test_columns_match_contract(world):
    std = world["std"]
    assert header(std / "orders.csv") == formatter.ORDER_COLS
    assert header(std / "payout_lines.csv") == formatter.PAYOUT_COLS
    assert header(std / "bank_deposits.csv") == formatter.DEPOSIT_COLS
    contract = (ROOT / "CONTRACT.md").read_text()
    # payout lines: the CONTRACT.md columns first, in order, then the agreed extras
    contract_payout = contract.split("## Standard payout line")[1].split("`")[1]
    contract_payout = [c.split(" (")[0].strip() for c in contract_payout.split(",")]
    assert formatter.PAYOUT_COLS[:len(contract_payout)] == contract_payout
    assert formatter.PAYOUT_COLS[len(contract_payout):] == ["promo_total", "promo_code", "placed_at"]
    assert "order_id, store_id, channel, placed_at (ISO), subtotal, tax, promo_code, source_file, source_row" in contract
    assert "deposit_id, channel, amount, deposit_date, memo, source_file, source_row" in contract


def test_every_row_traceable(world):
    for name in ("orders", "payout_lines", "bank_deposits", "refunds", "error_charges"):
        rows = read(world["std"] / f"{name}.csv")
        assert rows, name
        for r in rows:
            assert r["source_file"] and int(r["source_row"]) >= 2, (name, r)
    # spot check: source_row really points at that row of the raw file
    r = read(world["std"] / "orders.csv")[5]
    raw_rows = list(csv.reader((world["raw"] / r["source_file"]).open()))   # temp raw dir: path is relative to it
    assert raw_rows[int(r["source_row"]) - 1][0] == r["order_id"]


def test_standardized_values(world):
    pl = read(world["std"] / "payout_lines.csv")
    assert {r["channel"] for r in pl} == {"doordash", "ubereats", "grubhub", "direct_app"}
    assert all(r["store_id"].startswith("S") and len(r["store_id"]) == 3 for r in pl)
    assert all(len(r["payout_date"]) == 10 and r["payout_date"][4] == "-" for r in pl)
    for r in pl:
        deductions = sum(float(r[c] or 0) for c in formatter.DEDUCTIONS)
        assert abs(float(r["gross"]) - deductions - float(r["net"])) < 0.005, r
    assert all("T" in r["placed_at"] for r in read(world["std"] / "orders.csv"))


def test_exact_duplicates_removed_but_disagreements_kept(world):
    key = {r["trap_type"]: r["order_id"] for r in world["key"]}
    pl = read(world["std"] / "payout_lines.csv")
    dup_lines = [r for r in pl if r["order_id"] == key["duplicate_rows"]]
    assert len(dup_lines) == 1
    dropped = read(world["std"] / "duplicates_removed.csv")
    assert len(dropped) == 1 and dropped[0]["table"] == "payout_lines"
    # trap 11: same error charged twice under two adjustment ids -> both lines must survive
    twice = [r for r in read(world["std"] / "error_charges.csv") if r["order_id"] == key["duplicate_error_charge"]]
    assert len(twice) == 2 and twice[0]["amount"] == twice[1]["amount"]


def test_deposits_reconcile_except_planted_traps(world):
    pl, dep = read(world["std"] / "payout_lines.csv"), read(world["std"] / "bank_deposits.csv")
    net = {}
    for r in pl:
        net[r["payout_id"]] = round(net.get(r["payout_id"], 0) + float(r["net"]), 2)
    traps = {r["order_id"]: r for r in world["key"] if r["trap_type"] in ("deposit_covers_two_weeks",
                                                                          "payout_deposit_mismatch")}
    assert len(traps) == 2 and all(k in net for k in traps)      # keyed by payout_id
    for d in dep:
        ids = [x.strip() for x in d["memo"].split("ID:")[1].split("+")]
        total = round(sum(net[i] for i in ids), 2)
        gap = round(total - float(d["amount"]), 2)
        hit = [traps[i] for i in ids if i in traps]
        if hit:
            assert gap == float(hit[0]["amount"])
            if hit[0]["trap_type"] == "deposit_covers_two_weeks":
                assert len(ids) == 2
        else:
            assert gap == 0, d


def test_unknown_layout_needs_a_human(tmp_path, world):
    maps = tmp_path / "mappings.json"
    shutil.copy(ROOT / "mappings.json", maps)
    formatter.format_all(world["raw"], tmp_path / "std", maps, confirm=False, interactive=False)
    assert not any(k.startswith("ubereats_layout") for k in formatter.load_mappings(maps))
    ue = [r for r in read(tmp_path / "std" / "payout_lines.csv") if r["channel"] == "ubereats"]
    full = [r for r in read(world["std"] / "payout_lines.csv") if r["channel"] == "ubereats"]
    assert 0 < len(ue) < len(full)                     # unseen week left out, not guessed
    saved = formatter.load_mappings(world["maps"])["ubereats_layout_1"]
    assert saved["confirmed_by"] == "human" and saved["table"] == "payout_lines"
    assert saved["columns"]["net_payout"] == "net" and saved["columns"]["pos_check_no"] == "order_id"


def test_what_the_checkers_read(world):
    """Formats agreed with matcher/checkers/deal_keeper: extras, sidecars, contract terms, email claims."""
    key = {r["trap_type"]: r["order_id"] for r in world["key"]}
    pl = read(world["std"] / "payout_lines.csv")
    assert all(r["payout_line_id"] for r in pl) and len({r["payout_line_id"] for r in pl}) == len(pl)
    bogo = next(r for r in pl if r["order_id"] == key["bogo_wrong_split"])
    assert float(bogo["promo_total"]) > 0 and bogo["promo_code"] and bogo["placed_at"]
    assert float(bogo["promo_merchant_funded"]) == float(bogo["promo_total"])      # charged 100%, contract says 50%
    assert any(r["platform_order_id"] == key["payout_without_pos_order"] for r in pl)
    assert header(world["std"] / "error_charges.csv") == formatter.ERROR_COLS
    late = next(r for r in read(world["std"] / "error_charges.csv") if r["order_id"] == key["error_charge_after_72h"])
    gap = datetime.fromisoformat(late["complaint_at"]) - datetime.fromisoformat(late["delivered_at"])
    assert gap.total_seconds() / 3600 > 72
    refund = read(world["std"] / "refunds.csv")[0]
    assert refund["order_id"] == key["refund_initiated_no_money"] and refund["status"] == "initiated"
    assert not refund["settled_at"]
    for f in (world["raw"] / "contracts").glob("*.json"):
        doc = json.loads(f.read_text())
        assert doc["doc_type"] in ("contract", "amendment") and doc["channel"]
        for t in doc["terms"]:
            assert {"term_type", "store_id", "value", "quote"} <= set(t)
    emails = [json.loads(f.read_text()) for f in (world["raw"] / "emails").glob("*.json")]
    assert sorted(e["sender_authorized"] for e in emails) == [False, True, True]
    assert all(e["kind"] == "email" and e["claims"] for e in emails)


def test_parse_money():
    assert formatter.parse_money("$12.50") == 12.5
    assert formatter.parse_money("($3.75)") == -3.75
    assert formatter.parse_money("-$3.75") == -3.75
    assert formatter.parse_money("1,234.56") == 1234.56
    assert formatter.parse_money("") is None
