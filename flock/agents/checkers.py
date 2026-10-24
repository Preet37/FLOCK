"""Checkers (owner: Thabhelo). The calculators. Plain Python only: NO LLM does money math.

IN:  order stories (from matcher) + deal terms (from deal_keeper)
OUT: findings (CONTRACT.md "Finding"), state NEW, each with expected, actual, gap and evidence

One checker per rule. They run independently (in parallel across orders = the "flock").
Edge cases each should catch are listed in FLOCK_CONTEXT.md section 13.
"""
from __future__ import annotations

from typing import Any

from flock.models import Finding, money

TOLERANCE = 0.05  # dollars; penny rounding is ignored (edge case 17)


def check_commission(story: dict[str, Any], terms: dict[str, Any]) -> Finding | None:
    """Edge case 7. Example of the pattern every checker should follow."""
    line = story["payout_lines"][0]
    rate = terms["commission"]["value"]
    expected = money(line["gross"] * rate)
    actual = money(line["commission"])
    gap = money(actual - expected)  # positive = platform took too much
    if abs(gap) <= TOLERANCE:
        return None
    return Finding(
        order_ids=[story["order_id"]], channel=story["channel"], check_type="commission",
        expected=expected, actual=actual, gap=gap,
        evidence=[
            {"source_file": line["source_file"], "row": line["source_row"], "what": f"commission charged {actual}"},
            {"source_file": terms["commission"]["source_file"], "quote": terms["commission"]["quote"],
             "authority": terms["commission"]["authority"]},
        ],
        reason=f"Charged {actual} commission; deal says {rate:.0%} of {line['gross']} = {expected}.",
    )


def check_promo(story, terms):          # TODO edge cases 8, 9
    return None


def check_tax(story, terms):            # TODO edge case 12
    return None


def check_error_charge(story, terms):   # TODO edge cases 10, 11, 13, 16 (72h rule, duplicates, legit charge)
    return None


def check_deposit(story, terms):        # TODO edge cases 5, 6
    return None


CHECKERS = [check_commission, check_promo, check_tax, check_error_charge, check_deposit]


def run_all(stories: list[dict[str, Any]], terms_for) -> list[Finding]:
    """terms_for(story) -> dict of effective terms for that story's channel/store/date."""
    out = []
    for story in stories:
        terms = terms_for(story)
        for chk in CHECKERS:
            try:
                f = chk(story, terms)
            except (KeyError, IndexError):
                f = None  # missing data for this rule; matcher/deal keeper should have flagged it
            if f:
                out.append(f)
    return out
