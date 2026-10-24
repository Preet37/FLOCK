"""Run Flock end to end.

  python -m flock.pipeline --reset                  # fresh run
  python -m flock.pipeline --reset --demo-approve   # also simulate the human: approve recoverables, reject conflicts
  python -m flock.pipeline --reset --forget         # also wipe learned rules (true day-one run)

Collect -> Read -> Connect -> Deal terms -> Check -> Judge -> Rank -> Draft -> [human] -> File -> Learn
Uses real data from data/std/ once Maja and Thabhelo's agents produce it; until then, fixtures/fake_findings.json.
"""
from __future__ import annotations

import argparse
import json

from flock.agents import checkers, clicky, deal_keeper, dispute_writer, formatter, learner, matcher, panel, ranker
from flock.gatekeeper import transition
from flock.models import Actor, Finding
from flock.store import ROOT, JsonStore

FIXTURES = ROOT / "fixtures" / "fake_findings.json"
CHECKER = Actor(id="checkers", role="agent")
DEMO_HUMAN = Actor(id="demo-controller", role="human")


OPTIONAL_TABLES = ("error_charges", "refunds")  # extra std tables the checkers use when the formatter writes them


def detect_real() -> list[dict]:
    """Matcher + Deal Keeper + Checkers on data/std/ and data/raw/{contracts,emails}. Empty list if no data yet."""
    tables = dict(formatter.format_all())
    for name in OPTIONAL_TABLES:
        p = formatter.STD / f"{name}.csv"
        if p.exists():
            tables[name] = p
    stories, match_findings = matcher.match(tables)
    terms = deal_keeper.extract_terms()
    found = [f.to_dict() for f in match_findings]
    found += [f.to_dict() for f in checkers.run_all(stories, lambda s: deal_keeper.terms_for(terms, s))]
    return found


def detect() -> tuple[list[dict], str]:
    found = detect_real()
    if found:
        return found, "real data (data/std)"
    fixtures = json.loads(FIXTURES.read_text())
    return [Finding(**f).to_dict() for f in fixtures], "fixtures/fake_findings.json"


def run(reset: bool = False, demo_approve: bool = False, forget: bool = False) -> JsonStore:
    store = JsonStore()
    if reset:
        store.reset()
        (ROOT / "data" / "sandbox_portal" / "disputes.jsonl").unlink(missing_ok=True)
    if forget:
        learner.RULES.unlink(missing_ok=True)

    findings, source = detect()
    print(f"\n== Flock run on {source}: {len(findings)} findings ==")

    for f in findings:
        store.put_new(f)
        transition(store, f["finding_id"], "check", CHECKER, reason="Checker computed expected vs actual in code")

    for f in store.all():
        if f["state"] == "CHECKED":
            transition(store, f["finding_id"], "judge", panel.ACTOR, patch=panel.decide(f), reason="Panel vote")

    for f in store.all():
        if f["state"] == "JUDGED" and f["outcome"] == "recoverable":
            transition(store, f["finding_id"], "rank", ranker.ACTOR, patch={"rank_score": ranker.score(f)},
                       reason="gap x win probability - effort")

    for f in ranker.within_budget([x for x in store.all() if x["state"] == "JUDGED" and x["outcome"] == "recoverable"]):
        transition(store, f["finding_id"], "draft", dispute_writer.ACTOR,
                   patch={"dispute_draft": dispute_writer.draft(f)}, reason="Drafted dispute, waiting for human")

    if demo_approve:
        for f in store.all():
            if f["state"] != "JUDGED":
                continue
            if f["outcome"] == "recoverable" and f.get("dispute_draft"):
                transition(store, f["finding_id"], "approve", DEMO_HUMAN, reason="Evidence checks out")
            elif f["outcome"] == "conflict":
                transition(store, f["finding_id"], "reject", DEMO_HUMAN,
                           reason="Rep had no authority to change the deal; accept the charge")
        filed = clicky.file_approved(store)
        print(f"Clicky filed {len(filed)} disputes in the sandbox portal")

    proposed = learner.propose(store)
    summary(store, proposed)
    return store


def summary(store: JsonStore, proposed: list[dict]) -> None:
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
    a = ap.parse_args()
    run(reset=a.reset, demo_approve=a.demo_approve, forget=a.forget)
