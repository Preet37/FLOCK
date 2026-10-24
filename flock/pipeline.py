"""Run Flock end to end.

  python -m flock.pipeline --reset                  # fresh run
  python -m flock.pipeline --reset --demo-approve   # also simulate the human: approve recoverables, reject conflicts
  python -m flock.pipeline --reset --forget         # also wipe learned rules (true day-one run)
  python -m flock.pipeline --reset --pace 0.4       # sleep between moves so the dashboard counters visibly tick

Collect -> Read -> Connect -> Deal terms -> Check -> Judge -> Rank -> Draft -> [human] -> File -> Journal -> Learn
Journal (real data only): one draft journal entry per store/channel/payout, waiting for a human to approve.
Uses real data from data/std/ once Maja and Thabhelo's agents produce it; until then, fixtures/fake_findings.json.
Each run appends a one-line summary to data/state/runs.jsonl (week 1, week 2, ...) for the scoreboard;
--forget starts that history over, because it's a true day one.

Judge market (flock/market.py): the panel's votes are weighted by each judge's balance, every vote is recorded as a
stake, and bets settle through the gatekeeper when a human approves / rejects (or the platform pays). Balances carry
over between runs; --forget resets them. Conflicts are ordered by the value of a human looking; past the attention
budget (FLOCK_ATTENTION_BUDGET, default 10) they're marked 'deferred' and wait. The demo human skips deferred items.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flock import market
from flock.agents import (checkers, clicky, deal_keeper, dispute_writer, formatter, journal, learner, matcher, panel,
                          ranker)
from flock.gatekeeper import transition
from flock.models import Actor, Finding
from flock.store import ROOT, STATE_DIR, Store, get_journal_store, get_store

FIXTURES = ROOT / "fixtures" / "fake_findings.json"
RUNS = STATE_DIR / "runs.jsonl"
CHECKER = Actor(id="checkers", role="agent")
DEMO_HUMAN = Actor(id="demo-controller", role="human")


OPTIONAL_TABLES = ("error_charges", "refunds")  # extra std tables the checkers use when the formatter writes them


def detect_real(context: dict | None = None) -> list[dict]:
    """Matcher + Deal Keeper + Checkers on data/std/ and data/raw/{contracts,emails}. Empty list if no data yet.
    If `context` is given, it also gets what the Journal Entry Drafter needs: stories, terms_for, orders."""
    tables = dict(formatter.format_all())
    for name in OPTIONAL_TABLES:
        p = formatter.STD / f"{name}.csv"
        if p.exists():
            tables[name] = p
    stories, match_findings = matcher.match(tables)
    terms = deal_keeper.extract_terms()
    terms_for = lambda s: deal_keeper.terms_for(terms, s)  # noqa: E731
    found = [f.to_dict() for f in match_findings]
    found += [f.to_dict() for f in checkers.run_all(stories, terms_for)]
    if context is not None and stories:
        context.update(stories=stories, terms_for=terms_for, orders=matcher.load_tables(tables)["orders"])
    return found


def detect(fixtures: Path | None = None, context: dict | None = None) -> tuple[list[dict], str]:
    found = detect_real(context) if fixtures is None else []
    if found:
        return found, "real data (data/std)"
    if context is not None:
        context.clear()  # fixtures have no payout tables: nothing to journal
    path = Path(fixtures or FIXTURES)
    rows = json.loads(path.read_text())
    name = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name
    return [Finding(**f).to_dict() for f in rows], str(name)


def run(reset: bool = False, demo_approve: bool = False, forget: bool = False, pace: float = 0.0,
        store: Store | None = None, rules_path: Path | None = None, portal: Path | None = None,
        fixtures: Path | None = None, attention_budget: int | None = None) -> Store:
    """store / rules_path / portal / fixtures default to the repo paths; tests point them at a temp dir.
    attention_budget defaults to env FLOCK_ATTENTION_BUDGET (10)."""
    store = store or get_store()
    rules_path = Path(rules_path or learner.RULES)
    portal = Path(portal or clicky.PORTAL)
    run_id = "RUN-" + uuid.uuid4().hex[:8]
    jstore = get_journal_store(store)
    if reset:
        store.reset()
        jstore.reset()
        portal.unlink(missing_ok=True)
        market.void_open(store, "findings wiped by pipeline --reset")
    if forget:
        rules_path.unlink(missing_ok=True)
        RUNS.unlink(missing_ok=True)
        market.reset(store)

    def move(finding_id: str, action: str, actor: Actor, **kw) -> None:
        transition(store, finding_id, action, actor, **kw)
        if pace:
            time.sleep(pace)

    context: dict = {}
    findings, source = detect(fixtures, context)
    print(f"\n== Flock run on {source}: {len(findings)} findings ==")

    for f in findings:
        store.put_new(f)
        move(f["finding_id"], "check", CHECKER, reason="Checker computed expected vs actual in code")

    rules = learner.approved_rules(rules_path)
    weights = market.weights(store)  # balances carried over from earlier weeks' settled bets
    decisions = {f["finding_id"]: panel.decide(f, rules, weights) for f in store.all() if f["state"] == "CHECKED"}
    escalations = [{**store.get(fid), **d} for fid, d in decisions.items() if d["outcome"] == "conflict"]
    attention = market.allocate_attention(escalations, attention_budget, weights)
    for fid, d in decisions.items():
        patch = {**d, "attention": attention[fid]} if fid in attention else d  # labels only; still a conflict
        move(fid, "judge", panel.ACTOR, patch=patch, reason="Panel vote")
        market.record_stakes(store, fid, d["votes"], run_id)

    for f in store.all():
        if f["state"] == "JUDGED" and f["outcome"] == "recoverable":
            move(f["finding_id"], "rank", ranker.ACTOR, patch={"rank_score": ranker.score(f)},
                 reason="gap x win probability - effort")

    for f in ranker.within_budget([x for x in store.all() if x["state"] == "JUDGED" and x["outcome"] == "recoverable"]):
        move(f["finding_id"], "draft", dispute_writer.ACTOR,
             patch={"dispute_draft": dispute_writer.draft(f)}, reason="Drafted dispute, waiting for human")

    if demo_approve:
        for f in store.all():
            if f["state"] != "JUDGED":
                continue
            if f["outcome"] == "recoverable" and f.get("dispute_draft"):
                move(f["finding_id"], "approve", DEMO_HUMAN, reason="Evidence checks out")
            elif f["outcome"] == "conflict" and f.get("attention", {}).get("status") != "deferred":
                move(f["finding_id"], "reject", DEMO_HUMAN,
                     reason="Rep had no authority to change the deal; accept the charge")
        filed = clicky.file_approved(store, portal)
        print(f"Clicky filed {len(filed)} disputes in the sandbox portal")

    if context.get("stories"):
        entries = journal.draft(context["stories"], store.all(), context["terms_for"], context["orders"])
        drafted = journal.draft_into(jstore, entries)
        bad = sum(1 for je in entries if not je["balanced"])
        print(f"Journal drafted {len(drafted)} entries (store x channel x payout), {bad} unbalanced; "
              f"waiting for a human to approve")

    proposed = learner.propose(store, rules_path)
    record_run(store, source)
    summary(store, proposed)
    market_summary(store)
    return store


def market_summary(store: Store) -> None:
    s = market.snapshot(store)
    print("judge market: " + " | ".join(f"{r['judge'].replace('judge.', '')} {r['balance']:.2f}" for r in s["leaderboard"])
          + f" ({s['settled']} bets settled, {s['open_bets']} open)")
    a = s["attention"]
    if a["escalations"]:
        print(f"attention: {a['used']}/{a['budget']} tokens used"
              + (f", {len(a['deferred'])} deferred: {', '.join(d['finding_id'] for d in a['deferred'])}" if a["deferred"] else ""))


def record_run(store: Store, source: str) -> dict:
    """One line per run so the scoreboard can show week 1 vs week 2 human hand-offs."""
    rows = store.all()
    rec = [f for f in rows if f.get("outcome") == "recoverable"]
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "findings": len(rows),
        "recoverable": len(rec),
        "dollars": round(sum(abs(f["gap"]) for f in rec), 2),
        "to_human": sum(1 for f in rows if f.get("outcome") == "conflict"),
        "by_rule": sum(1 for f in rows if f.get("reason", "").startswith("Applied approved rule")),
        "learned_rules": len(learner.approved_rules()),
    }
    RUNS.parent.mkdir(parents=True, exist_ok=True)
    with RUNS.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def summary(store: Store, proposed: list[dict]) -> None:
    rows = store.all()
    by = lambda o: [f for f in rows if f.get("outcome") == o]
    rec = by("recoverable")
    print(f"correct: {len(by('correct'))} | recoverable: {len(rec)} (${sum(abs(f['gap']) for f in rec):.2f}) "
          f"| sent to a human: {len(by('conflict'))}")
    for f in sorted(rows, key=lambda x: x["finding_id"]):
        print(f"  {f['finding_id']} {f['channel']:<10} {f['check_type']:<13} gap {f['gap']:>7.2f}  "
              f"{f['outcome']:<11} {f['state']:<9} {f['reason'][:70]}")
    denied = [e for e in store.log() if not e["allowed"]]
    print(f"audit log: {len(store.log())} entries ({len(denied)} denied)")
    if proposed:
        print("Learner proposes:")
        for r in proposed:
            print(f"  {r['rule_id']}: {r['description']} (evidence: {r['evidence_count']})")
        print("  approve with: python -m flock.agents.learner --approve R-001")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--demo-approve", action="store_true")
    ap.add_argument("--forget", action="store_true")
    ap.add_argument("--pace", type=float, default=0.0, help="seconds to sleep after each move (for the live board)")
    a = ap.parse_args()
    run(reset=a.reset, demo_approve=a.demo_approve, forget=a.forget, pace=a.pace)
