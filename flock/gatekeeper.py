"""The one door. Every change to a finding goes through transition().

- Checks the move is legal (state machine in models.TRANSITIONS / ANNOTATIONS).
- Checks the actor's role may do it (agents propose, humans approve, gatekeeper files).
- Writes an append-only log entry with a before/after diff, in the same commit as the change.
- Denied attempts are logged too, then raise Denied.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from flock.models import ANNOTATIONS, TRANSITIONS, Actor
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
) -> dict[str, Any]:
    item = store.get(finding_id)
    before = copy.deepcopy(item)
    state = item["state"]

    if (state, action) in TRANSITIONS:
        to_state, roles = TRANSITIONS[(state, action)]
    elif (state, action) in ANNOTATIONS:
        to_state, roles = state, ANNOTATIONS[(state, action)]
    else:
        to_state, roles = None, set()

    allowed = to_state is not None and actor.role in roles
    # 'state' can never be set through a patch; only the state machine moves it.
    patch = {k: v for k, v in (patch or {}).items() if k not in ("state", "finding_id")}

    after = copy.deepcopy(before)
    if allowed:
        after.update(patch)
        after["state"] = to_state

    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actor": actor.id,
        "role": actor.role,
        "action": action,
        "subject_id": finding_id,
        "allowed": allowed,
        "before": before["state"],
        "after": after["state"],
        "diff": _diff(before, after) if allowed else {},
        "reason": reason if allowed else f"DENIED: {actor.role} may not '{action}' from {state}. {reason}".strip(),
        "evidence": evidence if evidence is not None else item.get("evidence", []),
    }
    store.commit(after if allowed else None, entry)

    if not allowed:
        raise Denied(f"{actor.id} ({actor.role}) cannot '{action}' finding {finding_id} in state {state}")
    return after
