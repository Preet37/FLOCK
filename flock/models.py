"""Shared shapes and constants. Mirrors CONTRACT.md. Change CONTRACT.md first, then this file."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

CHANNELS = ["doordash", "ubereats", "grubhub", "direct_app"]
CHECK_TYPES = ["commission", "promo", "tax", "error_charge", "deposit", "missing_order", "fee", "anomaly"]
OUTCOMES = ["correct", "recoverable", "conflict"]

# Strongest first. Used by the Deal Keeper when sources disagree.
AUTHORITY_RANK = ["contract", "amendment", "auth_email", "unauth_email", "call_note"]

# (state, action) -> (next_state, roles allowed to do it). The ONLY legal moves.
TRANSITIONS: dict[tuple[str, str], tuple[str, set[str]]] = {
    ("NEW", "check"): ("CHECKED", {"agent"}),
    ("CHECKED", "judge"): ("JUDGED", {"agent"}),
    ("JUDGED", "approve"): ("APPROVED", {"human"}),
    ("JUDGED", "reject"): ("REJECTED", {"human"}),
    ("APPROVED", "file"): ("FILED", {"gatekeeper"}),
    ("FILED", "recover"): ("RECOVERED", {"human", "gatekeeper"}),
}

# Actions that change fields but not state (e.g. ranker scoring, dispute writer drafting, an investigator attaching
# a case file). Still logged through transition(). (state, action) -> roles allowed.
ANNOTATIONS: dict[tuple[str, str], set[str]] = {
    ("CHECKED", "investigate"): {"agent"},
    ("JUDGED", "investigate"): {"agent"},
    ("JUDGED", "rank"): {"agent"},
    ("JUDGED", "draft"): {"agent"},
    ("APPROVED", "draft"): {"agent"},
    # Vendor follow-up on a claim that was filed but never paid (flock/agents/follow_up.py). Same rule as a dispute:
    # an agent drafts, only a human approves (or rejects), only the gatekeeper sends it (Clicky does the typing).
    ("FILED", "follow_up"): {"agent"},
    ("FILED", "approve_follow_up"): {"human"},
    ("FILED", "reject_follow_up"): {"human"},
    ("FILED", "send_follow_up"): {"gatekeeper"},
}

# Annotations that may only write certain fields. Anything else in the patch is denied (and logged): an
# investigator adds evidence for the human, it can never touch outcome, gap or any money field.
ANNOTATION_FIELDS: dict[str, set[str]] = {
    "investigate": {"case_file"},
    "follow_up": {"follow_up"},
    "approve_follow_up": {"follow_up"},
    "reject_follow_up": {"follow_up"},
    "send_follow_up": {"follow_up"},
}

# The follow-up's own steps, inside the `follow_up` field: action -> (states it may start from, state it must end in).
# None = no follow-up yet. A sent or rejected follow-up can be followed by a new draft (the claim is still unpaid).
FOLLOW_UP_STEPS: dict[str, tuple[set[str | None], str]] = {
    "follow_up": ({None, "REJECTED", "SENT"}, "DRAFT"),
    "approve_follow_up": ({"DRAFT"}, "APPROVED"),
    "reject_follow_up": ({"DRAFT"}, "REJECTED"),
    "send_follow_up": ({"APPROVED"}, "SENT"),
}
FOLLOW_UP_FIXED = ("claim_ref", "amount", "channel")   # what nobody may change after the draft (code computed them)


# Journal entries (flock/agents/journal.py): agents draft, humans approve, only the gatekeeper posts to the export.
JE_TRANSITIONS: dict[tuple[str, str], tuple[str, set[str]]] = {
    ("NEW", "draft"): ("DRAFT", {"agent"}),
    ("DRAFT", "approve"): ("APPROVED", {"human"}),
    ("DRAFT", "reject"): ("REJECTED", {"human"}),
    ("APPROVED", "post"): ("POSTED", {"gatekeeper"}),
}


@dataclass
class Actor:
    id: str      # e.g. "checker.commission", "panel", "maja", "clicky"
    role: str    # "agent" | "human" | "gatekeeper"


@dataclass
class Finding:
    order_ids: list[str]
    channel: str
    check_type: str
    expected: float
    actual: float
    gap: float
    evidence: list[dict[str, Any]] = field(default_factory=list)
    outcome: str | None = None
    confidence: float | None = None
    reason: str = ""
    votes: list[dict[str, Any]] = field(default_factory=list)
    rank_score: float | None = None
    dispute_draft: str | None = None
    state: str = "NEW"
    finding_id: str = field(default_factory=lambda: "F-" + uuid.uuid4().hex[:8])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def money(x: float) -> float:
    """All money math goes through here. Round to cents, no floats drifting."""
    return round(float(x) + 0.0, 2)


@dataclass(frozen=True)
class Machine:
    """One subject type the gatekeeper moves: its id field, legal moves, and an optional guard.
    guard(item, action) returns a reason to refuse an otherwise legal move (logged as a denial), or None."""
    name: str
    id_key: str
    transitions: dict[tuple[str, str], tuple[str, set[str]]]
    annotations: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    guard: Callable[[dict[str, Any], str], str | None] | None = None
    fields: dict[str, set[str]] = field(default_factory=dict)   # action -> the only fields its patch may write
    # patch_guard(item, action, patch) -> a reason to refuse, for moves whose patch content matters (follow-ups)
    patch_guard: Callable[[dict[str, Any], str, dict[str, Any]], str | None] | None = None


def _je_guard(je: dict[str, Any], action: str) -> str | None:
    """A journal entry can only be approved if its lines balance to the cent (recomputed, never trusted)."""
    if action != "approve":
        return None
    dr = sum(round(money(ln.get("debit") or 0) * 100) for ln in je.get("lines", []))
    cr = sum(round(money(ln.get("credit") or 0) * 100) for ln in je.get("lines", []))
    if dr != cr or not je.get("lines"):
        return f"entry does not balance: debits {dr / 100:.2f} != credits {cr / 100:.2f}"
    return None


def _follow_up_guard(f: dict[str, Any], action: str, patch: dict[str, Any]) -> str | None:
    """A follow-up moves DRAFT -> APPROVED -> SENT (or DRAFT -> REJECTED) one step at a time: the patch must land on
    the step's state, start from an allowed one, and keep the code-computed claim, amount and channel unchanged."""
    if action not in FOLLOW_UP_STEPS:
        return None
    starts, ends = FOLLOW_UP_STEPS[action]
    now = (f.get("follow_up") or {}).get("state")
    new = patch.get("follow_up")
    if now not in starts:
        return f"'{action}' needs a follow-up in {' or '.join(sorted(str(s) for s in starts))}, this one is {now}"
    if not isinstance(new, dict) or new.get("state") != ends:
        return f"'{action}' must set follow_up.state to {ends}"
    if action == "follow_up":
        if not str(new.get("draft") or "").strip():
            return "a follow-up draft needs its message"
        if new.get("claim_ref") != f.get("portal_ref") or money(new.get("amount") or 0) != money(abs(f["gap"])):
            return "a follow-up must name the filed claim (portal_ref) and its amount (|gap|), as computed in code"
        return None
    old = f["follow_up"]
    changed = [k for k in FOLLOW_UP_FIXED if new.get(k) != old.get(k)]
    if changed:
        return f"'{action}' may not change {', '.join(changed)}"
    if action == "send_follow_up" and not new.get("sent_ref"):
        return "a sent follow-up needs the portal's reference"
    return None


FINDING = Machine("finding", "finding_id", TRANSITIONS, ANNOTATIONS, fields=ANNOTATION_FIELDS,
                  patch_guard=_follow_up_guard)
JOURNAL = Machine("journal_entry", "je_id", JE_TRANSITIONS, guard=_je_guard)
