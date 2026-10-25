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

The live flock (flock/agents_live.py): every agent with a job writes spawn / working / done events to the store's
agent feed (worker per store, deposit checker, judge per finding, writer per draft, Clicky, the journal drafter,
the learner). An unexplained finding also gets an investigator sub-agent (flock/agents/investigator.py) that attaches
a case file through the gatekeeper; it never changes an outcome or a dollar. The dashboard's Flock view shows them.

For the frontend API (flock/api.py): a real-data run saves transactions.json (one row per payout per store, see
flock/transactions.py) next to the findings, and every run saves last_run.json: measured wall time, the data's volume
(stores, store-weeks) and this run's model calls / tokens / CREAO credits (llm.call_stats), for GET /api/metrics.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flock import agents_live, llm, market, rollup as rollups, scan, transactions
from flock.agents import (clicky, deal_keeper, dispute_writer, formatter, investigator, journal, learner, panel,
                          ranker)
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
LAST_RUN = "last_run.json"  # wall time, data volume and model calls of the latest run, for GET /api/metrics
LIVE_KEY = "answer_key.csv"  # flock.demo --keep leaves its generated key here (scripts/score.py reads it)  # summary lines printed; a year of data has thousands of findings


OPTIONAL_TABLES = ("error_charges", "refunds")  # extra std tables the checkers use when the formatter writes them


def detect_real(raw: Path | None = None, std: Path | None = None, mappings: Path | None = None,
                confirm: bool = False, interactive: bool = False, progress: Progress | None = None,
                context: dict | None = None, agents: agents_live.Tracker | None = None) -> list[dict]:
    """Formatter + Matcher + Deal Keeper + Checkers on data/raw -> data/std (or the given folders).
    Empty list if no data yet. If `context` is given, it also gets what the Journal Entry Drafter needs
    (stories, terms_for, orders) when the data is small enough to match in one process, plus the std tables and deal
    terms (tables, terms) for investigators. agents: the run's live tracker (flock/agents_live.py)."""
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
    if context is not None:
        context.update(tables=tables, terms=terms)
    return hold_unread(scan.detect(tables, terms, progress, context=context, agents=agents),
                       formatter.pending_layouts(std))


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
           agents: agents_live.Tracker | None = None, source_label: str | None = None,
           **data) -> tuple[list[dict], str]:
    """source_label names a dataset for people (flock.demo: "generated: <profile> seed N, S stores x W weeks") instead
    of the std folder's path, which for a generated run is a temp folder that is gone afterwards."""
    found = detect_real(progress=progress, context=context, agents=agents, **data) if fixtures is None else []
    if found:
        if source_label:
            return found, source_label
        where = data.get("std")
        return found, f"real data ({where})" if where else "real data (data/std)"
    if context is not None:
        context.clear()  # fixtures have no payout tables: nothing to journal
    path = Path(fixtures or FIXTURES)
    rows = json.loads(path.read_text())
    name = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name
    found = [Finding(**f).to_dict() for f in rows]
    if agents is not None:  # no stores in a fixtures file: one worker reads it
        w = agents.spawn("worker", subject=str(name), agent_id="worker:fixtures").working(f"reading {name}")
        for f in found:
            agents.owner[f["finding_id"]] = w.id
        w.done(f"{len(found)} findings from {name}", findings=len(found))
    return found, str(name)


