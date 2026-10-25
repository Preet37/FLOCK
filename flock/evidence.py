"""Evidence viewer: open the exact cell a finding points at.

Every finding's evidence carries `source_file` plus either `row` (the row number you'd see in Excel, header = row 1;
see flock/agents/formatter.py) or an exact `quote` (contracts, emails, Slack). resolve() turns one reference into a
small window of the file with the proving row (and column, when we can tell) highlighted; index() lists every
reference a set of findings makes, grouped by file, for the "all evidence" view.

Reads only inside the allowed roots (the repo's data/ and the state dir): a path that escapes them is refused.

API hook (flock/api.py, reads stay open):
    GET /api/evidence?ref=<path:row | json {file,row,column,quote}>  -> evidence.resolve(ref, evidence.roots(self.dir))
    GET /api/evidence/index                                          -> evidence.index(self.store.all())
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
WINDOW = 8           # rows either side of the proving row
MAX_BYTES = 50_000_000
KINDS = {".csv": "csv", ".json": "json", ".eml": "eml", ".md": "md", ".txt": "txt"}


class EvidenceError(ValueError):
    """A reference we won't or can't open (bad shape, outside the roots, missing file)."""


def roots(state_dir: Path | str | None = None) -> list[Path]:
    out = [ROOT / "data"]
    if state_dir:
        out.append(Path(state_dir))
    return [r.resolve() for r in out]


def parse_ref(ref: Any) -> dict[str, Any]:
    """'path:12', 'path', a JSON string, or a dict with file/source_file, row/line, column, quote."""
    if isinstance(ref, str):
        s = ref.strip()
        if s.startswith("{"):
            try:
                ref = json.loads(s)
            except ValueError as e:
                raise EvidenceError(f"ref is not valid JSON: {e}") from None
        else:
            m = re.fullmatch(r"(.+?):(\d+)", s)
            ref = {"file": m.group(1), "row": int(m.group(2))} if m else {"file": s}
    if not isinstance(ref, dict):
        raise EvidenceError("ref must be 'path:row' or an object with a file")
    file = ref.get("file") or ref.get("source_file")
    if not file or not isinstance(file, str):
        raise EvidenceError("ref has no file")
    row = ref.get("row", ref.get("line"))
    try:
        row = int(row) if row not in (None, "") else None
    except (TypeError, ValueError):
        raise EvidenceError("row must be a number") from None
    return {"file": file, "row": row, "column": ref.get("column"), "quote": ref.get("quote"),
            "what": ref.get("what")}


def locate(file: str, allowed: Iterable[Path]) -> Path:
    """The real path for a ref's file, only if it sits inside one of the allowed roots."""
    if "\x00" in file:
        raise EvidenceError("bad path")
    allowed = [Path(r).resolve() for r in allowed]
    p = Path(file)
    candidates = [p] if p.is_absolute() else [ROOT / p] + [r / p for r in allowed]
    for c in candidates:
        real = c.resolve()
        if not any(real == r or r in real.parents for r in allowed):
            continue
        if real.is_file():
            return real
    for c in candidates:   # say why: outside the roots beats "missing"
        real = c.resolve()
        if not any(real == r or r in real.parents for r in allowed):
            raise EvidenceError(f"{file} is outside the evidence folders")
    raise EvidenceError(f"{file} not found")


def _column_for(header: list[str], cells: list[str], what: str | None, column: Any) -> int | None:
    if isinstance(column, int) and 0 <= column < len(header):
        return column
    if isinstance(column, str) and column in header:
        return header.index(column)
    if not what:
        return None
    # the numbers the finding quotes ("commission charged 3.75 on gross 15.00") -> the first cell holding one
    def as_num(c: str) -> float | None:
        t = c.strip().replace("$", "").replace(",", "")
        if t.startswith("(") and t.endswith(")"):
            t = "-" + t[1:-1]
        try:
            return float(t)
        except ValueError:
            return None
    for n in re.findall(r"-?\d[\d,]*(?:\.\d+)?", what):
        try:
            val = float(n.replace(",", ""))
        except ValueError:
            continue
        for i, c in enumerate(cells):
            v = as_num(c)
            if v is not None and abs(v) == abs(val) and c.strip():
                return i
    # an id the finding names (BNK-5221775, UE-PO-20260908) -> the cell that contains it
    for tok in re.findall(r"[A-Za-z]+-[A-Za-z0-9-]+", what):
        for i, c in enumerate(cells):
            if tok in c:
                return i
    words = {w.lower() for w in re.findall(r"[A-Za-z_]{4,}", what)}
    for i, h in enumerate(header):
        if h.lower() in words or any(part in words for part in h.lower().split("_") if len(part) > 3):
            return i
    return None


