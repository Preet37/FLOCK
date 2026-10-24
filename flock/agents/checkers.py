"""Checkers (owner: Thabhelo). The calculators. Plain Python only: NO LLM does money math.

IN:  stories (from matcher) + deal terms (from deal_keeper.terms_for)
OUT: findings (CONTRACT.md "Finding"), state NEW, each with expected, actual, gap and evidence

One checker per rule. They run independently (in parallel across orders = the "flock").
Edge cases each should catch are listed in FLOCK_CONTEXT.md section 13.

Gap sign: positive = Dave's is owed money (platform took too much, or the bank got less than the statement).
Each term in `terms` may carry "conflicts": claims a human should see (e.g. an unauthorized rep email, edge case 15).
If the payout matches the winning term but a conflicting claim says otherwise, the checker still emits a finding
against that claim, with both quotes in the evidence, so the panel sends it to a human instead of deciding silently.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from flock.models import Finding, money

TOLERANCE = 0.05  # dollars; penny rounding is ignored (edge case 17)
ERROR_WINDOW_HOURS = 72  # default when no "error_window" term is found (edge case 10)
STUCK_REFUND = {"initiated", "pending", "processing"}
FEE_CHECK_TYPE = "fee"  # proposed addition to CONTRACT.md check_type (edge cases 14, 15)


# helpers -----------------------------------------------------------------
def _term_evidence(term: dict[str, Any]) -> list[dict[str, Any]]:
    ev = [{"source_file": term["source_file"], "quote": term["quote"], "authority": term["authority"]}]
    for c in term.get("conflicts", []):
        ev.append({"source_file": c["source_file"], "quote": c["quote"], "authority": c.get("authority"),
                   "conflict": True})
    return ev


def _against(term: dict[str, Any], compute: Callable[[Any], float], actual: float) -> tuple[float, dict[str, Any]]:
    """(expected, basis). Normally the winning term. If the payout matches it but a surfaced claim
    (weaker or equal authority) would give a different number, compare against that claim instead."""
    expected = compute(term["value"])
    if abs(actual - expected) > TOLERANCE:
        return expected, term
    for c in term.get("conflicts", []):
        alt = compute(c["value"])
        if abs(actual - alt) > TOLERANCE:
            return alt, c
    return expected, term


def _note(term: dict[str, Any], basis: dict[str, Any]) -> str:
    if term.get("unresolved"):
        return " Sources of equal or unknown authority disagree; a human must pick."
    if basis is not term:
        return (f" That matches the {term['authority']} ({term['value']}), but the {basis.get('authority') or 'unknown-authority'}"
                f" source ({basis['source_file']}) says {basis['value']}; a human must decide.")
    return ""


def _main_line(story: dict[str, Any]) -> dict[str, Any]:
    return max(story["payout_lines"], key=lambda ln: ln["gross"])


def _line_ev(line: dict[str, Any], what: str) -> dict[str, Any]:
    return {"source_file": line["source_file"], "row": line["source_row"], "what": what}


def _ts(s: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None) if s else None
    except ValueError:
        return None


# order checkers -----------------------------------------------------------
def check_commission(story: dict[str, Any], terms: dict[str, Any]) -> Finding | None:
    """Edge case 7. Example of the pattern every checker should follow."""
    line = _main_line(story)
    term = terms["commission"]
    actual = money(line["commission"])
    expected, basis = _against(term, lambda v: money(line["gross"] * float(v)), actual)
    gap = money(actual - expected)  # positive = platform took too much
    if abs(gap) <= TOLERANCE:
        return None
    return Finding(
        order_ids=[story["order_id"]], channel=story["channel"], check_type="commission",
        expected=expected, actual=actual, gap=gap,
        evidence=[_line_ev(line, f"commission charged {actual} on gross {line['gross']}")] + _term_evidence(term),
        reason=f"Charged {actual} commission; deal says {float(basis['value']):.0%} of {line['gross']} = {expected}."
               + _note(term, basis),
    )


def check_promo(story: dict[str, Any], terms: dict[str, Any]) -> Finding | None:
    """Edge cases 8 (BOGO charged 100% when the deal says 50/50) and 9 (promo charged outside its date range).
    Needs payout line promo_total (full promo cost before the split) to compute the store's share."""
    lines = story["payout_lines"]
    actual = money(sum(ln["promo_merchant_funded"] for ln in lines))
    if actual <= TOLERANCE:
        return None
    code = terms["promo_code"]
    line = next(ln for ln in lines if ln["promo_merchant_funded"])
    line_ev = [_line_ev(line, f"promo {code} cost {actual} charged to the store")]
    term = terms.get("promo_split")
    if term is None:  # edge case 9: the deal has this promo, but not on this date
        windows = terms["promo_windows"]
        if not windows:
            return None  # no deal mentions this promo; nothing to compare against
        span = ", ".join(f"{w.get('effective_from')} to {w.get('effective_to') or 'open'}" for w in windows)
        return Finding(
            order_ids=[story["order_id"]], channel=story["channel"], check_type="promo",
            expected=0.0, actual=actual, gap=actual,
            evidence=line_ev + [e for w in windows for e in _term_evidence(w)],
            reason=f"Store charged {actual} for promo {code} on {str(story['placed_at'])[:10]}, "
                   f"but the deal only runs that promo {span}.",
        )
    total = money(sum(ln.get("promo_total") or 0.0 for ln in lines))
    if total <= TOLERANCE:
        return None  # can't split a promo without its full cost
    expected, basis = _against(term, lambda v: money(total * float(v)), actual)
    gap = money(actual - expected)
    if abs(gap) <= TOLERANCE:
        return None
    return Finding(
        order_ids=[story["order_id"]], channel=story["channel"], check_type="promo",
        expected=expected, actual=actual, gap=gap,
        evidence=line_ev + _term_evidence(term),
        reason=f"Store charged {actual} of a {total} {code} promo; deal says the store funds "
               f"{float(basis['value']):.0%} = {expected}." + _note(term, basis),
    )


