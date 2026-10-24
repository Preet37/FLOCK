"""Formatter (owner: Maja). The translator: every messy file becomes the standard shape. No deciding, no deleting.

IN:  data/raw/<source>/*  (csv, xlsx; contracts/ and emails/ are the Deal Keeper's)
OUT: data/std/orders.csv, data/std/payout_lines.csv, data/std/bank_deposits.csv  (columns exactly as CONTRACT.md)
     every row keeps source_file + source_row (source_row = the row number you'd see in Excel, header = its own row)
     payout_lines.csv = CONTRACT.md columns, then extras the checkers read: promo_total, promo_code, placed_at
     sidecars (not in CONTRACT.md yet, proposed): error_charges.csv (delivered_at/complaint_at for the 72h rule),
     refunds.csv (direct app refund log), payout_summaries.csv (Grubhub summary sheet),
     duplicates_removed.csv (what cleaning dropped, and the row it duplicated)
     DoorDash error charges stay as their own payout lines (gross 0, net = -charge) AND appear in error_charges.csv

  python -m flock.agents.formatter              # unknown layouts: shows the proposed mapping, asks y/N
  python -m flock.agents.formatter --confirm    # accept proposed mappings without asking (scripted demo)

How a file is read:
  1. Find the header row (first 10 rows) and match it against the layouts in mappings.json.
  2. Unknown layout: ask the LLM to propose a mapping (llm.ask_json). Offline, a header-name heuristic proposes it.
     A human confirms once, the mapping is saved to mappings.json, and next time it's automatic. (Demo moment!)
  3. Standardize: "$12.50" / "(3.75)" / "1,234.56" -> numbers, all dates -> ISO, channel names lowercase,
     store names -> store ids (S01). Deductions are stored as positive numbers: net = gross - deductions.
  4. Cleaning = remove ONLY exact duplicate rows (edge case 1). Never delete a disagreement.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from flock import llm
from flock.models import CHANNELS, money
from flock.store import ROOT

RAW = ROOT / "data" / "raw"
STD = ROOT / "data" / "std"
MAPPINGS = ROOT / "mappings.json"

ORDER_COLS = ["order_id", "store_id", "channel", "placed_at", "subtotal", "tax", "promo_code", "source_file", "source_row"]
PAYOUT_COLS = ["payout_line_id", "channel", "store_id", "platform_order_id", "order_id", "gross", "commission",
               "promo_merchant_funded", "error_charge", "tax_withheld_by_platform", "other_fees", "net",
               "payout_id", "payout_date", "source_file", "source_row",
               # extras the checkers read (after the CONTRACT.md columns; proposed for CONTRACT.md):
               "promo_total", "promo_code", "placed_at"]
DEPOSIT_COLS = ["deposit_id", "channel", "amount", "deposit_date", "memo", "source_file", "source_row"]
# Sidecars: extra detail the contract tables have no column for (proposed CONTRACT.md additions)
ERROR_COLS = ["error_charge_id", "channel", "store_id", "platform_order_id", "order_id", "amount", "delivered_at",
              "complaint_at", "reason", "payout_id", "source_file", "source_row"]
REFUND_COLS = ["refund_id", "channel", "order_id", "amount", "status", "initiated_at", "settled_at",
               "source_file", "source_row"]
SUMMARY_COLS = ["payout_id", "channel", "period_start", "period_end", "order_count", "gross", "net", "payout_date",
                "source_file", "source_row"]

TABLES = {"orders": ORDER_COLS, "payout_lines": PAYOUT_COLS, "bank_deposits": DEPOSIT_COLS,
          "refunds": REFUND_COLS, "payout_summaries": SUMMARY_COLS}
EXTRA_FIELDS = {"payout_lines": ["reason", "delivered_at", "complaint_at"]}   # mapped, then routed to error_charges
MONEY = {"subtotal", "tax", "gross", "promo_total", "commission", "promo_merchant_funded", "error_charge", "tax_withheld_by_platform",
         "other_fees", "net", "amount"}
DEDUCTIONS = ["commission", "promo_merchant_funded", "error_charge", "tax_withheld_by_platform", "other_fees"]
DATETIMES = {"placed_at", "delivered_at", "complaint_at", "initiated_at", "settled_at"}
DATES = {"payout_date", "deposit_date", "period_start", "period_end"}
DATA_SOURCES = ["pos", "doordash", "ubereats", "grubhub", "direct_app", "bank", "unknown"]

CHANNEL_WORDS = [("doordash", r"door\s*dash"), ("ubereats", r"uber"), ("grubhub", r"grub\s*hub"),
                 ("direct_app", r"direct|payproc|online|\bapp\b|website")]
DATE_FORMATS = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %I:%M %p",
                "%m/%d/%Y %H:%M", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%b %d, %Y"]


# ---------------------------------------------------------------------------------------------------------------
# Standardizing single values
# ---------------------------------------------------------------------------------------------------------------

def parse_money(v: Any) -> float | None:
    """'$12.50' -> 12.5, '($3.75)' -> -3.75, '-$3.75' -> -3.75, '1,234.56' -> 1234.56, '' -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return money(v)
    s = str(v).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")") or "-" in s
    digits = re.sub(r"[^0-9.]", "", s)
    if not digits or digits == ".":
        return None
    x = float(digits)
    return money(-x if neg else x)


