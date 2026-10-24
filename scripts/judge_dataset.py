"""Training set for a tuned local judge (owner: Thabhelo). Step 1 of "the one genuinely trained piece".

  python scripts/judge_dataset.py                                   # seeds 1-50 (val 46-50), bagel chain, 2 weeks
  python scripts/judge_dataset.py --seeds 1-20 --val-seeds 18-20 --stores 8
  python scripts/judge_dataset.py --seeds 101-105                   # refused: those are the benchmark's held-out seeds

What it writes (data/training/):
  judge_train.jsonl, judge_val.jsonl   one line per (finding, judge): {"messages": [system, user, assistant]}
                                       system  = that judge's persona + the panel SYSTEM prompt + the JSON-only line,
                                                 exactly what flock/llm.py sends at inference
                                       user    = json.dumps(panel._payload(finding)), exactly what the panel sends
                                       assistant = {"outcome", "confidence", "reason"}, the shape panel._parse_call reads
  manifest.json                        seeds used, the held-out seeds that were locked out, counts per label and
                                       per check_type, and the prepare settings, so a run can be reproduced

Where truth comes from: each seed's generated answer key. A finding's label is the key's expected_outcome for its
orders; a finding on orders the key never planted is "correct" (nothing to dispute). The user payload never contains
ids and evidence paths read data/raw/... as in production, so the model cannot learn the seed. Numbers were computed in code; the model learns judgment, not arithmetic.

Held-out lock: scripts/benchmark.py scores on DEFAULT_SEEDS (101-110). Any training or validation seed in that range is
refused before a single file is generated, and the manifest records the lock, so the proof table stays honest.

Step 2 (on the GX10): LoRA an 8B instruct model on judge_train.jsonl, watch judge_val.jsonl, serve it with vLLM, then
  python scripts/benchmark.py --contestants rules,local,local-tuned --tuned-model <id the server reports>
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flock.agents import panel  # noqa: E402

OUT_DIR = ROOT / "data" / "training"
DEFAULT_SEEDS = "1-50"
DEFAULT_VAL = "46-50"
DEFAULT_PROFILE = "ca_bagel_chain"
JSON_ONLY = "\nRespond with a single JSON object and nothing else."   # llm.call_json appends this at inference
DEAL_AUTHORITY = {"contract", "amendment", "auth_email"}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


benchmark = _load("flock_benchmark", ROOT / "scripts" / "benchmark.py")
HELD_OUT = set(benchmark.parse_seeds(benchmark.DEFAULT_SEEDS))


def check_lock(seeds: list[int]) -> None:
    bad = sorted(set(seeds) & HELD_OUT)
    if bad:
        raise SystemExit(f"refused: seeds {bad} are the benchmark's held-out test set ({benchmark.DEFAULT_SEEDS}). "
                         "Train on anything else.")


# ---- labels ----------------------------------------------------------------------------------------------------------
def load_key(path: Path) -> dict[str, dict[str, str]]:
    import csv

    with path.open(newline="") as fh:
        return {r["order_id"]: r for r in csv.DictReader(fh)}


def label(f: dict[str, Any], key: dict[str, dict[str, str]]) -> tuple[str, str | None]:
    """(outcome, trap_type). Orders the key never planted have nothing to dispute."""
    rows = [key[o] for o in f["order_ids"] if o in key]
    if not rows:
        return "correct", None
    outcomes = Counter(r["expected_outcome"] for r in rows)
    outcome = outcomes.most_common(1)[0][0]
    trap = Counter(r["trap_type"] for r in rows if r["expected_outcome"] == outcome).most_common(1)[0][0]
    return outcome, trap


def confidence_for(outcome: str, f: dict[str, Any]) -> float:
    """Honest, not bold: strong paper earns 0.9, a learned or two-source case 0.8, a split 0.7 (the market scores it)."""
    if outcome == "conflict":
        return 0.7
    strong = any(e.get("authority") in DEAL_AUTHORITY for e in f.get("evidence", []))
    return 0.9 if (strong or outcome == "correct") else 0.8


def reason_for(outcome: str, f: dict[str, Any]) -> str:
    """One plain sentence, in the finding's own words. Never the trap's name."""
    own = (f.get("reason") or "").split(". ")[0].strip().rstrip(".")
    if outcome == "conflict":
        weak = [e.get("authority") for e in f.get("evidence", []) if e.get("authority") in panel.WEAK_AUTHORITY]
        if weak:
            return "The only support for changing the deal comes from someone without authority; a human must decide."
        return "The sources disagree or a second source is missing; a human must decide."
    if outcome == "correct":
        return (own + ". The charge matches the deal, so there is nothing to dispute.") if own else \
            "The charge matches the deal, so there is nothing to dispute."
    return (own + ". The payout breaks a term the documents support; dispute it.") if own else \
        "The payout breaks a term the documents support; dispute it."


def example(f: dict[str, Any], judge: str, outcome: str) -> dict[str, Any]:
    return {"messages": [
        {"role": "system", "content": panel.JUDGES[judge] + "\n" + panel.SYSTEM + JSON_ONLY},
        {"role": "user", "content": json.dumps(panel._payload(f))},
        {"role": "assistant", "content": json.dumps({"outcome": outcome, "confidence": confidence_for(outcome, f),
                                                      "reason": reason_for(outcome, f)})},
    ]}


# ---- build -----------------------------------------------------------------------------------------------------------
def build(seeds: list[int], val_seeds: list[int], profile: str = DEFAULT_PROFILE, stores: int | None = None,
          weeks: int = 2, days: int | None = None, trap_rate: float | None = None, max_per_label: int | None = None,
          out_dir: Path | None = None, quiet: bool = True) -> dict[str, Any]:
    check_lock(seeds + val_seeds)
    out_dir = Path(out_dir or OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    days = days or weeks * 7
    val = set(val_seeds)
    per_split: dict[str, list[dict[str, Any]]] = {"train": [], "val": []}
    labels: dict[str, Counter] = {"train": Counter(), "val": Counter()}
    by_check: Counter = Counter()
    by_trap: Counter = Counter()
    findings_seen = 0
    cap: Counter = Counter()
    for seed in sorted(set(seeds) | val):
        p = benchmark.prepare(seed, profile, stores, days, trap_rate, quiet=quiet)
        key = load_key(p["key"])
        split = "val" if seed in val else "train"
        for f in p["findings"]:
            f = benchmark.production_paths(f)   # evidence cites data/raw/..., never the seed folder
            outcome, trap = label(f, key)
            if max_per_label and split == "train":
                if cap[outcome] >= max_per_label:
                    continue
                cap[outcome] += 1
            findings_seen += 1
            labels[split][outcome] += 1
            by_check[f["check_type"]] += 1
            by_trap[trap or "no_trap"] += 1
            for judge in panel.JUDGES:
                per_split[split].append(example(f, judge, outcome))
        print(f"seed {seed} ({split}): {len(p['findings'])} findings -> {len(p['findings']) * len(panel.JUDGES)} examples",
              flush=True)
    paths = {}
    for split, rows in per_split.items():
        path = out_dir / f"judge_{split}.jsonl"
        with path.open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        paths[split] = str(path)
    manifest = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "train_seeds": sorted(set(seeds) - val), "val_seeds": sorted(val),
        "held_out_locked": sorted(HELD_OUT), "profile": profile, "stores": stores, "days": days, "trap_rate": trap_rate,
        "judges": list(panel.JUDGES), "findings": findings_seen,
        "examples": {s: len(r) for s, r in per_split.items()},
        "labels": {s: dict(c) for s, c in labels.items()},
        "by_check_type": dict(by_check), "by_trap_type": dict(by_trap),
        "format": "messages (system/user/assistant); assistant is the JSON panel._parse_call reads",
        "files": paths,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"\nwrote {manifest['examples']['train']} train and {manifest['examples']['val']} val examples "
          f"from {findings_seen} findings -> {out_dir.relative_to(ROOT) if out_dir.is_relative_to(ROOT) else out_dir}")
    print("labels: " + " | ".join(f"{s}: " + ", ".join(f"{k} {v}" for k, v in sorted(c.items())) for s, c in labels.items()))
    print("held out, never touched: " + benchmark.DEFAULT_SEEDS)
    return manifest


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Build the tuned judge's training set from generated seeds (held-out seeds locked).")
    ap.add_argument("--seeds", default=DEFAULT_SEEDS, help="training seeds, e.g. 1-50")
    ap.add_argument("--val-seeds", default=DEFAULT_VAL, help="validation seeds (a subset or separate), e.g. 46-50")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--stores", type=int, default=None, help="default: the profile's store count")
    ap.add_argument("--weeks", type=int, default=2)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--trap-rate", type=float, default=None, help="raise it for more positives per seed")
    ap.add_argument("--max-per-label", type=int, default=None, help="cap training findings per outcome, for balance")
    ap.add_argument("--out", default=None, help=f"output folder (default {OUT_DIR.relative_to(ROOT)})")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)
    build(benchmark.parse_seeds(a.seeds), benchmark.parse_seeds(a.val_seeds), a.profile, a.stores, a.weeks, a.days,
          a.trap_rate, a.max_per_label, Path(a.out) if a.out else None, quiet=not a.verbose)


if __name__ == "__main__":
    main()
