"""Collector (owner: Maja). The mail carrier: picks everything up, never opens it.

IN:  an inbox folder with whatever files arrived (generator output, or in production, portal downloads)
OUT: the same files copied untouched into data/raw/<source>/

Production note for the pitch: here it reads simulated exports; in real life it would pull reports
from each merchant portal (DoorDash already offers downloadable financial reports).
"""
from __future__ import annotations

import shutil
from pathlib import Path

from flock.store import ROOT

RAW = ROOT / "data" / "raw"
SOURCES = ["pos", "doordash", "ubereats", "grubhub", "direct_app", "bank", "contracts", "emails"]


def guess_source(path: Path) -> str | None:
    name = path.name.lower()
    for s in SOURCES:
        if s.replace("_", "") in name.replace("_", "").replace("-", ""):
            return s
    return None


def collect(inbox: Path) -> list[Path]:
    """Copy each file into data/raw/<source>/. Unknown files go to data/raw/unknown/ for the Formatter to ask about."""
    out = []
    for f in sorted(Path(inbox).rglob("*")):
        if not f.is_file():
            continue
        src = guess_source(f) or "unknown"
        dest = RAW / src / f.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)  # untouched copy, timestamps kept
        out.append(dest)
    return out


if __name__ == "__main__":
    import sys

    paths = collect(Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "generator" / "out"))
    print(f"collected {len(paths)} files")
