"""Matcher + Deal Keeper + Checkers on hand-written CONTRACT.md tables (tests/fixtures/std_*.csv, raw/).

One row per planted trap: the right finding (check_type, gap) and, through the offline panel, the right outcome.
Doesn't depend on the generator.
"""
import shutil
from pathlib import Path

import pytest

from flock.agents import checkers, deal_keeper, learner, matcher, panel
from flock.gatekeeper import transition
from flock.models import Actor
from flock.store import JsonStore

FIX = Path(__file__).resolve().parent / "fixtures"
TABLES = {n: FIX / f"std_{n}.csv" for n in ("orders", "payout_lines", "bank_deposits", "error_charges", "refunds")}

# key (order_id, or platform/payout id), trap, check_type, gap, outcome after the offline panel
TRAPS = [
    ("ORD-2010", "3 sold, never paid", "missing_order", 18.75, "recoverable"),
    ("DD-X99", "4 payout with no POS order", "missing_order", 3.00, "conflict"),
    ("DD-PAY-W1", "5 one deposit covers two weeks", "deposit", 0.00, "correct"),
    ("GH-PAY-W1", "6 statement total != deposit", "deposit", 5.00, "recoverable"),
    ("ORD-2001", "7 commission wrong tier", "commission", 1.00, "recoverable"),
    ("ORD-2007", "8 BOGO charged 100%, deal says 50/50", "promo", 7.00, "recoverable"),
    ("ORD-2006", "9 promo outside its date range", "promo", 4.00, "recoverable"),
    ("ORD-2002", "10 error charge reported 72h+ after delivery", "error_charge", 8.40, "recoverable"),
    ("ORD-2003", "11 same error charge twice", "error_charge", 6.00, "recoverable"),
    ("ORD-2005", "12 tax deducted though platform collected it", "tax", 3.50, "recoverable"),
    ("ORD-2011", "13 refund initiated, no money moved", "error_charge", 10.00, "conflict"),
    ("ORD-2008", "14 authorized rep waived the fee", "fee", 1.50, "recoverable"),
    ("ORD-2009", "15 unauthorized rep waived the fee", "fee", 2.00, "conflict"),
    ("ORD-2004", "16 legit error charge", "error_charge", 0.00, "correct"),
]
NO_FINDING = ["ORD-2012",   # 17 penny rounding: 4.01 vs 4.00 commission
              "ORD-2013",   # fallback-matched, everything correct
              "ORD-2014",   # clean order
              "ORD-2015"]   # in-store order: never expected in a payout


@pytest.fixture(scope="module")
def run():
    stories, found = matcher.match(TABLES)
    terms = deal_keeper.extract_terms(FIX / "raw" / "contracts", FIX / "raw" / "emails")
    found += checkers.run_all(stories, lambda s: deal_keeper.terms_for(terms, s))
    return stories, terms, found


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOCK_OFFLINE", "1")
    monkeypatch.setattr(learner, "RULES", tmp_path / "no_rules.json")  # day-one panel, no learned rules


def by_key(found):
    out = {}
    for f in found:
        for k in f.order_ids:
            out.setdefault(k, []).append(f)
    return out


@pytest.mark.parametrize("key,trap,check_type,gap,outcome", TRAPS, ids=[t[1] for t in TRAPS])
def test_trap(run, key, trap, check_type, gap, outcome):
    hits = by_key(run[2]).get(key, [])
    assert len(hits) == 1, f"{trap}: expected one finding for {key}, got {[(f.check_type, f.gap) for f in hits]}"
    f = hits[0]
    assert f.check_type == check_type
    assert f.gap == pytest.approx(gap)
    assert f.state == "NEW"
    assert f.evidence and all(e.get("source_file") for e in f.evidence)
    assert any("row" in e for e in f.evidence) or check_type == "missing_order"
    assert panel.decide(f.to_dict())["outcome"] == outcome


def test_no_false_alarms(run):
    found = by_key(run[2])
    for key in NO_FINDING:
        assert key not in found, f"{key} should not produce a finding: {found.get(key)}"
    disputed = [f for f in run[2] if abs(f.gap) > checkers.TOLERANCE]
    assert len(disputed) == len([t for t in TRAPS if t[3] > 0])


def test_exact_and_fallback_matching(run):
    stories = {s["order_id"]: s for s in run[0] if s["kind"] == "order"}
    assert stories["ORD-2001"]["match_confidence"] == 1.0
    fb = stories["ORD-2013"]
    assert fb["payout_lines"][0]["platform_order_id"] == "DD-7713"
    assert 0.5 <= fb["match_confidence"] < 1.0
    assert stories["ORD-2001"]["deposit_id"] == "DEP-1"
    assert stories["ORD-2014"]["deposit_id"] == "DEP-1"  # week 2 order, same lump deposit (edge case 5)
    assert len(stories["ORD-2003"]["error_charges"]) == 2  # second row linked by platform_order_id


