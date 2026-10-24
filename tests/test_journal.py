"""Journal Entry Drafter: balanced entries, tax_rule handling, disputes as receivables, traceability,
approval through the gatekeeper (agents can't approve, only the gatekeeper posts), and the GL export format."""
import csv
import shutil
from pathlib import Path

import pytest

from dashboard.app import create_app
from flock.agents import checkers, deal_keeper, journal, matcher, panel
from flock.gatekeeper import GATEKEEPER, Denied, transition
from flock.models import JOURNAL, Actor
from flock.store import JournalStore, JsonStore

FIX = Path(__file__).resolve().parent / "fixtures"
TABLES = {n: FIX / f"std_{n}.csv" for n in ("orders", "payout_lines", "bank_deposits", "error_charges", "refunds")}
AGENT = Actor("journal", "agent")
HUMAN = Actor("controller", "human")


def cents(x):
    return round(x * 100)


def judged(found):
    out = []
    for f in found:
        d = f.to_dict()
        d.update(panel.decide(d, []))
        d["state"] = "JUDGED"
        out.append(d)
    return out


@pytest.fixture(scope="module")
def world():
    stories, found = matcher.match(TABLES)
    terms = deal_keeper.extract_terms(FIX / "raw" / "contracts", FIX / "raw" / "emails")
    terms_for = lambda s: deal_keeper.terms_for(terms, s)  # noqa: E731
    found += checkers.run_all(stories, terms_for)
    return {"stories": stories, "findings": judged(found), "terms_for": terms_for,
            "orders": matcher.load_tables(TABLES)["orders"]}


@pytest.fixture(scope="module")
def entries(world):
    return journal.draft(world["stories"], world["findings"], world["terms_for"], world["orders"])


def fid(world, check_type, key):
    return next(f["finding_id"] for f in world["findings"] if f["check_type"] == check_type and key in f["order_ids"])


def by_id(entries, je_id):
    return next(j for j in entries if j["je_id"] == je_id)


def receivable(je, finding_id):
    return sum(ln["debit"] for ln in je["lines"] if ln["finding_id"] == finding_id and ln["account"] == "1250")


# balanced + traceable --------------------------------------------------------------
def test_one_entry_per_store_channel_payout_and_all_balance(entries):
    assert {j["je_id"] for j in entries} == {
        "JE-S01-doordash-DD-PAY-W1", "JE-S01-doordash-DD-PAY-W2", "JE-S01-grubhub-GH-PAY-W1",
        "JE-S02-grubhub-GH-PAY-W1", "JE-S01-ubereats-UE-PAY-W1", "JE-S01-direct_app-DA-PAY-W1"}
    for je in entries:
        dr = sum(cents(ln["debit"]) for ln in je["lines"])
        cr = sum(cents(ln["credit"]) for ln in je["lines"])
        assert dr == cr and je["balanced"] and cents(je["total_debit"]) == dr, je["je_id"]
        assert all((ln["debit"] > 0) != (ln["credit"] > 0) for ln in je["lines"])  # one side per line
    assert sorted(j["journal_no"] for j in entries) == [f"FLK{i:05d}" for i in range(1, 7)]


def test_every_line_is_traceable(entries):
    for je in entries:
        for ln in je["lines"]:
            has_rows = any(s["source_file"] and s["rows"] for s in ln["sources"])
            assert has_rows or ln["finding_id"], (je["je_id"], ln)


def test_cash_is_the_bank_deposit_split_across_stores(world, entries):
    gh = [by_id(entries, "JE-S01-grubhub-GH-PAY-W1"), by_id(entries, "JE-S02-grubhub-GH-PAY-W1")]
    cash = [ln for je in gh for ln in je["lines"] if ln["account"] == "1010"]
    assert sum(cents(ln["debit"]) for ln in cash) == cents(17.10)  # DEP-2, to the cent
    assert all(ln["sources"][0]["source_file"].endswith("std_bank_deposits.csv") for ln in cash)
    # edge case 5: one deposit for two DoorDash payouts -> both entries' cash adds up to DEP-1
    dd = [by_id(entries, "JE-S01-doordash-DD-PAY-W1"), by_id(entries, "JE-S01-doordash-DD-PAY-W2")]
    assert sum(cents(ln["debit"]) for je in dd for ln in je["lines"] if ln["account"] == "1010") == cents(143.88)


