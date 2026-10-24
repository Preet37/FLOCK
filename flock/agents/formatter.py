"""Formatter (owner: Maja). The translator: every messy file becomes the standard shape. No deciding, no deleting.

IN:  data/raw/<source>/*  (csv, xlsx, ...)
OUT: data/std/orders.csv, data/std/payout_lines.csv, data/std/bank_deposits.csv  (columns in CONTRACT.md)
     every row keeps source_file + source_row

TODO (Maja):
  1. For each known source, a column map in mappings.json, e.g. {"doordash": {"Subtotal": "gross", ...}}
  2. For an unknown layout: ask the LLM to propose a mapping (llm.ask_json), show it to a human once,
     save the confirmed mapping to mappings.json. Next time it's automatic. (Demo moment!)
  3. Standardize: "$12.50" -> 12.50, all dates -> ISO, channel names lowercase.
  4. Cleaning = remove ONLY exact duplicate rows (edge case 1). Never delete a disagreement.
"""
from __future__ import annotations

import json
from pathlib import Path

from flock.store import ROOT

STD = ROOT / "data" / "std"
MAPPINGS = ROOT / "mappings.json"

ORDER_COLS = ["order_id", "store_id", "channel", "placed_at", "subtotal", "tax", "promo_code", "source_file", "source_row"]
PAYOUT_COLS = ["payout_line_id", "channel", "store_id", "platform_order_id", "order_id", "gross", "commission",
               "promo_merchant_funded", "error_charge", "tax_withheld_by_platform", "other_fees", "net",
               "payout_id", "payout_date", "source_file", "source_row"]
DEPOSIT_COLS = ["deposit_id", "channel", "amount", "deposit_date", "memo", "source_file", "source_row"]


def load_mappings() -> dict:
    return json.loads(MAPPINGS.read_text()) if MAPPINGS.exists() else {}


def format_all() -> dict[str, Path]:
    """Returns paths of standardized tables that exist. Stub: returns whatever is already in data/std/."""
    STD.mkdir(parents=True, exist_ok=True)
    found = {}
    for name in ("orders", "payout_lines", "bank_deposits"):
        p = STD / f"{name}.csv"
        if p.exists():
            found[name] = p
    return found
