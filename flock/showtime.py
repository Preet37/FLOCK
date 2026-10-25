"""Showtime: everything the 3-minute demo needs, prepared the night before (owner: Preet).

One story: 100K transactions a month collapse into a few lump-sum deposits; Flock breaks a lump sum down, finds the
few mistakes, and shows what's still owed.

  python -m flock.showtime prepare [--months 4] [--seed 2025]   # ~2 min offline; prints the cheat sheet
  python -m flock.showtime status                               # what's loaded, and whether it's still untouched
  python -m flock.showtime approve --actor preet                # fallback: approve the headline from a terminal
  python -m flock.showtime restore                              # put the prepared state back after a rehearsal
  python -m flock.showtime reset                                # back to the plain Quickstart demo state

prepare, in order (all simulated data, the bagel chain, generator --scenario):
  1. N prior months (prior_month, a different seed each, oldest first) run through the whole pipeline with findings
     rolled up per lump sum (flock/rollup.py). The demo human (demo-controller, as in --demo-approve) approves the
     recoverables and rejects what was sent to a human; Clicky files; the platform "pays" the filed claims (recover,
     through the gatekeeper), except one claim in the last prior month that stays unpaid; the Learner proposes rules
     and the demo human approves them before the next month. So runs.jsonl, the judge market and rules.json show a
     real learning curve: fewer hand-offs to a human every month.
  2. The demo month (demo_month: DoorDash keeps charging the pre-amendment commission at 3 stores, plus a few small
     incidents). The demo human makes the routine calls, EXCEPT the headline finding (the biggest one) and anything
     sent to a human: those wait for the presenter, live.
  3. The unpaid claim from the last prior month is carried into the demo state (replayed through the gatekeeper with
     its original reference) and the follow-up agent drafts a reminder for it, waiting for a human.
  4. The cheat sheet (data/state/showtime.md, plus showtime.json) is computed from this run: headline id, its
     platform and payout, expected vs arrived vs short for that lump sum, the metrics cards, the month-over-month
     hand-offs and the exact clicks. A snapshot is kept so `restore` undoes a rehearsal in a second.

Never touches data/answer_key.csv or mappings.json (a temp copy is used). Generated files live in data/showtime/.
Works offline (FLOCK_OFFLINE=1) and with live providers; FLOCK_LIVE_JUDGE_BUDGET is applied by the pipeline as usual.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flock import frontend, payouts, pipeline, transactions
from flock.agents import clicky, collector, follow_up, learner, panel
from flock.agents.dispute_writer import PLATFORM_NAME
from flock.demo import generator, scoreboard
from flock.gatekeeper import GATEKEEPER, Denied, transition
from flock.models import Actor, money
from flock.progress import Progress, progress_path
from flock.store import ROOT, STATE_DIR, Store, get_journal_store, get_store

DEFAULT_SEED = 2025
DEFAULT_MONTHS = 4
WORKDIR = ROOT / "data" / "showtime"
MARKER = ".flock_showtime"
MD_NAME, JSON_NAME = "showtime.md", "showtime.json"
HUMAN = pipeline.DEMO_HUMAN          # the simulated human of --demo-approve
DASHBOARD = "http://127.0.0.1:5050"
API = "http://127.0.0.1:8787"


def month_name(month: str) -> str:
    start, _ = generator.month_span(month)
    return start.strftime("%B %Y")


def prior_months(month: str, n: int) -> list[str]:
    return [generator.shift_month(month, k - n) for k in range(n)]


def month_seed(seed: int, back: int) -> int:
    """The seed of the month `back` months before the demo month (0 = the demo month itself)."""
    return seed + 1000 * back


def _clean(ws: Path) -> None:
    """Wipe a previous prepare's files, never a folder showtime didn't make."""
    if ws.exists() and any(ws.iterdir()):
        if not (ws / MARKER).exists():
            raise SystemExit(f"{ws} has files showtime didn't make; pick an empty --workdir")
        shutil.rmtree(ws)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / MARKER).write_text("made by python -m flock.showtime prepare; safe to delete\n")


def _say(quiet: bool, *msg: Any) -> None:
    if not quiet:
        print(*msg, flush=True)


# one month through the pipeline ------------------------------------------------------------------------------------
def run_month(ws: Path, month: str, scenario: str, plan: int | None, seed: int, stores: int | None, store: Store,
              rules_path: Path, portal: Path, mappings: Path, runs_path: Path, demo_approve: bool, forget: bool,
              quiet: bool) -> dict[str, Any]:
    folder = ws / month
    out, raw, std = folder / "out", folder / "raw", folder / "std"
    t0 = time.monotonic()
    world = generator.generate(seed, stores, out=out, scenario=scenario, month=month, plan=plan)
    sc = world["scenario"]
    collector.collect(out, raw)
    label = f"showtime {month} ({scenario}), seed {seed}"
    source = f"generated: {generator.SCENARIO_PROFILE} {month_name(month)} ({scenario}) seed {seed}, " \
             f"{sc['stores']} stores x {world['days']} days"
    stream = io.StringIO() if quiet else sys.stderr
    progress = Progress(progress_path(store), stream=stream, run=label)
    (ws / "logs").mkdir(exist_ok=True)
    log = ws / "logs" / f"{month}.log"
    with log.open("w") as fh, contextlib.redirect_stdout(fh):
        pipeline.run(reset=True, forget=forget, demo_approve=demo_approve, store=store, rules_path=rules_path,
                     portal=portal, raw=raw, std=std, mappings=mappings, confirm=True, interactive=False,
                     progress=progress, runs_path=runs_path, label=label, source_label=source, rollup=True,
                     period=month_name(month))
        result = scoreboard.score(store, verbose=True, runs_path=runs_path, answer_key=out / "answer_key.csv")
    rows = store.all()
    return {"month": month, "name": month_name(month), "scenario": scenario, "plan": plan, "seed": seed,
            "orders": sc["orders"], "stores": sc["stores"], "deposits": sc["deposits"], "findings": len(rows),
            "to_human": sum(1 for f in rows if f.get("outcome") == "conflict"),
            "by_rule": sum(1 for f in rows if f.get("reason", "").startswith("Applied approved rule")),
            "caught": result["caught"], "total": result["total"], "false_alarms": result["false_alarms"],
            "seconds": round(time.monotonic() - t0, 1), "log": str(log), "out": str(out), "std": str(std),
            "judging": (frontend.last_run(store) or {}).get("judging"), "scenario_summary": sc}


def platform_pays(store: Store, keep: str | None, month: str) -> list[str]:
    """The platform credits every filed claim on its next payout except `keep` (it stays unpaid)."""
    paid = []
    for f in store.all():
        if f["state"] == "FILED" and f["finding_id"] != keep:
            transition(store, f["finding_id"], "recover", GATEKEEPER,
                       reason=f"Platform credited claim {f.get('portal_ref')} on its next payout "
                              f"(simulated by showtime prepare for {month_name(month)})")
            paid.append(f["finding_id"])
    return paid


def pick_unpaid(store: Store) -> dict[str, Any] | None:
    """The claim that stays unpaid: the short deposit when there is one, else the biggest filed claim."""
    filed = [f for f in store.all() if f["state"] == "FILED"]
    return min(filed, key=lambda f: (f["check_type"] != "deposit", -abs(f["gap"]), f["finding_id"]), default=None)


def keep_claim(store: Store, f: dict[str, Any], portal: Path, month: str) -> dict[str, Any]:
    rows = []
    if portal.exists():
        rows = [json.loads(x) for x in portal.read_text().splitlines() if x.strip()]
    return {"month": month, "finding": copy.deepcopy(f), "log": store.log(f["finding_id"]),
            "portal": [r for r in rows if r.get("portal_ref") == f.get("portal_ref")]}


def carry_over(store: Store, claim: dict[str, Any], portal: Path) -> dict[str, Any]:
    """Re-open last month's unpaid claim in this month's store. Each move goes through the gatekeeper again, with a
    reason that names the run and time it really happened; the sandbox portal gets its original filing back."""
    f = copy.deepcopy(claim["finding"])
    fid, when = f["finding_id"], month_name(claim["month"])
    orig = {e["action"]: e for e in claim["log"] if e.get("allowed")}
    store.put_new({**f, "state": "NEW"})
    note = f"Carried over from the {when} run, still unpaid"

    def at(action: str) -> str:
        e = orig.get(action) or {}
        return f"{e.get('actor', '?')} at {e.get('at', '?')}"

    transition(store, fid, "check", pipeline.CHECKER, reason=f"{note}: checked there by {at('check')}")
    transition(store, fid, "judge", panel.ACTOR, reason=f"{note}: judged there by {at('judge')}")
    approver = Actor(id=(orig.get("approve") or {}).get("actor") or HUMAN.id, role="human")
    transition(store, fid, "approve", approver, reason=f"{note}: approved there by {at('approve')}")
    filed = orig.get("file") or {}
    transition(store, fid, "file", GATEKEEPER, patch={"portal_ref": f.get("portal_ref")},
               reason=f"{note}: filed there in the sandbox portal as {f.get('portal_ref')} by {at('file')}",
               evidence=filed.get("evidence"))
    have = set()
    if portal.exists():
        have = {json.loads(x).get("portal_ref") for x in portal.read_text().splitlines() if x.strip()}
    portal.parent.mkdir(parents=True, exist_ok=True)
    with portal.open("a") as fh:
        for r in claim["portal"]:
            if r.get("portal_ref") not in have:
                fh.write(json.dumps(r) + "\n")
    return store.get(fid)


def pick_headline(store: Store) -> dict[str, Any] | None:
    """The finding the presenter approves live: the biggest drafted dispute."""
    ready = [f for f in store.all() if f["state"] == "JUDGED" and f.get("outcome") == "recoverable"
             and f.get("dispute_draft")]
    return min(ready, key=lambda f: (-abs(f["gap"]), f["finding_id"]), default=None)


# the lump sum ------------------------------------------------------------------------------------------------------
def lump_sum(store: Store, scenario: dict[str, Any], headline: dict[str, Any]) -> dict[str, Any] | None:
    """The lump sum the headline sits in, exactly as the dashboard's Lump sums tab shows it (flock/payouts.py over this
    run's transactions.json): what arrived, what it should have been per the deal, what is short and still owed, and
    the findings inside it. Plus the answer key's own figures for the same deposit, to check against."""
    doc = frontend.read_json(Path(getattr(store, "dir", STATE_DIR)) / transactions.FILE_NAME)
    payout_id = (headline.get("rollup") or {}).get("payout_id")
    if not doc or not payout_id:
        return None
    try:
        d = payouts.detail(doc, store.all(), f"{headline['channel']}:{payout_id}")
    except KeyError:
        return None
    truth = next((x for x in scenario.get("lump_sums", []) if payout_id in x["payout_ids"]), None)
    return {"payout_id": payout_id, "channel": d["channel"], "platform": d["platform"], "deposit_id": d["deposit_id"],
            "deposit_date": d["deposit_date"], "covers_payouts": d["covers_payouts"], "orders": d["orders"],
            "stores": d["stores"], "gross": d["gross"], "statement_net": d["net"], "arrived": d["received"],
            "expected": d["expected_net"], "short": d["shortfall"], "owed": d["owed"], "status": d["status"],
            "findings": [{"finding_id": f["finding_id"], "check_type": f["check_type"], "gap": f.get("gap"),
                          "counts_as": f.get("counts_as")} for f in d.get("findings_detail", [])],
            "answer_key": truth}


