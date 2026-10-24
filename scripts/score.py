"""Scoreboard (owner: Ira). Compares Flock's calls to data/answer_key.csv.

caught       = the system's outcome matches the answer key
               (a 'conflict' trap settled by a human-approved learned rule also counts: the human already decided it)
missed       = traps the system got wrong or never found
false alarms = things the system wanted to dispute that the answer key says are fair
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flock.store import JsonStore  # noqa: E402


def score() -> dict:
    key = list(csv.DictReader((ROOT / "data" / "answer_key.csv").open()))
    findings = JsonStore().all()
    by_order = {o: f for f in findings for o in f["order_ids"]}

    caught, missed, false_alarm, by_rule = [], [], [], []
    for row in key:
        f = by_order.get(row["order_id"])
        got = f.get("outcome") if f else None
        learned = bool(f and f.get("reason", "").startswith("Applied approved rule"))
        if got == row["expected_outcome"] or (row["expected_outcome"] == "conflict" and learned):
            caught.append(row)
            if learned:
                by_rule.append(row)
        else:
            missed.append((row, got))
        if row["expected_outcome"] == "correct" and got == "recoverable":
            false_alarm.append(row)

    recovered = sum(float(r["amount"]) for r in caught if r["expected_outcome"] == "recoverable")
    to_human = sum(1 for f in findings if f.get("outcome") == "conflict")
    print(f"caught {len(caught)}/{len(key)} traps | missed {len(missed)} | false alarms {len(false_alarm)}")
    print(f"recoverable dollars found: ${recovered:.2f} | sent to a human: {to_human} | settled by learned rules: {len(by_rule)}")
    for row, got in missed:
        print(f"  MISSED {row['order_id']} {row['trap_type']}: expected {row['expected_outcome']}, got {got}")
    return {"caught": len(caught), "total": len(key), "missed": len(missed),
            "false_alarms": len(false_alarm), "recovered": recovered, "to_human": to_human}


if __name__ == "__main__":
    score()
