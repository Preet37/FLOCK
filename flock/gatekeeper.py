"""The one door. Every change to a finding (or a journal entry) goes through transition().

- Checks the move is legal (state machine in models.TRANSITIONS / ANNOTATIONS; journal entries: models.JE_TRANSITIONS).
- Checks the actor's role may do it (agents propose, humans approve, gatekeeper files / posts).
- Runs the subject type's guard, if any (a journal entry that doesn't balance can't be approved).
- Keeps field-limited annotations to their fields (models.ANNOTATION_FIELDS: an investigator's 'investigate' may
  only write case_file; a patch touching anything else is denied).
- Writes an append-only log entry with a before/after diff, in the same commit as the change.
- Denied attempts are logged too, then raise Denied.
- After an allowed approve / reject / recover on a finding (ground truth), the judge market settles its bets on it.
  The market only reads the committed move and writes its own ledger; it never changes a finding.

`machine` picks the subject type (models.FINDING by default, models.JOURNAL for journal entries); every
subject type writes to the same audit log, and each entry says which type it was (subject_type).
"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from typing import Any

from flock import market
from flock.models import FINDING, Actor, Machine
from flock.store import Store


class Denied(Exception):
    pass


GATEKEEPER = Actor(id="gatekeeper", role="gatekeeper")


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[Any]]:
    keys = set(before) | set(after)
    return {k: [before.get(k), after.get(k)] for k in sorted(keys) if before.get(k) != after.get(k)}


def transition(
    store: Store,
    finding_id: str,
    action: str,
    actor: Actor,
    patch: dict[str, Any] | None = None,
    reason: str = "",
    evidence: list[Any] | None = None,
    machine: Machine = FINDING,
) -> dict[str, Any]:
    item = store.get(finding_id)
    before = copy.deepcopy(item)
    state = item["state"]

    if (state, action) in machine.transitions:
        to_state, roles = machine.transitions[(state, action)]
    elif (state, action) in machine.annotations:
        to_state, roles = state, machine.annotations[(state, action)]
    else:
        to_state, roles = None, set()

    allowed = to_state is not None and actor.role in roles
    if allowed and machine.id_key not in item:  # e.g. the journal machine pointed at a finding
        blocked = f"{finding_id} is not a {machine.name}"
    else:
        blocked = machine.guard(before, action) if allowed and machine.guard else None
    if allowed and not blocked and action in machine.fields:
        extra = sorted(set(patch or {}) - machine.fields[action])
        if extra:
            blocked = f"'{action}' may only write {', '.join(sorted(machine.fields[action]))}, not {', '.join(extra)}"
    allowed = allowed and not blocked
    # 'state' can never be set through a patch; only the state machine moves it.
    patch = {k: v for k, v in (patch or {}).items() if k not in ("state", machine.id_key)}

    after = copy.deepcopy(before)
    if allowed:
        after.update(patch)
        after["state"] = to_state

    if blocked:
        denial = f"DENIED: {blocked}. {reason}".strip()
    else:
        denial = f"DENIED: {actor.role} may not '{action}' from {state}. {reason}".strip()
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actor": actor.id,
        "role": actor.role,
        "action": action,
        "subject_id": finding_id,
        "subject_type": machine.name,
        "allowed": allowed,
        "before": before["state"],
        "after": after["state"],
        "diff": _diff(before, after) if allowed else {},
        "reason": reason if allowed else denial,
        "evidence": evidence if evidence is not None else item.get("evidence", []),
    }
    store.commit(after if allowed else None, entry)

    if not allowed:
        why = f": {blocked}" if blocked else ""
        raise Denied(f"{actor.id} ({actor.role}) cannot '{action}' {machine.name} {finding_id} in state {state}{why}")
    if machine is FINDING and action in market.TRUTH:
        try:
            market.on_transition(store, finding_id, action)
        except Exception as e:  # the move is committed and logged; a market hiccup must not undo or hide it
            print(f"[market] could not settle bets on {finding_id} after '{action}': {e}", file=sys.stderr)
    return after