def check_tax(story: dict[str, Any], terms: dict[str, Any]) -> Finding | None:
    """Edge case 12: sales tax taken out of the payout even though the platform collected (and remits) it."""
    term = terms["tax_rule"]
    lines = story["payout_lines"]
    actual = money(sum(ln["tax_withheld_by_platform"] for ln in lines))
    expected, basis = _against(term, lambda v: 0.0 if str(v) == "platform_remits" else actual, actual)
    gap = money(actual - expected)
    if abs(gap) <= TOLERANCE:
        return None
    line = max(lines, key=lambda ln: ln["tax_withheld_by_platform"])
    pos = story.get("pos") or {}
    return Finding(
        order_ids=[story["order_id"]], channel=story["channel"], check_type="tax",
        expected=expected, actual=actual, gap=gap,
        evidence=[_line_ev(line, f"tax {actual} deducted from the payout"),
                  {"source_file": pos.get("source_file"), "row": pos.get("source_row"),
                   "what": f"register tax {pos.get('tax')} collected by {story['channel']}"}] + _term_evidence(term),
        reason=f"{actual} sales tax was deducted from the payout, but {story['channel']} collected and remits "
               f"the tax itself, so nothing should come out of the store's money." + _note(term, basis),
    )


def check_fee(story: dict[str, Any], terms: dict[str, Any]) -> Finding | None:
    """Edge cases 14 (authorized rep waived the fee -> recoverable) and 15 (unauthorized rep -> conflict)."""
    term = terms["fee"]
    lines = story["payout_lines"]
    actual = money(sum(ln["other_fees"] for ln in lines))
    expected, basis = _against(term, lambda v: money(float(v)), actual)
    gap = money(actual - expected)
    if abs(gap) <= TOLERANCE:
        return None
    line = max(lines, key=lambda ln: ln["other_fees"])
    return Finding(
        order_ids=[story["order_id"]], channel=story["channel"], check_type=FEE_CHECK_TYPE,
        expected=expected, actual=actual, gap=gap,
        evidence=[_line_ev(line, f"fees {actual} charged")] + _term_evidence(term),
        reason=f"Charged {actual} in fees; the {basis.get('authority')} says {expected}." + _note(term, basis),
    )


def _charges(story: dict[str, Any]) -> list[dict[str, Any]]:
    """Error charges for the order: the error_charges table if present, else the payout lines' error_charge."""
    if story.get("error_charges"):
        return story["error_charges"]
    return [{"error_charge_id": ln.get("payout_line_id"), "amount": ln["error_charge"], "reason": None,
             "source_file": ln["source_file"], "source_row": ln["source_row"]}
            for ln in story["payout_lines"] if ln["error_charge"] > TOLERANCE]


def _stuck_refunds(story: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in story.get("refunds", [])
            if str(r.get("status", "")).lower() in STUCK_REFUND and (r.get("money_moved") or 0.0) <= TOLERANCE]


def check_error_charge(story: dict[str, Any], terms: dict[str, Any]) -> Finding | None:
    """Edge cases 10 (complaint 72+ hours after delivery), 11 (same charge twice), 16 (legit charge).
    Always returns a finding when there is an error charge: a legit one has gap 0, so the panel calls it
    'correct' and nothing gets disputed. Charges tied to a refund that never moved are left to check_refund."""
    stuck = _stuck_refunds(story)
    charges = [c for c in _charges(story)
               if not any(abs(c["amount"] - r["amount"]) <= TOLERANCE for r in stuck)]
    if not charges:
        return None
    window_term = terms.get("error_window")
    window = float(window_term["value"]) if window_term else ERROR_WINDOW_HOURS
    seen, problems, evidence = set(), [], []
    expected = 0.0
    for c in sorted(charges, key=lambda c: (str(c.get("reported_at") or ""), c.get("source_row") or 0)):
        key = (money(c["amount"]), str(c.get("reason") or "").strip().lower())
        delivered, reported = _ts(c.get("delivered_at")), _ts(c.get("reported_at"))
        hours = (reported - delivered).total_seconds() / 3600 if delivered and reported else None
        what = f"error charge {c['amount']}" + (f" ({c['reason']})" if c.get("reason") else "")
        if hours is not None:
            what += f", reported {hours:.0f}h after delivery"
        if key in seen:
            problems.append(f"{c['amount']} charged twice for the same thing")
            what += ": DUPLICATE"
        elif hours is not None and hours >= window:
            problems.append(f"{c['amount']} for a complaint made {hours:.0f}h after delivery (limit {window:.0f}h)")
            what += ": TOO LATE"
        else:
            expected = money(expected + c["amount"])
        seen.add(key)
        evidence.append({"source_file": c["source_file"], "row": c["source_row"], "what": what})
    actual = money(sum(c["amount"] for c in charges))
    gap = money(actual - expected)
    if window_term:
        evidence += _term_evidence(window_term)
    if abs(gap) <= TOLERANCE:
        reason = f"Error charge of {actual} was reported in time and charged once; it is fair, do not dispute."
    else:
        reason = "Error charge should not apply: " + "; ".join(problems) + "."
    return Finding(
        order_ids=[story["order_id"]], channel=story["channel"], check_type="error_charge",
        expected=expected, actual=actual, gap=gap, evidence=evidence, reason=reason,
    )