# prepare -----------------------------------------------------------------------------------------------------------
def prepare(months: int = DEFAULT_MONTHS, seed: int = DEFAULT_SEED, stores: int | None = None,
            month: str = generator.DEMO_MONTH, store: Store | None = None, rules_path: Path | None = None,
            portal: Path | None = None, workdir: Path | None = None, quiet: bool = False) -> dict[str, Any]:
    store = store or get_store()
    state_dir = Path(getattr(store, "dir", STATE_DIR))
    rules_path = Path(rules_path or learner.RULES)
    portal = Path(portal or clicky.PORTAL)
    ws = Path(workdir or WORKDIR)
    _clean(ws)
    mappings = ws / "mappings_temp_copy.json"
    shutil.copy(ROOT / "mappings.json", mappings)          # the committed file is only ever read
    runs_path = pipeline.runs_default(store)
    t0 = time.monotonic()
    history: list[dict[str, Any]] = []
    claim = None
    _say(quiet, f"== showtime prepare: {months} prior month(s) + {month_name(month)}, seed {seed} ==")
    for i, m in enumerate(prior_months(month, months)):
        rec = run_month(ws, m, "prior_month", i, month_seed(seed, months - i), stores, store, rules_path, portal,
                        mappings, runs_path, demo_approve=True, forget=(i == 0), quiet=quiet)
        last = i == months - 1
        keep = pick_unpaid(store) if last else None
        rec["recovered"] = len(platform_pays(store, keep["finding_id"] if keep else None, m))
        if keep:
            claim = keep_claim(store, store.get(keep["finding_id"]), portal, m)
        proposals = learner._read(learner._proposed_path(store))
        rec["rules_approved"] = [learner.approve(r["rule_id"], HUMAN, store=store, rules_path=rules_path)["description"]
                                 for r in proposals]
        history.append(rec)
        _say(quiet, f"  {rec['name']}: {rec['orders']:,} orders, {rec['findings']} findings, {rec['to_human']} sent "
                    f"to a human, {rec['by_rule']} settled by learned rules, caught {rec['caught']}/{rec['total']}, "
                    f"{rec['false_alarms']} false alarms ({rec['seconds']}s{judging(rec)}); rules approved after it: "
                    f"{len(rec['rules_approved'])}")
    demo = run_month(ws, month, "demo_month", None, month_seed(seed, 0), stores, store, rules_path, portal, mappings,
                     runs_path, demo_approve=False, forget=(months == 0), quiet=quiet)
    headline = pick_headline(store)
    for f in store.all():   # the demo human's routine calls; the headline and anything sent to a human wait
        if (f["state"] == "JUDGED" and f.get("outcome") == "recoverable" and f.get("dispute_draft")
                and f["finding_id"] != (headline or {}).get("finding_id")):
            transition(store, f["finding_id"], "approve", HUMAN, reason="Evidence checks out")
    demo["filed"] = clicky.file_approved(store, portal)
    carried = None
    if claim:
        carried = carry_over(store, claim, portal)
        follow_up.draft(store, [carried["finding_id"]])
        carried = store.get(carried["finding_id"])
    shutil.copy(Path(demo["out"]) / "answer_key.csv", state_dir / pipeline.LIVE_KEY)
    _say(quiet, f"  {demo['name']} (on stage): {demo['orders']:,} orders, {demo['findings']} findings, "
                f"{demo['to_human']} sent to a human, caught {demo['caught']}/{demo['total']} "
                f"({demo['seconds']}s{judging(demo)})")
    info = summarize(store, history, demo, headline, carried, seed, month, round(time.monotonic() - t0, 1), rules_path)
    write_sheet(state_dir, info)
    save_snapshot(ws / "snapshot", state_dir, portal, rules_path)
    _say(quiet, "\n" + cheat_sheet(info))
    return info


