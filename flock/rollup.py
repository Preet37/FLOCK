"""Roll per-order findings up to the lump sum they sit in (owner: Preet). Plain Python, no model.

The Checkers write one finding per order. When the same deal term is broken on many orders in one weekly payout
(DoorDash charging the old commission at three stores for a month is ~1,900 orders), that is one mistake per lump sum,
not 1,900: a person approves it once and the platform gets one claim per payout.

  rolled = rollup(findings, payout_index(tables["payout_lines"]))

Grouped together: findings with the same channel, check_type, payout_id, gap direction and the same deal claims in
the evidence (same quotes, same authority, same conflict flags), for the order-level term checks only (commission,
promo, tax, fee). A group of one stays exactly as it was. Error charges, deposits, missing orders and anomalies
are never grouped: each has its own facts.

The rolled-up finding keeps every order id (the answer key and the journal still see each order), sums expected /
actual / gap in code (money() per order, then summed), lists every source row it came from, and keeps the per-order
gaps in `rollup.parts` so the total can be re-added by anyone. Opt-in: pipeline.run(rollup=True) (flock.showtime).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

from flock.models import Finding, money

GROUPED_CHECKS = ("commission", "promo", "tax", "fee")
MIN_GROUP = 2
PLATFORM = {"doordash": "DoorDash", "ubereats": "Uber Eats", "grubhub": "Grubhub", "direct_app": "direct app"}


def payout_index(payout_lines: Path | str | None) -> dict[str, dict[str, Any]]:
    """order_id -> {payout_id, store_id, gross} from std payout_lines.csv (one streaming pass)."""
    out: dict[str, dict[str, Any]] = {}
    if not payout_lines or not Path(payout_lines).exists():
        return out
    with Path(payout_lines).open(newline="") as fh:
        for row in csv.DictReader(fh):
            oid = row.get("order_id") or ""
            if not oid or float(row.get("gross") or 0) <= 0:   # error-charge lines have gross 0
                continue
            got = out.setdefault(oid, {"payout_id": row.get("payout_id"), "store_id": row.get("store_id"),
                                       "gross": 0.0})
            got["gross"] = money(got["gross"] + float(row.get("gross") or 0))
    return out


def _claims(f: dict[str, Any]) -> tuple:
    return tuple((e.get("source_file"), e.get("quote"), e.get("authority"), bool(e.get("conflict")), e.get("locator"))
                 for e in f.get("evidence", []) if e.get("quote"))


def rollup(findings: list[dict[str, Any]], index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings in, findings out (same order for everything not grouped; each group lands where its first member was)."""
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    keys: list[tuple | None] = []
    for f in findings:
        info = index.get(f["order_ids"][0]) if len(f.get("order_ids") or []) == 1 else None
        if f.get("check_type") not in GROUPED_CHECKS or info is None or not info.get("payout_id"):
            keys.append(None)
            continue
        k = (f["channel"], f["check_type"], info["payout_id"], f["gap"] > 0, _claims(f))
        groups[k].append(f)
        keys.append(k)
    out, placed = [], set()
    for f, k in zip(findings, keys):
        if k is None or len(groups[k]) < MIN_GROUP:
            out.append(f)
        elif k not in placed:
            placed.add(k)
            out.append(merge(groups[k], k[2], index))
    return out


def merge(members: list[dict[str, Any]], payout_id: str, index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    first = members[0]
    ids = [m["order_ids"][0] for m in members]
    expected = money(sum(money(m["expected"]) for m in members))
    actual = money(sum(money(m["actual"]) for m in members))
    gap = money(sum(money(m["gap"]) for m in members))
    gross = money(sum(index[i]["gross"] for i in ids))
    stores = sorted({index[i]["store_id"] for i in ids if index[i].get("store_id")})
    rows: dict[str, list[int]] = defaultdict(list)
    for m in members:
        for e in m.get("evidence", []):
            if not e.get("quote") and e.get("source_file") and e.get("row") is not None:
                rows[e["source_file"]].append(e["row"])
    ct, ch = first["check_type"], first["channel"]
    evidence = [{"source_file": sf, "row": min(rs), "rows": sorted(rs),
                 "what": f"{len(rs)} rows, {ct.replace('_', ' ')} checked line by line: charged {actual:,.2f}, "
                         f"the deal says {expected:,.2f}"} for sf, rs in rows.items()]
    evidence += [e for e in first.get("evidence", []) if e.get("quote")]
    platform = PLATFORM.get(ch, ch)
    where = (f"{len(ids):,} {platform} orders at {len(stores)} store{'s' if len(stores) != 1 else ''} "
             f"({', '.join(stores)}) in payout {payout_id}")
    if ct == "commission" and gross:
        reason = (f"{where}: commission charged {actual:,.2f} on {gross:,.2f} of food sales ({actual / gross:.1%}); "
                  f"the deal says {expected / gross:.1%} = {expected:,.2f}. Gap {gap:,.2f}, every order recomputed "
                  f"in code. One order: {first['reason']}")
    else:
        reason = (f"{where}, all against the same deal term: charged {actual:,.2f}, the deal says {expected:,.2f}, "
                  f"gap {gap:,.2f}. One order: {first['reason']}")
    f = Finding(order_ids=ids, channel=ch, check_type=ct, expected=expected, actual=actual, gap=gap,
                evidence=evidence, reason=reason).to_dict()
    f["rollup"] = {"payout_id": payout_id, "orders": len(ids), "stores": stores, "gross": gross,
                   "parts": {m["order_ids"][0]: money(m["gap"]) for m in members}}
    return f
