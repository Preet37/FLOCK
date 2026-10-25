"""Transactions: one row per payout per store (platform x store x payout_id), for the frontend's transactions table.

Built once per real-data pipeline run and saved as <state dir>/transactions.json, so GET /api/transactions only pages
through a file (it never re-matches anything). Year scale is ~12k rows (56 stores x 4 channels x 52 weeks).

  rows      payout row: channel, store_id, payout_id, payout_date, gross, deductions (commission, promo, error charges,
            tax withheld, other fees, total), net, the bank deposit it landed in (id, full amount, this store's share,
            date, the payouts it covers, match confidence), deposit_gap, deposit_status, finding_ids, source file + rows.
            Plus one "order" row per sold-never-paid order (a register order in no payout at all).
  data      volume of the run: stores, days, weeks, store_weeks, orders, payout lines, deposits, period. GET /api/metrics
            prices human time from store_weeks.

How the pieces are found (plain Python, cents, no model):
  - payout totals: one streaming pass over std/payout_lines.csv, summed per (channel, payout_id, store_id).
  - bank deposit: the Matcher's deposit stories (matcher.reconcile_deposits) link each (channel, payout_id) to a
    deposit. A platform deposit covers every store, so it is split across the stores' rows by statement net (exact
    to the cent, largest remainder), the same split the Journal Entry Drafter uses. deposit_status = matched when the
    store's share equals its net within $0.05, short / over when it doesn't, missing_deposit when no deposit was found.
  - finding_ids: order-level findings by the order ids on the row's payout lines; deposit findings by payout_id.

status is decided when the API reads the file, from the findings' current state (a human may have approved since):
  sold_never_paid > missing_deposit > short / over > disputed > needs_review > recovered > matched
  disputed      a linked finding is money we expect back (journal.is_open: recoverable, approved or filed, not rejected)
  needs_review  a linked finding is a conflict waiting for a human
  recovered     every open linked finding was paid back (state RECOVERED)
`flags` lists every status that applies, not just the first.
"""
from __future__ import annotations

import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flock.agents import journal
from flock.agents.matcher import parse_ts
from flock.models import money

FILE_NAME = "transactions.json"
TOLERANCE_CENTS = 5
DEDUCTION_COLS = [("commission", "commission"), ("promo_merchant_funded", "promo"), ("error_charge", "error_charges"),
                  ("tax_withheld_by_platform", "tax_withheld"), ("other_fees", "other_fees")]
STATUS_ORDER = ["sold_never_paid", "missing_deposit", "short", "over", "disputed", "needs_review", "recovered", "matched"]
MAX_LIMIT = 500


def _cents(v: Any) -> int:
    try:
        return round(float(v or 0) * 100)
    except ValueError:
        return 0


def _dollars(c: int) -> float:
    return money(c / 100)


def _day(v: Any) -> str | None:
    ts = parse_ts(v)
    return ts.date().isoformat() if ts else None


def _rows_of(src: Any):
    """Rows from a CSV path (streamed) or a list of dicts (tests)."""
    if src is None:
        return
    if isinstance(src, (str, Path)):
        if not Path(src).exists():
            return
        with Path(src).open(newline="") as fh:
            yield from csv.DictReader(fh)
    else:
        yield from src