def judging(rec: dict[str, Any]) -> str:
    """', live judges on N of M' when a live judge answered this month (FLOCK_LIVE_JUDGE_BUDGET applies), else ''."""
    j = rec.get("judging") or {}
    if not j.get("live"):
        return ""
    return f", live judges on {j['used']} of {j['judged'] - j['by_rule']}" + (
        f" (FLOCK_LIVE_JUDGE_BUDGET={j['budget']})" if j.get("budget") is not None else "")


def why_human(f: dict[str, Any]) -> str:
    """One line on why a finding waits for a person: the claim nobody with authority made, else the panel's split."""
    claim = next((e for e in f.get("evidence", []) if e.get("conflict")), None)
    if claim:
        who = {"unauth_email": "someone not on the contract's authorized contacts", "call_note": "a call note",
               None: "a sender of unknown authority"}.get(claim.get("authority"), claim.get("authority"))
        q = claim.get("quote") or ""
        return f'{who} wrote "{q[:110]}{"..." if len(q) > 110 else ""}"'
    return f["reason"][:140]


def summarize(store: Store, history: list[dict], demo: dict, headline: dict | None, carried: dict | None, seed: int,
              month: str, seconds: float, rules_path: Path | None = None) -> dict[str, Any]:
    sc = demo["scenario_summary"]
    rows = store.all()
    cards = {c["id"]: c for c in frontend.metrics(store, get_journal_store(store))["cards"]}
    t, a, m = cards["agent_time"], cards["accuracy"], cards["money_saved"]
    lump = lump_sum(store, sc, headline) if headline else None
    waiting = [f for f in rows if f["state"] == "JUDGED" and f.get("outcome") == "conflict"]
    strip = lambda r: {k: v for k, v in r.items() if k not in ("scenario_summary",)}
    return {
        "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seed": seed, "month": month,
        "month_name": month_name(month), "seconds": seconds, "company": sc["company"],
        "scenario": {k: sc[k] for k in ("orders", "orders_by_channel", "stores", "payouts", "deposits", "deposited",
                                        "expected_by_deal", "shortfall", "incidents", "big_error", "answer_key_rows",
                                        "period_start", "period_end")},
        "months": [strip(r) for r in history] + [strip(demo)],
        "headline": ({k: headline[k] for k in ("finding_id", "channel", "check_type", "expected", "actual", "gap",
                                                "reason")} | {"orders": len(headline["order_ids"]),
                                                              "stores": (headline.get("rollup") or {}).get("stores")})
        if headline else None,
        "lump_sum": lump,
        "waiting_for_presenter": [{"finding_id": f["finding_id"], "channel": f["channel"],
                                   "check_type": f["check_type"], "gap": f["gap"], "why": why_human(f)}
                                  for f in waiting],
        "follow_up": ({"finding_id": carried["finding_id"], "claim_ref": carried["follow_up"]["claim_ref"],
                       "amount": carried["follow_up"]["amount"], "platform": carried["follow_up"]["platform"],
                       "filed_on": carried["follow_up"]["filed_on"], "state": carried["follow_up"]["state"],
                       "check_type": carried["check_type"], "draft": carried["follow_up"]["draft"]}
                      if carried and carried.get("follow_up") else None),
        "metrics": {
            "agent_seconds": t["inputs"]["agent_seconds"], "human_hours": t["value"]["human_hours"],
            "human_baseline": t["inputs"]["human_baseline"], "store_weeks": t["inputs"]["store_weeks"],
            "accuracy": a["value"]["accuracy"], "caught": a["value"]["caught"], "total": a["value"]["total"],
            "false_alarms": a["value"]["false_alarms"], "money_usd": m["value"]["usd"],
            "money_split": {k: m["value"][k] for k in ("recovered", "filed", "approved", "pending")}},
        "rules": [r["description"] for r in learner.approved_rules(rules_path)],
    }


