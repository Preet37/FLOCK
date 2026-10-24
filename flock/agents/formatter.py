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
from itertools import chain, islice
from pathlib import Path
from typing import Any

from flock import llm
from flock.models import CHANNELS, money
from flock.progress import Progress, ensure, progress_path
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


# ---------------------------------------------------------------------------------------------------------------
# Fast path: the same per-value rules as standardize_row(), applied a column at a time to chunks of rows
# (each distinct value is converted once; high-cardinality timestamps are parsed vectorized with pandas,
# anything pandas can't parse falls back to parse_when). Rows stream to disk, so a year of orders fits in memory.
# ---------------------------------------------------------------------------------------------------------------

CHUNK_ROWS = 100_000
VECTOR_DATES = 2_000   # distinct timestamps above this are parsed with pandas instead of strptime one by one


def _mapped(col: list, fn) -> list:
    """fn applied to each distinct value once (ids, money and dates repeat a lot)."""
    table = {u: fn(u) for u in dict.fromkeys(col)}
    return [table[v] for v in col]


def _money_fn(flip: bool):
    def f(v: Any) -> float | None:
        x = parse_money(v)
        return money(-x) if (x is not None and flip) else x
    return f


def _when_fn(fmt: str | None, iso: bool):
    def f(v: Any) -> str:
        d = parse_when(v, fmt)
        if not d:
            return ""
        return d.isoformat(timespec="seconds") if iso else d.date().isoformat()
    return f


def _dates(col: list, fmt: str | None, iso: bool) -> list:
    fn = _when_fn(fmt, iso)
    uniq = list(dict.fromkeys(col))
    strs = [u for u in uniq if isinstance(u, str) and u.strip()]
    table: dict[Any, str] = {}
    if fmt and len(strs) > VECTOR_DATES:
        import pandas as pd

        parsed = pd.to_datetime(pd.Series([s.strip() for s in strs], dtype=object), format=fmt, errors="coerce")
        text_out = parsed.dt.strftime("%Y-%m-%dT%H:%M:%S" if iso else "%Y-%m-%d")
        for s, ok, out in zip(strs, parsed.notna().tolist(), text_out.tolist()):
            if ok:
                table[s] = out
    for u in uniq:
        if u not in table:
            table[u] = fn(u)
    return [table[v] for v in col]


def standardize_columns(header: list[str], rows: list[list[Any]], m: dict) -> dict[str, list]:
    """Column-wise standardize_row(): {field: [values]} for one chunk of data rows (same results, much faster)."""
    n = len(rows)
    width = len(header)
    index = {h: i for i, h in enumerate(header) if h}   # last duplicate header wins, like the dict in apply_mapping
    cols = list(zip(*[r[:width] if len(r) >= width else list(r) + [None] * (width - len(r)) for r in rows])) \
        if n else [() for _ in header]
    out: dict[str, list] = {k: [v] * n for k, v in m.get("constants", {}).items()}
    dates = m.get("dates", {})
    for col_name, field in m["columns"].items():
        i = index.get(col_name)
        out[field] = list(cols[i]) if i is not None and i < len(cols) else [None] * n
    for f in list(out):
        col = out[f]
        if f in MONEY:
            out[f] = _mapped(col, _money_fn(f in m.get("negate", [])))
        elif f in DATETIMES:
            out[f] = _dates(col, dates.get(f), iso=True)
        elif f in DATES:
            out[f] = _dates(col, dates.get(f), iso=False)
        else:
            out[f] = _mapped(col, text)
    if m.get("channel_from"):
        i = index.get(m["channel_from"])
        out["channel"] = _mapped(list(cols[i]) if i is not None else [None] * n, normalize_channel)
    if "channel" in out:
        out["channel"] = _mapped(out["channel"], normalize_channel)
    if "store_id" in out:
        out["store_id"] = _mapped(out["store_id"], normalize_store)
    return out


def _nonempty(row: list[Any]) -> bool:
    for c in row:
        if c is not None and c != "" and (not isinstance(c, str) or c.strip()):
            return True
    return False


def iter_sheets(path: Path):
    """Yields (sheet, head, rest): head = first 10 (rownum, row) pairs, rest = an iterator over every later pair.
    Streams: a CSV or xlsx is never loaded whole."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(path, data_only=True, read_only=True)
        try:
            for ws in wb.worksheets:
                it = ((i, list(r)) for i, r in enumerate(ws.iter_rows(values_only=True), start=1))
                head = list(islice(it, 10))
                yield ws.title, head, it
        finally:
            wb.close()
        return
    with path.open(newline="", encoding="utf-8-sig") as fh:
        it = enumerate(csv.reader(fh), start=1)
        head = list(islice(it, 10))
        yield None, head, it


def count_rows(path: Path) -> int:
    """Cheap row estimate for the progress bar (newlines, or the sheet dimensions for xlsx)."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        return 0  # sizing a sheet means parsing it; the total grows as its rows are read
    n = 0
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            n += block.count(b"\n")
    return n