def run(reset: bool = False, demo_approve: bool = False, forget: bool = False, pace: float = 0.0,
        store: Store | None = None, rules_path: Path | None = None, portal: Path | None = None,
        fixtures: Path | None = None, raw: Path | None = None, std: Path | None = None,
        mappings: Path | None = None, confirm: bool = False, interactive: bool = False,
        progress: Progress | None = None, runs_path: Path | None = None, label: str = "",
        attention_budget: int | None = None, source_label: str | None = None, rollup: bool = False,
        period: str | None = None) -> Store:
    """store / rules_path / portal / fixtures default to the repo paths; tests point them at a temp dir.
    raw / std / mappings point the Formatter at another dataset (flock.demo); confirm / interactive are its
    unseen-layout question. progress defaults to a terminal bar + progress.json next to the store.
    attention_budget defaults to env FLOCK_ATTENTION_BUDGET (10). source_label: a readable name for the data (else the
    std folder's path). rollup: group per-order findings that break the same deal term in the same weekly payout into
    one finding per lump sum (flock/rollup.py; flock.showtime turns it on). period: what the run covers, for the
    scoreboard's history ("May 2026"; default "Week N")."""
    store = store or get_store()
    rules_path = Path(rules_path or learner.RULES)
    portal = Path(portal or clicky.PORTAL)
    runs_path = Path(runs_path or runs_default(store))
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
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.monotonic()
    try:
        with batched(store):
            info = _run(store, rules_path, portal, fixtures, data, demo_approve, pace, progress, runs_path,
                        jstore, attention_budget, source_label, rollup, period)
    except BaseException:
        progress.finish("failed")
        raise
    progress.finish()
    write_last_run(store, {**info, "label": label or "pipeline", "started_at": started_at,
                           "seconds": round(time.monotonic() - t0, 3), "pace": pace})
    return store


def write_last_run(store: Store, info: dict) -> dict:
    """last_run.json next to the findings: what GET /api/metrics measures from. Never breaks a run."""
    rec = {**info, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "findings": len(store.all()), "llm": llm.call_stats()}
    try:
        path = Path(getattr(store, "dir", None) or STATE_DIR) / LAST_RUN
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=1))
        tmp.replace(path)
    except OSError:
        pass
    return rec


def _run(store: Store, rules_path: Path, portal: Path, fixtures: Path | None, data: dict, demo_approve: bool,
         pace: float, progress: Progress, runs_path: Path, jstore: Store, attention_budget: int | None,
         source_label: str | None = None, rollup: bool = False, period: str | None = None) -> dict:
    run_id = "RUN-" + uuid.uuid4().hex[:8]
    flock = agents_live.Tracker(store, run_id)
    try:
        return {"run_id": run_id, **_stages(store, rules_path, portal, fixtures, data, demo_approve, pace, progress,
                                            runs_path, jstore, attention_budget, run_id, flock, source_label,
                                            rollup, period)}
    finally:
        flock.close()


