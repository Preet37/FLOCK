"""Matcher (owner: Thabhelo). The detective's corkboard: pins every record about one order together.

IN:  data/std/orders.csv, payout_lines.csv, bank_deposits.csv
OUT: list of "order stories":
     {"order_id", "store_id", "channel", "pos": {...}, "payout_lines": [...], "deposit_id", "match_confidence"}
     plus findings for anything that won't link (check_type="missing_order")

TODO (Thabhelo):
  1. Exact ID match first (order_id on payout line).
  2. Fallback: same store + channel, placed_at within a time window, amount within tolerance -> confidence score.
  3. Payout lines -> deposits: sum of net by payout_id vs deposit amount (edge cases 5 and 6).
  4. POS order with no payout line -> missing_order finding (edge case 3). Payout with no POS order -> finding (edge case 4).
"""
from __future__ import annotations

from typing import Any


def match(tables: dict) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns (order_stories, findings). Stub returns nothing until real data exists."""
    return [], []
