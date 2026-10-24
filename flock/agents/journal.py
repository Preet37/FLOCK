"""Journal Entry Drafter (owner: Preet). The entry a staff accountant posts to the general ledger (NetSuite / QuickBooks).

IN:  matcher stories (order + deposit), judged findings, deal terms (for tax_rule), POS orders (for sold-never-paid)
OUT: one balanced journal entry per store per channel per payout, state NEW -> DRAFT (agent), then a human approves,
     then the gatekeeper posts it by writing it to a NetSuite/QuickBooks-style CSV import in data/exports/.

Granularity: one entry per (store, channel, payout_id). A payout is the platform's own settlement unit (one week
here), so every entry ties to one statement and one bank deposit; location = store, class = channel in the export.
A platform payout covers every store, so the bank deposit is split across the stores' entries by each store's
statement net (exact to the cent, largest remainder). One deposit that covers two payouts (edge case 5) is split
the same way across both payouts' entries.

Lines (signed amounts in cents, + = debit; plain Python, no model touches money):
  Dr Cash - Operating             store's share of the bank deposit (cites the deposit row)
  Dr Commission / Promotions / Fees / Error charges   what the platform deducted, per payout line
  Dr Due from Platform (Disputed) the part of any deduction an open finding says we get back, one line per
                                  finding_id, instead of expense. Open = recoverable and not rejected, or a human
                                  approved it. Conflicts still waiting on a human stay in expense, with a note.
  Cr Delivery Sales               gross food sales per payout line (pre-tax)
  Tax, by the deal's tax_rule for the order's date:
    platform_remits (marketplace facilitator): no sales tax liability. Tax withheld from the payout is an
                                  improper deduction: disputed -> receivable, otherwise Sales Tax Withheld expense.
    merchant_remits:              Cr Sales Tax Payable = register tax on the order; tax the platform withheld is
                                  Dr Sales Tax Payable (remitted on our behalf); what is left is
                                  Dr Sales Tax Due from Platform (register tax minus any tax passed through in net).
    no tax_rule in effect:        no tax lines (tax is recorded from the register); noted on the entry.
  Deposit vs statement: the store's share of a gap goes to the receivable (open deposit finding), Payouts in
  Transit (no deposit found), or Payout Variance (anything else).
  Sold, never paid (edge case 3, open finding): Dr receivable / Cr Delivery Sales, on the store's next payout entry.
  Anything a payout line can't explain (net != gross - deductions) goes to Payout Clearing, never silently dropped.

Every line carries sources (source_file + rows) and/or a finding_id. Accounts come from chart_of_accounts.json.
"""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from flock.gatekeeper import GATEKEEPER, transition
from flock.models import JOURNAL, Actor, money
from flock.store import ROOT, Store

ACTOR = Actor(id="journal", role="agent")
COA_PATH = ROOT / "chart_of_accounts.json"
EXPORTS = ROOT / "data" / "exports"
TOLERANCE = 0.05
CHANNEL_NAME = {"doordash": "DoorDash", "ubereats": "Uber Eats", "grubhub": "Grubhub", "direct_app": "Direct app"}
EXPORT_COLUMNS = ["Journal No", "Date", "Account", "Account Name", "Debit", "Credit", "Memo", "Location", "Class",
                  "Flock JE", "Finding", "Source"]

# payout line column -> (finding check_type that can dispute it, expense role)
DEDUCTIONS = [
    ("commission", "commission", "commission"),
    ("promo_merchant_funded", "promo", "promotions"),
    ("other_fees", "fee", "fees"),
    ("error_charge", "error_charge", "error_charges"),
    ("tax_withheld_by_platform", "tax", "tax_withheld"),
]
DECIDED_FOR_US = {"APPROVED", "FILED", "RECOVERED"}


# helpers ------------------------------------------------------------------
def cents(x: Any) -> int:
    return round(money(x or 0) * 100)


def dollars(c: int) -> float:
    return money(c / 100)


def load_coa(path: Path | None = None) -> dict[str, Any]:
    coa = json.loads(Path(path or COA_PATH).read_text())
    accounts = {a["number"]: a for a in coa["accounts"]}
    for role, num in list(coa["roles"].items()) + [
            (r, n) for over in coa.get("channel_roles", {}).values() for r, n in over.items()]:
        if num not in accounts:
            raise ValueError(f"chart of accounts: role {role!r} points at unknown account {num}")
    return {**coa, "by_number": accounts}