def check_refund(story: dict[str, Any], terms: dict[str, Any]) -> Finding | None:
    """Edge case 13: refund log says 'initiated' but no money moved, yet the store was charged for it.
    Reported as check_type 'error_charge' (the charge to the store) until CONTRACT.md has a refund type."""
    stuck = _stuck_refunds(story)
    charged = _charges(story)
    hits = [(r, c) for r in stuck for c in charged if abs(c["amount"] - r["amount"]) <= TOLERANCE]
    if not hits:
        return None
    actual = money(sum(c["amount"] for _, c in hits))
    evidence = []
    for r, c in hits:
        evidence.append({"source_file": r["source_file"], "row": r["source_row"],
                         "what": f"refund {r.get('refund_id')} of {r['amount']} status '{r['status']}', "
                                 f"money moved {r.get('money_moved') or 0.0}"})
        evidence.append({"source_file": c["source_file"], "row": c["source_row"], "what": f"store charged {c['amount']}"})
    return Finding(
        order_ids=[story["order_id"]], channel=story["channel"], check_type="error_charge",
        expected=0.0, actual=actual, gap=actual, evidence=evidence,
        reason=f"Store was charged {actual} for a refund the log shows only as 'initiated'; no money ever "
               f"reached the customer.",
    )


# deposit checker -------------------------------------------------------------
def check_deposit(story: dict[str, Any], terms: dict[str, Any] | None = None) -> Finding | None:
    """Edge cases 5 (one deposit covers two weeks: totals agree -> 'correct' finding, no dispute)
    and 6 (statement total != bank deposit -> gap). order_ids carries the payout_ids involved."""
    expected, actual = story["statement_total"], story["deposit_amount"]
    gap = money(expected - actual)  # positive = the bank got less than the statements promised
    pids = story["payout_ids"]
    if abs(gap) <= TOLERANCE and len(pids) == 1:
        return None
    dep = story.get("deposit")
    evidence = [{"source_file": dep["source_file"], "row": dep["source_row"],
                 "what": f"bank deposit {dep['deposit_id']} of {actual} on {dep['deposit_date']}"}] if dep else []
    for pid in pids:
        lines = [ln for ln in story["lines"] if ln.get("payout_id") == pid]
        evidence.append({"source_file": lines[0]["source_file"], "row": lines[0]["source_row"],
                         "what": f"payout {pid} statement net {money(sum(ln['net'] for ln in lines))} "
                                 f"({len(lines)} lines)"})
    if dep is None:
        reason = f"Payout {pids[0]} promised {expected}, but no bank deposit for it was found."
    elif abs(gap) <= TOLERANCE:
        reason = f"One deposit of {actual} covers {len(pids)} payouts ({', '.join(pids)}); totals agree."
    else:
        reason = f"Statement(s) {', '.join(pids)} total {expected}, but the bank received {actual}."
    return Finding(
        order_ids=list(pids), channel=story["channel"], check_type="deposit",
        expected=expected, actual=actual, gap=gap, evidence=evidence, reason=reason,
    )


ORDER_CHECKERS = [check_commission, check_promo, check_tax, check_fee, check_error_charge, check_refund]
DEPOSIT_CHECKERS = [check_deposit]
CHECKERS = ORDER_CHECKERS + DEPOSIT_CHECKERS


def run_all(stories: list[dict[str, Any]], terms_for) -> list[Finding]:
    """terms_for(story) -> dict of effective terms for that story's channel/store/date."""
    out = []
    for story in stories:
        is_deposit = story.get("kind") == "deposit"
        terms = {} if is_deposit else terms_for(story)
        for chk in DEPOSIT_CHECKERS if is_deposit else ORDER_CHECKERS:
            try:
                f = chk(story, terms)
            except (KeyError, IndexError, StopIteration):
                f = None  # missing data for this rule; matcher/deal keeper should have flagged it
            if f:
                out.append(f)
    return out