def test_sales_credit_is_gross(entries):
    je = by_id(entries, "JE-S01-ubereats-UE-PAY-W1")
    assert [(ln["account"], ln["credit"]) for ln in je["lines"] if ln["credit"]] == [("4100", 20.0)]
    da = by_id(entries, "JE-S01-direct_app-DA-PAY-W1")
    assert [ln["account"] for ln in da["lines"] if ln["credit"]] == ["4110"]  # channel override in the chart


# disputes -> receivable ----------------------------------------------------------------
def test_open_findings_go_to_due_from_platform_not_expense(world, entries):
    dd = by_id(entries, "JE-S01-doordash-DD-PAY-W1")
    assert receivable(dd, fid(world, "commission", "ORD-2001")) == 1.00
    assert receivable(dd, fid(world, "error_charge", "ORD-2002")) == 8.40
    assert receivable(dd, fid(world, "error_charge", "ORD-2003")) == 6.00
    assert receivable(dd, fid(world, "tax", "ORD-2005")) == 3.50
    assert receivable(dd, fid(world, "missing_order", "ORD-2010")) == 18.75  # sold, never paid
    gh = by_id(entries, "JE-S01-grubhub-GH-PAY-W1")
    promo = [ln for ln in gh["lines"] if ln["account"] == "6120"]
    assert receivable(gh, fid(world, "promo", "ORD-2007")) == 7.00 and promo[0]["debit"] == 7.00  # 50/50 split
    # deposit shortfall (edge case 6) split across the two stores' entries, all on the same finding
    dep = fid(world, "deposit", "GH-PAY-W1")
    both = receivable(gh, dep) + receivable(by_id(entries, "JE-S02-grubhub-GH-PAY-W1"), dep)
    assert cents(both) == cents(5.00)


def test_receivable_lines_carry_the_finding_id_and_its_source(world, entries):
    f = fid(world, "commission", "ORD-2001")
    ln = next(ln for ln in by_id(entries, "JE-S01-doordash-DD-PAY-W1")["lines"] if ln["finding_id"] == f)
    assert ln["account_name"] == "Due from Platform (Disputed)" and f in ln["memo"]
    assert ln["sources"] == [{"source_file": "data/raw/test/std_payout_lines.csv", "rows": [2]}]


def test_rejected_and_pending_findings_stay_in_expense(world):
    findings = [dict(f) for f in world["findings"]]
    f2001 = next(f for f in findings if f["check_type"] == "commission")
    f2001["state"] = "REJECTED"
    jes = journal.draft(world["stories"], findings, world["terms_for"], world["orders"])
    dd = by_id(jes, "JE-S01-doordash-DD-PAY-W1")
    assert receivable(dd, f2001["finding_id"]) == 0
    assert dd["balanced"]
    ue = by_id(jes, "JE-S01-ubereats-UE-PAY-W1")  # trap 15 is a conflict waiting on a human
    assert not ue["finding_ids"] and any("waiting on a human" in n for n in ue["notes"])
    assert [ln["debit"] for ln in ue["lines"] if ln["account"] == "6130"] == [2.0]


def test_human_approved_conflict_becomes_a_receivable(world):
    findings = [dict(f) for f in world["findings"]]
    conflict = next(f for f in findings if f["check_type"] == "fee" and "ORD-2009" in f["order_ids"])
    conflict["state"] = "APPROVED"
    ue = by_id(journal.draft(world["stories"], findings, world["terms_for"], world["orders"]), "JE-S01-ubereats-UE-PAY-W1")
    assert receivable(ue, conflict["finding_id"]) == 2.0 and ue["balanced"]


# tax_rule --------------------------------------------------------------------------------
def tax_world(rule, withheld=0.0):
    pos = {"order_id": "O1", "store_id": "S01", "channel": "grubhub", "placed_at": "2026-10-05T12:00:00",
           "subtotal": 30.0, "tax": 2.70, "source_file": "data/std/orders.csv", "source_row": 2}
    net = round(30.0 - 6.0 - withheld, 2)
    line = {"payout_line_id": "L1", "channel": "grubhub", "store_id": "S01", "platform_order_id": "G1",
            "order_id": "O1", "gross": 30.0, "commission": 6.0, "promo_merchant_funded": 0.0, "error_charge": 0.0,
            "tax_withheld_by_platform": withheld, "other_fees": 0.0, "net": net, "payout_id": "GP1",
            "payout_date": "2026-10-09", "source_file": "data/std/payout_lines.csv", "source_row": 2}
    dep = {"deposit_id": "B1", "channel": "grubhub", "amount": net, "deposit_date": "2026-10-10",
           "source_file": "data/std/bank_deposits.csv", "source_row": 2}
    stories = [{"kind": "order", "order_id": "O1", "store_id": "S01", "channel": "grubhub",
                "placed_at": pos["placed_at"], "pos": pos, "payout_lines": [line]},
               {"kind": "deposit", "deposit_id": "B1", "channel": "grubhub", "deposit": dep, "payout_ids": ["GP1"],
                "lines": [line], "statement_total": net, "deposit_amount": net}]
    term = {"tax_rule": {"value": rule, "conflicts": []}} if rule else {}
    return stories, (lambda s: term)