def _stages(store: Store, rules_path: Path, portal: Path, fixtures: Path | None, data: dict, demo_approve: bool,
            pace: float, progress: Progress, runs_path: Path, jstore: Store, attention_budget: int | None,
            run_id: str, flock: agents_live.Tracker, source_label: str | None = None, rollup: bool = False,
            period: str | None = None) -> dict:
    def move(finding_id: str, action: str, actor: Actor, **kw) -> None:
        transition(store, finding_id, action, actor, **kw)
        if pace:
            flock.flush()  # the live board shows each agent's latest state during the pause
            time.sleep(pace)

    context: dict = {}
    findings, source = detect(fixtures, progress, context, agents=flock, source_label=source_label, **data)
    if rollup and (context.get("tables") or {}).get("payout_lines"):
        findings = roll_up(findings, context["tables"]["payout_lines"], flock)
    print(f"\n== Flock run on {source}: {len(findings)} findings ==")
    volume = save_transactions(store, context, findings, source)

    progress.stage("log findings", total=len(findings), unit="findings")
    for i, f in enumerate(findings, 1):
        store.put_new(f)
        move(f["finding_id"], "check", CHECKER, reason="Checker computed expected vs actual in code")
        progress.update(i)

    # investigators, only for unexplained findings: a deposit / missing_order gap with one source behind it
    book = investigator.Casebook(context.get("tables"), context.get("terms"))
    jobs = [(f, t, flock.owner.get(f["finding_id"])) for f in store.all()
            if f["state"] == "CHECKED" and (t := investigator.detection_trigger(f))]
    if jobs:
        progress.stage("investigate", total=len(jobs), unit="findings")
        investigator.run(store, jobs, book, flock)

    rules = learner.approved_rules(rules_path)
    weights = market.weights(store)  # balances carried over from earlier weeks' settled bets
    todo = [f for f in store.all() if f["state"] == "CHECKED"]
    progress.stage("judge", total=len(todo), unit="findings")
    plan = judge_plan(todo, rules)
    judges = {}
    for f in todo:  # spawned in order, in this thread (the tracker is not shared with the pool)
        judges[f["finding_id"]] = flock.spawn("judge", parent=flock.owner.get(f["finding_id"]), subject=f["finding_id"],
                                              agent_id=f"judge:{f['finding_id']}").working("3 judges voting")
        flock.judge_of[f["finding_id"]] = judges[f["finding_id"]].id
    decisions = {}
    for i, (f, d) in enumerate(judge_all(todo, rules, weights, plan), 1):  # results in todo order
        decisions[f["finding_id"]] = d
        judges[f["finding_id"]].done(f"{d['outcome']}: " + (", ".join(f"{v['judge'].replace('judge.', '')} {v['outcome']}"
                                                                      for v in d["votes"] if v.get("round") in (None, 2))
                                                           or d["reason"][:120]), outcome=d["outcome"])
        progress.update(i)
    if plan["live"]:
        print(f"judges: live models on {plan['used']} of {len(todo)} findings"
              + (f" (FLOCK_LIVE_JUDGE_BUDGET={plan['budget']}; {plan['outside']} on offline rules)"
                 if plan["budget"] is not None else "")
              + f", {plan['concurrency']} at a time")
    escalations = [{**store.get(fid), **d} for fid, d in decisions.items() if d["outcome"] == "conflict"]
    attention = market.allocate_attention(escalations, attention_budget, weights)
    for fid, d in decisions.items():
        patch = {**d, "attention": attention[fid]} if fid in attention else d  # labels only; still a conflict
        move(fid, "judge", panel.ACTOR, patch=patch, reason="Panel vote")
        market.record_stakes(store, fid, d["votes"], run_id)

    # investigators for what the panel sent to a human without a review memo
    jobs = [(f, t, flock.judge_of.get(f["finding_id"])) for f in store.all()
            if f["state"] == "JUDGED" and (t := investigator.conflict_trigger(f))]
    if jobs:
        progress.stage("investigate", total=len(jobs), unit="findings")
        investigator.run(store, jobs, book, flock)

    todo = [f for f in store.all() if f["state"] == "JUDGED" and f["outcome"] == "recoverable"]
    progress.stage("rank", total=len(todo), unit="findings")
    for i, f in enumerate(todo, 1):
        move(f["finding_id"], "rank", ranker.ACTOR, patch={"rank_score": ranker.score(f)},
             reason="gap x win probability - effort")
        progress.update(i)

    for f in ranker.within_budget([x for x in store.all() if x["state"] == "JUDGED" and x["outcome"] == "recoverable"]):
        writer = flock.spawn("writer", parent=flock.judge_of.get(f["finding_id"]), subject=f["finding_id"],
                             agent_id=f"writer:{f['finding_id']}").working("drafting the dispute")
        move(f["finding_id"], "draft", dispute_writer.ACTOR,
             patch={"dispute_draft": dispute_writer.draft(f)}, reason="Drafted dispute, waiting for human")
        writer.done(f"drafted a ${abs(f['gap']):.2f} dispute to {f['channel']}, waiting for a human")

    if demo_approve:
        for f in store.all():
            if f["state"] != "JUDGED":
                continue
            if f["outcome"] == "recoverable" and f.get("dispute_draft"):
                move(f["finding_id"], "approve", DEMO_HUMAN, reason="Evidence checks out")
            elif f["outcome"] == "conflict" and f.get("attention", {}).get("status") != "deferred":
                move(f["finding_id"], "reject", DEMO_HUMAN,
                     reason="Rep had no authority to change the deal; accept the charge")
        bot = flock.spawn("clicky", subject="sandbox portal", agent_id="clicky").working("filing approved disputes")
        filed = clicky.file_approved(store, portal)
        bot.done(f"filed {len(filed)} disputes in the sandbox portal", filed=len(filed))
        print(f"Clicky filed {len(filed)} disputes in the sandbox portal")

    if context.get("stories"):
        jagent = flock.spawn("journal", subject="journal entries", agent_id="journal").working(
            "drafting entries per store x channel x payout")
        entries = journal.draft(context["stories"], store.all(), context["terms_for"], context["orders"])
        drafted = journal.draft_into(jstore, entries)
        bad = sum(1 for je in entries if not je["balanced"])
        jagent.done(f"drafted {len(drafted)} entries, {bad} unbalanced", drafted=len(drafted))
        print(f"Journal drafted {len(drafted)} entries (store x channel x payout), {bad} unbalanced; "
              f"waiting for a human to approve")
    elif context.get("tables"):   # real data (fixtures clear context)
        print("Journal: skipped at year scale (split detection keeps no order stories); run a week at a time to draft")

    teacher = flock.spawn("learner", subject="rules", agent_id="learner").working("reading human decisions")
    proposed = learner.propose(store, rules_path)
    teacher.done(f"proposed {len(proposed)} rule(s)", proposed=len(proposed))
    record_run(store, source, runs_path, rules_path, period)
    summary(store, proposed)
    market_summary(store)
    print("flock: " + ", ".join(f"{n} {role}{'s' if n != 1 else ''}" for role, n in flock.counts.items())
          + f" ({flock.total()} agents)")
    airgap_summary(store)
    return {"source": source, "data": volume, "demo_approve": demo_approve,
            "judging": {k: plan[k] for k in ("live", "concurrency", "budget", "used", "outside", "judged", "by_rule")}}


