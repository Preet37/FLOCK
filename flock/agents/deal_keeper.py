"""Deal Keeper (owner: Thabhelo). The contract manager: what deal was actually in effect, and says so with a quote.

IN:  data/raw/contracts/*, data/raw/emails/*  (ideally read by a local model on the GX10)
OUT: list of deal terms (CONTRACT.md "Deal term"), each with quote + authority

Authority, strongest first: contract > amendment > auth_email > unauth_email > call_note.
Stronger claim wins automatically. Equal strength or unknown authority -> a conflict (goes to a human).
"""
from __future__ import annotations

from typing import Any

from flock.models import AUTHORITY_RANK


def resolve(claims: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Pick the winning claim among claims about the SAME term (same channel, store, term_type, date).

    Returns (winner, conflicting_claims). If conflicting_claims is non-empty, a human must decide.
    This part is plain code and already works; use it.
    """
    if not claims:
        return None, []
    rank = {a: i for i, a in enumerate(AUTHORITY_RANK)}
    ordered = sorted(claims, key=lambda c: rank.get(c.get("authority"), 99))
    best = ordered[0]
    if best.get("authority") not in rank:
        return None, claims
    ties = [c for c in ordered if rank.get(c.get("authority")) == rank[best["authority"]] and c["value"] != best["value"]]
    if ties:
        return None, [best] + ties
    return best, []


def extract_terms() -> list[dict[str, Any]]:
    """TODO (Thabhelo): read contracts + emails with llm.ask_json, return deal terms with exact quotes."""
    return []