def lines_by_account(je):
    out = {}
    for ln in je["lines"]:
        out.setdefault(ln["account"], 0)
        out[ln["account"]] += cents(ln["debit"]) - cents(ln["credit"])
    return out


def test_platform_remits_no_sales_tax_liability():
    stories, tf = tax_world("platform_remits")
    [je] = journal.draft(stories, [], tf)
    acc = lines_by_account(je)
    assert "2200" not in acc and "1260" not in acc and je["tax_rule"] == "platform_remits"
    assert acc == {"1010": 2400, "6110": 600, "4100": -3000}


def test_platform_remits_withheld_tax_is_disputed_or_expense():
    stories, tf = tax_world("platform_remits", withheld=2.70)
    tax_f = {"finding_id": "F-TAX", "order_ids": ["O1"], "channel": "grubhub", "check_type": "tax", "gap": 2.70,
             "outcome": "recoverable", "state": "JUDGED", "expected": 0.0, "actual": 2.70}
    [je] = journal.draft(stories, [tax_f], tf)
    assert receivable(je, "F-TAX") == 2.70 and "6160" not in lines_by_account(je) and je["balanced"]
    [je] = journal.draft(stories, [], tf)  # no open finding: the withholding is an expense
    assert lines_by_account(je)["6160"] == 270 and je["balanced"]


def test_merchant_remits_books_sales_tax_payable():
    stories, tf = tax_world("merchant_remits")
    [je] = journal.draft(stories, [], tf)
    acc = lines_by_account(je)
    assert acc["2200"] == -270          # Cr Sales Tax Payable = register tax
    assert acc["1260"] == 270           # the platform collected it and still owes it to us
    assert je["balanced"] and je["tax_rule"] == "merchant_remits"
    stories, tf = tax_world("merchant_remits", withheld=2.70)  # platform remitted it on our behalf
    [je] = journal.draft(stories, [], tf)
    acc = lines_by_account(je)
    assert acc["2200"] == 0 and acc["1260"] == 270 and je["balanced"]
    assert sorted((ln["debit"], ln["credit"]) for ln in je["lines"] if ln["account"] == "2200") == [(0.0, 2.7), (2.7, 0.0)]


def test_no_tax_rule_no_tax_lines_and_a_note():
    stories, tf = tax_world(None)
    [je] = journal.draft(stories, [], tf)
    assert "2200" not in lines_by_account(je) and any("No tax_rule" in n for n in je["notes"])


def test_allocate_is_exact():
    assert journal.allocate(1000, [1, 1, 1]) == [334, 333, 333]
    assert sum(journal.allocate(1710, [830, 1380])) == 1710
    assert journal.allocate(500, [0, 0]) == [500, 0]


def test_bad_chart_of_accounts_is_refused(tmp_path):
    p = tmp_path / "coa.json"
    p.write_text('{"accounts": [{"number": "1010", "name": "Cash", "type": "asset"}], "roles": {"cash": "9999"}}')
    with pytest.raises(ValueError):
        journal.load_coa(p)


# approval through the gatekeeper ------------------------------------------------------------
@pytest.fixture
def stores(tmp_path, entries):
    base = JsonStore(tmp_path / "state")
    js = JournalStore(base)
    journal.draft_into(js, [dict(je, lines=[dict(ln) for ln in je["lines"]]) for je in entries])
    return base, js


def test_drafts_are_logged_in_the_same_audit_log(stores, entries):
    base, js = stores
    assert {j["state"] for j in js.all()} == {"DRAFT"} and base.all() == []  # findings never see a JE
    drafts = [e for e in base.log() if e["action"] == "draft"]
    assert len(drafts) == len(entries) and {e["subject_type"] for e in drafts} == {"journal_entry"}
    assert all(e["role"] == "agent" and e["allowed"] for e in drafts)
    assert journal.draft_into(js, entries) == []  # re-running never overwrites an entry


