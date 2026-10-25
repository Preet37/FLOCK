"""Judge market (owner: Preet). The panel's confidence bets settle against reality, and human attention is a budget.

A. Bets that settle against reality
  Every judge vote is a forecast p = P(the finding is recoverable):
      'recoverable' at confidence c -> p = c      'correct' at c -> p = 1 - c      'conflict' -> p = 0.5 (no lean)
  The bet is placed when the panel votes (a 'stake' entry). It settles when ground truth arrives through the gatekeeper:
      human approve  -> truth 'recoverable'   (dispute it: the money is owed)
      human reject   -> truth 'correct'       (accept the charge; e.g. a non-binding rep email was overruled)
      RECOVERED      -> truth 'recoverable'   (the platform paid; only settles bets still open)
  Payout uses the Brier score, a proper scoring rule, rescaled so a coin flip pays 0:
      payout = SCALE * (1 - 4 * (p - y)^2)     y = 1 if truth is 'recoverable' else 0
  So a confident right call earns up to +10, "I don't know" (a conflict vote) earns 0, a confident wrong call loses up
  to 30. Why not "win or lose your stake"? Then expected profit is stake * (2q - 1), so any judge that is right more than
  half the time maximises profit by always claiming 100%: it rewards overconfidence. Under a proper rule the best
  strategy is to report your true confidence, so a balance measures calibration, not bravado. Brier over log: bounded
  (one bad call can't zero a judge) and it's the forecasting standard. Each stake entry records what the judge can
  win and what it has at risk. A judge never loses more than its balance.

  Balances start at START and are derived from the ledger (never stored). panel.decide weights each judge's confidence
  by its balance share. The unanimity rule and the code guardrails stay on top, so weights can only move a unanimous
  call across the THRESHOLD; they can never overrule a disagreement, the tolerance rule or the weak-evidence rule.

B. Human attention is a budget
  Each period (one pipeline run = one week of statements) the controller has FLOCK_ATTENTION_BUDGET tokens (default 10).
  Escalations (outcome 'conflict') are ordered by the value of a human looking:
      value = |gap| * (1 - weighted confidence)      weighted confidence = |2 * p_bar - 1|, p_bar = balance-weighted p
  The top N are 'queued'; the rest are 'deferred': still visible, still a conflict, still waiting for a human. The budget
  never turns an escalation into an automatic decision.

The ledger (market_ledger.jsonl in the store's state dir) is append-only. Kinds: stake, settle, void (bets whose findings
were wiped by --reset), reset (pipeline --forget: balances back to START; history before it is kept, just not counted).

  python -m flock.market                    # leaderboard, open bets, attention
  python -m flock.market --recover F-0001   # a human marks a FILED dispute paid (through the gatekeeper)
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flock.models import Actor, money
from flock.store import STATE_DIR, Store, get_store

LEDGER_NAME = "market_ledger.jsonl"
START = 100.0
SCALE = 10.0
DEFAULT_BUDGET = 10
JUDGES = ("judge.strict", "judge.operator", "judge.skeptic")  # same order as panel.JUDGES

TRUTH = {"approve": "recoverable", "reject": "correct", "recover": "recoverable"}
SETTLE_ROLES = {"approve": {"human"}, "reject": {"human"}, "recover": {"human", "gatekeeper"}}
CLI_HUMAN = Actor(id="cli-human", role="human")

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ledger_path(store: Store | None) -> Path:
    return Path(getattr(store, "dir", STATE_DIR)) / LEDGER_NAME


def entries(store: Store | None, since_reset: bool = True) -> list[dict[str, Any]]:
    """Ledger entries oldest first; by default only those after the latest 'reset'."""
    path = ledger_path(store)
    if not path.exists():
        return []
    lines = path.read_text().split("\n")
    rows = [json.loads(line) for line in lines[:-1] if line.strip()]  # last piece: "" or a line still being written
    if since_reset:
        last = max((i for i, r in enumerate(rows) if r["kind"] == "reset"), default=-1)
        rows = rows[last + 1:]
    return rows


def _append(store: Store | None, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path = ledger_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


# --- scoring -----------------------------------------------------------------

def forecast(vote: dict[str, Any]) -> float:
    """P(recoverable) implied by one vote."""
    c = float(vote["confidence"])
    return {"recoverable": c, "correct": 1 - c}.get(vote["outcome"], 0.5)


def payout(p: float, truth: str) -> float:
    y = 1.0 if truth == "recoverable" else 0.0
    return round(SCALE * (1 - 4 * (p - y) ** 2), 2)


def stake_size(confidence: float) -> int:
    """Headline bet size shown in the UI: credits the judge puts behind its call."""
    return round(10 * float(confidence))


# --- balances ----------------------------------------------------------------

def balances(store: Store | None) -> dict[str, float]:
    bal = {j: START for j in JUDGES}
    for e in entries(store):
        if e["kind"] == "settle":
            bal[e["judge"]] = money(bal.get(e["judge"], START) + e["payout"])
    return bal


def weights(store: Store | None) -> dict[str, float]:
    """Balance share per judge, for panel.decide. A judge at zero has no say in the weighted confidence."""
    bal = {j: max(b, 0.0) for j, b in balances(store).items()}
    total = sum(bal.values())
    if total <= 0:
        return {j: 1 / len(bal) for j in bal}
    return {j: b / total for j, b in bal.items()}


def history(store: Store | None) -> dict[str, list[float]]:
    """Balance after each settlement, per judge, starting at START (for sparklines)."""
    bal = {j: START for j in JUDGES}
    hist = {j: [START] for j in JUDGES}
    for e in entries(store):
        if e["kind"] == "settle":
            j = e["judge"]
            bal[j] = money(bal.get(j, START) + e["payout"])
            hist.setdefault(j, [START]).append(bal[j])
    return hist


def open_bets(store: Store | None, finding_id: str | None = None) -> list[dict[str, Any]]:
    rows = entries(store)
    closed = {e["bet_id"] for e in rows if e["kind"] in ("settle", "void")}
    return [e for e in rows if e["kind"] == "stake" and e["bet_id"] not in closed
            and (finding_id is None or e["finding_id"] == finding_id)]


# --- writing the ledger --------------------------------------------------------

def final_votes(votes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each judge's last word: the after-debate vote if there was a debate. The reviewer is not a judge and never bets."""
    judges = [v for v in votes if v.get("round") != "audit" and v.get("judge") != "reviewer"]
    if any(v.get("round") == 2 for v in judges):
        judges = [v for v in judges if v.get("round") == 2]
    return judges


