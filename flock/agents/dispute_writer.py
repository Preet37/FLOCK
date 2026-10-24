"""Dispute Writer (owner: Preet). Drafts the dispute message, citing the evidence. The draft is not sent:
a human approves, then Clicky files it. This is the "five seconds on top", not the product.
"""
from __future__ import annotations

import json
from typing import Any

from flock import llm
from flock.models import Actor

ACTOR = Actor(id="dispute_writer", role="agent")

PLATFORM_NAME = {"doordash": "DoorDash", "ubereats": "Uber Eats", "grubhub": "Grubhub", "direct_app": "delivery partner"}


def draft(f: dict[str, Any]) -> str:
    text = llm.ask_text(
        "Write a short, polite, factual dispute to a delivery platform's merchant support on behalf of a restaurant. "
        "Use only the facts given. Cite each piece of evidence. Under 120 words. No em dashes.",
        json.dumps({k: f[k] for k in ("channel", "check_type", "order_ids", "expected", "actual", "gap", "evidence", "reason")}),
    )
    if text:
        return text
    cites = "; ".join(
        e.get("quote") or f"{e.get('source_file')} row {e.get('row')}" for e in f.get("evidence", [])
    )
    return (
        f"Hello {PLATFORM_NAME.get(f['channel'], f['channel'])} merchant support,\n\n"
        f"We're disputing a {f['check_type'].replace('_', ' ')} on order(s) {', '.join(f['order_ids'])}. "
        f"Expected ${f['expected']:.2f}, charged ${f['actual']:.2f}, a difference of ${abs(f['gap']):.2f}. "
        f"{f['reason']}\n\nEvidence: {cites}.\n\nPlease credit the difference on our next payout. Thank you."
    )
