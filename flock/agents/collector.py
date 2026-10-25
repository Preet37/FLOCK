"""Collector (owner: Maja). The mail carrier: picks everything up, never opens it.

IN:  an inbox folder with whatever files arrived (generator output, or in production, portal downloads)
OUT: the same files copied untouched into data/raw/<source>/, plus data/raw/manifest.csv
     (source, file, sha256, copied_at). Running it twice copies nothing new.
     Deal sources: contracts/, emails/ (.json, .txt, .eml), notes/ (.md meeting notes) and slack/ (a Slack export
     folder: channels.json, users.json, <channel>/<YYYY-MM-DD>.json). Files under a source folder keep their path
     inside it (slack/ubereats-partnership/2026-01-19.json), so a Slack export lands as the same tree.
     A bare .eml is an email and a bare .md a note, wherever it was dropped.

  python -m flock.agents.collector                      # inbox defaults to generator/out
  python -m flock.agents.collector path/to/inbox

Never collected: answer_key.csv and scenario.json (the grader's copies, agents must not see them) and dotfiles.
Production note for the pitch: here it reads simulated exports; in real life it would pull reports
from each merchant portal (DoorDash already offers downloadable financial reports).
"""
from __future__ import annotations

import csv
import hashlib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from flock.store import ROOT

RAW = ROOT / "data" / "raw"
INBOX = ROOT / "generator" / "out"
SOURCES = ["pos", "doordash", "ubereats", "grubhub", "direct_app", "bank", "contracts", "emails", "slack", "notes"]
BY_SUFFIX = {".eml": "emails", ".md": "notes"}
SKIP = {"answer_key.csv", "scenario.json"}  # the grader's copies: agents must not see them
MANIFEST_COLS = ["source", "file", "sha256", "copied_at"]


def guess_source(path: Path) -> str | None:
    """Folder name wins (contracts/doordash_contract.json is a contract), then the file type (.eml, .md), then the
    file name."""
    for part in path.parent.parts:   # outermost first: a Slack channel folder named "doordash" stays in slack/
        if part.lower() in SOURCES:
            return part.lower()
    if path.suffix.lower() in BY_SUFFIX:
        return BY_SUFFIX[path.suffix.lower()]
    tokens = re.split(r"[^a-z0-9]+", path.stem.lower())   # whole words: "bank_deposits" is not "pos"
    joined = "".join(tokens)
    for s in SOURCES:
        if s.replace("_", "") in tokens or ("_" in s and s.replace("_", "") in joined):
            return s
    return None


def inside(rel: Path, source: str) -> Path:
    """The file's path below its source folder (slack/general/2026-01-05.json -> general/2026-01-05.json);
    just the file name when the source came from the name."""
    parts = [p.lower() for p in rel.parent.parts]
    if source in parts:
        i = parts.index(source)
        return Path(*rel.parts[i + 1:])
    return Path(rel.name)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(raw: Path = RAW) -> list[dict]:
    p = raw / "manifest.csv"
    return list(csv.DictReader(p.open())) if p.exists() else []


def collect(inbox: Path = INBOX, raw: Path = RAW) -> list[Path]:
    """Copy each new file into raw/<source>/. Unknown files go to raw/unknown/ for the Formatter to ask about.

    Idempotent: a file whose sha256 is already in the manifest is skipped. A different file with a name
    that's already taken is kept under <name>.<sha8><ext>, never overwritten. Returns the newly copied paths.
    """
    inbox, raw = Path(inbox), Path(raw)
    raw.mkdir(parents=True, exist_ok=True)
    seen = {r["sha256"] for r in read_manifest(raw)}
    new_rows, out = [], []
    for f in sorted(inbox.rglob("*")):
        if not f.is_file() or f.name in SKIP or f.name.startswith("."):
            continue
        digest = sha256(f)
        if digest in seen:
            continue
        rel = f.relative_to(inbox)
        src = guess_source(rel) or "unknown"
        dest = raw / src / inside(rel, src)
        if dest.exists():
            dest = dest.with_name(f"{f.stem}.{digest[:8]}{f.suffix}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)  # untouched copy, timestamps kept
        seen.add(digest)
        out.append(dest)
        new_rows.append({"source": src, "file": dest.relative_to(raw).as_posix(), "sha256": digest,
                         "copied_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if new_rows:
        manifest = raw / "manifest.csv"
        is_new = not manifest.exists()
        with manifest.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=MANIFEST_COLS, lineterminator="\n")
            if is_new:
                w.writeheader()
            w.writerows(new_rows)
    return out


if __name__ == "__main__":
    import sys

    paths = collect(Path(sys.argv[1]) if len(sys.argv) > 1 else INBOX)
    print(f"collected {len(paths)} new files into {RAW.relative_to(ROOT)}/ (manifest: data/raw/manifest.csv)")
    for p in paths:
        print(f"  {p.relative_to(RAW)}")