def roll_up(findings: list[dict], payout_lines: Path, flock: agents_live.Tracker) -> list[dict]:
    """One finding per broken deal term per weekly payout (flock/rollup.py). The rolled-up finding's worker is the
    worker that found its first order."""
    by_order = {f["order_ids"][0]: f["finding_id"] for f in findings if len(f.get("order_ids") or []) == 1}
    rolled = rollups.rollup(findings, rollups.payout_index(payout_lines))
    for f in rolled:
        if f.get("rollup"):
            owner = flock.owner.get(by_order.get(f["order_ids"][0]))
            if owner:
                flock.owner[f["finding_id"]] = owner
    groups = [f for f in rolled if f.get("rollup")]
    if groups:
        print(f"rolled up {sum(f['rollup']['orders'] for f in groups):,} per-order findings into {len(groups)} "
              f"(one per broken deal term per weekly payout); {len(rolled)} findings in all")
    return rolled


DEFAULT_LIVE_CONCURRENCY = 6   # findings judged at once when a live judge is configured (CREAO pacing still applies)


def _env_int(name: str) -> int | None:
    v = os.environ.get(name, "").strip()
    if not v:
        return None
    try:
        return max(0, int(v))
    except ValueError:
        print(f"[pipeline] ignoring {name}={v!r}: not a whole number")
        return None


def live_judges() -> bool:
    """Would any of the three judges reach a live model on this box?"""
    return any(llm.available(panel._route(j)) for j in panel.JUDGES)


def judge_concurrency(live: bool) -> int:
    """FLOCK_JUDGE_CONCURRENCY findings judged at once. Default 1 offline (nothing to wait on), 6 with a live judge."""
    n = _env_int("FLOCK_JUDGE_CONCURRENCY")
    return max(1, n) if n is not None else (DEFAULT_LIVE_CONCURRENCY if live else 1)


def budget_risk(f: dict) -> tuple:
    """Who gets the live judges first: the API's risk score before the panel votes (flock/frontend.py RISK_FORMULA:
    |gap|, weak authority, |gap| x uncertainty), then |gap|, then finding_id so ties are stable."""
    from flock import frontend  # frontend reads pipeline's outputs; import late to keep the module graph one-way
    return (-frontend.risk(f)["risk"], -abs(f["gap"]), f["finding_id"])


