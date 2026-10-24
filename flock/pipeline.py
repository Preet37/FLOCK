"""Run Flock end to end.

  python -m flock.pipeline --reset                  # fresh run
  python -m flock.pipeline --reset --demo-approve   # also simulate the human: approve recoverables, reject conflicts
  python -m flock.pipeline --reset --forget         # also wipe learned rules (true day-one run)
  python -m flock.pipeline --reset --pace 0.4       # sleep between moves so the dashboard counters visibly tick
  python -m flock.pipeline --reset --raw DIR --std DIR --mappings FILE --confirm   # another dataset (flock.demo)

Collect -> Read -> Connect -> Deal terms -> Check -> Judge -> Rank -> Draft -> [human] -> File -> Learn
Uses real data from data/std/ once Maja and Thabhelo's agents produce it; until then, fixtures/fake_findings.json.
Each run appends a one-line summary to data/state/runs.jsonl (week 1, week 2, ...) for the scoreboard;
--forget starts that history over, because it's a true day one.
Progress (stage, done/total, rows/sec) goes to the terminal and to data/state/progress.json for the dashboard.
At year scale detection runs per store in parallel (flock/scan.py) and the store batches its writes.

Judge market (flock/market.py): the panel's votes are weighted by each judge's balance, every final vote is recorded as
a stake, and bets settle through the gatekeeper when a human approves / rejects (or the platform pays). Balances carry
over between runs; --forget resets them. Conflicts are ordered by the value of a human looking; past the attention
budget (FLOCK_ATTENTION_BUDGET, default 10) they're marked 'deferred' and wait. The demo human skips deferred items.
Journal entries (flock/agents/journal.py) are drafted from the matched payouts after the human decisions, waiting for
a human to approve; at year scale (split detection) they are skipped with a note.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flock import llm, market, scan
from flock.agents import clicky, deal_keeper, dispute_writer, formatter, journal, learner, panel, ranker
from flock.gatekeeper import transition
from flock.models import Actor, Finding
from flock.progress import Progress, ensure, progress_path
from flock.store import ROOT, STATE_DIR, Store, batched, get_journal_store, get_store

FIXTURES = ROOT / "fixtures" / "fake_findings.json"
RUNS = STATE_DIR / "runs.jsonl"
CHECKER = Actor(id="checkers", role="agent")
DEMO_HUMAN = Actor(id="demo-controller", role="human")
SHOW_FINDINGS = 40
AIRGAP_NAME = "airgap.json"  # where this run's model calls went (llm.airgap_report), for GET /api/airgap
LIVE_KEY = "answer_key.csv"  # flock.demo --keep leaves its generated key here (scripts/score.py reads it)  # summary lines printed; a year of data has thousands of findings


OPTIONAL_TABLES = ("error_charges", "refunds")  # extra std tables the checkers use when the formatter writes them


def detect_real(raw: Path | None = None, std: Path | None = None, mappings: Path | None = None,
                confirm: bool = False, interactive: bool = False, progress: Progress | None = None,
                context: dict | None = None) -> list[dict]:
    """Formatter + Matcher + Deal Keeper + Checkers on data/raw -> data/std (or the given folders).
    Empty list if no data yet. If `context` is given, it also gets what the Journal Entry Drafter needs
    (stories, terms_for, orders) when the data is small enough to match in one process."""
    std = Path(std or formatter.STD)
    raw = Path(raw or formatter.RAW)
    tables = dict(formatter.format_all(raw, std, mappings, confirm=confirm, interactive=interactive,
                                       progress=progress))
    for name in OPTIONAL_TABLES:
        p = std / f"{name}.csv"
        if p.exists():
            tables[name] = p
    terms = deal_keeper.extract_terms(raw / "contracts", raw / "emails") if raw != formatter.RAW \
        else deal_keeper.extract_terms()
    return hold_unread(scan.detect(tables, terms, progress, context=context), formatter.pending_layouts(std))


def hold_unread(found: list[dict], pending: list[dict]) -> list[dict]:
    """A statement still waiting for a human to confirm its layout was not read, so "sold, never paid" for that
    channel can't be decided yet: those findings are held back (not disputed), and the run says so."""
    channels = {p["source"] for p in pending}
    if not channels:
        return found
    held = [f for f in found if f["check_type"] == "missing_order" and f["channel"] in channels and not f["actual"]]
    if held:
        print(f"[pipeline] held back {len(held)} 'sold, never paid' checks for {', '.join(sorted(channels))}: "
              f"{len(pending)} statement(s) wait for a human to confirm the layout "
              "(answer y, or run with --confirm).")
    ids = {id(f) for f in held}
    return [f for f in found if id(f) not in ids]