def build(tables: dict[str, Any], deposit_stories: list[dict[str, Any]], findings: list[dict[str, Any]],
          source: str = "") -> dict[str, Any]:
    """The transactions document for one run. tables: std table name -> CSV path (or rows)."""
    want = {o for f in findings for o in f.get("order_ids") or []}
    groups: dict[tuple, dict[str, Any]] = {}
    where: dict[tuple, tuple] = {}            # (channel, order / platform id) -> group key, only for ids in findings
    n_lines = 0
    for ln in _rows_of(tables.get("payout_lines")):
        n_lines += 1
        key = (ln.get("channel"), ln.get("payout_id") or None, ln.get("store_id") or "UNKNOWN")
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"payout_date": ln.get("payout_date"), "gross": 0, "net": 0, "lines": 0, "files": {},
                               **{name: 0 for _, name in DEDUCTION_COLS}}
        g["gross"] += _cents(ln.get("gross"))
        g["net"] += _cents(ln.get("net"))
        for col, name in DEDUCTION_COLS:
            g[name] += _cents(ln.get(col))
        g["lines"] += 1
        sf = ln.get("source_file")
        if sf:
            try:
                row = int(ln.get("source_row") or 0) or None
            except ValueError:
                row = None
            span = g["files"].setdefault(sf, [row, row])
            if row is not None:
                span[0] = row if span[0] is None else min(span[0], row)
                span[1] = row if span[1] is None else max(span[1], row)
        for k in ("order_id", "platform_order_id", "payout_line_id"):
            if ln.get(k) and ln[k] in want:
                where[(ln.get("channel"), ln[k])] = key

    stores: set[str] = set()
    first = last = None
    n_orders = 0
    orders_wanted: dict[str, dict[str, Any]] = {}
    for o in _rows_of(tables.get("orders")):
        n_orders += 1
        if o.get("store_id"):
            stores.add(o["store_id"])
        ts = parse_ts(o.get("placed_at"))
        if ts:
            first = ts if first is None or ts < first else first
            last = ts if last is None or ts > last else last
        if o.get("order_id") in want:
            orders_wanted[o["order_id"]] = o

    # bank deposits: split each deposit across the (payout, store) rows it covers, by statement net
    deposit_of: dict[tuple, dict[str, Any]] = {}
    share: dict[tuple, int] = {}
    for ds in deposit_stories:
        keys = sorted((k for k in groups if k[0] == ds["channel"] and k[1] in ds["payout_ids"]),
                      key=lambda k: (str(k[1]), str(k[2])))
        for pid in ds["payout_ids"]:
            deposit_of[(ds["channel"], pid)] = ds
        if ds.get("deposit") and keys:
            for k, c in zip(keys, journal.allocate(_cents(ds["deposit_amount"]), [groups[k]["net"] for k in keys])):
                share[k] = c

    linked: dict[tuple, list[str]] = {k: [] for k in groups}
    unpaid: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for f in sorted(findings, key=lambda x: x["finding_id"]):
        ids = f.get("order_ids") or []
        if f.get("check_type") == "deposit":
            for k in groups:
                if k[0] == f["channel"] and k[1] in ids:
                    linked[k].append(f["finding_id"])
            continue
        if f.get("check_type") == "missing_order" and money(f.get("expected") or 0) > 0 \
                and money(f.get("actual") or 0) == 0 and ids and ids[0] in orders_wanted:
            unpaid.append((f, orders_wanted[ids[0]]))
            continue
        for k in {where[(f["channel"], i)] for i in ids if (f["channel"], i) in where}:
            linked[k].append(f["finding_id"])

    rows = []
    for k in sorted(groups, key=lambda k: (str(k[0]), str(k[1]), str(k[2]))):
        channel, pid, store = k
        g = groups[k]
        ds = deposit_of.get((channel, pid))
        dep = (ds or {}).get("deposit")
        if dep:
            gap = share.get(k, 0) - g["net"]
            status = "matched" if abs(gap) <= TOLERANCE_CENTS else ("short" if gap < 0 else "over")
            deposit = {"deposit_id": dep.get("deposit_id"), "amount": money(dep.get("amount") or 0),
                       "store_share": _dollars(share.get(k, 0)), "date": _day(dep.get("deposit_date")),
                       "covers_payouts": list(ds["payout_ids"]), "match_confidence": ds.get("match_confidence"),
                       "source_file": dep.get("source_file"), "source_row": dep.get("source_row")}
        else:
            gap, status, deposit = -g["net"], "missing_deposit", None
        ded = {name: _dollars(g[name]) for _, name in DEDUCTION_COLS}
        ded["total"] = _dollars(sum(g[name] for _, name in DEDUCTION_COLS))
        rows.append({
            "txn_id": f"{channel}:{pid}:{store}", "kind": "payout", "channel": channel,
            "platform": journal.CHANNEL_NAME.get(channel, channel), "store_id": store, "payout_id": pid,
            "payout_date": _day(g["payout_date"]), "gross": _dollars(g["gross"]), "deductions": ded,
            "net": _dollars(g["net"]), "deposit": deposit, "deposit_gap": _dollars(gap), "deposit_status": status,
            "finding_ids": linked[k],
            "source": {"lines": g["lines"], "files": [{"file": f, "first_row": a, "last_row": b}
                                                      for f, (a, b) in sorted(g["files"].items())]},
        })
    for f, o in unpaid:
        sub = _cents(o.get("subtotal"))
        rows.append({
            "txn_id": f"order:{o['order_id']}", "kind": "order", "channel": o.get("channel"),
            "platform": journal.CHANNEL_NAME.get(o.get("channel"), o.get("channel")), "store_id": o.get("store_id"),
            "payout_id": None, "payout_date": None, "placed_at": o.get("placed_at"), "order_id": o["order_id"],
            "gross": _dollars(sub), "deductions": {**{name: 0.0 for _, name in DEDUCTION_COLS}, "total": 0.0},
            "net": 0.0, "deposit": None, "deposit_gap": _dollars(-sub), "deposit_status": "missing_payout",
            "finding_ids": [f["finding_id"]],
            "source": {"lines": 1, "files": [{"file": o.get("source_file"), "first_row": _int(o.get("source_row")),
                                              "last_row": _int(o.get("source_row"))}]},
        })

    days = (last.date() - first.date()).days + 1 if first and last else None
    weeks = math.ceil(days / 7) if days else None
    data = {"stores": len(stores) or len({k[2] for k in groups}) or None, "days": days, "weeks": weeks,
            "store_weeks": (len(stores) * weeks) if stores and weeks else None,
            "orders": n_orders, "payout_lines": n_lines, "deposits": sum(1 for d in deposit_stories if d.get("deposit")),
            "period_start": first.date().isoformat() if first else None,
            "period_end": last.date().isoformat() if last else None}
    return {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source, "data": data,
            "rows": rows}


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def empty(source: str, note: str) -> dict[str, Any]:
    return {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source, "data": None,
            "rows": [], "note": note}