def write_sheet(state_dir: Path, info: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / JSON_NAME).write_text(json.dumps(info, indent=1))
    (state_dir / MD_NAME).write_text(cheat_sheet(info) + "\n")


def _usd(x: float | None) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


def cheat_sheet(info: dict[str, Any]) -> str:
    sc, h, lump, met = info["scenario"], info["headline"], info["lump_sum"], info["metrics"]
    out = [f"# Showtime cheat sheet: {info['company']} (simulated), {info['month_name']}",
           f"Prepared {info['prepared_at']} in {info['seconds']}s, seed {info['seed']}. Every number below was computed "
           "by this run (findings, std tables, the generated answer key); nothing is typed in by hand.", "",
           "## The story",
           f"- {sc['orders']:,} orders in {info['month_name']} across {sc['stores']} stores and "
           f"{len(sc['orders_by_channel'])} channels collapsed into {sc['deposits']} lump-sum bank deposits "
           f"({_usd(sc['deposited'])}).",
           f"- By the deals in force they should have been {_usd(sc['expected_by_deal'])}: "
           f"{_usd(sc['shortfall'])} short (answer key)."]
    if sc.get("big_error"):
        b = sc["big_error"]
        out.append(f"- The big one: DoorDash kept charging {b['old_rate']:.0%} at {', '.join(b['stores'])} after the "
                   f"amendment cut it to {b['new_rate']:.0%} on {b['amendment_effective']}: {b['orders']:,} orders, "
                   f"{_usd(b['overcharge'])} this month.")
    out += ["", "## Headline finding (approve it live)"]
    if h:
        out.append(f"- **{h['finding_id']}**: {PLATFORM_NAME.get(h['channel'], h['channel'])} {h['check_type']}, gap **{_usd(h['gap'])}** "
                   f"({h['orders']:,} orders" + (f" at {', '.join(h['stores'])}" if h.get("stores") else "") + ")")
        if lump:
            out += [f"- Sits in {lump['platform']} payout **{lump['payout_id']}**, deposit {lump['deposit_id']} on "
                    f"{lump['deposit_date']}: one lump sum for {lump['orders']:,} orders from {lump['stores']} stores "
                    "(dashboard: **Lump sums** tab)",
                    f"- Lump sum: expected **{_usd(lump['expected'])}** | arrived **{_usd(lump['arrived'])}** | "
                    f"short **{_usd(lump['short'])}** (findings inside: "
                    + ", ".join(f"{x['finding_id']} {x['check_type']} {_usd(x['gap'])}" for x in lump["findings"]) + ")"]
            k = lump.get("answer_key")
            if k:
                paid = money(k["shortfall"] - k.get("unpaid_net", 0.0))
                out.append(f"- Answer key for the same deposit: expected {_usd(k['expected_by_deal'])}, deposited "
                           f"{_usd(k['deposited'])}, short {_usd(k['shortfall'])}"
                           + (f" ({_usd(k['unpaid_net'])} of it is an order sold but never paid, which belongs to no "
                              f"lump sum on the dashboard: {_usd(paid)} on the paid orders)"
                              if k.get("unpaid_net") else ""))
    else:
        out.append("- none: no drafted dispute waited for a human (check the logs in data/showtime/logs)")
    out += ["", "## Metrics cards (after you approve, money moves from pending to filed)",
            f"1. Agent time: {met['agent_seconds']}s of agent wall time vs {met['human_hours']} human hours "
            f"({met['store_weeks']} store-weeks x 60 min, {met['human_baseline']})",
            f"2. Accuracy: {met['caught']:,}/{met['total']:,} answer-key rows"
            + (f" ({met['accuracy']:.1%})" if met["accuracy"] is not None else "") + f", {met['false_alarms']} false alarms",
            f"3. Money found: {_usd(met['money_usd'])} (filed {_usd(met['money_split']['filed'])}, recovered "
            f"{_usd(met['money_split']['recovered'])}, pending {_usd(met['money_split']['pending'])})",
            "", "## Human hand-offs, month over month"]
    out.append(" | ".join(f"{r['name']}: {r['to_human']}" for r in info["months"]))
    for r in info["months"]:
        extra = f"; rules approved after it: {len(r.get('rules_approved') or [])}" if r["scenario"] == "prior_month" else ""
        out.append(f"- {r['name']}: {r['orders']:,} orders, {r['findings']} findings, {r['to_human']} to a human, "
                   f"{r['by_rule']} settled by learned rules, caught {r['caught']:,}/{r['total']:,}{extra}")
    if info["waiting_for_presenter"]:
        out += ["", "## Sent to a human on stage (Flock won't decide these)"]
        out += [f"- {w['finding_id']} {w['channel']} {w['check_type']} {_usd(w['gap'])}: {w['why']}"
                for w in info["waiting_for_presenter"]]
    fu = info.get("follow_up")
    if fu:
        out += ["", "## Vendor follow-up waiting for approval",
                f"- {fu['finding_id']}: {fu['platform']} claim {fu['claim_ref']} filed {fu['filed_on']} for "
                f"{_usd(fu['amount'])} ({fu['check_type']}), still unpaid. Draft state: {fu['state']}."]
    hid = h["finding_id"] if h else "<headline>"
    out += ["", "## Exact clicks", f"Dashboard: `python -m dashboard.app` -> {DASHBOARD} (Acting as: your name)",
            "1. **Live board**: say the story line above while the counters sit at this month's run."]
    n = 2
    if lump:
        out.append(f"{n}. **Lump sums** tab -> {lump['platform']} **{lump['payout_id']}** (status {lump['status']}): "
                   f"expected {_usd(lump['expected'])}, received {_usd(lump['arrived'])}, short {_usd(lump['short'])}; "
                   f"the waterfall shows the commission line, the findings list shows **{hid}**.")
        n += 1
    out += [f"{n}. **Human queue** tab -> the one drafted dispute waiting is **{hid}** "
            f"({_usd(h['gap']) if h else ''}). Type a reason (\"amendment says 20%\"), click **Approve**.",
            f"{n + 1}. Click **File 1 approved via Clicky**: the toast shows the SBX- reference."]
    n += 2
    if info["waiting_for_presenter"]:
        w = info["waiting_for_presenter"][0]
        out.append(f"{n}. Same tab, the conflict **{w['finding_id']}** ({w['channel']} {w['check_type']}, "
                   f"{_usd(w['gap'])}): Flock won't decide it. Type \"not binding: rep not on the contract\", "
                   "click **Reject** (accept the charge).")
        n += 1
    out += [f"{n}. **Scoreboard** tab: the month-over-month hand-offs above; **Judge market** tab: balances.",
            f"{n + 1}. Follow-up (terminal): `python -m flock.agents.follow_up approve "
            f"{fu['finding_id'] if fu else '<id>'} --actor <you> --send`",
            "", "API instead of the dashboard (approve also files via Clicky):",
            f"`curl -X POST {API}/api/findings/{hid}/approve -H 'Content-Type: application/json' "
            "-d '{\"actor\": \"<you>\", \"reason\": \"amendment says 20%\"}'`",
            "No browser at all: `python -m flock.showtime approve --actor <you>` (same gatekeeper move, then Clicky).",
            "", "After a rehearsal: `python -m flock.showtime restore` (seconds). Back to the Quickstart: "
            "`python -m flock.showtime reset`."]
    return "\n".join(out)


