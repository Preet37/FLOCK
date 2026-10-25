"""Panel (owner: Preet). Three judges each read a finding + evidence, call it, and bet a confidence.

All three agree with high average confidence -> the system decides on its own.
Any disagreement or low confidence -> 'conflict', a human decides.
That's the "betting" idea with a real job: it decides WHEN to bring a human in.

How a live panel runs (judges can be local models, Groq or NVIDIA; see flock/llm.py):
  1. Round 1: each judge votes on its own, in parallel, without seeing the others.
  2. If they disagree: ONE debate round. Each judge sees the other two votes and reasons and may revise.
     Both rounds are kept in `votes` (round 1, round 2).
  3. Still split -> conflict, a human decides. The reviewer (Claude by default) does NOT break the tie; it
     writes `review_memo` for the human: each side, and what evidence would settle it.
  4. Audit sample: FLOCK_AUDIT_RATE (default 0.1) of the findings the panel decided on its own, picked by a
     hash of finding_id (same finding, same answer every run), get a reviewer second opinion. If the reviewer
     disagrees, the finding becomes a conflict and goes to a human; the reason says so.
Offline the debate and the reviewer are no-ops, so the demo's numbers don't move.

Approved rules (rules.json, from the Learner) are applied first and skip the panel.
With no provider configured, each judge uses offline rules so the demo still runs. If a live judge errors or
times out, that judge falls back to offline rules and the finding's reason says so.

Judge market (flock/market.py): each vote is a bet that settles when a human approves / rejects or the platform pays.
decide() can take the judges' balance shares as weights: the average confidence becomes balance-weighted, so a judge
with a better track record counts for more. Unanimity is still required and the guardrails below still apply, so
weights can only move a unanimous call across THRESHOLD, never overrule a disagreement or a guardrail.

Live-model budget (flock/pipeline.py, FLOCK_LIVE_JUDGE_BUDGET): a run can give the live judges to only its N riskiest
findings. decide(..., live=False) is how the rest are judged: offline rules for all three votes, no debate, no reviewer,
each vote marked budget="offline" and the reason saying "offline rules: outside this run's live-model budget".

Two code guardrails sit on top of the judges, because they are policy, not judgment:
  - a gap within TOLERANCE is never disputed (penny rounding), and never sent for a second opinion
  - a deal-term finding with no contract, amendment or authorized email behind it is never auto-disputed
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flock import llm
from flock.agents import learner
from flock.models import Actor, money

THRESHOLD = 0.75
TOLERANCE = 0.05
DEFAULT_AUDIT_RATE = 0.1

TERM_CHECKS = {"commission", "promo", "tax", "error_charge", "fee"}   # need a deal document behind them (a fee or
# fee waiver is a deal term: one backed only by an unauthorized rep must go to a human, not count as two sources)
DEAL_AUTHORITY = {"contract", "amendment", "auth_email"}       # documents that can set a deal term
WEAK_AUTHORITY = {"unauth_email", "call_note"}
OUTCOMES = ("correct", "recoverable", "conflict")
BUDGET_NOTE = "offline rules: outside this run's live-model budget"

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

DEBATE = (
    "This is the debate round. You voted once already; the other two judges' votes and reasons are below. "
    "Weigh their arguments against the evidence. Change your call only if they convinced you; keep it if not."
)

REVIEWER = (
    "You are a senior accounts-receivable reviewer auditing a three-judge panel's call on a delivery-payout "
    "discrepancy. Numbers were already computed in code; do NOT recompute them. Give your own call: 'correct', "
    "'recoverable' or 'conflict' (sources disagree or authority is unclear, a human must decide). "
    'Return {"outcome": ..., "confidence": 0..1, "reason": "one sentence"}.'
)

MEMO = (
    "A three-judge panel could not agree on a delivery-payout discrepancy, so a human will decide. You do NOT decide. "
    "Write a short escalation memo for that human (under 120 words, plain text, no em dashes): the position of each "
    "side and its strongest reason, then what specific evidence would settle it. Numbers were computed in code; "
    "do not recompute them."
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


def _payload(f: dict[str, Any]) -> dict[str, Any]:
    return {k: f[k] for k in ("check_type", "channel", "expected", "actual", "gap", "evidence", "reason")}


def _route(judge: str) -> tuple[str, str]:
    return llm.route("judge", list(JUDGES).index(judge))


def _parse_call(got: dict[str, Any] | None) -> dict[str, Any] | None:
    """A usable {outcome, confidence, reason} from a model reply, or None."""
    if got is None:
        return None
    outcome = str(got.get("outcome", "")).strip().lower()
    conf = _confidence(got.get("confidence"))
    if outcome in OUTCOMES and conf is not None:
        return {"outcome": outcome, "confidence": conf, "reason": str(got.get("reason", ""))[:300]}
    return None


def _brief(v: dict[str, Any]) -> dict[str, Any]:
    return {"judge": v["judge"], "outcome": v["outcome"], "confidence": v["confidence"], "reason": v["reason"]}


def vote(judge: str, f: dict[str, Any], live: bool = True) -> dict[str, Any]:
    if not live:  # outside the run's live-model budget: offline rules, said plainly on the vote
        v = _offline_vote(judge, f)
        return {**v, "judge": judge, "budget": "offline", "reason": f"[{BUDGET_NOTE}] {v['reason']}"}
    r = _route(judge)
    got, err = llm.call_json(JUDGES[judge] + "\n" + SYSTEM, json.dumps(_payload(f)), r=r)
    call = _parse_call(got)
    if call:
        return {**call, "judge": judge, "model": llm.label(r)}
    if got is not None:
        err = f"judge returned an unusable answer: {json.dumps(got)[:80]}"
    v = {**_offline_vote(judge, f), "judge": judge}
    if err:  # err is None only when we're offline on purpose
        v.update(reason=f"[offline fallback: {err}] {v['reason']}", fallback=err)
    return v


def revise(judge: str, f: dict[str, Any], own: dict[str, Any], others: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Debate round: the judge sees the other votes and may change its call.

    None when this judge can't debate (offline, or its first vote already fell back to offline rules).
    """
    r = _route(judge)
    if own.get("fallback") or own.get("budget") == "offline" or not llm.available(r):
        return None
    user = json.dumps({"finding": _payload(f), "your_vote": _brief(own), "other_judges": [_brief(o) for o in others]})
    got, err = llm.call_json(JUDGES[judge] + "\n" + SYSTEM + "\n" + DEBATE, user, r=r)
    call = _parse_call(got)
    if call is None:
        err = err or f"judge returned an unusable answer: {json.dumps(got)[:80]}"
        return {**own, "reason": f"[kept round 1 vote: {err}] {own['reason']}", "debate_error": err}
    return {**call, "judge": judge, "model": llm.label(r)}