def account_for(coa: dict[str, Any], role: str, channel: str) -> dict[str, Any]:
    num = coa.get("channel_roles", {}).get(channel, {}).get(role) or coa["roles"][role]
    return coa["by_number"][num]


def _day(s: Any) -> str | None:
    return str(s).strip()[:10] if s else None


def _week(day: str | None) -> str | None:
    if not day:
        return None
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def _line_key(ln: dict[str, Any]) -> tuple:
    return (ln.get("channel"), ln.get("payout_line_id"), ln.get("source_file"), ln.get("source_row"))


def is_open(f: dict[str, Any]) -> bool:
    """Money we expect back: a positive gap, recoverable and not rejected, or a human approved the dispute."""
    if money(f.get("gap") or 0) <= TOLERANCE or f.get("state") == "REJECTED":
        return False
    return f.get("outcome") == "recoverable" or f.get("state") in DECIDED_FOR_US


def is_pending(f: dict[str, Any]) -> bool:
    return f.get("outcome") == "conflict" and f.get("state") == "JUDGED" and money(f.get("gap") or 0) > TOLERANCE


def allocate(total: int, weights: list[int]) -> list[int]:
    """Split `total` cents across weights exactly (largest remainder). All to the first if the weights sum to 0."""
    if not weights:
        return []
    w_sum = sum(weights)
    if w_sum == 0:
        return [total] + [0] * (len(weights) - 1)
    exact = [Fraction(total * w, w_sum) for w in weights]
    out = [math.floor(x) for x in exact]
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - out[i]), i))
    for i in order[: total - sum(out)]:
        out[i] += 1
    return out


# findings index -----------------------------------------------------------------
class _Disputes:
    """Which deductions open findings claim back. Each finding's gap is spent once, capped at what was deducted."""

    def __init__(self, findings: list[dict[str, Any]]):
        self.by_id = {f["finding_id"]: f for f in findings}
        self.pool: dict[tuple, list[list]] = defaultdict(list)   # (order_id, check_type) -> [[fid, cents left]]
        self.pending: dict[tuple, list[str]] = defaultdict(list)
        self.deposit: dict[tuple, dict] = {}                      # (channel, payout_id) -> finding
        self.unlinked: dict[tuple, dict] = {}                     # (channel, platform id) -> trap-4 finding
        self.never_paid: list[dict] = []                          # trap-3 findings
        for f in sorted(findings, key=lambda x: x["finding_id"]):
            ct, ids = f.get("check_type"), f.get("order_ids") or []
            if ct == "deposit":
                for pid in ids:
                    self.deposit[(f["channel"], pid)] = f
            elif ct == "missing_order":
                if money(f.get("expected") or 0) > 0 and money(f.get("actual") or 0) == 0:
                    if is_open(f):
                        self.never_paid.append(f)
                else:
                    for k in ids:
                        self.unlinked[(f["channel"], k)] = f
            elif ids and is_open(f):
                self.pool[(ids[0], ct)].append([f["finding_id"], cents(f["gap"])])
            elif ids and is_pending(f):
                self.pending[(ids[0], ct)].append(f["finding_id"])

    def take(self, order_id: str | None, check_type: str, amount: int) -> list[tuple[str, int]]:
        """[(finding_id, cents)] of `amount` that open findings dispute; the rest is expense."""
        out = []
        for slot in self.pool.get((order_id, check_type), []):
            if amount <= 0:
                break
            got = min(amount, slot[1])
            if got > 0:
                out.append((slot[0], got))
                slot[1] -= got
                amount -= got
        return out