def record_stakes(store: Store, finding_id: str, votes: list[dict[str, Any]], run_id: str = "") -> list[dict[str, Any]]:
    """One stake per judge, on its final vote. Rule-settled findings have no votes, so no bets."""
    rows = []
    for v in final_votes(votes):
        p = forecast(v)
        rows.append({
            "kind": "stake", "at": _now(), "bet_id": "B-" + uuid.uuid4().hex[:8], "run_id": run_id,
            "finding_id": finding_id, "judge": v["judge"], "outcome": v["outcome"],
            "confidence": float(v["confidence"]), "p_recoverable": round(p, 4), "stake": stake_size(v["confidence"]),
            "to_win": max(payout(p, "recoverable"), payout(p, "correct")),
            "at_risk": max(0.0, -min(payout(p, "recoverable"), payout(p, "correct"))),
        })
    _append(store, rows)
    return rows


def settle(store: Store, finding_id: str, action: str) -> list[dict[str, Any]]:
    """Settle every open bet on a finding after a gatekeeper-approved approve / reject / recover.

    Requires the matching allowed entry in the audit log, so nothing can settle a bet without a real
    human (or gatekeeper) move. Idempotent: a bet settles once; RECOVERED after approve finds nothing open.
    """
    if action not in TRUTH:
        raise ValueError(f"'{action}' is not ground truth; settles on {', '.join(TRUTH)}")
    moved = [e for e in store.log(finding_id)
             if e["allowed"] and e["action"] == action and e["role"] in SETTLE_ROLES[action]]
    if not moved:
        raise PermissionError(f"no allowed '{action}' by {'/'.join(sorted(SETTLE_ROLES[action]))} for {finding_id} "
                              "in the audit log; bets settle only on gatekeeper-recorded truth")
    truth = TRUTH[action]
    bal = balances(store)
    rows = []
    for b in open_bets(store, finding_id):
        pay = payout(b["p_recoverable"], truth)
        pay = max(pay, -bal.get(b["judge"], START))  # can't lose more than it has
        bal[b["judge"]] = money(bal.get(b["judge"], START) + pay)
        rows.append({
            "kind": "settle", "at": _now(), "bet_id": b["bet_id"], "finding_id": finding_id, "judge": b["judge"],
            "called": b["outcome"], "confidence": b["confidence"], "truth": truth, "source": action,
            "decided_by": moved[-1]["actor"], "payout": pay, "balance": bal[b["judge"]],
            "result": "won" if pay > 0 else "lost" if pay < 0 else "abstained",
        })
    _append(store, rows)
    return rows