def detect(fixtures: Path | None = None, progress: Progress | None = None, context: dict | None = None,
           **data) -> tuple[list[dict], str]:
    found = detect_real(progress=progress, context=context, **data) if fixtures is None else []
    if found:
        where = data.get("std")
        return found, f"real data ({where})" if where else "real data (data/std)"
    if context is not None:
        context.clear()  # fixtures have no payout tables: nothing to journal
    path = Path(fixtures or FIXTURES)
    rows = json.loads(path.read_text())
    name = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name
    return [Finding(**f).to_dict() for f in rows], str(name)


def run(reset: bool = False, demo_approve: bool = False, forget: bool = False, pace: float = 0.0,
        store: Store | None = None, rules_path: Path | None = None, portal: Path | None = None,
        fixtures: Path | None = None, raw: Path | None = None, std: Path | None = None,
        mappings: Path | None = None, confirm: bool = False, interactive: bool = False,
        progress: Progress | None = None, runs_path: Path | None = None, label: str = "",
        attention_budget: int | None = None) -> Store:
    """store / rules_path / portal / fixtures default to the repo paths; tests point them at a temp dir.
    raw / std / mappings point the Formatter at another dataset (flock.demo); confirm / interactive are its
    unseen-layout question. progress defaults to a terminal bar + progress.json next to the store.
    attention_budget defaults to env FLOCK_ATTENTION_BUDGET (10)."""
    store = store or get_store()
    rules_path = Path(rules_path or learner.RULES)
    portal = Path(portal or clicky.PORTAL)
    runs_path = Path(runs_path or RUNS)
    if progress is None:
        progress = Progress(progress_path(store), run=label or "pipeline")
    jstore = get_journal_store(store)
    if reset:
        store.reset()
        jstore.reset()
        portal.unlink(missing_ok=True)
        market.void_open(store, "findings wiped by pipeline --reset")
        (Path(getattr(store, "dir", STATE_DIR)) / LIVE_KEY).unlink(missing_ok=True)  # a kept live demo's key
    if forget:
        rules_path.unlink(missing_ok=True)
        runs_path.unlink(missing_ok=True)
        market.reset(store)
    data = {k: v for k, v in (("raw", raw), ("std", std), ("mappings", mappings)) if v is not None}
    if data or confirm or interactive:
        data.update(confirm=confirm, interactive=interactive)
    llm.reset_calls()  # the air-gap report counts this run's model calls only
    try:
        with batched(store):
            _run(store, rules_path, portal, fixtures, data, demo_approve, pace, progress, runs_path,
                 jstore, attention_budget)
    except BaseException:
        progress.finish("failed")
        raise
    progress.finish()
    return store