# snapshot, status, reset -------------------------------------------------------------------------------------------
def save_snapshot(dest: Path, state_dir: Path, portal: Path, rules_path: Path) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    shutil.copytree(state_dir, dest / "state")
    for name, p in (("portal.jsonl", portal), ("rules.json", rules_path)):
        if p.exists():
            shutil.copy2(p, dest / name)


def restore(store: Store | None = None, rules_path: Path | None = None, portal: Path | None = None,
            workdir: Path | None = None) -> dict[str, Any]:
    """Put the state `prepare` left back exactly (findings, log, market, runs, portal, rules)."""
    store = store or get_store()
    state_dir = Path(getattr(store, "dir", STATE_DIR))
    snap = Path(workdir or WORKDIR) / "snapshot"
    if not (snap / "state").exists():
        raise SystemExit("nothing to restore: run python -m flock.showtime prepare first")
    shutil.rmtree(state_dir, ignore_errors=True)
    shutil.copytree(snap / "state", state_dir)
    for name, p in (("portal.jsonl", Path(portal or clicky.PORTAL)), ("rules.json", Path(rules_path or learner.RULES))):
        p.unlink(missing_ok=True)
        if (snap / name).exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snap / name, p)
    return load(store)


def load(store: Store | None = None) -> dict[str, Any] | None:
    store = store or get_store()
    p = Path(getattr(store, "dir", STATE_DIR)) / JSON_NAME
    return json.loads(p.read_text()) if p.exists() else None


