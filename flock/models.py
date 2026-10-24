"""Shared shapes and constants. Mirrors CONTRACT.md. Change CONTRACT.md first, then this file."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

CHANNELS = ["doordash", "ubereats", "grubhub", "direct_app"]
CHECK_TYPES = ["commission", "promo", "tax", "error_charge", "deposit", "missing_order"]
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

# Actions that change fields but not state (e.g. ranker scoring, dispute writer drafting).
# Still logged through transition(). (state, action) -> roles allowed.
ANNOTATIONS: dict[tuple[str, str], set[str]] = {
    ("JUDGED", "rank"): {"agent"},
    ("JUDGED", "draft"): {"agent"},
    ("APPROVED", "draft"): {"agent"},
}


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


def _je_guard(je: dict[str, Any], action: str) -> str | None:
    """A journal entry can only be approved if its lines balance to the cent (recomputed, never trusted)."""
    if action != "approve":
        return None
    dr = sum(round(money(ln.get("debit") or 0) * 100) for ln in je.get("lines", []))
    cr = sum(round(money(ln.get("credit") or 0) * 100) for ln in je.get("lines", []))
    if dr != cr or not je.get("lines"):
        return f"entry does not balance: debits {dr / 100:.2f} != credits {cr / 100:.2f}"
    return None


FINDING = Machine("finding", "finding_id", TRANSITIONS, ANNOTATIONS)
JOURNAL = Machine("journal_entry", "je_id", JE_TRANSITIONS, guard=_je_guard)