def parse_when(v: Any, fmt: str | None = None) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime.combine(v, datetime.min.time())
    if isinstance(v, (int, float)):  # Excel serial date
        return datetime(1899, 12, 30) + timedelta(days=float(v))
    s = str(v).strip()
    for f in ([fmt] if fmt else []) + DATE_FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def normalize_channel(v: Any) -> str:
    s = str(v or "").strip().lower()
    if s in CHANNELS:
        return s
    for ch, pat in CHANNEL_WORDS:
        if re.search(pat, s):
            return ch
    return s.replace(" ", "")


def normalize_store(v: Any) -> str:
    s = str(v or "").strip()
    m = re.search(r"\bS\d{2,}\b", s)
    return m.group(0) if m else s


def text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


# ---------------------------------------------------------------------------------------------------------------
# Reading raw files into (sheet, rows) where rows are lists of cells with their 1-based row number
# ---------------------------------------------------------------------------------------------------------------

def read_sheets(path: Path) -> list[tuple[str | None, list[tuple[int, list[Any]]]]]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(path, data_only=True, read_only=True)
        out = []
        for ws in wb.worksheets:
            rows = [(i, list(r)) for i, r in enumerate(ws.iter_rows(values_only=True), start=1)]
            out.append((ws.title, rows))
        wb.close()
        return out
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [(None, [(i, r) for i, r in enumerate(csv.reader(fh), start=1)])]


def cells(row: list[Any]) -> list[str]:
    return [text(c) for c in row]


def find_layout(rows: list[tuple[int, list[Any]]], mappings: dict) -> tuple[str | None, int | None]:
    """Returns (layout_name, header_index_in_rows). Best = most mapped columns present."""
    best = (None, None, 0)
    for idx, (_, row) in enumerate(rows[:10]):
        header = set(cells(row))
        for name, m in mappings.items():
            cols = set(m["columns"])
            if cols and cols <= header and len(cols) > best[2]:
                best = (name, idx, len(cols))
    return best[0], best[1]


def guess_header(rows: list[tuple[int, list[Any]]]) -> int | None:
    """For unknown layouts: first row in the top 10 with 3+ non-empty, non-numeric text cells."""
    for idx, (_, row) in enumerate(rows[:10]):
        vals = [c for c in cells(row) if c]
        if len(vals) >= 3 and all(parse_money(c) is None or re.search(r"[a-zA-Z]", c) for c in vals):
            return idx
    return None


# ---------------------------------------------------------------------------------------------------------------
# Applying a mapping
# ---------------------------------------------------------------------------------------------------------------

def standardize_row(raw: dict[str, Any], m: dict) -> dict[str, Any]:
    rec: dict[str, Any] = dict(m.get("constants", {}))
    for col, field in m["columns"].items():
        rec[field] = raw.get(col)
    dates = m.get("dates", {})
    for f in list(rec):
        if f in MONEY:
            x = parse_money(rec[f])
            rec[f] = money(-x) if (x is not None and f in m.get("negate", [])) else x
        elif f in DATETIMES:
            d = parse_when(rec[f], dates.get(f))
            rec[f] = d.isoformat(timespec="seconds") if d else ""
        elif f in DATES:
            d = parse_when(rec[f], dates.get(f))
            rec[f] = d.date().isoformat() if d else ""
        else:
            rec[f] = text(rec[f])
    if m.get("channel_from"):
        rec["channel"] = normalize_channel(raw.get(m["channel_from"]))
    if "channel" in rec:
        rec["channel"] = normalize_channel(rec["channel"])
    if "store_id" in rec:
        rec["store_id"] = normalize_store(rec["store_id"])
    return rec


