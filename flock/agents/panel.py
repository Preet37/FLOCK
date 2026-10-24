"""Panel (owner: Preet). Three judges each read a finding + evidence, call it, and bet a confidence.

All three agree with high average confidence -> the system decides on its own.
Any disagreement or low confidence -> 'conflict', a human decides.
That's the "betting" idea with a real job: it decides WHEN to bring a human in.

Approved rules (rules.json, from the Learner) are applied first and skip the panel.
With no API key, each judge uses offline rules so the demo still runs. If a live judge errors or times out,
that judge falls back to offline rules and the finding's reason says so.

Two code guardrails sit on top of the judges, because they are policy, not judgment:
  - a gap within TOLERANCE is never disputed (penny rounding)
  - a deal-term finding with no contract, amendment or authorized email behind it is never auto-disputed
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flock import llm
from flock.agents import learner
from flock.models import Actor, money

THRESHOLD = 0.75
TOLERANCE = 0.05

TERM_CHECKS = {"commission", "promo", "tax", "error_charge"}   # need a deal document behind them
DEAL_AUTHORITY = {"contract", "amendment", "auth_email"}       # documents that can set a deal term
WEAK_AUTHORITY = {"unauth_email", "call_note"}

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


def within_tolerance(f: dict[str, Any]) -> bool:
    return money(abs(f["gap"])) <= TOLERANCE  # money() first: 10.15 - 10.10 is 0.05000000000000071


def weak_evidence(f: dict[str, Any]) -> bool:
    """True when nothing in the evidence could prove the claim on its own."""
    ev = f.get("evidence", [])
    if f["check_type"] in TERM_CHECKS:
        return not any(e.get("authority") in DEAL_AUTHORITY for e in ev)
    return len(ev) < 2  # deposit / missing_order: need two sources that disagree


def _offline_vote(judge: str, f: dict[str, Any]) -> dict[str, Any]:
    authorities = {e.get("authority") for e in f.get("evidence", [])}
    if within_tolerance(f):
        return {"outcome": "correct", "confidence": 0.9, "reason": "Gap is within rounding tolerance or the charge matches the deal."}
    if authorities & WEAK_AUTHORITY:
        if judge == "judge.operator":
            return {"outcome": "recoverable", "confidence": 0.6, "reason": "A rep promised it in writing; worth claiming."}
        return {"outcome": "conflict", "confidence": 0.7, "reason": "The only support is from someone without clear authority."}
    if weak_evidence(f):
        if judge == "judge.operator":
            return {"outcome": "recoverable", "confidence": 0.5, "reason": "Big gap, worth a look, but I can't point to the deal."}
        return {"outcome": "conflict", "confidence": 0.8, "reason": "No contract, amendment or second source proves this gap."}
    conf = 0.9 if judge != "judge.skeptic" or abs(f["gap"]) >= 5 else 0.7
    return {"outcome": "recoverable", "confidence": conf, "reason": "Payout breaks a term backed by a contract or authorized source."}


def _confidence(x: Any) -> float | None:
    try:
        c = float(str(x).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    if 1 < c <= 100:  # "85" or "85%"
        c /= 100
    return min(max(c, 0.0), 1.0)


def vote(judge: str, f: dict[str, Any]) -> dict[str, Any]:
    payload = {k: f[k] for k in ("check_type", "channel", "expected", "actual", "gap", "evidence", "reason")}
    got, err = llm.call_json(JUDGES[judge] + "\n" + SYSTEM, json.dumps(payload))
    if got is not None:
        outcome = str(got.get("outcome", "")).strip().lower()
        conf = _confidence(got.get("confidence"))
        if outcome in ("correct", "recoverable", "conflict") and conf is not None:
            return {"outcome": outcome, "confidence": conf, "reason": str(got.get("reason", ""))[:300], "judge": judge}
        err = f"judge returned an unusable answer: {json.dumps(got)[:80]}"
    v = {**_offline_vote(judge, f), "judge": judge}
    if err:  # err is None only when we're offline on purpose
        v.update(reason=f"[offline fallback: {err}] {v['reason']}", fallback=err)
    return v


def decide(f: dict[str, Any], rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Returns the patch for the 'judge' transition: outcome, confidence, votes, reason."""
    rule = learner.matching_rule(f, rules)
    if rule:
        return {"outcome": rule["outcome"], "confidence": 1.0, "votes": [],
                "reason": f"Applied approved rule {rule['rule_id']}: {rule['description']}"}

    with ThreadPoolExecutor(max_workers=len(JUDGES)) as pool:  # judges are independent; run them at once
        votes = list(pool.map(lambda j: vote(j, f), JUDGES))
    outcomes = {v["outcome"] for v in votes}
    mean = sum(v["confidence"] for v in votes) / len(votes)
    avg = round(mean, 2)
    if len(outcomes) == 1 and mean >= THRESHOLD:  # compare the unrounded mean: 0.7466 is not 75%
        outcome = outcomes.pop()
        reason = f"All 3 judges agree ({avg:.0%} avg confidence). {votes[0]['reason']}"
    else:
        outcome = "conflict"
        reason = "Judges disagree or aren't confident enough; sending to a human. " + " | ".join(
            f"{v['judge']}: {v['outcome']} ({v['confidence']:.0%})" for v in votes)

    if within_tolerance(f) and outcome != "correct":
        outcome, reason = "correct", f"Gap ${money(abs(f['gap'])):.2f} is within the ${TOLERANCE:.2f} tolerance; not disputed. " + reason
    elif outcome == "recoverable" and weak_evidence(f):
        outcome, reason = "conflict", "No deal document or second source backs this gap; a human must confirm. " + reason

    fell_back = [v for v in votes if v.get("fallback")]
    if fell_back:
        reason += f" ({len(fell_back)} of 3 judges used offline rules after an API error: {fell_back[0]['fallback']})"
    return {"outcome": outcome, "confidence": avg, "votes": votes, "reason": reason}


ACTOR = Actor(id="panel", role="agent")
