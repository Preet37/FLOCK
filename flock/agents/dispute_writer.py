"""Dispute Writer (owner: Preet). Drafts the dispute message, citing the evidence. The draft is not sent:
a human approves, then Clicky files it. This is the "five seconds on top", not the product.

Every draft, from Claude or the template, cites every evidence item and has no em dashes.
If Claude's draft leaves a source out, the full evidence list is appended.
No dollar figure in a draft comes from a model: if a model's draft states a $ amount that isn't one of the finding's
code-computed numbers (expected, actual, gap) or a number quoted in its evidence, the draft is thrown away and the
template (numbers straight from code) is used instead.
The finding's `reason` is NOT used: after judging it holds the panel's internal deliberation
("All 3 judges agree ..."), which must never be sent to a platform.
"""
from __future__ import annotations

import json
import re
import sys
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


DOLLARS = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _cents(text: str) -> int | None:
    try:
        return round(float(text.replace(",", "")) * 100)
    except ValueError:
        return None


def foreign_amounts(text: str, f: dict[str, Any]) -> list[str]:
    """$ amounts in `text` that are not the finding's own numbers (expected, actual, |gap|) or numbers in its evidence.
    Empty list = every dollar figure traces back to code or a source."""
    known = {_cents(str(abs(float(f.get(k) or 0)))) for k in ("expected", "actual", "gap")}
    for e in f.get("evidence", []):
        for v in e.values():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                known.add(_cents(str(abs(v))))
            elif isinstance(v, str):
                known.update(_cents(m) for m in NUMBER.findall(v))
    return [m for m in DOLLARS.findall(text) if _cents(m) not in known]


SHOW_ORDERS = 10   # a rolled-up finding (flock/rollup.py) can cover a thousand orders; the message names the payout


def orders_text(f: dict[str, Any]) -> str:
    ids = f["order_ids"]
    if len(ids) <= SHOW_ORDERS:
        return f"order(s) {', '.join(ids)}"
    where = f" in payout {f['rollup']['payout_id']}" if f.get("rollup") else ""
    return (f"{len(ids):,} orders{where} ({', '.join(ids[:SHOW_ORDERS])} and {len(ids) - SHOW_ORDERS:,} more; "
            "the full list is attached to the claim)")


def _brief(f: dict[str, Any]) -> dict[str, Any]:
    """What the writer model sees: the finding's facts, with a long order list cut to its first ids and a count."""
    out = {k: f[k] for k in ("channel", "check_type", "order_ids", "expected", "actual", "gap", "evidence")}
    if len(f["order_ids"]) > SHOW_ORDERS:
        out["order_ids"] = f["order_ids"][:SHOW_ORDERS]
        out["order_count"] = len(f["order_ids"])
        if f.get("rollup"):
            out["payout_id"] = f["rollup"]["payout_id"]
    return out


def template(f: dict[str, Any]) -> str:
    """The dispute without a model: the same facts, the evidence block, no wording to check."""
    return no_em_dashes(
        f"Hello {PLATFORM_NAME.get(f['channel'], f['channel'])} merchant support,\n\n"
        f"We're disputing a {f['check_type'].replace('_', ' ')} on {orders_text(f)}. "
        f"Expected ${f['expected']:.2f}, charged ${f['actual']:.2f}, a difference of ${abs(f['gap']):.2f}, "
        f"based on the evidence below.\n\n{_evidence_block(f)}\n\nPlease credit the difference on our next payout. Thank you."
    )


def draft(f: dict[str, Any], live: bool = True) -> str:
    """live=False: the template, no model call (a run's live-model budget is spent on its biggest disputes)."""
    if not live:
        return template(f)
    text = llm.ask_text(
        "Write a short, polite, factual dispute to a delivery platform's merchant support on behalf of a restaurant. "
        "Use only the facts given. Cite each piece of evidence by its source file. Under 120 words. No em dashes.",
        json.dumps(_brief(f)),
        r=llm.route("writer"),
    )
    if text and (bad := foreign_amounts(text, f)):
        print(f"[dispute_writer] {f.get('finding_id')}: model draft states ${', $'.join(bad)}, which no code or "
              "source computed; using the template", file=sys.stderr)
        text = None
    if text:
        if not _cites_all(text, f):
            text += "\n\n" + _evidence_block(f)
        return no_em_dashes(text)
    return template(f)
