"""Dispute Writer (owner: Preet). Drafts the dispute message, citing the evidence. The draft is not sent:
a human approves, then Clicky files it. This is the "five seconds on top", not the product.

Every draft, from Claude or the template, cites every evidence item and has no em dashes.
If Claude's draft leaves a source out, the full evidence list is appended.
The finding's `reason` is NOT used: after judging it holds the panel's internal deliberation
("All 3 judges agree ..."), which must never be sent to a platform.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from flock import llm
from flock.models import Actor

ACTOR = Actor(id="dispute_writer", role="agent")

PLATFORM_NAME = {"doordash": "DoorDash", "ubereats": "Uber Eats", "grubhub": "Grubhub", "direct_app": "delivery partner"}


def no_em_dashes(text: str) -> str:
    return re.sub(r"\s*—\s*", ", ", text)


def cite(e: dict[str, Any]) -> str:
    src = e.get("source_file") or "unnamed source"
    if e.get("quote"):
        return f'{src}: "{e["quote"]}"'
    where = f"{src} row {e['row']}" if e.get("row") is not None else src
    return f"{where} ({e['what']})" if e.get("what") else where


def _evidence_block(f: dict[str, Any]) -> str:
    lines = [f"{i}. {cite(e)}" for i, e in enumerate(f.get("evidence", []), 1)]
    return "Evidence:\n" + ("\n".join(lines) if lines else "none attached")


def _cites_all(text: str, f: dict[str, Any]) -> bool:
    for e in f.get("evidence", []):
        src = e.get("source_file") or ""
        if src and src not in text and Path(src).name not in text:
            return False
    return True


def draft(f: dict[str, Any]) -> str:
    text = llm.ask_text(
        "Write a short, polite, factual dispute to a delivery platform's merchant support on behalf of a restaurant. "
        "Use only the facts given. Cite each piece of evidence by its source file. Under 120 words. No em dashes.",
        json.dumps({k: f[k] for k in ("channel", "check_type", "order_ids", "expected", "actual", "gap", "evidence")}),
    )
    if text:
        if not _cites_all(text, f):
            text += "\n\n" + _evidence_block(f)
        return no_em_dashes(text)
    return no_em_dashes(
        f"Hello {PLATFORM_NAME.get(f['channel'], f['channel'])} merchant support,\n\n"
        f"We're disputing a {f['check_type'].replace('_', ' ')} on order(s) {', '.join(f['order_ids'])}. "
        f"Expected ${f['expected']:.2f}, charged ${f['actual']:.2f}, a difference of ${abs(f['gap']):.2f}, "
        f"based on the evidence below.\n\n{_evidence_block(f)}\n\nPlease credit the difference on our next payout. Thank you."
    )