# drafting -------------------------------------------------------------------------
class _Entry:
    """Signed parts of one journal entry, aggregated by (role, finding_id, part) into GL lines at the end."""

    def __init__(self, unit: dict[str, Any]):
        self.unit = unit
        self.parts: dict[tuple, dict[str, Any]] = {}
        self.notes: list[str] = []
        self.tax_rules: set[str] = set()

    def add(self, role: str, amount: int, memo: str, src: dict[str, Any] | None = None, finding_id: str | None = None,
            part: str = ""):
        """`part` keeps two amounts on the same account as separate lines (tax collected vs tax remitted)."""
        if amount == 0:
            return
        p = self.parts.setdefault((role, finding_id, part), {"cents": 0, "memo": memo, "sources": defaultdict(set)})
        p["cents"] += amount
        if src and src.get("source_file"):
            p["sources"][src["source_file"]].add(src.get("source_row"))

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def build(self, coa: dict[str, Any]) -> dict[str, Any]:
        u, ch = self.unit, self.unit["channel"]
        lines = []
        for (role, fid, _), p in self.parts.items():
            if p["cents"] == 0:
                continue
            acct = account_for(coa, role, ch)
            n_rows = sum(len(r) for r in p["sources"].values())
            memo = p["memo"] + (f" ({n_rows} rows)" if n_rows > 1 else "")
            lines.append({"account": acct["number"], "account_name": acct["name"], "account_type": acct["type"],
                          "debit": dollars(p["cents"]) if p["cents"] > 0 else 0.0,
                          "credit": dollars(-p["cents"]) if p["cents"] < 0 else 0.0,
                          "memo": memo, "finding_id": fid,
                          "sources": [{"source_file": f, "rows": sorted(r for r in rows if r is not None)}
                                      for f, rows in sorted(p["sources"].items())]})
        lines.sort(key=lambda ln: (ln["debit"] == 0, ln["account"], ln["finding_id"] or ""))
        for i, ln in enumerate(lines, 1):
            ln["line_no"] = i
        dr = sum(cents(ln["debit"]) for ln in lines)
        cr = sum(cents(ln["credit"]) for ln in lines)
        dep = u.get("deposit")
        day = _day(dep["deposit_date"]) if dep else _day(u.get("payout_date"))
        return {
            "je_id": f"JE-{u['store_id']}-{ch}-{u['payout_id']}", "journal_no": None, "date": day,
            "week": _week(_day(u.get("payout_date")) or day), "store_id": u["store_id"], "channel": ch,
            "payout_id": u["payout_id"], "payout_date": _day(u.get("payout_date")),
            "deposit_id": dep["deposit_id"] if dep else None,
            "tax_rule": ", ".join(sorted(self.tax_rules)) or None,
            "memo": f"{CHANNEL_NAME.get(ch, ch)} payout {u['payout_id']} for store {u['store_id']}",
            "lines": lines, "total_debit": dollars(dr), "total_credit": dollars(cr), "balanced": dr == cr,
            "finding_ids": sorted({ln["finding_id"] for ln in lines if ln["finding_id"]}),
            "notes": self.notes, "drafted_by": ACTOR.id, "state": "NEW",
        }


def _units(deposit_stories: list[dict[str, Any]], disputes: _Disputes) -> list[dict[str, Any]]:
    """One unit per (store, channel, payout) with its lines and its cash share of the bank deposit."""
    units = []
    for ds in deposit_stories:
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for ln in ds["lines"]:
            groups[(ln.get("payout_id"), ln.get("store_id") or "UNKNOWN")].append(ln)
        keys = sorted(groups, key=lambda k: (str(k[0]), str(k[1])))
        nets = [sum(cents(ln["net"]) for ln in groups[k]) for k in keys]
        shares = allocate(cents(ds["deposit_amount"]), nets)
        for (pid, store), net, share in zip(keys, nets, shares):
            lines = groups[(pid, store)]
            dep_f = disputes.deposit.get((ds["channel"], pid))
            units.append({"store_id": store, "channel": ds["channel"], "payout_id": pid,
                          "payout_date": lines[0].get("payout_date"), "deposit": ds.get("deposit"),
                          "deposit_payouts": ds["payout_ids"], "lines": lines, "net": net, "cash": share,
                          "deposit_finding": dep_f})
    return units