def _run(store: Store, rules_path: Path, portal: Path, fixtures: Path | None, data: dict, demo_approve: bool,
         pace: float, progress: Progress, runs_path: Path, jstore: Store, attention_budget: int | None) -> None:
    run_id = "RUN-" + uuid.uuid4().hex[:8]

    def move(finding_id: str, action: str, actor: Actor, **kw) -> None:
        transition(store, finding_id, action, actor, **kw)
        if pace:
            time.sleep(pace)

    context: dict = {}
    findings, source = detect(fixtures, progress, context, **data)
    print(f"\n== Flock run on {source}: {len(findings)} findings ==")

    progress.stage("log findings", total=len(findings), unit="findings")
    for i, f in enumerate(findings, 1):
        store.put_new(f)
        move(f["finding_id"], "check", CHECKER, reason="Checker computed expected vs actual in code")
        progress.update(i)

    rules = learner.approved_rules(rules_path)
    weights = market.weights(store)  # balances carried over from earlier weeks' settled bets
    todo = [f for f in store.all() if f["state"] == "CHECKED"]
    progress.stage("judge", total=len(todo), unit="findings")
    decisions = {}
    for i, f in enumerate(todo, 1):
        decisions[f["finding_id"]] = panel.decide(f, rules, weights)
        progress.update(i)
    escalations = [{**store.get(fid), **d} for fid, d in decisions.items() if d["outcome"] == "conflict"]
    attention = market.allocate_attention(escalations, attention_budget, weights)
    for fid, d in decisions.items():
        patch = {**d, "attention": attention[fid]} if fid in attention else d  # labels only; still a conflict
        move(fid, "judge", panel.ACTOR, patch=patch, reason="Panel vote")
        market.record_stakes(store, fid, d["votes"], run_id)

    todo = [f for f in store.all() if f["state"] == "JUDGED" and f["outcome"] == "recoverable"]
    progress.stage("rank", total=len(todo), unit="findings")
    for i, f in enumerate(todo, 1):
        move(f["finding_id"], "rank", ranker.ACTOR, patch={"rank_score": ranker.score(f)},
             reason="gap x win probability - effort")
        progress.update(i)

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
    elif source.startswith("real data"):
        print("Journal: skipped at year scale (split detection keeps no order stories); run a week at a time to draft")

    proposed = learner.propose(store, rules_path)
    record_run(store, source, runs_path)
    summary(store, proposed)
    market_summary(store)
    airgap_summary(store)


def airgap_summary(store: Store) -> dict:
    """Where this run's model calls went, saved for the dashboard. Printed when air-gapped or any model was called,
    so the offline Quickstart output doesn't change."""
    rep = llm.write_airgap_report(Path(getattr(store, "dir", None) or STATE_DIR) / AIRGAP_NAME)
    if rep["airgap"] or rep["calls"]:
        print(rep["line"])
    return rep


def market_summary(store: Store) -> None:
    s = market.snapshot(store)
    print("judge market: " + " | ".join(f"{r['judge'].replace('judge.', '')} {r['balance']:.2f}" for r in s["leaderboard"])
          + f" ({s['settled']} bets settled, {s['open_bets']} open)")
    a = s["attention"]
    if a["escalations"]:
        print(f"attention: {a['used']}/{a['budget']} tokens used"
              + (f", {len(a['deferred'])} deferred: {', '.join(d['finding_id'] for d in a['deferred'])}" if a["deferred"] else ""))


def record_run(store: Store, source: str, runs_path: Path | None = None) -> dict:
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
    runs_path = Path(runs_path or RUNS)
    runs_path.parent.mkdir(parents=True, exist_ok=True)
    with runs_path.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def summary(store: Store, proposed: list[dict]) -> None:
    rows = store.all()
    by = lambda o: [f for f in rows if f.get("outcome") == o]
    rec = by("recoverable")
    print(f"correct: {len(by('correct'))} | recoverable: {len(rec)} (${sum(abs(f['gap']) for f in rec):.2f}) "
          f"| sent to a human: {len(by('conflict'))}")
    shown = sorted(rows, key=lambda x: x["finding_id"])
    if len(shown) > SHOW_FINDINGS:  # big runs: the biggest gaps; the dashboard has the rest
        shown = sorted(sorted(rows, key=lambda x: -abs(x["gap"]))[:SHOW_FINDINGS], key=lambda x: x["finding_id"])
    for f in shown:
        print(f"  {f['finding_id']} {f['channel']:<10} {f['check_type']:<13} gap {f['gap']:>7.2f}  "
              f"{f['outcome']:<11} {f['state']:<9} {f['reason'][:70]}")
    if len(rows) > len(shown):
        print(f"  ... {len(rows) - len(shown)} more (largest {len(shown)} gaps shown; all on the dashboard)")
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
    ap.add_argument("--raw", help="raw data folder (default data/raw)")
    ap.add_argument("--std", help="standardized tables folder (default data/std)")
    ap.add_argument("--mappings", help="column maps (default mappings.json)")
    ap.add_argument("--confirm", action="store_true", help="accept proposed mappings for unseen layouts")
    a = ap.parse_args()
    run(reset=a.reset, demo_approve=a.demo_approve, forget=a.forget, pace=a.pace,
        raw=Path(a.raw) if a.raw else None, std=Path(a.std) if a.std else None,
        mappings=Path(a.mappings) if a.mappings else None, confirm=a.confirm)