def save(doc: dict[str, Any], state_dir: Path) -> Path:
    path = Path(state_dir) / FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    tmp.replace(path)
    return path


_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def load(state_dir: Path) -> dict[str, Any] | None:
    """The saved document (cached by mtime), or None before any run wrote one."""
    path = Path(state_dir) / FILE_NAME
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        doc = json.loads(path.read_text())
    except ValueError:
        return hit[1] if hit else None   # mid-write: the previous version
    _cache[str(path)] = (mtime, doc)
    return doc


# status (read time) --------------------------------------------------------------------------------------------------
def flags(row: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[str]:
    fs = [by_id[i] for i in row.get("finding_ids", []) if i in by_id]
    out = []
    if row["kind"] == "order":
        out.append("sold_never_paid")
    if row["deposit_status"] in ("missing_deposit", "short", "over"):
        out.append(row["deposit_status"])
    open_ = [f for f in fs if journal.is_open(f)]
    if any(f["state"] != "RECOVERED" for f in open_):
        out.append("disputed")
    if any(journal.is_pending(f) for f in fs):
        out.append("needs_review")
    if open_ and all(f["state"] == "RECOVERED" for f in open_):
        out.append("recovered")
    return out or ["matched"]


def with_status(row: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fl = flags(row, by_id)
    return {**row, "status": min(fl, key=STATUS_ORDER.index), "flags": fl}


def query(doc: dict[str, Any] | None, findings: list[dict[str, Any]], channel: str | None = None,
          store: str | None = None, status: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """One page of rows (newest payout first) plus summary counts over every row and over the filtered rows."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    if not doc:
        return {"total": 0, "limit": limit, "offset": offset, "rows": [], "summary": _summary([]), "data": None,
                "source": None, "note": "no pipeline run has written transactions yet (POST /api/run)"}
    by_id = {f["finding_id"]: f for f in findings}
    rows = [with_status(r, by_id) for r in doc["rows"]]
    picked = [r for r in rows if (not channel or r["channel"] == channel) and (not store or r["store_id"] == store)
              and (not status or status in r["flags"])]
    picked.sort(key=lambda r: (r.get("payout_date") or (r.get("placed_at") or "")[:10], r["store_id"] or "",
                               r["channel"] or ""), reverse=True)
    out = {"total": len(picked), "limit": limit, "offset": offset, "rows": picked[offset:offset + limit],
           "summary": _summary(rows), "filtered_summary": _summary(picked), "data": doc.get("data"),
           "source": doc.get("source"), "built_at": doc.get("built_at")}
    if doc.get("note"):
        out["note"] = doc["note"]
    return out


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    payouts = [r for r in rows if r["kind"] == "payout"]
    by_status = Counter(r["status"] for r in rows)
    return {
        "rows": len(rows), "payouts": len(payouts), "unpaid_orders": len(rows) - len(payouts),
        "by_status": {s: by_status.get(s, 0) for s in STATUS_ORDER},
        "by_channel": dict(sorted(Counter(r["channel"] for r in rows).items())),
        "stores": len({r["store_id"] for r in rows}),
        "gross": money(sum(r["gross"] for r in payouts)),
        "deductions": money(sum(r["deductions"]["total"] for r in payouts)),
        "net": money(sum(r["net"] for r in payouts)),
        "deposited": money(sum((r["deposit"] or {}).get("store_share") or 0 for r in payouts)),
        "with_findings": sum(1 for r in rows if r["finding_ids"]),
    }