def apply_mapping(path_label: str, rows: list[tuple[int, list[Any]]], header_idx: int, m: dict) -> list[dict]:
    header = cells(rows[header_idx][1])
    out = []
    for rownum, row in rows[header_idx + 1:]:
        if not any(text(c) for c in row):
            continue
        raw = {h: (row[i] if i < len(row) else None) for i, h in enumerate(header) if h}
        rec = standardize_row(raw, m)
        rec["source_file"], rec["source_row"] = path_label, rownum
        if m["table"] == "payout_lines" and not rec.get("payout_line_id"):
            # platform gives no line id: one line per order per platform, so channel + platform order id is unique
            rec["payout_line_id"] = (f"{rec.get('channel')}:{rec['platform_order_id']}" if rec.get("platform_order_id")
                                     else f"{path_label}:{rownum}")
        rec["_raw"] = tuple(text(c) for c in row)
        out.append(rec)
    return out


# ---------------------------------------------------------------------------------------------------------------
# Unknown layouts: propose (LLM or heuristic) -> human confirms -> saved to mappings.json
# ---------------------------------------------------------------------------------------------------------------

FIELD_HINTS: dict[str, dict[str, list[tuple[str, int]]]] = {
    "payout_lines": {
        "payout_line_id": [(r"transaction id|line id|settlement line", 3)],
        "platform_order_id": [(r"order (uuid|reference|ref\b|number|#|no)|platform order", 3)],
        "order_id": [(r"\bpos\b|check|merchant (order )?ref", 3)],
        "store_id": [(r"store|restaurant|location", 2)],
        "gross": [(r"gross|subtotal|food sales", 3), (r"sales", 1)],
        "commission": [(r"commission", 3), (r"service fee", 1)],
        "promo_merchant_funded": [(r"promo|promotion|discount", 3)],
        "promo_total": [(r"promo(tion)? (total|value)|total promo|customer discount", 4)],
        "promo_code": [(r"promo(tion)? code|coupon|^promotion$", 5)],
        "placed_at": [(r"order(ed)? (date|time|at|on)|placed|date/time", 3)],
        "error_charge": [(r"error|adjust", 3)],
        "tax_withheld_by_platform": [(r"tax", 3)],
        "other_fees": [(r"fees?\b", 2), (r"charge", 1)],
        "net": [(r"\bnet\b|total payout|payout amount", 3)],
        "payout_id": [(r"payout (id|ref)|payout reference|batch", 3), (r"payout$", 1)],
        "payout_date": [(r"paid|payout date|settle", 3)],
    },
    "orders": {
        "order_id": [(r"order (id|#|no|number)|check|^id$", 3)],
        "store_id": [(r"store|restaurant|location", 2)],
        "placed_at": [(r"placed|opened|created|order (date|time)|date|time", 2)],
        "subtotal": [(r"subtotal|net sales|items total|food", 3), (r"sales|amount", 1)],
        "tax": [(r"tax", 3)],
        "promo_code": [(r"promo|coupon|discount code", 3)],
        "channel": [(r"channel|source|platform", 2)],
    },
    "bank_deposits": {
        "deposit_id": [(r"reference|ref\b|transaction id|id$", 2)],
        "amount": [(r"amount|credit|deposit", 3)],
        "deposit_date": [(r"posting|posted|date", 3)],
        "memo": [(r"description|memo|details", 3)],
    },
}


def heuristic_mapping(header: list[str]) -> dict:
    """Offline fallback: score every header against field name hints, per table; best table wins."""
    best: dict | None = None
    for table, hints in FIELD_HINTS.items():
        pairs = []
        for h in header:
            norm = re.sub(r"[_\-]+", " ", h.lower()).strip()
            for field, pats in hints.items():
                score = max((w for p, w in pats if re.search(p, norm)), default=0)
                if score:
                    pairs.append((score, h, field))
        cols, used = {}, set()
        for score, h, field in sorted(pairs, key=lambda p: (-p[0], header.index(p[1]))):
            if h not in cols and field not in used:
                cols[h], _ = field, used.add(field)
        total = sum(s for s, h, f in pairs if cols.get(h) == f)
        if best is None or total > best["_score"]:
            best = {"table": table, "columns": cols, "_score": total}
    best.pop("_score")
    return best


