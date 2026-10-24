"""Run Flock end to end.

  python -m flock.pipeline --reset                  # fresh run
  python -m flock.pipeline --reset --demo-approve   # also simulate the human: approve recoverables, reject conflicts
  python -m flock.pipeline --reset --forget         # also wipe learned rules (true day-one run)
  python -m flock.pipeline --reset --pace 0.4       # sleep between moves so the dashboard counters visibly tick

Collect -> Read -> Connect -> Deal terms -> Check -> Judge -> Rank -> Draft -> [human] -> File -> Learn
Uses real data from data/std/ once Maja and Thabhelo's agents produce it; until then, fixtures/fake_findings.json.
Each run appends a one-line summary to data/state/runs.jsonl (week 1, week 2, ...) for the scoreboard;
--forget starts that history over, because it's a true day one.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

from flock.agents import checkers, clicky, deal_keeper, dispute_writer, formatter, learner, matcher, panel, ranker
from flock.gatekeeper import transition
from flock.models import Actor, Finding
from flock.store import ROOT, STATE_DIR, Store, get_store

FIXTURES = ROOT / "fixtures" / "fake_findings.json"
RUNS = STATE_DIR / "runs.jsonl"
CHECKER = Actor(id="checkers", role="agent")
DEMO_HUMAN = Actor(id="demo-controller", role="human")


def detect() -> tuple[list[dict], str]:
    tables = formatter.format_all()
    stories, match_findings = matcher.match(tables)
    terms = deal_keeper.extract_terms()
    found = [f.to_dict() for f in match_findings]
    found += [f.to_dict() for f in checkers.run_all(stories, lambda s: {})] if stories and terms else []
    if found:
        return found, "real data (data/std)"
    fixtures = json.loads(FIXTURES.read_text())
    return [Finding(**f).to_dict() for f in fixtures], "fixtures/fake_findings.json"


def run(reset: bool = False, demo_approve: bool = False, forget: bool = False, pace: float = 0.0) -> Store:
    store = get_store()
    if reset:
        store.reset()
        (ROOT / "data" / "sandbox_portal" / "disputes.jsonl").unlink(missing_ok=True)
    if forget:
        learner.RULES.unlink(missing_ok=True)
        RUNS.unlink(missing_ok=True)

    def move(finding_id: str, action: str, actor: Actor, **kw) -> None:
        transition(store, finding_id, action, actor, **kw)
        if pace:
            time.sleep(pace)

    findings, source = detect()
    print(f"\n== Flock run on {source}: {len(findings)} findings ==")

    for f in findings:
        store.put_new(f)
        move(f["finding_id"], "check", CHECKER, reason="Checker computed expected vs actual in code")

    for f in store.all():
        if f["state"] == "CHECKED":
            move(f["finding_id"], "judge", panel.ACTOR, patch=panel.decide(f), reason="Panel vote")

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
            elif f["outcome"] == "conflict":
                move(f["finding_id"], "reject", DEMO_HUMAN,
                     reason="Rep had no authority to change the deal; accept the charge")
        filed = clicky.file_approved(store)
        print(f"Clicky filed {len(filed)} disputes in the sandbox portal")

    proposed = learner.propose(store)
    record_run(store, source)
    summary(store, proposed)
    return store


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