def _find_quote(lines: list[str], quote: str | None) -> int | None:
    if not quote:
        return None
    q = " ".join(str(quote).split())
    for i, line in enumerate(lines):
        if q and q[:80] in " ".join(line.split()):
            return i
    return None


def resolve(ref: Any, allowed: Iterable[Path] | None = None, window: int = WINDOW) -> dict[str, Any]:
    r = parse_ref(ref)
    allowed = list(allowed or roots())
    path = locate(r["file"], allowed)
    if path.stat().st_size > MAX_BYTES:
        raise EvidenceError(f"{r['file']} is too large to open here")
    kind = KINDS.get(path.suffix.lower(), "txt")
    text = path.read_text(encoding="utf-8", errors="replace")
    out: dict[str, Any] = {"file": r["file"], "kind": kind, "what": r["what"], "quote": r["quote"]}
    if kind == "csv":
        table = list(csv.reader(io.StringIO(text)))
        header = table[0] if table else []
        total = len(table)                      # Excel rows, header = row 1
        row = r["row"]
        if row is None and r["quote"]:
            hit = _find_quote([",".join(t) for t in table], r["quote"])
            row = hit + 1 if hit is not None else None
        if row is not None and not 1 <= row <= total:
            raise EvidenceError(f"row {row} is outside {r['file']} (rows 1..{total})")
        focus = row or 2
        lo, hi = max(2, focus - window), min(total, focus + window)
        rows = [{"row": n, "cells": table[n - 1]} for n in range(lo, hi + 1)]
        col = _column_for(header, table[row - 1], r["what"], r["column"]) if row and row > 1 else None
        out.update(header=header, rows=rows, total_rows=total, line=row,
                   highlight={"row": row, "column": col, "column_name": header[col] if col is not None else None})
        return out
    lines = text.splitlines()
    line = r["row"]
    if line is None:
        hit = _find_quote(lines, r["quote"])
        line = hit + 1 if hit is not None else None
    if line is not None and not 1 <= line <= max(1, len(lines)):
        raise EvidenceError(f"line {line} is outside {r['file']} (lines 1..{len(lines)})")
    focus = line or 1
    lo, hi = max(1, focus - window), min(len(lines), focus + window)
    out.update(header=None, rows=[{"row": n, "cells": [lines[n - 1]]} for n in range(lo, hi + 1)],
               total_rows=len(lines), line=line, highlight={"row": line, "column": 0 if line else None,
                                                            "column_name": None})
    return out


def refs_of(finding: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for e in finding.get("evidence") or []:
        if not isinstance(e, dict) or not e.get("source_file"):
            continue
        ref = {"file": e["source_file"]}
        for k in ("row", "quote", "what", "authority", "locator"):
            if e.get(k) not in (None, ""):
                ref[k] = e[k]
        if e.get("conflict"):
            ref["conflict"] = True
        out.append(ref)
    return out


def index(findings: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Every evidence reference the findings make, per finding and per file (the aggregated data view)."""
    per_finding, files = [], {}
    for f in findings:
        fid = f.get("finding_id") or f.get("id")
        refs = refs_of(f)
        if not refs:
            continue
        per_finding.append({"finding_id": fid, "channel": f.get("channel"), "check_type": f.get("check_type"),
                            "gap": f.get("gap"), "state": f.get("state"), "refs": refs})
        for ref in refs:
            entry = files.setdefault(ref["file"], {"file": ref["file"], "kind": KINDS.get(Path(ref["file"]).suffix.lower(), "txt"),
                                                   "refs": 0, "findings": []})
            entry["refs"] += 1
            if fid not in entry["findings"]:
                entry["findings"].append(fid)
    return {"findings": per_finding, "files": sorted(files.values(), key=lambda x: (-x["refs"], x["file"])),
            "count": sum(len(p["refs"]) for p in per_finding)}