def test_deposit_matching(run):
    deps = {s["deposit_id"]: s for s in run[0] if s["kind"] == "deposit"}
    assert deps["DEP-1"]["payout_ids"] == ["DD-PAY-W1", "DD-PAY-W2"]
    assert deps["DEP-4"]["match_confidence"] == 1.0  # memo names the payout
    assert deps["DEP-2"]["statement_total"] == 22.10 and deps["DEP-2"]["deposit_amount"] == 17.10


def test_payout_never_deposited():
    lines = [{"payout_line_id": "L1", "channel": "doordash", "store_id": "S01", "platform_order_id": "D1",
              "order_id": "O1", "gross": 10, "commission": 2, "net": 8, "payout_id": "P9", "payout_date": "2026-10-09",
              "source_file": "x.csv", "source_row": 2}]
    stories = matcher.reconcile_deposits(matcher.load_tables({"payout_lines": lines})["payout_lines"], [])
    f = checkers.check_deposit(stories[0])
    assert f.order_ids == ["P9"] and f.expected == 8.0 and f.actual == 0.0 and f.gap == 8.0


# deal keeper ----------------------------------------------------------------
def test_amendment_supersedes_contract(run):
    t = deal_keeper.terms_for(run[1], {"channel": "grubhub", "store_id": "S01", "placed_at": "2026-10-05T13:00:00",
                                       "pos": {"promo_code": "BOGO"}, "payout_lines": []})
    assert t["promo_split"]["value"] == 0.5 and t["promo_split"]["authority"] == "amendment"
    assert t["commission"]["value"] == 0.15 and t["fee"]["value"] == 1.5  # "15%" and "$1.50" parsed


def test_unauthorized_email_is_surfaced_not_applied(run):
    winner, conflicts = deal_keeper.effective(run[1], "ubereats", "S01", "fee", "2026-10-06")
    assert winner["authority"] == "contract" and winner["value"] == 2.0
    assert [c["authority"] for c in conflicts] == ["unauth_email"]
    assert conflicts[0]["quote"].startswith("No worries")
    assert deal_keeper.find_conflicts(run[1])[0]["key"]["term_type"] == "fee"
    # outside the email's window there is nothing to argue about
    assert deal_keeper.effective(run[1], "ubereats", "S01", "fee", "2026-11-06")[1] == []


def test_authorized_email_store_specific(run):
    w, _ = deal_keeper.effective(run[1], "grubhub", "S02", "fee", "2026-10-06")
    assert w["authority"] == "auth_email" and w["value"] == 0
    w, _ = deal_keeper.effective(run[1], "grubhub", "S01", "fee", "2026-10-06")
    assert w["authority"] == "contract" and w["value"] == 1.5


def _claim(value, authority, src="a.json", start="2026-01-01"):
    return {"term_id": src, "channel": "doordash", "store_id": "all", "term_type": "commission", "value": value,
            "effective_from": start, "effective_to": None, "authority": authority, "quote": f"{value}", "source_file": src}


def test_equal_authority_conflict_goes_to_human():
    terms = [_claim(0.20, "contract", "a.json"), _claim(0.25, "contract", "b.json")]
    winner, conflicts = deal_keeper.effective(terms, "doordash", "S01", "commission", "2026-10-01")
    assert winner is None and len(conflicts) == 2
    story = {"kind": "order", "order_id": "O1", "store_id": "S01", "channel": "doordash", "placed_at": "2026-10-01",
             "payout_lines": [{"gross": 20.0, "commission": 6.0, "source_file": "p.csv", "source_row": 3}]}
    f = checkers.check_commission(story, deal_keeper.terms_for(terms, story))
    assert "equal or unknown authority" in f.reason
    assert any(e.get("conflict") for e in f.evidence)


def test_unknown_authority_is_a_conflict():
    terms = [_claim(0.20, None, "a.json")]
    assert deal_keeper.effective(terms, "doordash", "S01", "commission", "2026-10-01") == (None, terms)


