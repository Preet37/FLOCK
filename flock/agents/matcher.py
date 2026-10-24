"""Matcher (owner: Thabhelo). The detective's corkboard: pins every record about one order together.

IN:  data/std/orders.csv, payout_lines.csv, bank_deposits.csv  (CONTRACT.md shapes)
     optional: data/std/error_charges.csv, refunds.csv  (shapes below; proposed CONTRACT.md additions)
OUT: list of stories, two kinds:
     order story:   {"kind": "order", "order_id", "store_id", "channel", "placed_at", "pos": {...},
                     "payout_lines": [...], "error_charges": [...], "refunds": [...],
                     "payout_ids": [...], "deposit_id", "match_confidence"}
     deposit story: {"kind": "deposit", "deposit_id", "channel", "deposit": {...} | None,
                     "payout_ids": [...], "lines": [...], "statement_total", "deposit_amount", "match_confidence"}
     plus findings for anything that won't link (check_type="missing_order")

Linking, in order:
  1. Exact ID: payout line order_id (or platform_order_id) == POS order_id.             confidence 1.0
  2. Fallback: same store + channel, amount within tolerance, placed_at within a window -> confidence < 1.
     Adjustment lines (e.g. a separate error-charge line, gross 0) follow their platform_order_id.
  3. Payout lines -> deposits: sum net per payout_id, then link to a bank deposit by
     memo mention > exact amount (one payout, or 2-3 payouts summed: edge case 5) > nearest date (edge case 6).
  4. POS order with no payout line -> missing_order (edge case 3). Payout line with no POS order -> missing_order (edge case 4).

Optional tables (all money columns are dollars, timestamps ISO):
  error_charges.csv: error_charge_id, channel, store_id, platform_order_id, order_id, amount, reason,
                     delivered_at, reported_at, payout_id, source_file, source_row
  refunds.csv:       refund_id, channel, store_id, order_id, amount, status (initiated|completed|failed),
                     money_moved (dollars actually returned to the customer; blank = 0), source_file, source_row
Optional payout line columns: placed_at (sharpens fallback matching), promo_code, promo_total (full promo cost
before the split; needed by check_promo).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

from flock.models import CHANNELS, Finding, money

TOLERANCE = 0.05            # dollars; same as checkers
AMOUNT_TOLERANCE = 0.10     # fraction of the amount allowed for fallback order matching (min TOLERANCE)
TIME_WINDOW = timedelta(hours=2)          # when the payout line carries its own placed_at
PAYOUT_LOOKBACK = timedelta(days=14)      # otherwise: order placed within 14 days before the payout date
DEPOSIT_LAG = timedelta(days=7)           # a deposit lands within a week of its payout date
DEPOSIT_SPAN = timedelta(days=21)         # a lump deposit can cover payouts up to 3 weeks old (edge case 5)
MIN_CONFIDENCE = 0.5

MONEY_COLS = {"subtotal", "tax", "gross", "commission", "promo_merchant_funded", "error_charge",
              "tax_withheld_by_platform", "other_fees", "net", "amount", "promo_total", "money_moved"}


# loading ---------------------------------------------------------------
def _clean(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in row.items():
        if isinstance(v, str):
            v = v.strip()
        if k in MONEY_COLS:
            v = money(v) if v not in (None, "") else 0.0
        elif k == "source_row":
            v = int(v) if v not in (None, "") else None
        elif v == "":
            v = None
        out[k] = v
    return out


def load_tables(tables: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """tables: name -> Path to a CSV, or name -> list of row dicts (tests). Missing tables are empty."""
    out = {}
    for name in ("orders", "payout_lines", "bank_deposits", "error_charges", "refunds"):
        src = tables.get(name)
        if src is None:
            rows = []
        elif isinstance(src, (str, Path)):
            with open(src, newline="") as f:
                rows = list(csv.DictReader(f))
        else:
            rows = list(src)
        out[name] = [_clean(r) for r in rows]
    return out


def parse_ts(s: Any) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _ev(row: dict[str, Any], what: str) -> dict[str, Any]:
    return {"source_file": row.get("source_file"), "row": row.get("source_row"), "what": what}


# orders <-> payout lines -------------------------------------------------
def _fallback(line: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    """Best POS order for a payout line with no usable ID. Returns (order, confidence)."""
    gross = line["gross"]
    tol = max(TOLERANCE, abs(gross) * AMOUNT_TOLERANCE)
    line_ts, payout_ts = parse_ts(line.get("placed_at")), parse_ts(line.get("payout_date"))
    scored = []
    for o in candidates:
        if o["store_id"] != line["store_id"] or o["channel"] != line["channel"]:
            continue
        diff = abs(o["subtotal"] - gross)
        if diff > tol:
            continue
        ts = parse_ts(o["placed_at"])
        if line_ts and ts:
            gap = abs(ts - line_ts)
            if gap > TIME_WINDOW:
                continue
            time_score = 1 - 0.5 * gap / TIME_WINDOW
        elif payout_ts and ts:
            if not (payout_ts - PAYOUT_LOOKBACK <= ts <= payout_ts + timedelta(days=1)):
                continue
            time_score = 0.7  # only know it's the right week
        else:
            time_score = 0.5
        amount_score = 1.0 if diff <= TOLERANCE else 1 - 0.5 * diff / tol
        scored.append((round(0.95 * amount_score * time_score, 2), o))
    if not scored:
        return None, 0.0
    scored.sort(key=lambda x: -x[0])
    conf, best = scored[0]
    if len(scored) > 1 and scored[1][0] >= conf - 0.05:
        conf = round(conf * 0.6, 2)  # ambiguous: two orders look the same
    return (best, conf) if conf >= MIN_CONFIDENCE else (None, conf)


def _link_lines(orders: list[dict], lines: list[dict]) -> tuple[dict[str, list[dict]], dict[str, float], list[dict]]:
    by_id = {o["order_id"]: o for o in orders}
    linked: dict[str, list[dict]] = defaultdict(list)
    conf: dict[str, float] = {}
    plat_to_order: dict[tuple, str] = {}
    pending = []
    for ln in lines:  # 1. exact IDs
        oid = ln.get("order_id") if ln.get("order_id") in by_id else (
            ln.get("platform_order_id") if ln.get("platform_order_id") in by_id else None)
        if oid:
            linked[oid].append(ln)
            conf[oid] = 1.0
            plat_to_order[(ln["channel"], ln.get("platform_order_id"))] = oid
        else:
            pending.append(ln)
    unmatched = []
    by_slot: dict[tuple, list[dict]] = defaultdict(list)  # fallback candidates: same store + channel only
    if pending:
        for o in orders:
            by_slot[(o["store_id"], o["channel"])].append(o)
    for ln in sorted(pending, key=lambda x: -abs(x["gross"])):  # 2. fallback; real lines before adjustments
        key = (ln["channel"], ln.get("platform_order_id"))
        if ln.get("platform_order_id") and key in plat_to_order:
            linked[plat_to_order[key]].append(ln)
            continue
        free = [o for o in by_slot.get((ln["store_id"], ln["channel"]), ()) if o["order_id"] not in linked]
        best, c = _fallback(ln, free) if ln["gross"] else (None, 0.0)
        if best:
            linked[best["order_id"]].append(ln)
            conf[best["order_id"]] = c
            if ln.get("platform_order_id"):
                plat_to_order[key] = best["order_id"]
        else:
            unmatched.append(ln)
    return linked, conf, unmatched


# payouts <-> deposits ------------------------------------------------------
class PayoutTotals:
    """Running per-payout totals: (channel, payout_id) -> net in cents, line count, first line. Small enough to
    build in one pass over a year of payout lines without keeping the lines (see flock/scan.py)."""

    def __init__(self) -> None:
        self.groups: dict[tuple, dict[str, Any]] = {}

    def add(self, ln: dict[str, Any], keep_line: bool = False) -> None:
        key = (ln["channel"], ln.get("payout_id"))
        g = self.groups.get(key)
        if g is None:
            g = self.groups[key] = {"channel": ln["channel"], "payout_id": ln.get("payout_id"),
                                    "payout_date": ln.get("payout_date"), "lines": [], "cents": 0, "count": 0,
                                    "first": ln}
        if keep_line:
            g["lines"].append(ln)
        g["cents"] += round(ln["net"] * 100)
        g["count"] += 1

    def result(self) -> dict[tuple, dict[str, Any]]:
        for g in self.groups.values():
            g["total"] = money(g["cents"] / 100)
        return self.groups


def _payout_groups(lines: list[dict]) -> dict[tuple, dict[str, Any]]:
    totals = PayoutTotals()
    for ln in lines:
        totals.add(ln, keep_line=True)
    return totals.result()


def reconcile_deposits(lines: list[dict], deposits: list[dict],
                       groups: dict[tuple, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Group payout lines by payout_id and link each group to a bank deposit. Returns deposit stories.
    groups: pre-aggregated PayoutTotals.result() instead of lines (year-scale runs)."""
    groups = _payout_groups(lines) if groups is None else groups
    free = dict(groups)
    stories = []

    def take(dep, keys, confidence):
        gs = [free.pop(k) for k in keys]
        total = money(sum(g["total"] for g in gs))
        stories.append({"kind": "deposit", "deposit_id": dep["deposit_id"] if dep else None,
                        "channel": dep["channel"] if dep else gs[0]["channel"], "deposit": dep,
                        "payout_ids": [g["payout_id"] for g in gs], "lines": [ln for g in gs for ln in g["lines"]],
                        "payouts": {g["payout_id"]: {"first": g["first"], "count": g["count"], "total": g["total"]}
                                    for g in gs},
                        "statement_total": total, "deposit_amount": dep["amount"] if dep else 0.0,
                        "match_confidence": confidence})

    def in_range(g, dep, span):
        p, d = parse_ts(g["payout_date"]), parse_ts(dep["deposit_date"])
        return p is None or d is None or (d - span <= p <= d + timedelta(days=1))

    pending = []
    for dep in deposits:  # memo names the payout(s)
        keys = [k for k in free if k[0] == dep["channel"] and k[1] and k[1] in (dep.get("memo") or "")]
        if keys:
            take(dep, keys, 1.0)
        else:
            pending.append(dep)
    still = []
    for dep in pending:  # exact amount: one payout, then 2-3 payouts summed (edge case 5)
        cands = [k for k in free if k[0] == dep["channel"] and in_range(free[k], dep, DEPOSIT_SPAN)]
        hit = None
        for n in (1, 2, 3):
            for combo in combinations(cands, n):
                if abs(sum(free[k]["total"] for k in combo) - dep["amount"]) <= TOLERANCE:
                    hit = combo
                    break
            if hit:
                break
        if hit:
            take(dep, list(hit), 0.95 if len(hit) == 1 else 0.85)
        else:
            still.append(dep)
    for dep in still:  # nearest payout by date: amounts disagree (edge case 6)
        cands = [k for k in free if k[0] == dep["channel"] and in_range(free[k], dep, DEPOSIT_LAG)]
        if cands:
            d = parse_ts(dep["deposit_date"])
            k = min(cands, key=lambda k: abs((d - (parse_ts(free[k]["payout_date"]) or d)).days))
            take(dep, [k], 0.7)
    for k in list(free):  # payout that never reached the bank
        take(None, [k], 0.0)
    return stories