def on_transition(store: Store, finding_id: str, action: str) -> list[dict[str, Any]]:
    """Called by the gatekeeper after an allowed move. Only ground-truth actions touch the market."""
    return settle(store, finding_id, action) if action in TRUTH else []


def void_open(store: Store, reason: str) -> list[dict[str, Any]]:
    """Close open bets whose findings were wiped (pipeline --reset) without paying anyone."""
    rows = [{"kind": "void", "at": _now(), "bet_id": b["bet_id"], "finding_id": b["finding_id"],
             "judge": b["judge"], "reason": reason} for b in open_bets(store)]
    _append(store, rows)
    return rows


def reset(store: Store, reason: str = "pipeline --forget") -> None:
    """Balances back to START. Appends a marker; never deletes history."""
    _append(store, [{"kind": "reset", "at": _now(), "reason": reason}])


# --- attention budget ------------------------------------------------------------

def budget() -> int:
    try:
        return max(0, int(os.environ.get("FLOCK_ATTENTION_BUDGET", DEFAULT_BUDGET)))
    except ValueError:
        return DEFAULT_BUDGET


def weighted_confidence(votes: list[dict[str, Any]], w: dict[str, float] | None = None) -> float:
    """How sure the balance-weighted panel is about which way the money goes: |2 * p_bar - 1|, 0..1."""
    if not votes:
        return 1.0
    ws = [w.get(v["judge"], 0.0) if w else 1.0 for v in votes]
    if sum(ws) <= 0:
        ws = [1.0] * len(votes)
    total = sum(ws)
    p_bar = sum(wi * forecast(v) for wi, v in zip(ws, votes)) / total
    return abs(2 * p_bar - 1)


def attention_value(f: dict[str, Any], w: dict[str, float] | None = None) -> float:
    """Expected value of a human looking: dollars at stake x how unsure the panel is."""
    return round(abs(f["gap"]) * (1 - weighted_confidence(final_votes(f.get("votes", [])), w)), 2)


def allocate_attention(escalations: list[dict[str, Any]], tokens: int | None = None,
                       w: dict[str, float] | None = None) -> dict[str, dict[str, Any]]:
    """finding_id -> {value, rank, status, budget}. Top `tokens` by value are 'queued', the rest 'deferred'.

    Only ever labels; the caller keeps every escalation a 'conflict' that waits for a human.
    """
    tokens = budget() if tokens is None else tokens
    ranked = sorted(escalations, key=lambda f: (-attention_value(f, w), -abs(f["gap"]), f["finding_id"]))
    return {f["finding_id"]: {"value": attention_value(f, w), "rank": i, "budget": tokens,
                              "status": "queued" if i <= tokens else "deferred"}
            for i, f in enumerate(ranked, 1)}


