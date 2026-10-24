"""Learner (owner: Preet). Every human decision becomes a precedent; repeated ones become proposed rules.

Proposed rules need a human to approve them. Approved rules live in rules.json as plain, readable entries
(not hidden in model weights) and the Panel applies them on the next run, so fewer items go to a human.

  python -m flock.agents.learner              # show proposed rules
  python -m flock.agents.learner --approve R-001
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from typing import Any

from flock.store import ROOT, Store

RULES = ROOT / "rules.json"
PROPOSED = ROOT / "data" / "state" / "proposed_rules.json"
MIN_EVIDENCE = 2


def _read(path) -> list[dict[str, Any]]:
    return json.loads(path.read_text()) if path.exists() else []


def approved_rules() -> list[dict[str, Any]]:
    return _read(RULES)


def matching_rule(f: dict[str, Any]) -> dict[str, Any] | None:
    """A rule matches on channel + check_type + the authority level of the evidence it was learned from."""
    auth = sorted({e.get("authority") for e in f.get("evidence", []) if e.get("authority")})
    for r in approved_rules():
        if r["channel"] == f["channel"] and r["check_type"] == f["check_type"] and r.get("authority", []) == auth:
            return r
    return None


def propose(store: Store) -> list[dict[str, Any]]:
    """Look at human approve/reject decisions on conflicts and propose a rule when the same call repeats."""
    by_id = {f["finding_id"]: f for f in store.all()}
    groups: dict[tuple, list[str]] = defaultdict(list)
    for e in store.log():
        if e["role"] != "human" or not e["allowed"] or e["action"] not in ("approve", "reject"):
            continue
        f = by_id.get(e["subject_id"])
        if not f or not f.get("votes"):  # only learn from items the panel couldn't decide
            continue
        auth = tuple(sorted({x.get("authority") for x in f.get("evidence", []) if x.get("authority")}))
        decision = "recoverable" if e["action"] == "approve" else "correct"
        groups[(f["channel"], f["check_type"], auth, decision)].append(f["finding_id"])

    existing = {(r["channel"], r["check_type"], tuple(r.get("authority", [])), r["outcome"]) for r in approved_rules()}
    proposed = []
    for (channel, check_type, auth, decision), ids in groups.items():
        if len(ids) >= MIN_EVIDENCE and (channel, check_type, auth, decision) not in existing:
            proposed.append({
                "rule_id": f"R-{len(proposed) + 1:03d}", "channel": channel, "check_type": check_type,
                "authority": list(auth), "outcome": decision, "evidence_count": len(ids), "precedents": ids,
                "description": f"{channel} {check_type.replace('_', ' ')} backed by {', '.join(auth) or 'no document'}: "
                               f"{'always dispute' if decision == 'recoverable' else 'accept, do not dispute'}",
            })
    PROPOSED.parent.mkdir(parents=True, exist_ok=True)
    PROPOSED.write_text(json.dumps(proposed, indent=2))
    return proposed


def approve(rule_id: str, approver: str = "human") -> dict[str, Any]:
    rule = next(r for r in _read(PROPOSED) if r["rule_id"] == rule_id)
    rules = approved_rules()
    rule = {**rule, "rule_id": f"R-{len(rules) + 1:03d}", "approved_by": approver}
    RULES.write_text(json.dumps(rules + [rule], indent=2))
    return rule


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve")
    a = ap.parse_args()
    if a.approve:
        print("approved:", json.dumps(approve(a.approve), indent=2))
    else:
        print(json.dumps(_read(PROPOSED), indent=2) or "no proposed rules")
