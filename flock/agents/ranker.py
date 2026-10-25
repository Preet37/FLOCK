"""Ranker (owner: Preet). Plain Python. Which disputes are worth spending the limited dispute budget on?

score = gap x win_probability - effort_cost
Platforms limit how many disputes a merchant can file per period, so only the top N per channel get filed.
Win probabilities start as defaults and should be updated from past outcomes (the Learner's job later).
"""
from __future__ import annotations

from typing import Any

from flock.models import Actor, money

WIN_PROBABILITY = {  # [need: replace with real history once we have resolved disputes]
    "commission": 0.9, "promo": 0.8, "tax": 0.85, "error_charge": 0.75, "deposit": 0.7, "missing_order": 0.8,
    "fee": 0.8,       # a fee the contract (or an authorized waiver) says shouldn't be there: a quoted term, like promo
    "anomaly": 0.4,   # learned baseline, no deal term to quote: the platform can argue the pattern; worth less
}
EFFORT_COST = 1.00                       # dollars of staff time per dispute (assumption)
DISPUTE_LIMIT = {"doordash": 20, "ubereats": 20, "grubhub": 20, "direct_app": 999}  # per period (assumption)

ACTOR = Actor(id="ranker", role="agent")


def score(f: dict[str, Any]) -> float:
    return money(abs(f["gap"]) * WIN_PROBABILITY.get(f["check_type"], 0.5) - EFFORT_COST)


def within_budget(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recoverable findings, best first, capped per channel. Not worth the effort (score <= 0) = not filed.
    Unknown channels get no budget, so nothing is filed somewhere we haven't agreed a limit for."""
    used: dict[str, int] = {}
    out = []
    for f in sorted(findings, key=lambda x: x.get("rank_score") or 0, reverse=True):
        ch = f["channel"]
        if f.get("outcome") != "recoverable":
            continue
        if used.get(ch, 0) < DISPUTE_LIMIT.get(ch, 0) and (f.get("rank_score") or 0) > 0:
            used[ch] = used.get(ch, 0) + 1
            out.append(f)
    return out
