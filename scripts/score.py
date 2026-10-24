"""Scoreboard (owner: Ira). Compares Flock's calls to data/answer_key.csv.

caught       = the system's outcome matches the answer key
               (a 'conflict' trap settled by a human-approved learned rule also counts: the human already decided it)
missed       = traps the system got wrong or never found
false alarms = things the system wanted to dispute that the answer key says are fair
history      = one line per pipeline run (data/state/runs.jsonl): week 1 vs week 2 human hand-offs
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flock.store import STATE_DIR, Store, get_store  # noqa: E402

ANSWER_KEY = ROOT / "data" / "answer_key.csv"
RUNS = STATE_DIR / "runs.jsonl"


def history(path: Path = RUNS) -> list[dict]:
    """Pipeline runs since the last --forget, labelled Week 1, Week 2, ..."""
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [{**r, "label": f"Week {i}"} for i, r in enumerate(rows, 1)]


def score(store: Store | None = None, verbose: bool = True, runs_path: Path = RUNS) -> dict:
    key = list(csv.DictReader(ANSWER_KEY.open()))
    findings = (store or get_store()).all()
    by_order = {o: f for f in findings for o in f["order_ids"]}

    caught, missed, false_alarm, by_rule, traps = [], [], [], [], []
    for row in key:
        f = by_order.get(row["order_id"])
        got = f.get("outcome") if f else None
        learned = bool(f and f.get("reason", "").startswith("Applied approved rule"))
        quiet = f is None and row["expected_outcome"] == "correct"  # nothing to dispute and nothing raised
        ok = got == row["expected_outcome"] or quiet or (row["expected_outcome"] == "conflict" and learned)
        if ok:
            caught.append(row)
            if learned:
                by_rule.append(row)
        else:
            missed.append((row, got))
        alarm = row["expected_outcome"] == "correct" and got == "recoverable"
        if alarm:
            false_alarm.append(row)
        traps.append({**row, "amount": float(row["amount"]), "got": got, "caught": ok, "false_alarm": alarm,
                      "by_rule": learned, "finding_id": f["finding_id"] if f else None})

    recovered = sum(float(r["amount"]) for r in caught if r["expected_outcome"] == "recoverable")
    to_human = sum(1 for f in findings if f.get("outcome") == "conflict")
    runs = history(runs_path)
    if verbose:
        print(f"caught {len(caught)}/{len(key)} traps | missed {len(missed)} | false alarms {len(false_alarm)}")
        print(f"recoverable dollars found: ${recovered:.2f} | sent to a human: {to_human} | settled by learned rules: {len(by_rule)}")
        for row, got in missed:
            print(f"  MISSED {row['order_id']} {row['trap_type']}: expected {row['expected_outcome']}, got {got}")
        if runs:
            print("human hand-offs by run: " + " | ".join(f"{r['label']}: {r['to_human']}" for r in runs))
    return {"caught": len(caught), "total": len(key), "missed": len(missed),
            "false_alarms": len(false_alarm), "recovered": recovered, "to_human": to_human,
            "by_rule": len(by_rule), "traps": traps, "runs": runs}


if __name__ == "__main__":
    score()