def test_agent_cannot_approve_and_denial_is_logged(stores):
    base, js = stores
    je_id = "JE-S01-ubereats-UE-PAY-W1"
    with pytest.raises(Denied):
        transition(js, je_id, "approve", AGENT, machine=JOURNAL)
    last = base.log(je_id)[-1]
    assert last["allowed"] is False and last["role"] == "agent" and last["subject_type"] == "journal_entry"
    assert js.get(je_id)["state"] == "DRAFT"


def test_only_the_gatekeeper_posts(stores, tmp_path):
    base, js = stores
    je_id = "JE-S01-ubereats-UE-PAY-W1"
    with pytest.raises(Denied):
        transition(js, je_id, "post", HUMAN, machine=JOURNAL)  # can't skip approval
    transition(js, je_id, "approve", HUMAN, machine=JOURNAL, reason="ties to the bank")
    with pytest.raises(Denied):
        transition(js, je_id, "post", HUMAN, machine=JOURNAL)  # humans approve, they don't post
    r = journal.export_approved(js, tmp_path / "exports")
    assert r["posted"] == [je_id] and js.get(je_id)["state"] == "POSTED"
    post = base.log(je_id)[-1]
    assert post["actor"] == GATEKEEPER.id and post["role"] == "gatekeeper" and post["action"] == "post"
    assert [e["action"] for e in base.log(je_id) if e["allowed"]] == ["draft", "approve", "post"]
    assert journal.export_approved(js, tmp_path / "exports") == {"file": None, "posted": []}


def test_unbalanced_entry_cannot_be_approved(stores):
    base, js = stores
    je = js.get("JE-S01-ubereats-UE-PAY-W1")
    je["lines"][0]["debit"] = round(je["lines"][0]["debit"] + 0.01, 2)  # tamper with the file
    js._save({**js._load(), je["je_id"]: je})
    with pytest.raises(Denied, match="does not balance"):
        transition(js, je["je_id"], "approve", HUMAN, machine=JOURNAL)
    assert base.log(je["je_id"])[-1]["allowed"] is False


def test_finding_rules_unchanged(tmp_path):
    from flock.models import Finding
    s = JsonStore(tmp_path / "s")
    s.put_new(Finding(order_ids=["O"], channel="doordash", check_type="commission", expected=1, actual=2, gap=1,
                      finding_id="F-1").to_dict())
    with pytest.raises(Denied):
        transition(s, "F-1", "approve", HUMAN)  # NEW -> approve is still illegal
    with pytest.raises(Denied, match="not a journal_entry"):
        transition(s, "F-1", "draft", AGENT, machine=JOURNAL)  # the journal machine can't move a finding
    assert s.get("F-1")["state"] == "NEW" and s.log("F-1")[-1]["allowed"] is False


# export format -------------------------------------------------------------------------------
def test_export_csv_format(stores, tmp_path):
    base, js = stores
    for je in js.all():
        transition(js, je["je_id"], "approve", HUMAN, machine=JOURNAL)
    r = journal.export_approved(js, tmp_path / "exports")
    rows = list(csv.DictReader((tmp_path / "exports" / Path(r["file"]).name).open()))
    assert list(rows[0].keys()) == journal.EXPORT_COLUMNS
    assert {"Journal No", "Date", "Account", "Debit", "Credit", "Memo", "Location", "Class"} <= set(rows[0])
    by_no = {}
    for row in rows:
        assert (row["Debit"] == "") != (row["Credit"] == "")
        assert row["Date"][2] == "/" and row["Location"].startswith("S")
        by_no.setdefault(row["Journal No"], []).append(row)
    assert len(by_no) == len(js.all())
    for no, lines in by_no.items():
        dr = sum(cents(float(x["Debit"] or 0)) for x in lines)
        cr = sum(cents(float(x["Credit"] or 0)) for x in lines)
        assert dr == cr, no
    ue = next(x for x in rows if x["Flock JE"] == "JE-S01-ubereats-UE-PAY-W1" and x["Account"] == "1010")
    assert ue["Class"] == "Uber Eats" and ue["Location"] == "S01" and ue["Debit"] == "13.00"
    assert ue["Date"] == "10/11/2026" and ue["Account Name"] == "Cash - Operating"
    disputed = [x for x in rows if x["Account"] == "1250"]
    assert disputed and all(x["Finding"].startswith("F-") for x in disputed)


