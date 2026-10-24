"""Panel (owner: Preet). Three judges each read a finding + evidence, call it, and bet a confidence.

All three agree with high average confidence -> the system decides on its own.
Any disagreement or low confidence -> 'conflict', a human decides.
That's the "betting" idea with a real job: it decides WHEN to bring a human in.

Approved rules (rules.json, from the Learner) are applied first and skip the panel.
With no API key, each judge uses offline rules so the demo still runs.
"""
from __future__ import annotations

import json
from typing import Any

from flock import llm
from flock.agents import learner
from flock.models import Actor

THRESHOLD = 0.75
TOLERANCE = 0.05

JUDGES = {
    "judge.strict": "You are a strict auditor. Only call 'recoverable' if the evidence clearly proves the platform broke the deal.",
    "judge.operator": "You are a restaurant operator. You want every dollar back, but you won't fight a charge that's fair.",
    "judge.skeptic": "You are a skeptic. Look for reasons the evidence is incomplete, ambiguous, or from someone without authority.",
}

SYSTEM = (
    "You judge delivery-payout discrepancies for a restaurant group. Numbers were already computed in code; "
    "do NOT recompute them. Decide: 'correct' (charge is fair, do nothing), 'recoverable' (clear mistake with proof), "
    "or 'conflict' (sources disagree or authority is unclear, a human must decide). "
    'Return {"outcome": ..., "confidence": 0..1, "reason": "one sentence"}.'
)


def _offline_vote(judge: str, f: dict[str, Any]) -> dict[str, Any]:
    authorities = {e.get("authority") for e in f.get("evidence", [])}
    if abs(f["gap"]) <= TOLERANCE:
        return {"outcome": "correct", "confidence": 0.9, "reason": "Gap is within rounding tolerance or the charge matches the deal."}
    if "unauth_email" in authorities or "call_note" in authorities:
        if judge == "judge.operator":
            return {"outcome": "recoverable", "confidence": 0.6, "reason": "A rep promised it in writing; worth claiming."}
        return {"outcome": "conflict", "confidence": 0.7, "reason": "The only support is from someone without clear authority."}
    conf = 0.9 if judge != "judge.skeptic" or abs(f["gap"]) >= 5 else 0.7
    return {"outcome": "recoverable", "confidence": conf, "reason": "Payout breaks a term backed by a contract or authorized source."}


def vote(judge: str, f: dict[str, Any]) -> dict[str, Any]:
    payload = {k: f[k] for k in ("check_type", "channel", "expected", "actual", "gap", "evidence", "reason")}
    got = llm.ask_json(JUDGES[judge] + "\n" + SYSTEM, json.dumps(payload))
    if not got or got.get("outcome") not in ("correct", "recoverable", "conflict"):
        got = _offline_vote(judge, f)
    got["judge"] = judge
    got["confidence"] = float(got.get("confidence", 0))
    return got


def decide(f: dict[str, Any]) -> dict[str, Any]:
    """Returns the patch for the 'judge' transition: outcome, confidence, votes, reason."""
    rule = learner.matching_rule(f)
    if rule:
        return {"outcome": rule["outcome"], "confidence": 1.0, "votes": [],
                "reason": f"Applied approved rule {rule['rule_id']}: {rule['description']}"}

    votes = [vote(j, f) for j in JUDGES]
    outcomes = {v["outcome"] for v in votes}
    avg = round(sum(v["confidence"] for v in votes) / len(votes), 2)
    if len(outcomes) == 1 and avg >= THRESHOLD:
        outcome = outcomes.pop()
        reason = f"All 3 judges agree ({avg:.0%} avg confidence). {votes[0]['reason']}"
    else:
        outcome = "conflict"
        reason = "Judges disagree or aren't confident enough; sending to a human. " + " | ".join(
            f"{v['judge']}: {v['outcome']} ({v['confidence']:.0%})" for v in votes)
    return {"outcome": outcome, "confidence": avg, "votes": votes, "reason": reason}


ACTOR = Actor(id="panel", role="agent")