def llm_mapping(header: list[str], sample: list[list[str]], source: str) -> dict | None:
    fields = {t: [c for c in cols if c not in ("source_file", "source_row")] + EXTRA_FIELDS.get(t, [])
              for t, cols in TABLES.items() if t in FIELD_HINTS}
    system = ("You map columns of restaurant delivery-platform reports to a standard schema. Only map a column when "
              "you are confident; leave others out. Do not compute anything.")
    user = (f"Source folder: {source}\nHeaders: {json.dumps(header)}\nSample rows: {json.dumps(sample[:3])}\n"
            f"Target tables and fields: {json.dumps(fields)}\n"
            'Return {"table": "<one table>", "columns": {"<header>": "<field>"}}.')
    got = llm.ask_json(system, user)
    if not got or got.get("table") not in fields or not isinstance(got.get("columns"), dict):
        return None
    allowed = set(fields[got["table"]])
    cols = {h: f for h, f in got["columns"].items() if h in header and f in allowed}
    return {"table": got["table"], "columns": cols} if cols else None


def propose_mapping(header: list[str], data_rows: list[list[Any]], source: str) -> dict:
    sample = [cells(r) for r in data_rows[:5]]
    m = llm_mapping(header, sample, source)
    how = "llm"
    if m is None:
        m, how = heuristic_mapping(header), "heuristic"
    if source in CHANNELS and m["table"] == "payout_lines":
        m["constants"] = {"channel": source}
    elif m["table"] == "bank_deposits" and "memo" in m["columns"].values():
        m["channel_from"] = next(h for h, f in m["columns"].items() if f == "memo")
    # sign convention: if a deduction column is mostly negative in this file, flip it
    negate = []
    for h, f in m["columns"].items():
        if f in DEDUCTIONS:
            i = header.index(h)
            vals = [parse_money(r[i]) for r in data_rows if i < len(r)]
            vals = [v for v in vals if v]
            if vals and sum(v < 0 for v in vals) > len(vals) / 2:
                negate.append(f)
    if negate:
        m["negate"] = negate
    m["proposed_by"] = how
    return m


def show_mapping(label: str, m: dict) -> None:
    print(f"\n[formatter] Unknown layout in {label}. Proposed mapping ({m['proposed_by']}) -> table {m['table']}:")
    for h, f in m["columns"].items():
        print(f"    {h!r:<40} -> {f}{'  (sign flipped)' if f in m.get('negate', []) else ''}")
    for k, v in m.get("constants", {}).items():
        print(f"    {'(constant)':<40} -> {k} = {v}")


def ask_human(label: str, m: dict, confirm: bool, interactive: bool) -> bool:
    if not confirm and not (interactive and sys.stdin.isatty()):
        print(f"[formatter] Unknown layout in {label}: skipped until a human confirms a mapping. "
              "Run `python -m flock.agents.formatter` to review it.")
        return False
    show_mapping(label, m)
    if confirm:
        print("    confirmed (--confirm)")
        return True
    return input("    Save this mapping and use it from now on? [y/N] ").strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------------------------------------------

def load_mappings(path: Path = MAPPINGS) -> dict:
    return json.loads(Path(path).read_text()) if Path(path).exists() else {}


def save_mappings(mappings: dict, path: Path = MAPPINGS) -> None:
    Path(path).write_text(json.dumps(mappings, indent=2) + "\n")


def label_for(path: Path, sheet: str | None, raw: Path) -> str:
    try:
        rel = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        rel = path.relative_to(raw).as_posix()
    return f"{rel}#{sheet}" if sheet else rel


def raw_files(raw: Path) -> list[Path]:
    out = []
    for src in DATA_SOURCES:
        d = raw / src
        if d.is_dir():
            out += sorted(p for p in d.iterdir() if p.suffix.lower() in (".csv", ".xlsx", ".xlsm"))
    return out