# pipeline + dashboard --------------------------------------------------------------------------
def test_pipeline_real_path_drafts_journal(tmp_path, monkeypatch):
    from flock import pipeline
    from flock.agents import formatter

    std = tmp_path / "std"
    std.mkdir()
    for name, src in TABLES.items():
        shutil.copy(src, std / f"{name}.csv")
    monkeypatch.setattr(formatter, "STD", std)
    monkeypatch.setattr(deal_keeper, "RAW", FIX / "raw")
    monkeypatch.setattr(pipeline, "RUNS", tmp_path / "runs.jsonl")
    store = JsonStore(tmp_path / "state")
    pipeline.run(reset=True, demo_approve=True, store=store, rules_path=tmp_path / "r.json",
                 portal=tmp_path / "p.jsonl")
    js = JournalStore(store)
    assert len(js.all()) == 6 and all(j["state"] == "DRAFT" and j["balanced"] for j in js.all())
    filed = [f["finding_id"] for f in store.all() if f["state"] == "FILED"]
    booked = {fid for j in js.all() for fid in j["finding_ids"]}
    assert set(filed) <= booked  # every dispute we filed is on the books as money due back


def test_pipeline_fixture_path_drafts_nothing(tmp_path, monkeypatch):
    from flock import pipeline

    monkeypatch.setattr(pipeline, "RUNS", tmp_path / "runs.jsonl")
    store = JsonStore(tmp_path / "state")
    pipeline.run(reset=True, store=store, rules_path=tmp_path / "r.json", portal=tmp_path / "p.jsonl",
                 fixtures=pipeline.FIXTURES)
    assert JournalStore(store).all() == [] and not (tmp_path / "state" / "journal_entries.json").exists()


def test_dashboard_journal_endpoints(stores, tmp_path):
    base, js = stores
    c = create_app(base, runs_path=tmp_path / "runs.jsonl", journal_store=js, exports_dir=tmp_path / "ex").test_client()
    d = c.get("/api/journal").get_json()
    assert len(d["entries"]) == 6 and d["unbalanced"] == 0 and d["by_state"]["DRAFT"] == 6
    assert "lines" not in d["entries"][0] and d["debits"] == d["credits"]
    one = c.get("/api/journal/JE-S01-ubereats-UE-PAY-W1").get_json()
    assert one["entry"]["lines"] and [e["action"] for e in one["log"]] == ["draft"]
    assert c.get("/api/journal/JE-NOPE").status_code == 404
    r = c.post("/api/journal/JE-S01-ubereats-UE-PAY-W1/approve", json={"actor": "journal", "role": "agent"})
    assert r.status_code == 403 and r.get_json()["log_entry"]["allowed"] is False
    assert c.post("/api/journal/JE-S01-ubereats-UE-PAY-W1/approve", json={}).status_code == 400
    assert c.post("/api/journal/JE-S01-ubereats-UE-PAY-W1/post", json={"actor": "ira"}).status_code == 400
    r = c.post("/api/journal/JE-S01-ubereats-UE-PAY-W1/approve", json={"actor": "ira", "reason": "ties out"})
    assert r.status_code == 200 and r.get_json()["entry"]["state"] == "APPROVED"
    r = c.post("/api/journal/export").get_json()
    assert r["posted"] == ["JE-S01-ubereats-UE-PAY-W1"] and (tmp_path / "ex" / Path(r["file"]).name).exists()
    assert js.get("JE-S01-ubereats-UE-PAY-W1")["state"] == "POSTED"
    acts = [e["subject_type"] for e in c.get("/api/summary").get_json()["activity"]]
    assert "journal_entry" in acts


def test_source_endpoint_stays_inside_data(tmp_path):
    c = create_app(JsonStore(tmp_path / "s"), runs_path=tmp_path / "runs.jsonl").test_client()
    r = c.get("/api/source?file=data/answer_key.csv&row=2")
    assert r.status_code == 200 and r.get_json()["header"]
    assert c.get("/api/source?file=../README.md&row=2").status_code == 404
    assert c.get("/api/source?file=mappings.json&row=2").status_code == 404
    assert c.get("/api/source?file=data/answer_key.csv&row=x").status_code == 400