# the corkboard -------------------------------------------------------------
def searched_statements(lines: list[dict]) -> dict[str, set]:
    """Payout files searched per channel: the second source for "sold, never paid"."""
    statements: dict[str, set] = defaultdict(set)
    for ln in lines:
        if ln.get("source_file"):
            statements[ln["channel"]].add(ln["source_file"])
    return statements


def match(tables: dict) -> tuple[list[dict[str, Any]], list[Finding]]:
    """Returns (stories, findings). Stories = order stories + deposit stories (see module docstring)."""
    t = load_tables(tables)
    orders, lines = t["orders"], t["payout_lines"]
    if not orders and not lines:
        return [], []
    deposit_stories = reconcile_deposits(lines, t["bank_deposits"])
    deposit_of = {(ds["channel"], pid): ds["deposit_id"] for ds in deposit_stories for pid in ds["payout_ids"]}
    stories, findings = match_orders(t, deposit_of, searched_statements(lines))
    return stories + deposit_stories, findings


def match_orders(t: dict[str, list[dict]], deposit_of: dict[tuple, str] | None = None,
                 statements: dict[str, set] | None = None) -> tuple[list[dict[str, Any]], list[Finding]]:
    """Order stories + missing_order findings for loaded tables (load_tables). Works on any slice of the data
    that keeps each store's orders and payout lines together (flock/scan.py runs it once per store)."""
    orders, lines = t["orders"], t["payout_lines"]
    deposit_of = deposit_of or {}
    statements = searched_statements(lines) if statements is None else statements
    linked, conf, unmatched = _link_lines(orders, lines)

    plat_to_order = {(ln["channel"], ln.get("platform_order_id")): oid for oid, lns in linked.items() for ln in lns}
    errors, refunds = defaultdict(list), defaultdict(list)
    for e in t["error_charges"]:
        oid = e.get("order_id") or plat_to_order.get((e.get("channel"), e.get("platform_order_id")))
        if oid:
            errors[oid].append(e)
    for r in t["refunds"]:
        if r.get("order_id"):
            refunds[r["order_id"]].append(r)

    stories, findings = [], []
    for o in orders:
        lns = linked.get(o["order_id"])
        if lns:
            pids = sorted({ln.get("payout_id") for ln in lns if ln.get("payout_id")})
            dep_ids = sorted({deposit_of.get((o["channel"], p)) for p in pids} - {None})
            stories.append({"kind": "order", "order_id": o["order_id"], "store_id": o["store_id"],
                            "channel": o["channel"], "placed_at": o["placed_at"], "pos": o,
                            "payout_lines": lns, "error_charges": errors.get(o["order_id"], []),
                            "refunds": refunds.get(o["order_id"], []), "payout_ids": pids,
                            "deposit_id": dep_ids[0] if dep_ids else None,
                            "match_confidence": conf.get(o["order_id"], 1.0)})
        elif o["channel"] in CHANNELS:  # edge case 3: sold, never paid
            findings.append(Finding(
                order_ids=[o["order_id"]], channel=o["channel"], check_type="missing_order",
                expected=o["subtotal"], actual=0.0, gap=o["subtotal"],
                evidence=[_ev(o, f"{o['channel']} order {o['order_id']} for {o['subtotal']} in the register")]
                + [{"source_file": sf, "what": f"no payout line for {o['order_id']} in this statement"}
                   for sf in sorted(statements.get(o["channel"], ()))],
                reason=f"Register shows a {o['subtotal']} {o['channel']} order that is in no payout: sold, never paid.",
            ))

    by_plat: dict[tuple, list[dict]] = defaultdict(list)
    for ln in unmatched:
        by_plat[(ln["channel"], ln.get("platform_order_id") or ln.get("payout_line_id"))].append(ln)
    for (channel, pid), lns in by_plat.items():  # edge case 4: paid (or charged) for an order we never rang up
        deductions = money(sum(ln["commission"] + ln["promo_merchant_funded"] + ln["error_charge"]
                               + ln["tax_withheld_by_platform"] + ln["other_fees"] for ln in lns))
        findings.append(Finding(
            order_ids=sorted({lns[0].get("order_id"), pid} - {None, ""}), channel=channel, check_type="missing_order",
            expected=0.0, actual=deductions, gap=deductions,
            evidence=[_ev(ln, f"payout line {ln.get('payout_line_id')} gross {ln['gross']}, net {ln['net']}") for ln in lns],
            reason=f"{channel} paid out order {pid} (gross {money(sum(ln['gross'] for ln in lns))}) and took "
                   f"{deductions} in charges, but the register has no matching order.",
        ))
    return stories, findings