def dedupe(recs: list[dict], table: str, dropped: list[dict]) -> list[dict]:
    """Edge case 1: drop rows that are byte-for-byte the same as an earlier row. Everything else stays."""
    seen: dict[tuple, dict] = {}
    keep = []
    for r in recs:
        k = tuple((c, r.get(c)) for c in TABLES[table] if c not in ("source_file", "source_row")) + (("_raw", r["_raw"]),)
        if k in seen:
            first = seen[k]
            dropped.append({"table": table, "source_file": r["source_file"], "source_row": r["source_row"],
                            "duplicate_of": f"{first['source_file']}:{first['source_row']}"})
            continue
        seen[k] = r
        keep.append(r)
    return keep


def write_table(path: Path, cols: list[str], recs: list[dict]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(cols)
        for r in recs:
            w.writerow(["" if r.get(c) is None else (f"{r[c]:.2f}" if c in MONEY and isinstance(r[c], float) else r[c])
                        for c in cols])


def format_all(raw: Path | None = None, out: Path | None = None, mappings_path: Path | None = None,
               confirm: bool = False, interactive: bool = False, verbose: bool = False) -> dict[str, Path]:
    """Read every raw data file, write the standard tables, return {table: path} for those that exist.

    With nothing in data/raw/ this just returns whatever is already in data/std/ (so the pipeline keeps
    running on fixtures until real data shows up).
    """
    raw, out = Path(raw or RAW), Path(out or STD)  # resolved at call time so tests can repoint RAW/STD
    mappings_path = mappings_path or MAPPINGS
    out.mkdir(parents=True, exist_ok=True)
    files = raw_files(raw)
    if files:
        mappings = load_mappings(mappings_path)
        recs: dict[str, list[dict]] = {t: [] for t in TABLES}
        for path in files:
            source = path.parent.name
            for sheet, rows in read_sheets(path):
                label = label_for(path, sheet, raw)
                name, hidx = find_layout(rows, mappings)
                if name is None:
                    hidx = guess_header(rows)
                    if hidx is None:
                        if verbose:
                            print(f"[formatter] skipped {label}: no table found")
                        continue
                    header = cells(rows[hidx][1])
                    proposal = propose_mapping(header, [r for _, r in rows[hidx + 1:]], source)
                    if not ask_human(label, proposal, confirm, interactive):
                        continue
                    prefix = f"{source}_layout_"
                    name = f"{prefix}{sum(1 for k in mappings if k.startswith(prefix)) + 1}"
                    mappings[name] = {k: v for k, v in proposal.items() if k != "proposed_by"}
                    mappings[name]["confirmed_by"] = "human"
                    save_mappings(mappings, mappings_path)
                    print(f"    saved as '{name}' in {Path(mappings_path).name}; next time it's automatic")
                m = mappings[name]
                got = apply_mapping(label, rows, hidx, m)
                recs[m["table"]] += got
                if verbose:
                    print(f"[formatter] {label}: layout '{name}' -> {len(got)} {m['table']} rows")
        dropped: list[dict] = []
        for t in TABLES:
            recs[t] = dedupe(recs[t], t, dropped)
        errors = [dict(r, amount=r["error_charge"], error_charge_id=r["payout_line_id"])
                  for r in recs["payout_lines"] if r.get("error_charge")]
        for t, cols in TABLES.items():
            write_table(out / f"{t}.csv", cols, recs[t])
        write_table(out / "error_charges.csv", ERROR_COLS, errors)
        write_table(out / "duplicates_removed.csv", ["table", "source_file", "source_row", "duplicate_of"], dropped)
        if verbose:
            counts = ", ".join(f"{t} {len(recs[t])}" for t in TABLES)
            print(f"[formatter] wrote {out}: {counts}, error_charges {len(errors)}; "
                  f"removed {len(dropped)} exact duplicate rows")
    found = {}
    for name in ("orders", "payout_lines", "bank_deposits"):
        p = out / f"{name}.csv"
        if p.exists():
            found[name] = p
    return found


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Standardize data/raw/* into data/std/*.csv")
    ap.add_argument("--confirm", action="store_true", help="accept proposed mappings for unknown layouts")
    ap.add_argument("--raw", default=str(RAW))
    ap.add_argument("--out", default=str(STD))
    ap.add_argument("--mappings", default=str(MAPPINGS))
    a = ap.parse_args()
    format_all(Path(a.raw), Path(a.out), Path(a.mappings), confirm=a.confirm, interactive=True, verbose=True)