class _Out:
    """Every std table open for writing (to .tmp files, renamed at the end), plus streaming dedupe."""

    def __init__(self, out: Path):
        self.out = out
        self.cols = {**TABLES, "error_charges": ERROR_COLS,
                     "duplicates_removed": ["table", "source_file", "source_row", "duplicate_of"]}
        self.fh = {t: (out / f"{t}.csv.tmp").open("w", newline="") for t in self.cols}
        self.w = {t: csv.writer(fh, lineterminator="\n") for t, fh in self.fh.items()}
        for t, cols in self.cols.items():
            self.w[t].writerow(cols)
        self.seen: dict[str, dict[int, str]] = {t: {} for t in TABLES}
        self.counts = {t: 0 for t in self.cols}

    def write(self, table: str, label: str, rownums: list[int], recs: dict[str, list], raws: list[tuple]) -> int:
        """Drop exact duplicates (edge case 1: same standardized values AND same raw cells), write the rest."""
        n = len(rownums)
        recs["source_file"], recs["source_row"] = [label] * n, rownums
        if table == "payout_lines":
            ids, ch, pid = recs.get("payout_line_id"), recs.get("channel") or [None] * n, recs.get("platform_order_id")
            if ids is None or not all(ids):
                # platform gives no line id: one line per order per platform, so channel + platform order id is unique
                ids = ids or [""] * n
                pid = pid or [""] * n
                recs["payout_line_id"] = [x if x else (f"{c}:{p}" if p else f"{label}:{r}")
                                          for x, c, p, r in zip(ids, ch, pid, rownums)]
        key_cols = [recs.get(c, [None] * n) for c in TABLES[table] if c not in ("source_file", "source_row")]
        seen, keep, dropped = self.seen[table], [], []
        for i, k in enumerate(zip(*key_cols, raws)):
            h = hash(k)
            if h in seen:
                dropped.append([table, label, rownums[i], seen[h]])
            else:
                seen[h] = f"{label}:{rownums[i]}"
                keep.append(i)
        if dropped:
            self.w["duplicates_removed"].writerows(dropped)
            self.counts["duplicates_removed"] += len(dropped)
        if len(keep) < n:
            recs = {f: [v[i] for i in keep] for f, v in recs.items()}
        self._rows(table, recs)
        if table == "payout_lines" and "error_charge" in recs:
            idx = [i for i, x in enumerate(recs["error_charge"]) if x]
            if idx:
                err = {f: [v[i] for i in idx] for f, v in recs.items()}
                err["amount"], err["error_charge_id"] = err["error_charge"], err["payout_line_id"]
                self._rows("error_charges", err)
        return len(keep)

    def _rows(self, table: str, recs: dict[str, list]) -> None:
        cols = self.cols[table]
        n = len(recs["source_row"])
        out = []
        for c in cols:
            v = recs.get(c)
            if v is None:
                out.append([""] * n)
            elif c in MONEY:
                out.append(["" if x is None else (f"{x:.2f}" if isinstance(x, float) else x) for x in v])
            else:
                out.append(["" if x is None else x for x in v])
        self.w[table].writerows(zip(*out))
        self.counts[table] += n

    def close(self, ok: bool = True) -> None:
        for t, fh in self.fh.items():
            fh.close()
            tmp = self.out / f"{t}.csv.tmp"
            if ok:
                tmp.replace(self.out / f"{t}.csv")
            else:
                tmp.unlink(missing_ok=True)


def _chunks(rest, hidx_rownum: int, size: int = CHUNK_ROWS):
    """(rownums, rows) chunks of non-empty data rows after the header row."""
    nums, rows = [], []
    for rownum, row in rest:
        if rownum <= hidx_rownum or not _nonempty(row):
            continue
        nums.append(rownum)
        rows.append(row)
        if len(rows) >= size:
            yield nums, rows
            nums, rows = [], []
    if rows:
        yield nums, rows


STAMP = ".formatted"   # in data/std/: what the tables were last built from
PENDING = "pending_layouts.json"   # in data/std/: files skipped until a human confirms their layout


def pending_layouts(std: Path | None = None) -> list[dict[str, str]]:
    """Files the last run skipped because nobody has confirmed their layout yet: [{"file", "source"}]."""
    p = Path(std or STD) / PENDING
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return []


