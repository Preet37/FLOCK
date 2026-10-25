"""Lump sums: what each platform deposit should have been, per the deal, and what is still owed. For GET /api/payouts.

A platform pays a whole chain in one bank deposit per payout period. A controller asks of each one: what did the
statement say, what should it have said under our deals, what hit the bank, and how much is still owed? This module
answers that from the run's own files, in cents, with no model and no invented numbers:

  static part   transactions.json "payouts" (flock/transactions.py, written once per real-data run): per lump sum the
                gross, the deductions by type, statement net, the deposit and this payout's share of it, the store rows,
                the finding ids linked to it, and the deal terms in effect per deduction type.
  read time     the linked findings' current state (a human may have approved, rejected or marked one recovered).

The math (all cents; tests/test_payouts.py checks every identity):
  deduction line   actual = the statement's figure.  expected (per contract) = actual - the gaps of the findings on that
                   line that are money owed back (journal.is_open: recoverable or human-approved, positive gap, not
                   rejected). A checker computes each gap from the deal term (contract, amendment, authorized email),
                   so expected is the deal's figure. A conflict still waiting for a human is shown as in_question, not
                   counted; a rejected one means the statement's figure stands.
                   Can't compute: a store was charged for the line and no deal document sets that term for it. The line
                   then says so (computable false, note) and its expected is the statement's figure, unchecked.
  adjustments      statement net - (gross - deductions): anything else on the statement (rounding, tips, credits).
  expected net     gross - expected deductions + adjustments + other gaps owed (sold-never-paid orders, anomalies)
                   = statement net + every counted gap.
  received         this payout's share of the bank deposit (a deposit covering two payouts is split by statement net,
                   exactly, the same split the Journal Entry Drafter uses); 0 when no deposit was found.
  shortfall        expected net - received.
  deposit shortfall statement net - received.
  owed             open gaps (counted, not yet RECOVERED) + deposit shortfall still open (over $0.05, and its deposit
                   finding neither rejected nor recovered). What is still owed to the restaurant for this lump sum.

status (first that applies; flags lists all): missing_deposit > short_deposit > owed > needs_review > recovered > matched
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from flock import frontend, transactions
from flock.agents import journal
from flock.models import money

TOLERANCE_CENTS = transactions.TOLERANCE_CENTS
LINES = [name for _, name in transactions.DEDUCTION_COLS]
LINE_OF = {"commission": "commission", "promo": "promo", "error_charge": "error_charges", "tax": "tax_withheld",
           "fee": "other_fees"}       # finding check_type -> the deduction line it corrects
LABEL = {"commission": "Commission", "promo": "Promotions (store's share)", "error_charges": "Error charges",
         "tax_withheld": "Tax withheld", "other_fees": "Other fees", "other": "Other gaps the checkers found"}
TERM_NAME = {"commission": "commission rate", "promo": "promo split", "error_charges": "error-report window",
             "tax_withheld": "tax rule", "other_fees": "fee"}
STATUS_ORDER = ["missing_deposit", "short_deposit", "owed", "needs_review", "recovered", "matched"]
COUNTS_AS = ("owed", "recovered", "in_question", "contract_stands", "fair", "deposit")


def _c(v: Any) -> int:
    return transactions._cents(v)


def _d(c: int) -> float:
    return transactions._dollars(c)


def counts_as(f: dict[str, Any]) -> str:
    """How a finding moves its lump sum: owed / recovered (counted in expected), in_question (a conflict waiting for a
    human), contract_stands (a human rejected it), fair (the panel called the charge correct, or no money either way),
    deposit (the deposit line handles it)."""
    if f.get("check_type") == "deposit":
        return "deposit"
    if f.get("state") == "REJECTED":
        return "contract_stands"
    if journal.is_open(f):
        return "recovered" if f.get("state") == "RECOVERED" else "owed"
    if journal.is_pending(f):
        return "in_question"
    return "fair"


def _brief(f: dict[str, Any], line: str, store: str | None) -> dict[str, Any]:
    r = frontend.risk(f)
    return {"finding_id": f["finding_id"], "check_type": f.get("check_type"), "line": line, "store_id": store,
            "order_ids": (f.get("order_ids") or [])[:4], "expected": f.get("expected"), "actual": f.get("actual"),
            "gap": f.get("gap"), "outcome": f.get("outcome"), "state": f.get("state"), "counts_as": counts_as(f),
            "risk": r["risk"], "risk_label": r["risk_label"], "portal_ref": f.get("portal_ref"),
            "attention": f.get("attention"), "needs_human": f.get("state") == "JUDGED" and (
                f.get("outcome") == "conflict" or bool(f.get("dispute_draft"))),
            "reason": (f.get("reason") or "")[:200]}


def _store_of(doc: dict[str, Any], p: dict[str, Any]) -> dict[str, str]:
    """finding_id -> store for the order-level findings on this payout's rows (a deposit finding spans stores)."""
    rows = _rows_by_txn(doc)
    out: dict[str, str] = {}
    for tid in p["txn_ids"]:
        for fid in rows.get(tid, {}).get("finding_ids", []):
            out.setdefault(fid, rows[tid]["store_id"])
    return out