def draft(stories: list[dict[str, Any]], findings: list[dict[str, Any]],
          terms_for: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
          orders: list[dict[str, Any]] | None = None, coa: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Journal entries (state NEW) for every payout in the matcher's deposit stories. Pure: no store writes."""
    coa = coa or load_coa()
    disputes = _Disputes(findings)
    order_stories = [s for s in stories if s.get("kind") == "order"]
    story_of = {_line_key(ln): s for s in order_stories for ln in s["payout_lines"]}
    units = _units([s for s in stories if s.get("kind") == "deposit"], disputes)
    tax_cache: dict[int, str | None] = {}
    taxed_orders: set[str] = set()

    def tax_rule(story: dict[str, Any], cache: bool = True) -> str | None:
        if terms_for is None:
            return None
        k = id(story)
        if not cache or k not in tax_cache:
            t = (terms_for(story) or {}).get("tax_rule")
            rule = str(t["value"]) if t and not t.get("unresolved") else None
            if not cache:
                return rule
            tax_cache[k] = rule
        return tax_cache[k]

    entries: dict[tuple, _Entry] = {}
    for u in units:
        e = entries[(u["store_id"], u["channel"], u["payout_id"])] = _Entry(u)
        ch, pid = u["channel"], u["payout_id"]
        dep = u["deposit"]
        dep_src = dep if dep else None
        e.add("cash", u["cash"], f"Bank deposit {dep['deposit_id']}, store share" if dep else "No bank deposit", dep_src)
        if dep and len(u["deposit_payouts"]) > 1:
            e.note(f"Deposit {dep['deposit_id']} covers payouts {', '.join(u['deposit_payouts'])}; "
                   f"cash split across them by statement net.")
        gap = u["net"] - u["cash"]
        f = u["deposit_finding"]
        src0 = dep_src or u["lines"][0]
        if gap and f and is_open(f):
            e.add("disputed_receivable", gap, f"Disputed deposit {f['finding_id']}: bank short vs statement",
                  src0, f["finding_id"])
        elif gap and dep is None:
            e.add("payout_in_transit", gap, f"Payout {pid} not yet in the bank", src0)
            if f and is_pending(f):
                e.note(f"{f['finding_id']} (deposit) is waiting on a human.")
        elif gap:
            e.add("payout_variance", gap, f"Deposit vs statement {pid}", src0)
            if f and is_pending(f):
                e.note(f"{f['finding_id']} (deposit) is waiting on a human; booked as variance until decided.")

        for ln in u["lines"]:
            story = story_of.get(_line_key(ln))
            label = f"{CHANNEL_NAME.get(ch, ch)} {pid}"
            e.add("sales", -cents(ln["gross"]), f"Gross sales, {label}", ln)
            unlinked = None
            if story is None:
                unlinked = disputes.unlinked.get((ch, ln.get("platform_order_id"))) or \
                    disputes.unlinked.get((ch, ln.get("payout_line_id")))
            rule = tax_rule(story) if story is not None else tax_rule({
                "channel": ch, "store_id": u["store_id"], "placed_at": ln.get("placed_at") or ln.get("payout_date"),
                "payout_lines": [ln]}, cache=False)
            if rule:
                e.tax_rules.add(rule)
            deducted = 0
            for col, check_type, role in DEDUCTIONS:
                amt = cents(ln.get(col))
                deducted += amt
                if not amt:
                    continue
                if unlinked is not None and is_open(unlinked):
                    e.add("disputed_receivable", amt, f"Disputed {unlinked['finding_id']}: charges on an order "
                          f"the register never rang up", ln, unlinked["finding_id"])
                    continue
                if unlinked is not None and is_pending(unlinked):
                    e.note(f"{unlinked['finding_id']} (missing_order) is waiting on a human; booked as expense "
                           f"until decided.")
                oid = story["order_id"] if story else None
                for fid, got in disputes.take(oid, check_type, amt):
                    fnd = disputes.by_id[fid]
                    e.add("disputed_receivable", got, f"Disputed {fnd['check_type']} {fid} on "
                          f"{', '.join(fnd['order_ids'])}", ln, fid)
                    amt -= got
                for fid in disputes.pending.get((oid, check_type), []):
                    e.note(f"{fid} ({check_type}) is waiting on a human; booked as expense until decided.")
                if not amt:
                    continue
                if col == "tax_withheld_by_platform" and rule == "merchant_remits":
                    e.add("sales_tax_payable", amt, f"Sales tax remitted by {CHANNEL_NAME.get(ch, ch)} on our behalf",
                          ln, part="remitted")
                else:
                    e.add(role, amt, f"{role.replace('_', ' ').capitalize()}, {label}", ln)
            residual = cents(ln["net"]) - (cents(ln["gross"]) - deducted)  # anything the columns don't explain
            if rule == "merchant_remits":
                owed = -residual  # tax passed through in net reduces what the platform still owes us
                if story is not None and story["order_id"] not in taxed_orders:
                    taxed_orders.add(story["order_id"])
                    pos = story.get("pos") or {}
                    tax = cents(pos.get("tax"))
                    e.add("sales_tax_payable", -tax, "Sales tax collected (register), merchant remits", pos)
                    owed += tax
                e.add("tax_due_from_platform", owed, f"Sales tax collected by {CHANNEL_NAME.get(ch, ch)}, "
                      f"not yet passed to us", ln)
            elif residual:
                e.add("payout_clearing", -residual, f"Unexplained payout amount, {label}", ln)
                e.note(f"Payout line {ln.get('payout_line_id')} net does not equal gross minus deductions; "
                       f"difference held in clearing for review.")
        if not e.tax_rules:
            e.note("No tax_rule in effect for this channel; sales tax is recorded from the register, not this payout.")

    for f in disputes.never_paid:  # edge case 3: on the store's next payout entry for that channel
        o = next((o for o in orders or [] if o.get("order_id") == f["order_ids"][0]), None)
        if o is None:
            continue
        placed = _day(o.get("placed_at")) or ""
        mine = sorted((u for u in units if u["channel"] == f["channel"] and u["store_id"] == o["store_id"]),
                      key=lambda u: _day(u.get("payout_date")) or "")
        target = next((u for u in mine if (_day(u.get("payout_date")) or "") >= placed), mine[-1] if mine else None)
        if target is None:
            continue
        e = entries[(target["store_id"], target["channel"], target["payout_id"])]
        amt = cents(f["gap"])
        e.add("disputed_receivable", amt, f"Disputed {f['finding_id']}: order {o['order_id']} sold, never paid",
              o, f["finding_id"])
        e.add("sales", -amt, f"Gross sales, order {o['order_id']} missing from every payout", o, f["finding_id"])

    out = [e.build(coa) for _, e in sorted(entries.items(), key=lambda kv: (kv[0][0], kv[0][2], kv[0][1]))]
    for i, je in enumerate(sorted(out, key=lambda j: (j["date"] or "", j["store_id"], j["channel"])), 1):
        je["journal_no"] = f"FLK{i:05d}"
    return out


# store + gatekeeper ---------------------------------------------------------------
def draft_into(jstore: Store, entries: list[dict[str, Any]]) -> list[str]:
    """Save new entries and move each NEW -> DRAFT as the journal agent. Existing je_ids are left alone."""
    have = {j["je_id"] for j in jstore.all()}
    drafted = []
    for je in entries:
        if je["je_id"] in have:
            continue
        jstore.put_new(je)
        transition(jstore, je["je_id"], "draft", ACTOR, machine=JOURNAL,
                   reason=f"Drafted from payout {je['payout_id']}: debits {je['total_debit']:.2f} = credits "
                          f"{je['total_credit']:.2f}" if je["balanced"] else "Drafted, DOES NOT BALANCE",
                   evidence=[{"finding_id": fid} for fid in je["finding_ids"]]
                   + ([{"deposit_id": je["deposit_id"]}] if je["deposit_id"] else []))
        drafted.append(je["je_id"])
    return drafted


def export_approved(jstore: Store, out_dir: Path | None = None) -> dict[str, Any]:
    """The gatekeeper posts: every APPROVED entry goes into one CSV import file, then moves APPROVED -> POSTED."""
    approved = sorted((j for j in jstore.all() if j["state"] == "APPROVED"), key=lambda j: j["journal_no"] or "")
    if not approved:
        return {"file": None, "posted": []}
    out_dir = Path(out_dir or EXPORTS)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    path = out_dir / f"journal_entries_{stamp:%Y%m%dT%H%M%S}.csv"
    n = 1
    while path.exists():
        n += 1
        path = out_dir / f"journal_entries_{stamp:%Y%m%dT%H%M%S}_{n}.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(EXPORT_COLUMNS)
        for je in approved:
            w.writerows(export_rows(je))
    rel = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    posted = []
    for je in approved:
        transition(jstore, je["je_id"], "post", GATEKEEPER, machine=JOURNAL,
                   patch={"export_file": rel, "posted_at": stamp.isoformat(timespec="seconds")},
                   reason=f"Posted {je['journal_no']} to the GL import file", evidence=[{"source_file": rel}])
        posted.append(je["je_id"])
    return {"file": rel, "posted": posted}


def export_rows(je: dict[str, Any]) -> list[list[str]]:
    """One CSV row per GL line: NetSuite / QuickBooks journal import columns (dates MM/DD/YYYY)."""
    day = date.fromisoformat(je["date"]).strftime("%m/%d/%Y") if je.get("date") else ""
    rows = []
    for ln in je["lines"]:
        src = ln["sources"][0] if ln["sources"] else None
        n = sum(len(s["rows"]) for s in ln["sources"])
        ref = f"{src['source_file']}:{','.join(map(str, src['rows'][:3]))}" if src else ""
        if n > 3:
            ref += f" (+{n - 3} more)"
        rows.append([je["journal_no"], day, ln["account"], ln["account_name"],
                     f"{ln['debit']:.2f}" if ln["debit"] else "", f"{ln['credit']:.2f}" if ln["credit"] else "",
                     ln["memo"], je["store_id"], CHANNEL_NAME.get(je["channel"], je["channel"]), je["je_id"],
                     ln["finding_id"] or "", ref])
    return rows