def status(store: Store | None = None, rules_path: Path | None = None) -> str:
    store = store or get_store()
    info = load(store)
    rows = store.all()
    if not info:
        src = (frontend.last_run(store) or {}).get("source") or "nothing yet"
        return (f"No showtime loaded. The store has {len(rows)} finding(s) from: {src}.\n"
                "Prepare it: python -m flock.showtime prepare")
    by_id = {f["finding_id"]: f for f in rows}
    h = info.get("headline") or {}
    hs = by_id.get(h.get("finding_id"), {}).get("state", "missing")
    ready = hs == "JUDGED"
    lines = [f"Showtime loaded: {info['company']} {info['month_name']}, prepared {info['prepared_at']} "
             f"(seed {info['seed']}, {len(info['months']) - 1} prior month(s)).",
             f"Headline {h.get('finding_id')}: {hs}" + (" (ready for the presenter)" if ready else
                                                         " (already decided: python -m flock.showtime restore)"),
             "Hand-offs by month: " + " | ".join(f"{r['name']}: {r['to_human']}" for r in info["months"]),
             f"Learned rules in force: {len(learner.approved_rules(rules_path))}"]
    for w in info.get("waiting_for_presenter") or []:
        lines.append(f"Waiting for a human: {w['finding_id']} ({by_id.get(w['finding_id'], {}).get('state', 'missing')})")
    fu = info.get("follow_up")
    if fu:
        now = (by_id.get(fu["finding_id"], {}).get("follow_up") or {}).get("state", "missing")
        lines.append(f"Follow-up on {fu['claim_ref']} ({fu['finding_id']}): {now}")
    lines.append(f"Cheat sheet: {Path(getattr(store, 'dir', STATE_DIR)) / MD_NAME}")
    return "\n".join(lines)