def attention_summary(findings: list[dict[str, Any]], log: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Budget used / remaining for the current period (the findings in the store)."""
    esc = [f for f in findings if f.get("attention")]
    tokens = max((f["attention"].get("budget", 0) for f in esc), default=budget())
    queued = [f for f in esc if f["attention"]["status"] == "queued"]
    deferred = [f for f in esc if f["attention"]["status"] == "deferred"]
    decided = {e["subject_id"] for e in (log or []) if e["allowed"] and e["role"] == "human"
               and e["action"] in ("approve", "reject")}
    item = lambda f: {"finding_id": f["finding_id"], "channel": f["channel"], "check_type": f["check_type"],
                      "gap": f["gap"], "state": f["state"], **f["attention"], "decided": f["finding_id"] in decided}
    return {
        "budget": tokens, "used": len(queued), "remaining": max(0, tokens - len(queued)),
        "escalations": len(esc), "decided": sum(1 for f in esc if f["finding_id"] in decided),
        "queued": [item(f) for f in sorted(queued, key=lambda f: f["attention"]["rank"])],
        "deferred": [item(f) for f in sorted(deferred, key=lambda f: f["attention"]["rank"])],
    }


# --- read model for the dashboard -------------------------------------------------

def snapshot(store: Store, recent: int = 25) -> dict[str, Any]:
    rows = entries(store)
    bal, w, hist = balances(store), weights(store), history(store)
    settles = [e for e in rows if e["kind"] == "settle"]
    opened = open_bets(store)
    board = []
    for j in JUDGES:
        mine = [e for e in settles if e["judge"] == j]
        board.append({
            "judge": j, "balance": bal[j], "weight": round(w[j], 4), "history": hist[j],
            "won": sum(1 for e in mine if e["result"] == "won"), "lost": sum(1 for e in mine if e["result"] == "lost"),
            "abstained": sum(1 for e in mine if e["result"] == "abstained"),
            "open": sum(1 for e in opened if e["judge"] == j), "pnl": money(bal[j] - START),
        })
    board.sort(key=lambda r: -r["balance"])
    return {
        "start": START, "scale": SCALE, "scoring": "brier",
        "rule": "payout = 10 x (1 - 4 x (p - y)^2); p = P(recoverable) from the vote, y = 1 if truth is recoverable",
        "leaderboard": board,
        "open_bets": len(opened), "settled": len(settles),
        "recent": [e for e in reversed(rows) if e["kind"] in ("stake", "settle", "void")][:recent],
        "attention": attention_summary(store.all(), store.log()),
    }


def _print(store: Store) -> None:
    s = snapshot(store)
    print(f"Judge market ({s['settled']} settled, {s['open_bets']} open bets; Brier payout, start {START:.0f})")
    for r in s["leaderboard"]:
        print(f"  {r['judge']:<15} {r['balance']:>8.2f}  weight {r['weight']:.0%}  "
              f"won {r['won']} lost {r['lost']} abstained {r['abstained']} open {r['open']}")
    a = s["attention"]
    print(f"Attention: {a['used']}/{a['budget']} tokens used, {len(a['deferred'])} deferred")
    for d in a["deferred"]:
        print(f"  deferred {d['finding_id']} value {d['value']:.2f} (gap {d['gap']:.2f})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--recover", metavar="FINDING_ID", help="a human marks a FILED dispute as paid by the platform")
    a = ap.parse_args()
    st = get_store()
    if a.recover:
        from flock.gatekeeper import Denied, transition  # the market never moves a finding itself
        try:
            transition(st, a.recover, "recover", CLI_HUMAN, reason="Platform paid the dispute (marked by a human)")
        except Denied as e:  # logged by the gatekeeper; on stage, a plain sentence instead of a traceback
            raise SystemExit(f"market: {e} (only a FILED dispute can be marked recovered)") from None
        except KeyError:
            raise SystemExit(f"market: no finding {a.recover}") from None
        print(f"{a.recover} RECOVERED")
    _print(st)