def _debate(f: dict[str, Any], first: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """One round. Returns the round 2 votes, or None if no judge could take part."""
    with ThreadPoolExecutor(max_workers=len(first)) as pool:
        revised = list(pool.map(lambda v: revise(v["judge"], f, v, [o for o in first if o is not v]), first))
    if all(r is None for r in revised):
        return None
    second = []
    for own, new in zip(first, revised):
        new = dict(new if new is not None else own)
        new.update(round=2, revised=(new["outcome"], new["confidence"]) != (own["outcome"], own["confidence"]))
        second.append(new)
    return second


def audit_rate() -> float:
    try:
        return min(max(float(os.environ.get("FLOCK_AUDIT_RATE", DEFAULT_AUDIT_RATE)), 0.0), 1.0)
    except ValueError:
        return DEFAULT_AUDIT_RATE


def in_audit_sample(finding_id: str, rate: float | None = None) -> bool:
    """Deterministic: the same finding is always in or out of the sample for a given rate."""
    rate = audit_rate() if rate is None else rate
    bucket = int(hashlib.sha256(finding_id.encode()).hexdigest()[:8], 16) / 0x100000000
    return bucket < rate


def review(f: dict[str, Any], outcome: str, votes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Reviewer second opinion on a call the panel made on its own. None offline or on any failure."""
    r = llm.route("reviewer")
    if not llm.available(r):
        return None
    user = json.dumps({"finding": _payload(f), "panel_outcome": outcome, "panel_votes": [_brief(v) for v in votes]})
    call = _parse_call(llm.call_json(REVIEWER, user, r=r)[0])
    return {**call, "judge": "reviewer", "round": "audit", "model": llm.label(r)} if call else None


def memo(f: dict[str, Any], votes: list[dict[str, Any]]) -> str | None:
    """Escalation memo for the human who decides a split. The reviewer explains; it does not decide."""
    r = llm.route("reviewer")
    if not llm.available(r):
        return None
    user = json.dumps({"finding": _payload(f), "votes": [_brief(v) for v in votes]})
    text = llm.ask_text(MEMO, user, max_tokens=400, r=r)
    return re.sub(r"\s*—\s*", ", ", text)[:1200] if text else None


def weighted_mean(votes: list[dict[str, Any]], weights: dict[str, float] | None = None) -> float:
    """Average confidence, each judge weighted by its market balance share. No weights = plain mean."""
    w = [(weights or {}).get(v["judge"], 0.0) for v in votes]
    if not weights or sum(w) <= 0:
        return sum(v["confidence"] for v in votes) / len(votes)
    return round(sum(wi * v["confidence"] for wi, v in zip(w, votes)) / sum(w), 9)  # drop float noise from shares


def decide(f: dict[str, Any], rules: list[dict[str, Any]] | None = None,
           weights: dict[str, float] | None = None, live: bool = True) -> dict[str, Any]:
    """Returns the patch for the 'judge' transition: outcome, confidence, votes, reason (+ review_memo on a split).

    weights: judge -> balance share from flock.market.weights(); None means every judge counts the same.
    live=False: outside the run's live-model budget; offline rules only (no model call), marked on votes and reason.
    Pure apart from model calls, so the pipeline can run decide() for several findings at once.
    """
    rule = learner.matching_rule(f, rules)
    if rule:
        return {"outcome": rule["outcome"], "confidence": 1.0, "votes": [],
                "reason": f"Applied approved rule {rule['rule_id']}: {rule['description']}"}

    with ThreadPoolExecutor(max_workers=len(JUDGES)) as pool:  # judges are independent; run them at once
        first = list(pool.map(lambda j: vote(j, f) if live else vote(j, f, live=False), JUDGES))
    final, record, debated = first, first, ""
    if len({v["outcome"] for v in first}) > 1:
        second = _debate(f, first)
        if second:
            final, record = second, [{**v, "round": 1} for v in first] + second
            changed = [f"{a['judge']} {a['outcome']} -> {b['outcome']}" for a, b in zip(first, second)
                       if a["outcome"] != b["outcome"]]
            debated = "After one debate round " + (f"({', '.join(changed)}): " if changed else "(nobody changed their call): ")

    outcomes = {v["outcome"] for v in final}
    split = len(outcomes) > 1
    if weights:
        final = [{**v, "weight": round(weights.get(v["judge"], 0.0), 4)} for v in final]
        record = record[:len(record) - len(final)] + final
    mean = weighted_mean(final, weights)
    avg = round(mean, 2)
    how = "balance-weighted confidence" if weights else "avg confidence"
    if not split and mean >= THRESHOLD:  # compare the unrounded mean: 0.7466 is not 75%
        outcome = next(iter(outcomes))
        reason = f"{debated}All 3 judges agree ({avg:.0%} {how}). {final[0]['reason']}"
    else:
        outcome = "conflict"
        reason = debated + "Judges disagree or aren't confident enough; sending to a human. " + " | ".join(
            f"{v['judge']}: {v['outcome']} ({v['confidence']:.0%})" for v in final)
    panel_call = outcome  # what the judges decided before the code guardrails

    if within_tolerance(f) and outcome != "correct":
        outcome, reason = "correct", f"Gap ${money(abs(f['gap'])):.2f} is within the ${TOLERANCE:.2f} tolerance; not disputed. " + reason
    elif outcome == "recoverable" and weak_evidence(f):
        outcome, reason = "conflict", "No deal document or second source backs this gap; a human must confirm. " + reason

    fell_back = [v for v in final if v.get("fallback")]
    if fell_back:
        reason += f" ({len(fell_back)} of 3 judges used offline rules after an API error: {fell_back[0]['fallback']})"

    patch = {"outcome": outcome, "confidence": avg, "votes": record, "reason": reason}
    if not live:
        patch.update(reason=f"{reason} ({BUDGET_NOTE})", live_budget="outside")
        return patch  # no reviewer memo or audit second opinion either: both are model calls
    if outcome == "conflict" and split:
        text = memo(f, final)  # the reviewer explains the split to the human; it never breaks the tie
        if text:
            patch["review_memo"] = text
    elif (outcome == panel_call != "conflict" and not within_tolerance(f)
          and in_audit_sample(f["finding_id"])):
        second_opinion = review(f, outcome, final)
        if second_opinion:
            patch["votes"] = record + [second_opinion]
            who = f"the reviewer ({second_opinion['model']})"
            if second_opinion["outcome"] != outcome:
                patch["outcome"] = "conflict"
                patch["reason"] = (f"Audit sample: {who} disagrees with the panel's '{outcome}' and says "
                                   f"'{second_opinion['outcome']}': {second_opinion['reason']} Sending to a human. "
                                   f"Panel: {reason}")
            else:
                patch["reason"] = f"{reason} Audit sample: {who} agrees."
    return patch


ACTOR = Actor(id="panel", role="agent")