_rows_cache: dict[int, tuple[Any, dict[str, dict[str, Any]]]] = {}


def _rows_by_txn(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hit = _rows_cache.get(id(doc))
    if hit and hit[0] is doc:
        return hit[1]
    idx = {r["txn_id"]: r for r in doc.get("rows", [])}
    _rows_cache.clear()
    _rows_cache[id(doc)] = (doc, idx)
    return idx


def compute(p: dict[str, Any], by_id: dict[str, dict[str, Any]], stores: dict[str, str] | None = None,
            detail: bool = False) -> dict[str, Any]:
    """One lump sum with the findings' current state applied. detail=True adds lines, findings and the waterfall."""
    stores = stores or {}
    gross, net, received = _c(p["gross"]), _c(p["net"]), _c(p["received"]) if p.get("deposit") else 0
    acc = {name: {"actual": _c(p["deductions"].get(name)), "owed": 0, "recovered": 0, "in_question": 0,
                  "findings": []} for name in LINES + ["other"]}
    deposit_findings, missing, briefs = [], [], []
    for fid in p["finding_ids"]:
        f = by_id.get(fid)
        if f is None:
            missing.append(fid)
            continue
        how = counts_as(f)
        line = "deposit" if how == "deposit" else LINE_OF.get(f.get("check_type"), "other")
        if detail:   # a deposit finding spans every store of the lump sum
            briefs.append(_brief(f, line, None if how == "deposit" else stores.get(fid)))
        if how == "deposit":
            deposit_findings.append(f)
            continue
        a = acc[line]
        a["findings"].append(fid)
        if how in ("owed", "recovered", "in_question"):
            a[how] += _c(f.get("gap"))

    lines = []
    for name in LINES + ["other"]:
        a = acc[name]
        counted = a["owed"] + a["recovered"]
        line = {"key": name, "label": LABEL[name], "actual": _d(a["actual"]), "expected": _d(a["actual"] - counted),
                "gap": _d(counted), "owed": _d(a["owed"]), "recovered": _d(a["recovered"]),
                "in_question": _d(a["in_question"]), "findings": len(a["findings"]), "finding_ids": a["findings"],
                "computable": True, "basis": None, "note": None}
        if name == "other":
            line.update(expected=_d(counted), basis="order-level findings that aren't a deduction (sold never paid, "
                                                    "paid without a register order, anomalies): money owed on top")
        else:
            terms = p.get("terms", {}).get(name) or []
            no_terms = p.get("no_terms", {}).get(name) or []
            line["terms"] = terms
            if not p.get("terms_known", True):
                line.update(computable=False if a["actual"] else True,
                            note="this run's file has no deal terms; expected is the statement figure less the "
                                 "checkers' gaps" if a["actual"] else None)
            elif no_terms and name != "error_charges":   # error charges use the default 72h window with no term
                line.update(computable=False,
                            note=f"no deal document sets a {TERM_NAME[name]} for {', '.join(sorted(set(no_terms)))}; "
                                 f"expected shown at the statement figure, not checked")
            if a["actual"] == 0 and not a["findings"]:
                line["basis"] = "nothing charged"
            elif terms:
                t = terms[0]
                line["basis"] = (f"{TERM_NAME[name]} {t.get('value')} ({t.get('authority')}, {t.get('source_file')})"
                                 + (f" and {len(terms) - 1} more" if len(terms) > 1 else ""))
            elif name == "error_charges":
                line["basis"] = "default 72h error-report window (no deal term)"
        lines.append(line)

    counted_all = sum(acc[n]["owed"] + acc[n]["recovered"] for n in acc)
    open_gaps = sum(acc[n]["owed"] for n in acc)
    recovered = sum(acc[n]["recovered"] for n in acc)
    in_question = sum(acc[n]["in_question"] for n in acc)
    adjustments = net - (gross - sum(acc[n]["actual"] for n in LINES))
    expected_net = net + counted_all
    deposit_shortfall = net - received
    closed = [f for f in deposit_findings if f.get("state") in ("REJECTED", "RECOVERED")]
    deposit_owed = deposit_shortfall if deposit_shortfall > TOLERANCE_CENTS and not closed else 0
    owed = open_gaps + deposit_owed
    computable = all(ln["computable"] for ln in lines)
    pending = sum(1 for fid in p["finding_ids"] if fid in by_id and journal.is_pending(by_id[fid]))

    flags = []
    if not p.get("deposit"):
        flags.append("missing_deposit")
    elif deposit_owed:
        flags.append("short_deposit")
    if open_gaps > TOLERANCE_CENTS:
        flags.append("owed")
    if pending:
        flags.append("needs_review")
    if recovered and not open_gaps and not deposit_owed:
        flags.append("recovered")
    flags = flags or ["matched"]
    dep = p.get("deposit") or {}
    out = {
        "payout_id": p["payout_id"], "payout_key": p["payout_key"], "channel": p["channel"], "platform": p["platform"],
        "payout_date": p["payout_date"], "deposit_id": dep.get("deposit_id"), "deposit_date": dep.get("date"),
        "deposit_amount": dep.get("amount"), "covers_payouts": dep.get("covers_payouts") or [],
        "received": _d(received), "stores": p["n_stores"], "orders": p["orders"], "payout_lines": p["lines"],
        "gross": _d(gross), "deductions": {ln["key"]: ln["actual"] for ln in lines if ln["key"] != "other"},
        "deductions_total": _d(sum(acc[n]["actual"] for n in LINES)), "adjustments": _d(adjustments),
        "net": _d(net), "expected_net": _d(expected_net), "expected_net_computable": computable,
        "shortfall": _d(expected_net - received), "deposit_shortfall": _d(deposit_shortfall),
        "open_gaps": _d(open_gaps), "deposit_owed": _d(deposit_owed), "owed": _d(owed), "recovered": _d(recovered),
        "in_question": _d(in_question), "findings": len(p["finding_ids"]),
        "findings_by_counts_as": dict(Counter(counts_as(by_id[f]) for f in p["finding_ids"] if f in by_id)),
        "status": min(flags, key=STATUS_ORDER.index), "flags": flags,
    }
    if not computable:
        out["not_computable"] = [ln["key"] for ln in lines if not ln["computable"]]
    if missing:
        out["missing_findings"] = missing
    if detail:
        out.update(lines=lines, findings_detail=sorted(briefs, key=lambda b: (-b["risk"], b["finding_id"])),
                   deposit=dep or None, deposit_findings=[f["finding_id"] for f in deposit_findings],
                   waterfall=waterfall(out, lines, dep),
                   owed_breakdown={"open_gaps": _d(open_gaps), "deposit_shortfall": _d(deposit_owed),
                                   "total": _d(owed),
                                   "formula": "owed = open recoverable gaps (not yet recovered) + deposit shortfall "
                                              "still open",
                                   "deposit_closed_by": [f["finding_id"] for f in closed]})
    return out


def waterfall(p: dict[str, Any], lines: list[dict[str, Any]], dep: dict[str, Any]) -> list[dict[str, Any]]:
    """Gross -> each deduction (actual vs contract) -> statement net -> expected net -> deposit -> shortfall. Each step
    carries the running total so a chart can draw floating bars; actual and expected run side by side."""
    steps = [{"key": "gross", "label": "Gross sales on the statement", "kind": "total", "amount": p["gross"],
              "running": p["gross"], "running_expected": p["gross"]}]
    run_a = run_e = _c(p["gross"])
    for ln in lines:
        if ln["key"] == "other":
            continue
        run_a -= _c(ln["actual"])
        run_e -= _c(ln["expected"])
        steps.append({"key": ln["key"], "label": ln["label"], "kind": "deduction", "amount": -ln["actual"],
                      "expected": -ln["expected"], "gap": ln["gap"], "in_question": ln["in_question"],
                      "computable": ln["computable"], "note": ln["note"], "basis": ln["basis"],
                      "finding_ids": ln["finding_ids"], "running": _d(run_a), "running_expected": _d(run_e)})
    if _c(p["adjustments"]):
        run_a += _c(p["adjustments"])
        run_e += _c(p["adjustments"])
        steps.append({"key": "adjustments", "label": "Other statement adjustments", "kind": "adjustment",
                      "amount": p["adjustments"], "expected": p["adjustments"], "running": _d(run_a),
                      "running_expected": _d(run_e)})
    steps.append({"key": "statement_net", "label": "Net per statement", "kind": "total", "amount": p["net"],
                  "running": p["net"], "running_expected": _d(run_e)})
    other = next(ln for ln in lines if ln["key"] == "other")
    if _c(other["gap"]) or other["findings"]:
        run_e += _c(other["gap"])
        steps.append({"key": "other", "label": other["label"], "kind": "gap", "amount": 0.0,
                      "expected": other["gap"], "in_question": other["in_question"],
                      "finding_ids": other["finding_ids"], "running": p["net"], "running_expected": _d(run_e)})
    steps.append({"key": "expected_net", "label": "Expected net per contract", "kind": "total",
                  "amount": p["expected_net"], "computable": p["expected_net_computable"], "running": p["net"],
                  "running_expected": p["expected_net"]})
    steps.append({"key": "deposit", "label": "Deposit received" + (f" ({dep.get('deposit_id')})" if dep else
                                                                    " (no deposit found)"),
                  "kind": "total", "amount": p["received"], "running": p["received"],
                  "running_expected": p["expected_net"]})
    steps.append({"key": "shortfall", "label": "Shortfall (expected - received)", "kind": "result",
                  "amount": p["shortfall"], "running": p["received"], "running_expected": p["expected_net"]})
    return steps


def _payouts(doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not doc:
        return []
    if "payouts" in doc:
        return doc["payouts"]
    return transactions.payouts_of(doc.get("rows", []), terms_known=False)   # a file from before payouts were saved


def listing(doc: dict[str, Any] | None, findings: list[dict[str, Any]], channel: str | None = None,
            status: str | None = None) -> dict[str, Any]:
    """GET /api/payouts: every lump sum (newest first) plus totals over them."""
    if not doc:
        return {"payouts": [], "total": 0, "summary": _summary([]), "unpaid_orders": {"count": 0, "gross": 0.0},
                "source": None, "note": "no pipeline run has written transactions yet (POST /api/run)"}
    by_id = {f["finding_id"]: f for f in findings}
    rows = [compute(p, by_id) for p in _payouts(doc)]
    picked = [r for r in rows if (not channel or r["channel"] == channel) and (not status or status in r["flags"])]
    picked.sort(key=lambda r: (r["payout_date"] or "", r["channel"] or ""), reverse=True)
    unpaid = [r for r in doc.get("rows", []) if r["kind"] == "order"]
    out = {"payouts": picked, "total": len(picked), "summary": _summary(rows), "filtered_summary": _summary(picked),
           "unpaid_orders": {"count": len(unpaid), "gross": money(sum(r["gross"] for r in unpaid)),
                             "note": "orders in the register that are in no payout at all (sold, never paid): they "
                                     "belong to no lump sum; see GET /api/transactions?status=sold_never_paid"},
           "source": doc.get("source"), "built_at": doc.get("built_at"), "formula": FORMULA}
    if doc.get("note"):
        out["note"] = doc["note"]
    return out


def detail(doc: dict[str, Any] | None, findings: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """GET /api/payouts/<payout_id or channel:payout_id>: the waterfall, per-store contribution, every finding inside
    it, and what's still owed. KeyError if there is no such payout."""
    ps = _payouts(doc)
    p = next((x for x in ps if x["payout_key"] == key), None) or next(
        (x for x in ps if str(x["payout_id"]) == key), None)
    if p is None:
        raise KeyError(f"payout {key}")
    by_id = {f["finding_id"]: f for f in findings}
    stores = _store_of(doc, p)
    out = compute(p, by_id, stores, detail=True)
    rows = _rows_by_txn(doc)
    per = []
    for tid in p["txn_ids"]:
        r = rows.get(tid)
        if r is None:
            continue
        fs = [by_id[f] for f in r["finding_ids"] if f in by_id and by_id[f].get("check_type") != "deposit"]
        per.append({"store_id": r["store_id"], "txn_id": tid, "orders": r.get("orders"), "gross": r["gross"],
                    "deductions": r["deductions"]["total"], "net": r["net"],
                    "received": (r.get("deposit") or {}).get("store_share") or 0.0, "deposit_gap": r["deposit_gap"],
                    "findings": len(r["finding_ids"]),
                    "open_gaps": _d(sum(_c(f.get("gap")) for f in fs if counts_as(f) == "owed")),
                    "share_of_gross": round(_c(r["gross"]) / _c(p["gross"]), 4) if _c(p["gross"]) else None})
    out["stores_detail"] = sorted(per, key=lambda s: s["store_id"] or "")
    out["source"], out["built_at"], out["formula"] = doc.get("source"), doc.get("built_at"), FORMULA
    return out


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tot = lambda k: _d(sum(_c(r[k]) for r in rows))   # noqa: E731
    by = Counter(r["status"] for r in rows)
    return {"payouts": len(rows), "gross": tot("gross"), "deductions": tot("deductions_total"), "net": tot("net"),
            "expected_net": tot("expected_net"), "received": tot("received"), "shortfall": tot("shortfall"),
            "owed": tot("owed"), "open_gaps": tot("open_gaps"), "deposit_owed": tot("deposit_owed"),
            "recovered": tot("recovered"), "in_question": tot("in_question"),
            "by_status": {s: by.get(s, 0) for s in STATUS_ORDER},
            "by_channel": dict(sorted(Counter(r["channel"] for r in rows).items()))}


FORMULA = {
    "expected_deduction": "statement figure - gaps owed back on that line (findings recoverable or human-approved, "
                          "each gap computed in code from the deal term)",
    "expected_net": "gross - expected deductions + statement adjustments + other gaps owed = statement net + every "
                    "counted gap",
    "shortfall": "expected net - deposit received",
    "deposit_shortfall": "statement net - deposit received",
    "owed": "open gaps (not yet recovered) + deposit shortfall still open",
    "in_question": "gaps on conflicts still waiting for a human: not counted until a human approves",
}