def approve_headline(actor: str, store: Store | None = None, portal: Path | None = None,
                     reason: str = "Amendment says 20%; DoorDash charged 25%") -> dict[str, Any]:
    """The on-stage move without a browser: a human approves the headline (gatekeeper), then Clicky files it."""
    store = store or get_store()
    info = load(store)
    if not info or not info.get("headline"):
        raise SystemExit("no showtime headline loaded: python -m flock.showtime prepare")
    fid = info["headline"]["finding_id"]
    transition(store, fid, "approve", Actor(id=actor, role="human"), reason=reason)
    refs = clicky.file_approved(store, Path(portal or clicky.PORTAL))
    return {"finding_id": fid, "filed": refs, "state": store.get(fid)["state"], "portal_ref": store.get(fid).get("portal_ref")}


def reset(store: Store | None = None, rules_path: Path | None = None, portal: Path | None = None,
          quiet: bool = False) -> None:
    """The plain Quickstart demo state: fixtures, no learned rules, week 1 run with the demo human."""
    store = store or get_store()
    state_dir = Path(getattr(store, "dir", STATE_DIR))
    for name in (JSON_NAME, MD_NAME):
        (state_dir / name).unlink(missing_ok=True)
    with contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext():
        pipeline.run(reset=True, forget=True, demo_approve=True, store=store, rules_path=rules_path, portal=portal,
                     progress=Progress(progress_path(store), stream=io.StringIO()) if quiet else None)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m flock.showtime", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["prepare", "status", "approve", "restore", "reset"])
    ap.add_argument("--months", type=int, default=DEFAULT_MONTHS, help="prior months before the demo month")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--stores", type=int, default=None, help="default: the profile's 56")
    ap.add_argument("--month", default=generator.DEMO_MONTH, help="the demo month, YYYY-MM")
    ap.add_argument("--actor", default="presenter", help="approve: who is approving (a human)")
    a = ap.parse_args(argv)
    if a.command == "prepare":
        prepare(max(0, a.months), a.seed, a.stores, a.month)
    elif a.command == "approve":
        try:
            r = approve_headline(a.actor)
        except Denied as e:
            sys.exit(f"showtime: {e} (python -m flock.showtime restore puts it back)")
        print(f"{r['finding_id']} approved by {a.actor}; Clicky filed it in the sandbox portal as {r['portal_ref']}")
    elif a.command == "restore":
        restore()
        print(status())
    elif a.command == "reset":
        reset()
        print("Back to the Quickstart demo state (fixtures, no learned rules, week 1). "
              "python -m flock.showtime restore brings the show back.")
    else:
        print(status())


if __name__ == "__main__":
    main()
