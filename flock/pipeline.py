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
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from flock import scan
from flock.agents import clicky, deal_keeper, dispute_writer, formatter, learner, panel, ranker
from flock.gatekeeper import transition
from flock.models import Actor, Finding
from flock.progress import Progress, ensure, progress_path
from flock.store import ROOT, STATE_DIR, Store, batched, get_store

FIXTURES = ROOT / "fixtures" / "fake_findings.json"
RUNS = STATE_DIR / "runs.jsonl"
CHECKER = Actor(id="checkers", role="agent")
DEMO_HUMAN = Actor(id="demo-controller", role="human")
SHOW_FINDINGS = 40
LIVE_KEY = "answer_key.csv"  # flock.demo --keep leaves its generated key here (scripts/score.py reads it)  # summary lines printed; a year of data has thousands of findings


OPTIONAL_TABLES = ("error_charges", "refunds")  # extra std tables the checkers use when the formatter writes them


def detect_real(raw: Path | None = None, std: Path | None = None, mappings: Path | None = None,
                confirm: bool = False, interactive: bool = False, progress: Progress | None = None) -> list[dict]:
    """Formatter + Matcher + Deal Keeper + Checkers on data/raw -> data/std (or the given folders).
    Empty list if no data yet."""
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
    return hold_unread(scan.detect(tables, terms, progress), formatter.pending_layouts(std))


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


def detect(fixtures: Path | None = None, progress: Progress | None = None, **data) -> tuple[list[dict], str]:
    found = detect_real(progress=progress, **data) if fixtures is None else []
    if found:
        where = data.get("std")
        return found, f"real data ({where})" if where else "real data (data/std)"
    path = Path(fixtures or FIXTURES)
    rows = json.loads(path.read_text())
    name = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name
    return [Finding(**f).to_dict() for f in rows], str(name)


def run(reset: bool = False, demo_approve: bool = False, forget: bool = False, pace: float = 0.0,
        store: Store | None = None, rules_path: Path | None = None, portal: Path | None = None,
        fixtures: Path | None = None, raw: Path | None = None, std: Path | None = None,
        mappings: Path | None = None, confirm: bool = False, interactive: bool = False,
        progress: Progress | None = None, runs_path: Path | None = None, label: str = "") -> Store:
    """store / rules_path / portal / fixtures default to the repo paths; tests point them at a temp dir.
    raw / std / mappings point the Formatter at another dataset (flock.demo); confirm / interactive are its
    unseen-layout question. progress defaults to a terminal bar + progress.json next to the store."""
    store = store or get_store()
    rules_path = Path(rules_path or learner.RULES)
    portal = Path(portal or clicky.PORTAL)
    runs_path = Path(runs_path or RUNS)
    if progress is None:
        progress = Progress(progress_path(store), run=label or "pipeline")
    if reset:
        store.reset()
        portal.unlink(missing_ok=True)
        (Path(getattr(store, "dir", STATE_DIR)) / LIVE_KEY).unlink(missing_ok=True)  # a kept live demo's key
    if forget:
        rules_path.unlink(missing_ok=True)
        runs_path.unlink(missing_ok=True)
    data = {k: v for k, v in (("raw", raw), ("std", std), ("mappings", mappings)) if v is not None}
    if data or confirm or interactive:
        data.update(confirm=confirm, interactive=interactive)
    try:
        with batched(store):
            _run(store, rules_path, portal, fixtures, data, demo_approve, pace, progress, runs_path)
    except BaseException:
        progress.finish("failed")
        raise
    progress.finish()
    return store


def _run(store: Store, rules_path: Path, portal: Path, fixtures: Path | None, data: dict, demo_approve: bool,
         pace: float, progress: Progress, runs_path: Path) -> None:

    def move(finding_id: str, action: str, actor: Actor, **kw) -> None:
        transition(store, finding_id, action, actor, **kw)
        if pace:
            time.sleep(pace)

    findings, source = detect(fixtures, progress, **data)
    print(f"\n== Flock run on {source}: {len(findings)} findings ==")

    progress.stage("log findings", total=len(findings), unit="findings")
    for i, f in enumerate(findings, 1):
        store.put_new(f)
        move(f["finding_id"], "check", CHECKER, reason="Checker computed expected vs actual in code")
        progress.update(i)

    rules = learner.approved_rules(rules_path)
    todo = [f for f in store.all() if f["state"] == "CHECKED"]
    progress.stage("judge", total=len(todo), unit="findings")
    for i, f in enumerate(todo, 1):
        move(f["finding_id"], "judge", panel.ACTOR, patch=panel.decide(f, rules), reason="Panel vote")
        progress.update(i)

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
            elif f["outcome"] == "conflict":
                move(f["finding_id"], "reject", DEMO_HUMAN,
                     reason="Rep had no authority to change the deal; accept the charge")
        filed = clicky.file_approved(store, portal)
        print(f"Clicky filed {len(filed)} disputes in the sandbox portal")

    proposed = learner.propose(store, rules_path)
    record_run(store, source, runs_path)
    summary(store, proposed)


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