def test_unreadable_document_goes_to_model_hook(tmp_path, monkeypatch):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "emails").mkdir()
    (tmp_path / "contracts" / "scan.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "emails" / "note.txt").write_text("hey, call me about the fee\n")
    claims, unread = deal_keeper.load_claims(tmp_path / "contracts", tmp_path / "emails")
    assert claims == [] and len(unread) == 2

    seen = []
    def fake_reader(path, text):
        seen.append(path.name)
        return [{"term_type": "commission", "value": 0.2, "authority": "contract", "quote": "20%"}] if path.suffix == ".pdf" else []
    monkeypatch.setattr(deal_keeper, "DOCUMENT_READER", fake_reader)
    claims, unread = deal_keeper.load_claims(tmp_path / "contracts", tmp_path / "emails")
    assert sorted(seen) == ["note.txt", "scan.pdf"]
    assert claims[0]["authority"] == "contract" and claims[0]["source_file"].endswith("scan.pdf")
    assert len(unread) == 1


def test_contract_terms_have_contract_shape(run):
    for t in run[1]:
        assert set(deal_keeper.TERM_FIELDS) <= set(t)


# checkers in isolation ---------------------------------------------------------
def _story(**line):
    base = {"payout_line_id": "L1", "gross": 20.0, "commission": 4.0, "promo_merchant_funded": 0.0, "error_charge": 0.0,
            "tax_withheld_by_platform": 0.0, "other_fees": 0.0, "net": 16.0, "source_file": "p.csv", "source_row": 2}
    return {"kind": "order", "order_id": "O1", "store_id": "S01", "channel": "doordash",
            "placed_at": "2026-10-03T12:00:00", "pos": {"tax": 1.8, "source_file": "pos.csv", "source_row": 5},
            "payout_lines": [{**base, **line}], "error_charges": [], "refunds": []}


def test_duplicate_error_charge_from_payout_lines_only():
    s = _story(error_charge=5.0)
    s["payout_lines"].append({**s["payout_lines"][0], "payout_line_id": "L2", "gross": 0.0, "commission": 0.0,
                              "source_row": 9})
    f = checkers.check_error_charge(s, {})
    assert (f.expected, f.actual, f.gap) == (5.0, 10.0, 5.0)


def test_error_charge_just_under_72h_is_fair():
    s = _story(error_charge=4.0)
    s["error_charges"] = [{"amount": 4.0, "reason": "missing item", "delivered_at": "2026-10-03T12:00:00",
                           "reported_at": "2026-10-06T11:59:00", "source_file": "e.csv", "source_row": 2}]
    assert checkers.check_error_charge(s, {}).gap == 0.0


def test_completed_refund_is_not_flagged():
    s = _story(error_charge=10.0)
    s["refunds"] = [{"refund_id": "R1", "amount": 10.0, "status": "completed", "money_moved": 10.0,
                     "source_file": "r.csv", "source_row": 2}]
    assert checkers.check_refund(s, {}) is None
    assert checkers.check_error_charge(s, {}).gap == 0.0


def test_tax_rule_merchant_remits_is_fine():
    term = {"value": "merchant_remits", "source_file": "c.json", "quote": "q", "authority": "contract", "conflicts": []}
    assert checkers.check_tax(_story(tax_withheld_by_platform=1.8), {"tax_rule": term}) is None


def test_promo_without_full_cost_is_skipped():
    term = {"value": 0.5, "source_file": "c.json", "quote": "q", "authority": "contract", "conflicts": []}
    s = _story(promo_merchant_funded=6.0)
    assert checkers.check_promo(s, {"promo_code": "BOGO", "promo_split": term, "promo_windows": [term]}) is None


def test_penny_rounding_ignored_on_every_money_check():
    term = {"value": 1.0, "source_file": "c.json", "quote": "q", "authority": "contract", "conflicts": []}
    s = _story(commission=4.04, other_fees=1.03)
    assert checkers.check_commission(s, {"commission": {**term, "value": 0.2}}) is None
    assert checkers.check_fee(s, {"fee": term}) is None
    dep = {"kind": "deposit", "channel": "doordash", "payout_ids": ["P1"], "lines": [], "statement_total": 100.00,
           "deposit_amount": 99.97, "deposit": None}
    assert checkers.check_deposit(dep) is None


# wired through the gatekeeper ---------------------------------------------------
def test_pipeline_real_path(tmp_path, monkeypatch):
    from flock import pipeline
    from flock.agents import formatter

    std = tmp_path / "std"
    std.mkdir()
    for name, src in TABLES.items():
        shutil.copy(src, std / f"{name}.csv")
    monkeypatch.setattr(formatter, "STD", std)
    monkeypatch.setattr(deal_keeper, "RAW", FIX / "raw")
    found = pipeline.detect_real()
    assert len(found) == len(TRAPS)

    store = JsonStore(tmp_path / "state")
    agent = Actor("checkers", "agent")
    for f in found:
        store.put_new(f)
        transition(store, f["finding_id"], "check", agent, reason="test")
        transition(store, f["finding_id"], "judge", panel.ACTOR, patch=panel.decide(store.get(f["finding_id"])))
    outcomes = sorted(f["outcome"] for f in store.all())
    assert outcomes.count("conflict") == 3 and outcomes.count("correct") == 2  # traps 4, 13, 15 go to a human
    assert all(e["allowed"] for e in store.log())