def judge_plan(todo: list[dict], rules: list[dict]) -> dict:
    """Which findings get live judges this run (FLOCK_LIVE_JUDGE_BUDGET = N riskiest; unset = all) and how many are
    judged at once. Findings an approved rule settles never call a model, so they don't use the budget. Offline
    (no live judge configured) the budget is not applied: nothing is marked as outside it."""
    live = live_judges()
    budget = _env_int("FLOCK_LIVE_JUDGE_BUDGET")
    by_rule = {f["finding_id"] for f in todo if learner.matching_rule(f, rules)}
    panel_ids = [f for f in todo if f["finding_id"] not in by_rule]
    if not live:
        chosen = set()
    elif budget is None:
        chosen = {f["finding_id"] for f in panel_ids}
    else:
        chosen = {f["finding_id"] for f in sorted(panel_ids, key=budget_risk)[:budget]}
    return {"live": live, "concurrency": judge_concurrency(live), "budget": budget if live else None,
            "chosen": chosen, "rule_ids": by_rule, "used": len(chosen), "outside": len(panel_ids) - len(chosen) if live else 0,
            "judged": len(todo), "by_rule": len(by_rule)}


def judge_all(todo: list[dict], rules: list[dict], weights: dict, plan: dict):
    """(finding, panel decision) for each finding, in todo order. With concurrency > 1 the panels run in a thread pool
    (each panel already runs its 3 judges at once; llm paces CREAO run creates across all threads). Only decide()
    runs in the pool; transitions, stakes and progress stay with the caller, in this same order, so the result is
    identical to the sequential run."""
    def one(f: dict) -> dict:
        fid = f["finding_id"]
        live = not plan["live"] or fid in plan["chosen"] or fid in plan["rule_ids"]
        return panel.decide(f, rules, weights, live=live)

    if plan["concurrency"] <= 1 or len(todo) <= 1:
        for f in todo:
            yield f, one(f)
        return
    with ThreadPoolExecutor(max_workers=plan["concurrency"]) as pool:
        yield from zip(todo, pool.map(one, todo))


def save_transactions(store: Store, context: dict, findings: list[dict], source: str) -> dict | None:
    """transactions.json for GET /api/transactions (one row per payout per store). Returns the data's volume."""
    state_dir = Path(getattr(store, "dir", None) or STATE_DIR)
    tables = context.get("tables") or {}
    try:
        if tables.get("payout_lines"):   # fixtures clear context: no tables
            deposits = context.get("deposit_stories") or [s for s in context.get("stories", [])
                                                          if s.get("kind") == "deposit"]
            doc = transactions.build(tables, deposits, findings, source, terms=context.get("terms"))
        else:
            doc = transactions.empty(source, f"this run read {source}: hand-made findings with no payout tables, "
                                             "so there are no transactions (POST /api/run with dataset=demo)")
        transactions.save(doc, state_dir)
        return doc.get("data")
    except OSError as e:  # the API's table is a view; it must never stop a run
        print(f"[pipeline] could not save transactions: {e}")
        return None


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


def runs_default(store: Store) -> Path:
    """Where a run's week-by-week history goes: next to the store's findings. The repo store keeps RUNS
    (data/state/runs.jsonl); a temp store (tests, sandboxes) must never append to the repo's history."""
    d = getattr(store, "dir", None)
    if d is None or Path(d).resolve() == STATE_DIR.resolve():
        return RUNS
    return Path(d) / "runs.jsonl"


def record_run(store: Store, source: str, runs_path: Path | None = None, rules_path: Path | None = None,
               period: str | None = None) -> dict:
    """One line per run so the scoreboard can show week 1 vs week 2 human hand-offs.
    learned_rules counts the rules this run used (rules_path), not the repo's rules.json. period (optional) names what
    the run covers ("May 2026"); the scoreboard labels the run with it instead of "Week N"."""
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
        "learned_rules": len(learner.approved_rules(rules_path)),
    }
    if period:
        entry["period"] = period
    runs_path = Path(runs_path or runs_default(store))
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