def _stamp(files: list[Path], mappings_path: Path) -> str:
    """Fingerprint of the inputs: every raw file's name, size and mtime, plus the column maps."""
    import hashlib

    h = hashlib.sha256()
    for f in files:
        st = f.stat()
        h.update(f"{f}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    mp = Path(mappings_path)
    h.update(mp.read_bytes() if mp.exists() else b"")
    return h.hexdigest()


def format_all(raw: Path | None = None, out: Path | None = None, mappings_path: Path | None = None,
               confirm: bool = False, interactive: bool = False, verbose: bool = False,
               progress: Progress | None = None) -> dict[str, Path]:
    """Read every raw data file, write the standard tables, return {table: path} for those that exist.

    With nothing in data/raw/ this just returns whatever is already in data/std/ (so the pipeline keeps
    running on fixtures until real data shows up). progress (flock.progress.Progress) gets one "format" stage.
    If nothing changed since the last complete run (same raw files, same mappings), the tables are reused:
    the pipeline formats again on every run, and a year of data takes a while to read.
    """
    raw, out = Path(raw or RAW), Path(out or STD)  # resolved at call time so tests can repoint RAW/STD
    mappings_path = mappings_path or MAPPINGS
    progress = ensure(progress)
    out.mkdir(parents=True, exist_ok=True)
    files = raw_files(raw)
    stamp = _stamp(files, mappings_path) if files else ""
    if files and (out / STAMP).exists() and (out / STAMP).read_text() == stamp \
            and all((out / f"{t}.csv").exists() for t in TABLES):
        if verbose:
            print(f"[formatter] {out}: inputs unchanged since the last run, tables reused")
        files = []
    if files:
        (out / STAMP).unlink(missing_ok=True)
        complete = True
        pending: list[dict[str, str]] = []
        mappings = load_mappings(mappings_path)
        progress.stage("format", total=sum(count_rows(p) for p in files))
        sink = _Out(out)
        done = 0
        try:
            for path in files:
                source = path.parent.name
                for sheet, head, rest in iter_sheets(path):
                    label = label_for(path, sheet, raw)
                    name, hidx = find_layout(head, mappings)
                    first = None
                    if name is None:
                        hidx = guess_header(head)
                        if hidx is None:
                            if verbose:
                                print(f"[formatter] skipped {label}: no table found")
                            complete = False
                            continue
                        header = cells(head[hidx][1])
                        chunks = _chunks(chain(head, rest), head[hidx][0])
                        first = next(chunks, ([], []))
                        proposal = propose_mapping(header, first[1], source)
                        if not ask_human(label, proposal, confirm, interactive):
                            complete = False
                            pending.append({"file": label, "source": source})
                            continue
                        prefix = f"{source}_layout_"
                        name = f"{prefix}{sum(1 for k in mappings if k.startswith(prefix)) + 1}"
                        mappings[name] = {k: v for k, v in proposal.items() if k != "proposed_by"}
                        mappings[name]["confirmed_by"] = "human"
                        save_mappings(mappings, mappings_path)
                        print(f"    saved as '{name}' in {Path(mappings_path).name}; next time it's automatic")
                        chunks = chain([first], chunks)
                    else:
                        chunks = _chunks(chain(head, rest), head[hidx][0])
                    m = mappings[name]
                    header = cells(head[hidx][1])
                    got = 0
                    for rownums, rows in chunks:
                        if not rows:
                            continue
                        recs = standardize_columns(header, rows, m)
                        if sheet is None:  # csv: cells are already strings
                            raws = [tuple(r) for r in rows]
                        else:
                            raws = [tuple(text(c) for c in r) for r in rows]
                        sink.write(m["table"], label, rownums, recs, raws)
                        got += len(rows)
                        done += len(rows)
                        progress.update(done, total=max(progress.total or 0, done))
                    if verbose:
                        print(f"[formatter] {label}: layout '{name}' -> {got} {m['table']} rows")
        except BaseException:
            sink.close(ok=False)
            raise
        sink.close()
        progress.update(done)
        (out / PENDING).write_text(json.dumps(pending, indent=2) + "\n")
        if complete:  # a skipped layout means a later run (after a human says yes) must read everything again
            (out / STAMP).write_text(_stamp(files, mappings_path))
        if verbose:
            counts = ", ".join(f"{t} {sink.counts[t]}" for t in TABLES)
            print(f"[formatter] wrote {out}: {counts}, error_charges {sink.counts['error_charges']}; "
                  f"removed {sink.counts['duplicates_removed']} exact duplicate rows")
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
    format_all(Path(a.raw), Path(a.out), Path(a.mappings), confirm=a.confirm, interactive=True, verbose=True,
               progress=Progress(progress_path()))
