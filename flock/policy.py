"""Owner approval policy: a standing decision a person makes once, so routine claims file without a click each time.

  {"auto_file_recoverable": true, "max_amount": 500, "min_confidence": 0.85, "channels": "all"}

An owner sets it (POST /api/policy, logged in the audit log as their decision). During a run, a drafted dispute is
approved under the policy only when all of these hold: outcome recoverable, every judge in the final round voted
recoverable (no split, no rule, no fallback), confidence >= min_confidence, |gap| <= max_amount, and its channel is
allowed. The gatekeeper records the approval with actor "policy (set by <name> on <date>)". Conflicts and anything
above the cap always wait for a person.

Stored in <state dir>/policy.json. With no file, FLOCK_DEMO_USER (the hosted demo's owner) gets the demo policy:
auto-file recoverable claims up to $500 at 85% or more.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flock.gatekeeper import transition
from flock.models import Actor

FILE = "policy.json"
DEMO = {"auto_file_recoverable": True, "max_amount": 500.0, "min_confidence": 0.85, "channels": "all"}
KEYS = ("auto_file_recoverable", "max_amount", "min_confidence", "channels")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(state_dir: Path | str) -> dict[str, Any] | None:
    """The policy in force: the saved one, else the demo owner's default (FLOCK_DEMO_USER), else None."""
    p = Path(state_dir) / FILE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except ValueError:
            return None
    who = os.environ.get("FLOCK_DEMO_USER", "").strip()
    if who:
        return {**DEMO, "set_by": who, "set_at": None, "default": True}
    return None


def validate(body: dict[str, Any]) -> dict[str, Any]:
    out = {}
    out["auto_file_recoverable"] = bool(body.get("auto_file_recoverable", True))
    try:
        out["max_amount"] = float(body.get("max_amount", DEMO["max_amount"]))
        out["min_confidence"] = float(body.get("min_confidence", DEMO["min_confidence"]))
    except (TypeError, ValueError):
        raise ValueError("max_amount and min_confidence must be numbers") from None
    if out["max_amount"] < 0 or not 0 <= out["min_confidence"] <= 1:
        raise ValueError("max_amount must be >= 0 and min_confidence between 0 and 1")
    ch = body.get("channels", "all")
    if ch != "all" and not (isinstance(ch, list) and all(isinstance(c, str) for c in ch)):
        raise ValueError('channels must be "all" or a list of channel names')
    out["channels"] = ch
    return out


def save(store: Any, state_dir: Path | str, body: dict[str, Any], who: str) -> dict[str, Any]:
    """An owner sets the policy: saved next to the state and written to the audit log as their decision."""
    pol = {**validate(body), "set_by": who, "set_at": _now()}
    p = Path(state_dir) / FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(pol, indent=1))
    store.commit(None, {"at": pol["set_at"], "actor": who, "role": "human", "action": "set_policy",
                        "subject_type": "policy", "subject_id": "approval-policy", "allowed": True,
                        "before": None, "after": "SET", "diff": {k: pol[k] for k in KEYS},
                        "reason": describe(pol), "evidence": []})
    return pol


def describe(pol: dict[str, Any] | None) -> str:
    if not pol or not pol.get("auto_file_recoverable"):
        return "Every dispute waits for a person."
    ch = "all channels" if pol.get("channels") == "all" else ", ".join(pol.get("channels") or [])
    return (f"Auto-file recoverable claims up to ${pol['max_amount']:,.0f} when all three judges agree at "
            f"{pol['min_confidence']:.0%} or more ({ch}); conflicts and bigger claims wait for a person.")


def actor_for(pol: dict[str, Any]) -> Actor:
    day = (pol.get("set_at") or "")[:10] or "setup"
    return Actor(id=f"policy (set by {pol.get('set_by') or 'owner'} on {day})", role="human")


def eligible(f: dict[str, Any], pol: dict[str, Any] | None) -> bool:
    if not pol or not pol.get("auto_file_recoverable"):
        return False
    if f.get("state") != "JUDGED" or f.get("outcome") != "recoverable" or not f.get("dispute_draft"):
        return False
    final = [v for v in f.get("votes") or [] if v.get("round") in (None, 2) and v.get("judge") != "reviewer"]
    if len(final) < 3 or any(v.get("outcome") != "recoverable" or v.get("fallback") for v in final):
        return False
    if (f.get("confidence") or 0) < pol["min_confidence"] or abs(f.get("gap") or 0) > pol["max_amount"]:
        return False
    ch = pol.get("channels", "all")
    return ch == "all" or f.get("channel") in ch


def apply(store: Any, pol: dict[str, Any] | None) -> list[str]:
    """Approve every eligible drafted dispute under the policy (through the gatekeeper). Returns their ids."""
    if not pol or not pol.get("auto_file_recoverable"):
        return []
    who = actor_for(pol)
    done = []
    for f in store.all():
        if eligible(f, pol):
            transition(store, f["finding_id"], "approve", who,
                       reason=f"Approved under the owner's policy: {describe(pol)}")
            done.append(f["finding_id"])
    return done


def is_policy_actor(actor: str | None) -> bool:
    return str(actor or "").startswith("policy (")
