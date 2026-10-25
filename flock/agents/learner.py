"""Learner (owner: Preet). Every human decision becomes a precedent; repeated ones become proposed rules.

Proposed rules need a human to approve them. Approved rules live in rules.json as plain, readable entries
(not hidden in model weights) and the Panel applies them on the next run, so fewer items go to a human.
The Learner only ever writes proposed_rules.json. Only approve(), called by a human, writes rules.json.

  python -m flock.agents.learner              # show proposed rules
  python -m flock.agents.learner --approve R-001
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flock.models import Actor
from flock.store import ROOT, Store, get_store

RULES = ROOT / "rules.json"
PROPOSED_NAME = "proposed_rules.json"
PROPOSED = ROOT / "data" / "state" / PROPOSED_NAME
MIN_EVIDENCE = 2
CLI_HUMAN = Actor(id="cli-human", role="human")


def _read(path) -> list[dict[str, Any]]:
    path = Path(path)
    return json.loads(path.read_text() or "[]") if path.exists() else []


def _proposed_path(store: Store | None) -> Path:
    return store.dir / PROPOSED_NAME if store is not None else PROPOSED


def _key(r: dict[str, Any]) -> tuple:
    return r["channel"], r["check_type"], tuple(r.get("authority", [])), r["outcome"]


def authority_of(f: dict[str, Any]) -> list[str]:
    return sorted({e.get("authority") for e in f.get("evidence", []) if e.get("authority")})


def approved_rules(rules_path: Path | None = None) -> list[dict[str, Any]]:
    # A rule without approved_by never applies, even if someone copies a proposal into rules.json by hand.
    return [r for r in _read(rules_path or RULES) if r.get("approved_by")]


def matching_rule(f: dict[str, Any], rules: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """A rule matches on channel + check_type + the authority level of the evidence it was learned from."""
    auth = authority_of(f)
    for r in (approved_rules() if rules is None else rules):
        if r["channel"] == f["channel"] and r["check_type"] == f["check_type"] and r.get("authority", []) == auth:
            return r
    return None


def propose(store: Store, rules_path: Path | None = None) -> list[dict[str, Any]]:
    """Look at human approve/reject decisions on conflicts and propose a rule when the same call repeats.

    Only items the panel sent to a human count (outcome 'conflict'). One finding counts once (its latest
    human decision). A group where humans decided both ways is not proposed: there is no rule to learn.
    """
    by_id = {f["finding_id"]: f for f in store.all()}
    latest: dict[str, str] = {}
    for e in store.log():
        if e["role"] != "human" or not e["allowed"] or e["action"] not in ("approve", "reject"):
            continue
        f = by_id.get(e["subject_id"])
        if not f or f.get("outcome") != "conflict" or not f.get("votes"):  # only learn from items the panel couldn't decide
            continue
        latest[f["finding_id"]] = "recoverable" if e["action"] == "approve" else "correct"

    groups: dict[tuple, list[str]] = defaultdict(list)
    for fid, decision in latest.items():
        f = by_id[fid]
        groups[(f["channel"], f["check_type"], tuple(authority_of(f)), decision)].append(fid)

    existing = {_key(r) for r in approved_rules(rules_path)}
    proposed = []
    for (channel, check_type, auth, decision), ids in sorted(groups.items()):
        other = "correct" if decision == "recoverable" else "recoverable"
        if (channel, check_type, auth, other) in groups:  # humans disagreed with each other
            continue
        if len(ids) >= MIN_EVIDENCE and (channel, check_type, auth, decision) not in existing:
            proposed.append({
                "rule_id": f"R-{len(proposed) + 1:03d}", "channel": channel, "check_type": check_type,
                "authority": list(auth), "outcome": decision, "evidence_count": len(ids), "precedents": sorted(ids),
                "description": f"{channel} {check_type.replace('_', ' ')} backed by {', '.join(auth) or 'no document'}: "
                               f"{'always dispute' if decision == 'recoverable' else 'accept, do not dispute'}",
            })
    for rule in proposed:
        rule["rationale"], rule["rationale_by"] = rationale(rule, by_id)
    path = _proposed_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposed, indent=2))
    return proposed


def rationale(rule: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """One or two sentences a person reads before approving the rule: the writer model (e.g. CREAO) when it is
    configured, else a template. Only wording: the rule's match and outcome are fixed above, and a human approves it."""
    n = rule["evidence_count"]
    auth = ", ".join(rule["authority"]) or "no document"
    plain = (f"Humans made the same call {n} times on {rule['channel']} {rule['check_type'].replace('_', ' ')} findings "
             f"backed by {auth}: {'disputed' if rule['outcome'] == 'recoverable' else 'accepted the charge'}. "
             "Approving this lets Flock settle the next one the same way and say so.")
    try:
        from flock import llm
        r = llm.route("writer")
        if llm.available(r):
            facts = {"rule": rule["description"], "precedents": [
                {k: by_id.get(i, {}).get(k) for k in ("finding_id", "channel", "check_type", "gap", "reason")}
                for i in rule["precedents"][:5]]}
            text = llm.ask_text("In two short sentences, explain to a restaurant's finance reviewer why this rule was "
                                "proposed, from these precedents only. No numbers that are not given. No em dashes.",
                                json.dumps(facts, default=str), max_tokens=160, r=r)
            if text and text.strip():
                return text.strip().replace("\u2014", ", "), "writer model"
    except Exception:
        pass
    return plain, "template"


def approve(rule_id: str, approver: Actor = CLI_HUMAN, store: Store | None = None,
            rules_path: Path | None = None) -> dict[str, Any]:
    """Human-only. Copies a proposed rule into rules.json and logs who approved it."""
    if not isinstance(approver, Actor) or approver.role != "human":
        raise PermissionError(f"only a human may approve a rule, not {approver!r}")
    rules_path = Path(rules_path or RULES)
    proposals = _read(_proposed_path(store))
    rule = next((r for r in proposals if r["rule_id"] == rule_id), None)
    if rule is None:
        raise KeyError(f"no proposed rule {rule_id}; proposed: {[r['rule_id'] for r in proposals] or 'none'}")
    rules = _read(rules_path)
    same = next((r for r in rules if _key(r) == _key(rule) and r.get("approved_by")), None)
    if same:  # approving twice must not create a duplicate rule
        return same
    taken = {r.get("rule_id") for r in rules}
    new_id, n = rule_id, len(rules) + 1
    while new_id in taken:  # proposals restart at R-001 each run; only renumber when that id is already approved
        new_id, n = f"R-{n:03d}", n + 1
    rule = {**rule, "rule_id": new_id, "approved_by": approver.id,
            "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if new_id != rule_id:
        rule["proposed_as"] = rule_id
    rules_path.write_text(json.dumps(rules + [rule], indent=2))
    if store is not None:
        store.commit(None, {
            "at": rule["approved_at"], "actor": approver.id, "role": approver.role, "action": "approve_rule",
            "subject_type": "rule",
            "subject_id": rule["rule_id"], "allowed": True, "before": "PROPOSED", "after": "APPROVED",
            "diff": {}, "reason": rule["description"], "evidence": rule["precedents"],
        })
    return rule


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve")
    a = ap.parse_args()
    if a.approve:
        try:
            print("approved:", json.dumps(approve(a.approve, CLI_HUMAN, store=get_store()), indent=2))
        except KeyError as e:  # a typo'd rule id on stage: say so plainly, no traceback
            raise SystemExit(f"learner: {e.args[0]}") from None
    else:
        print(json.dumps(_read(PROPOSED), indent=2) if _read(PROPOSED) else "no proposed rules")
