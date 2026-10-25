"""Read models for the frontend (flock/api.py serves them; dashboard/app.py mirrors the cheap ones). Read-only: nothing
here writes a finding, a log line or a dollar.

  risk(f)                 0-100 risk score + label on every finding (formula below)
  metrics(store, ...)     the four bottom cards: agent vs human time, accuracy, cost, money saved
  agent_detail(store, id) one agent's activity timeline, subject, parent / children, case file link, screens
  screen_file(parts)      a screenshot path under data/screens or data/demos, path-traversal safe

Every metric value traces to run data (last_run.json, findings, the audit log, journal entries, the answer key) or to
an assumption that is labelled ASSUMPTION with its source. Nothing is invented: a value we can't measure is null with a
note saying why.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from flock import agents_live, market, workflow
from flock.agents import journal
from flock.models import money
from flock.store import ROOT, STATE_DIR, Store

LAST_RUN = "last_run.json"        # written by flock.pipeline.write_last_run
SCREENS_DIR = Path(os.environ.get("FLOCK_SCREENS_DIR") or ROOT / "data" / "screens")
RESULTS_JSON = ROOT / "data" / "benchmarks" / "results.json"
WEEKS_PER_YEAR = 52

# Roster for the UI. Order is pipeline order. "actor" matches the id written into the audit log; "live" lists the
# roles this agent's work shows up as in the live feed (flock/agents_live.py); "log" lists audit-log actor ids.
AGENTS: list[dict[str, Any]] = [
    {"actor": "collector", "name": "Collector", "owner": "Maja", "role": "agent", "live": [], "log": [],
     "job": "Pulls the raw statements, payouts, bank lines, contracts and emails into data/raw."},
    {"actor": "formatter", "name": "Formatter", "owner": "Maja", "role": "agent", "live": [], "log": [],
     "job": "Turns each platform's messy export into the standard tables in data/std."},
    {"actor": "matcher", "name": "Matcher", "owner": "Thabhelo", "role": "agent", "live": ["worker", "checker"],
     "log": [], "job": "Connects every register order to its payout line and bank deposit."},
    {"actor": "deal_keeper", "name": "Deal Keeper", "owner": "Thabhelo", "role": "agent", "live": [], "log": [],
     "job": "Reads contracts, amendments and emails into deal terms and ranks them by authority."},
    {"actor": "checkers", "name": "Checkers", "owner": "Thabhelo", "role": "agent", "live": ["worker", "checker"],
     "log": ["checkers"], "job": "Compares what the deal says to what was paid. Plain Python does the money math."},
    {"actor": "anomaly", "name": "Anomaly Detector", "owner": "Thabhelo", "role": "agent", "live": [], "log": [],
     "job": "Learns each store and app's normal fees, commission and timing from history and flags jumps no rule covers."},
    {"actor": "investigator", "name": "Investigator", "owner": "Preet", "role": "agent", "live": ["investigator"],
     "log": ["investigator"],
     "job": "Spawned only for an unexplained finding: gathers nearby facts into a case file. Never changes a dollar."},
    {"actor": "panel", "name": "Panel", "owner": "Preet", "role": "agent", "live": ["judge"], "log": ["panel"],
     "job": "Three judges vote on each finding. Agreement decides alone; disagreement goes to a human."},
    {"actor": "ranker", "name": "Ranker", "owner": "Preet", "role": "agent", "live": [], "log": ["ranker"],
     "job": "Scores recoverable findings by dollars times win odds minus effort, and sets the budget."},
    {"actor": "dispute_writer", "name": "Dispute Writer", "owner": "Preet", "role": "agent", "live": ["writer"],
     "log": ["dispute_writer"], "job": "Drafts the message to the platform. A human reads it before anything is sent."},
    {"actor": "gatekeeper", "name": "Gatekeeper", "owner": "Ira", "role": "gatekeeper", "live": [], "log": ["gatekeeper"],
     "job": "The one door. Checks every move against the state machine and writes the audit log."},
    {"actor": "clicky", "name": "Clicky", "owner": "Preet", "role": "agent", "live": ["clicky"], "log": [],
     "job": "Files approved disputes in the sandbox portal. The gatekeeper records the filing."},
    {"actor": "journal", "name": "Journal Entry Drafter", "owner": "Preet", "role": "agent", "live": ["journal"],
     "log": ["journal"], "job": "Drafts one balanced journal entry per store x channel x payout for a human to approve."},
    {"actor": "learner", "name": "Learner", "owner": "Preet", "role": "agent", "live": ["learner"], "log": [],
     "job": "Turns repeated human decisions into proposed rules. Humans approve rules before they apply."},
]

# live roles that are more than one roster agent
LIVE_ROLES: dict[str, dict[str, str]] = {
    "worker": {"name": "Store worker (Matcher + Checkers + Anomaly Detector)",
               "job": "One per store: matches that store's orders to payout lines and runs the checkers on them."},
    "checker": {"name": "Deposit reconciler (Matcher + Checkers)",
                "job": "Links every payout to a bank deposit across stores and checks the amounts."},
}

JUDGES = [
    {"id": "judge.strict", "name": "Strict auditor", "stance": "Only calls recoverable when the evidence clearly proves the platform broke the deal."},
    {"id": "judge.operator", "name": "Operator", "stance": "Wants every dollar back, but will not fight a fair charge."},
    {"id": "judge.skeptic", "name": "Skeptic", "stance": "Looks for incomplete, ambiguous evidence or a source without authority."},
]


def state_dir(store: Store) -> Path:
    return Path(getattr(store, "dir", None) or STATE_DIR)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def last_run(store: Store) -> dict[str, Any] | None:
    return read_json(state_dir(store) / LAST_RUN)


# risk --------------------------------------------------------------------------------------------------------------
RISK_GAP_CAP = 100.0          # dollars: a gap this big or bigger maxes the dollar term
RISK_ATTENTION_CAP = 25.0     # dollars of attention value (gap x panel uncertainty) that max the attention term
RISK_WEIGHTS = {"dollars": 45, "uncertainty": 25, "authority": 20, "attention": 10}   # sums to 100
WEAK_AUTHORITY = {"unauth_email", "call_note"}
RISK_FORMULA = ("risk = 45*D + 25*U + 20*A + 10*V, rounded, 0-100. D = ln(1+|gap|)/ln(1+100) capped at 1; "
                "U = 1 - panel confidence (0.5 before the panel votes); A = 1 if the panel sent it to a human "
                "(conflict), 0.5 if any evidence comes from a source without authority (unauth_email, call_note), "
                "else 0; V = attention value / $25 capped at 1, where attention value = |gap| x (1 - the "
                "balance-weighted panel confidence) (flock/market.py). Label: low < 34 <= med < 67 <= high.")


def risk(f: dict[str, Any]) -> dict[str, Any]:
    """Risk of a finding: how much money, how unsure the panel is, whether the sources conflict on authority, and how
    much a human look is worth. Pure function of the finding; see RISK_FORMULA."""
    gap = abs(float(f.get("gap") or 0))
    d = min(1.0, math.log1p(gap) / math.log1p(RISK_GAP_CAP))
    conf = f.get("confidence")
    u = 0.5 if conf is None else min(1.0, max(0.0, 1 - float(conf)))
    weak = any((e or {}).get("authority") in WEAK_AUTHORITY for e in f.get("evidence") or [])
    a = 1.0 if f.get("outcome") == "conflict" else (0.5 if weak else 0.0)
    att = (f.get("attention") or {}).get("value")
    if att is None:
        att = market.attention_value(f) if f.get("votes") else gap * u
    v = min(1.0, max(0.0, float(att)) / RISK_ATTENTION_CAP)
    parts = {"dollars": d, "uncertainty": u, "authority": a, "attention": v}
    score = round(sum(RISK_WEIGHTS[k] * x for k, x in parts.items()))
    score = max(0, min(100, score))
    return {"risk": score, "risk_label": "low" if score < 34 else ("med" if score < 67 else "high"),
            "risk_factors": {k: round(x, 3) for k, x in parts.items()}}


def with_risk(f: dict[str, Any]) -> dict[str, Any]:
    return {**f, **risk(f)}


# metrics -------------------------------------------------------------------------------------------------------------
CONTEXT_SRC = "FLOCK_CONTEXT.md §6 (Lunchbox) and §7 (worked example)"
HUMAN_ASSUMPTION = {
    "minutes_per_store_week": 60.0,
    "label": "ASSUMPTION",
    "claim": "Reconciling one location can take an hour (vendor claim). FLOCK_CONTEXT.md §7 applies it as ~1 hour "
             "per location per week and marks the per-week cadence as unverified ([need: per week? verify]).",
    "source": "https://lunchbox.io/third-party-disputes-the-silent-restaurant-revenue-killer",
    "cited_in": CONTEXT_SRC,
}


def _benchmark(results_json: Path | None = None) -> dict[str, Any] | None:
    data = read_json(Path(results_json or RESULTS_JSON))
    if not data or not data.get("current"):
        return None
    return data


def human_baseline(results_json: Path | None = None) -> dict[str, Any]:
    """Minutes a person spends per store-week: measured (benchmark human contestant), env override, or the cited
    assumption."""
    bench = _benchmark(results_json)
    human = (bench or {}).get("current", {}).get("human") or {}
    if human.get("human_minutes_per_store_week"):
        return {"minutes_per_store_week": float(human["human_minutes_per_store_week"]), "label": "MEASURED",
                "claim": f"A teammate worked one store-week from the raw files with a spreadsheet and a timer "
                         f"(scope {human.get('config', {}).get('human_scope') or human.get('scope') or 'see results'}).",
                "source": "data/benchmarks/results.json (scripts/benchmark.py --human ... --human-minutes)"}
    env = os.environ.get("FLOCK_HUMAN_MINUTES_PER_STORE_WEEK", "").strip()
    if env:
        try:
            return {"minutes_per_store_week": float(env), "label": "ASSUMPTION",
                    "claim": "Set by the operator in FLOCK_HUMAN_MINUTES_PER_STORE_WEEK.",
                    "source": "env FLOCK_HUMAN_MINUTES_PER_STORE_WEEK"}
        except ValueError:
            pass
    return dict(HUMAN_ASSUMPTION)


def _agent_seconds(store: Store, run: dict[str, Any] | None) -> tuple[float | None, str]:
    if run and run.get("seconds") is not None:
        return float(run["seconds"]), f"{state_dir(store).name}/{LAST_RUN}: pipeline wall time (time.monotonic)"
    rows = agents_live.events(store)
    if not rows:
        return None, "no run recorded"
    rid = rows[-1].get("run")
    stamps = sorted(e.get("at") for e in rows if e.get("run") == rid and e.get("at"))
    from datetime import datetime
    try:
        secs = (datetime.fromisoformat(stamps[-1]) - datetime.fromisoformat(stamps[0])).total_seconds()
    except (ValueError, IndexError):
        return None, "no run recorded"
    return secs, "agent feed: first to last event of the latest run (agent_status.jsonl)"


def time_card(store: Store, run: dict[str, Any] | None, results_json: Path | None = None) -> dict[str, Any]:
    secs, secs_src = _agent_seconds(store, run)
    data = (run or {}).get("data") or {}
    base = human_baseline(results_json)
    sw = data.get("store_weeks")
    agent_h = round(secs / 3600, 6) if secs is not None else None
    human_h = round(sw * base["minutes_per_store_week"] / 60, 2) if sw else None
    to_human = sum(1 for f in store.all() if f.get("outcome") == "conflict")
    card = {
        "id": "agent_time", "title": "Agent hours vs human hours", "unit": "hours",
        "value": {"agent_hours": agent_h, "human_hours": human_h,
                  "hours_saved": round(human_h - agent_h, 2) if human_h is not None and agent_h is not None else None,
                  "multiple": round(human_h / agent_h) if human_h and agent_h else None},
        "formula": "agent hours = measured wall time of the last pipeline run / 3600. human hours = store-weeks in "
                   "the run's data x minutes a person needs per store-week / 60. hours saved = human - agent; "
                   "multiple = human / agent.",
        "inputs": {"agent_seconds": round(secs, 3) if secs is not None else None, "stores": data.get("stores"),
                   "weeks": data.get("weeks"), "store_weeks": sw,
                   "minutes_per_store_week": base["minutes_per_store_week"], "human_baseline": base["label"],
                   "sent_to_a_human": to_human},
        "sources": [{"what": "agent_seconds", "label": "MEASURED", "source": secs_src},
                    {"what": "store_weeks", "label": "MEASURED" if sw else "MISSING",
                     "source": "distinct stores x weeks spanned by std/orders.csv placed_at (flock/transactions.py)"},
                    {"what": "minutes_per_store_week", **base}],
        "notes": [f"{to_human} finding(s) still go to a human for a decision; that review time is counted on neither "
                  "side.", "Data is simulated (generator/generate.py)."],
    }
    if not sw:
        card["notes"].insert(0, "The last run read hand-made fixtures, which have no stores or weeks to price: "
                                "POST /api/run with dataset=demo for a run with real volume.")
    return card


def _scoreboard():
    spec = importlib.util.spec_from_file_location("flock_scoreboard", ROOT / "scripts" / "score.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def accuracy_card(store: Store, results_json: Path | None = None) -> dict[str, Any]:
    sb = _scoreboard()
    key = sb.answer_key_for(store)
    s = sb.score(store, verbose=False, runs_path=state_dir(store) / "runs.jsonl", answer_key=key)
    acc = round(s["caught"] / s["total"], 4) if s["total"] else None
    card = {
        "id": "accuracy", "title": "Agent accuracy", "unit": "fraction",
        "value": {"accuracy": acc, "caught": s["caught"], "total": s["total"], "missed": s["missed"],
                  "false_alarms": s["false_alarms"]},
        "formula": "accuracy = answer-key rows Flock got right / all answer-key rows (scripts/score.py: the outcome "
                   "matches, a quiet 'correct' counts, a conflict settled by a human-approved rule counts). False "
                   "alarms = rows the key calls fair that Flock wanted to dispute.",
        "inputs": {"answer_key": str(Path(key).relative_to(ROOT)) if Path(key).is_relative_to(ROOT) else str(key),
                   "findings": len(store.all()), "settled_by_rules": s["by_rule"], "sent_to_a_human": s["to_human"]},
        "sources": [{"what": "answer key", "label": "SIMULATED",
                     "source": ("the hand-made fixtures' answer key (data/answer_key.csv)" if Path(key) == sb.ANSWER_KEY
                                else "the generator plants every trap and writes its answer key "
                                     "(generator/generate.py), kept next to the state by the dataset run")
                     + "; scored with scripts/score.py"}],
        "notes": ["Simulated data: the answer key was written by whoever planted the traps, so this is accuracy on "
                  "planted traps, not on real platform statements."],
        "benchmark": None,
    }
    bench = _benchmark(results_json)
    if bench:
        card["benchmark"] = {name: {k: c.get(k) for k in ("label", "accuracy", "caught", "total", "false_alarms",
                                                          "cost_per_1000", "per_minute", "status", "seeds", "at")}
                             for name, c in bench["current"].items()}
        card["sources"].append({"what": "benchmark", "label": "MEASURED", "source": "data/benchmarks/results.json"})
    return card


def _price(model_id: str) -> tuple[float, float] | None:
    from flock import llm
    return next((p for k, p in llm.LIST_PRICES.items() if k in model_id), None)


def cost_card(store: Store, run: dict[str, Any] | None) -> dict[str, Any]:
    stats = (run or {}).get("llm") or {}
    by_provider = stats.get("by_provider") or {}
    findings = (run or {}).get("findings") or len(store.all())
    parts, unknown, notes = [], [], []
    local = sum(n for p, n in by_provider.items() if p == "local")
    parts.append({"what": "local models", "calls": local, "usd": 0.0,
                  "basis": "on this network: $0 per call (the GX10's power is not metered)"})
    tok_usd = 0.0
    for model_id, t in (stats.get("tokens") or {}).items():
        price = _price(model_id)
        if price is None:
            unknown.append(f"no list price for {model_id}")
            parts.append({"what": f"tokens {model_id}", "tokens": t, "usd": None, "basis": "no list price on file"})
            continue
        usd = round((t["input"] * price[0] + t["output"] * price[1]) / 1_000_000, 6)
        tok_usd += usd
        parts.append({"what": f"tokens {model_id}", "tokens": t, "usd": usd,
                      "basis": f"server-reported tokens x list price ${price[0]}/${price[1]} per 1M in/out "
                               "(flock/llm.py LIST_PRICES)"})
    for p in ("anthropic", "groq", "nvidia"):
        if by_provider.get(p) and not stats.get("tokens"):
            unknown.append(f"{by_provider[p]} {p} call(s) without reported tokens")
    creao = stats.get("creao") or {}
    credits = creao.get("credits") or 0.0
    rate = os.environ.get("FLOCK_CREAO_USD_PER_CREDIT", "").strip()
    credit_usd = 0.0
    if credits:
        try:
            credit_usd = round(credits * float(rate), 6) if rate else None
        except ValueError:
            credit_usd = None
        if credit_usd is None:
            unknown.append("CREAO /v1 credits have no $ rate (set FLOCK_CREAO_USD_PER_CREDIT)")
    v2_usd = round(float(creao.get("usd") or 0.0), 6)
    if creao.get("unpriced"):
        unknown.append(f"{creao['unpriced']} CREAO model usage(s) with no price in llm.CREAO_USD_PER_M")
    if by_provider.get("creaoapp"):
        unknown.append(f"{by_provider['creaoapp']} agent.creao.ai app call(s) report no usage (the wallet is the truth)")
    n_creao = by_provider.get("creao", 0) + by_provider.get("creaoapp", 0)
    creao_usd = None if credit_usd is None else round(v2_usd + credit_usd, 6)
    parts.append({"what": "CREAO hosted agents", "calls": n_creao, "credits": credits,
                  "input_tokens": creao.get("input_tokens", 0), "output_tokens": creao.get("output_tokens", 0),
                  "usd": creao_usd,
                  "basis": ("CREAO-reported tokens x the Developer Console's per-model prices (llm.CREAO_USD_PER_M)"
                            + (f" + credits x ${rate}/credit" if credits and rate else
                               " + credits with no $ rate" if credits else "")) if n_creao else "no CREAO calls"})
    calls = stats.get("calls", 0)
    if not calls:
        notes.append("Zero model calls: every AI role ran on offline rules (FLOCK_OFFLINE=1), which costs $0.")
    known = round(tok_usd + v2_usd + (credit_usd or 0.0), 6)
    total = known if not unknown else None
    return {
        "id": "cost", "title": "Agent cost", "unit": "USD",
        "value": {"usd": total, "usd_known_part": known, "complete": not unknown,
                  "credits": credits, "model_calls": calls,
                  "usd_per_1000_findings": round(total / findings * 1000, 4) if total is not None and findings else None,
                  "credits_per_1000_findings": round(credits / findings * 1000, 4) if findings and credits else None},
        "formula": "usd = local calls x $0 + server-reported tokens x list price per model + CREAO-reported "
                   "tokens x the console's per-model price + CREAO /v1 credits x FLOCK_CREAO_USD_PER_CREDIT. Unknown parts (no price, no rate) make usd null and are listed; "
                   "usd_known_part is what we can price. Per 1,000 findings = usd / findings x 1000.",
        "inputs": {"by_provider": by_provider, "by_host": stats.get("by_host") or {}, "findings": findings,
                   "creao_by_agent": creao.get("by_agent") or {}, "parts": parts},
        "sources": [{"what": "model calls", "label": "MEASURED" if run else "MISSING",
                     "source": f"{LAST_RUN}: llm.call_stats() saved at the end of the run"}],
        "unknown": unknown, "notes": notes,
    }


def money_card(store: Store, run: dict[str, Any] | None, jstore: Store | None = None) -> dict[str, Any]:
    rows = store.all()
    open_ = [f for f in rows if journal.is_open(f)]
    tot = lambda fs: money(sum(abs(f["gap"]) for f in fs))
    split = {"recovered": tot([f for f in open_ if f["state"] == "RECOVERED"]),
             "filed": tot([f for f in open_ if f["state"] == "FILED"]),
             "approved": tot([f for f in open_ if f["state"] == "APPROVED"]),
             "pending": tot([f for f in open_ if f["state"] not in ("RECOVERED", "FILED", "APPROVED")])}
    undecided = [f for f in rows if journal.is_pending(f)]
    found = tot(open_)
    due, entries = 0.0, 0
    if jstore is not None:
        for je in jstore.all():
            if je.get("state") == "REJECTED":
                continue
            d = sum(ln["debit"] for ln in je.get("lines", []) if ln.get("finding_id") and ln.get("account_type") == "asset")
            if d:
                entries += 1
                due += d
    weeks = ((run or {}).get("data") or {}).get("weeks")
    mult = round(WEEKS_PER_YEAR / weeks, 4) if weeks else None
    return {
        "id": "money_saved", "title": "Money saved", "unit": "USD",
        "value": {"usd": found, **split, "due_from_platform_booked": money(due),
                  "annualized_projection": money(found * mult) if mult else None},
        "formula": "usd = sum of |gap| over findings that are money we expect back (recoverable, approved, filed or "
                   "recovered, never rejected; the same test the journal uses). Split by state. "
                   "due_from_platform_booked = debits to 'Due from Platform' lines (finding-linked asset lines) in "
                   "the drafted journal entries. annualized_projection = usd x 52 / weeks in the data.",
        "inputs": {"findings_counted": len(open_), "undecided_conflicts": len(undecided),
                   "undecided_conflict_usd": tot(undecided), "journal_entries_with_receivable": entries,
                   "weeks": weeks, "projection_multiplier": mult},
        "sources": [{"what": "dollars", "label": "MEASURED", "source": "findings (gap computed in code by the "
                                                                       "checkers)"},
                    {"what": "due_from_platform_booked", "label": "MEASURED",
                     "source": "journal_entries.json (flock/agents/journal.py)"},
                    {"what": "annualized_projection", "label": "PROJECTION",
                     "source": f"x {mult} = 52 weeks / {weeks} week(s) in the data; assumes every week leaks like "
                               "these did" if mult else "no weeks in the data (fixtures run)"}],
        "notes": ["Would have been lost if the payouts were posted as-is. Undecided conflicts are not counted.",
                  "Simulated data: how much leaks is set by the generator's trap rate, so the projection describes "
                  "the simulation, not a real chain."]
        + ([] if entries else ["No journal entries with a receivable (fixtures run, or year-scale runs skip the "
                               "journal)."]),
    }


def metrics(store: Store, jstore: Store | None = None, results_json: Path | None = None) -> dict[str, Any]:
    run = last_run(store)
    return {"run": {k: (run or {}).get(k) for k in ("run_id", "label", "source", "started_at", "finished_at",
                                                    "seconds", "findings", "data")} if run else None,
            "cards": [time_card(store, run, results_json), accuracy_card(store, results_json), cost_card(store, run),
                      money_card(store, run, jstore)]}


# agents --------------------------------------------------------------------------------------------------------------
SCREEN_NOTE = ("Only Clicky has a screen: it drives a browser in the sandbox portal. Every other agent is plain Python "
               "with no UI; its live screen is its activity timeline.")
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SCREEN_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webm": "video/webm"}


def _brief(a: dict[str, Any]) -> dict[str, Any]:
    return {k: a.get(k) for k in ("agent_id", "role", "subject", "state", "summary", "started", "ended", "parent_id",
                                  "count", "done", "failed", "pooled") if a.get(k) is not None}


def clicky_screens(limit: int = 12) -> dict[str, Any]:
    """Latest filing screenshots (data/screens, browser mode) and the latest demonstration's step screenshots."""
    shots = []
    if SCREENS_DIR.is_dir():
        files = sorted((p for p in SCREENS_DIR.iterdir() if p.suffix.lower() in SCREEN_TYPES and p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        shots = [{"file": p.name, "url": f"/api/screens/{p.name}"} for p in files]
    demo = None
    root = Path(workflow.DEMOS_DIR)
    if root.is_dir():
        dirs = sorted((d for d in root.iterdir() if d.is_dir() and SAFE_NAME.fullmatch(d.name)),
                      key=lambda d: d.stat().st_mtime, reverse=True)
        if dirs:
            d = dirs[0]
            steps = sorted(p.name for p in d.glob("step-*.png"))
            demo = {"demo_id": d.name, "steps": [f"/api/screens/demos/{d.name}/{n}" for n in steps],
                    "video": f"/api/screens/demos/{d.name}/video.webm" if (d / "video.webm").exists() else None}
    return {"filings": shots, "demonstration": demo, "latest": shots[0]["url"] if shots else
            (demo["steps"][-1] if demo and demo["steps"] else None)}


def screen_file(parts: list[str]) -> Path | None:
    """parts after /api/screens/: [name] in data/screens, or ["demos", demo_id, name] in data/demos. Every segment must
    be a plain file name (no '..', no slashes, no encoding), the type an image or video, and the resolved path inside
    its folder. Anything else: None."""
    if len(parts) == 1:
        base, rel = SCREENS_DIR, parts
    elif len(parts) == 3 and parts[0] == "demos":
        base, rel = Path(workflow.DEMOS_DIR), parts[1:]
    else:
        return None
    if not all(SAFE_NAME.fullmatch(p) and ".." not in p for p in rel):
        return None
    if Path(rel[-1]).suffix.lower() not in SCREEN_TYPES:
        return None
    base = base.resolve()
    path = base.joinpath(*rel).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        return None
    return path


def agent_detail(store: Store, agent_id: str, limit: int = 100) -> dict[str, Any]:
    """A roster agent ("panel", "clicky", ...) or a live agent ("worker:S01", "judge:F-0001", "judge*"). Raises KeyError."""
    rows = agents_live.events(store)
    rid = rows[-1].get("run") if rows else None
    mine = [e for e in rows if e.get("run") == rid]
    folded = agents_live.fold(mine)
    roster = next((a for a in AGENTS if a["actor"] == agent_id), None)
    if roster is not None:
        live = [a for a in folded.values() if a.get("role") in roster["live"]]
        live_ids = {a["agent_id"] for a in live}
        log = [e for e in store.log() if e["actor"] in roster["log"]
               or (agent_id == "clicky" and e["action"] == "file")]
        activity = [{"at": e["at"], "kind": "audit", "action": e["action"], "subject_id": e["subject_id"],
                     "allowed": e["allowed"], "summary": e.get("reason", "")} for e in log]
        activity += [{"at": e["at"], "kind": "agent", "action": e["event"], "subject_id": e.get("subject"),
                      "agent_id": e["agent_id"], "summary": e.get("summary", "")} for e in mine
                     if e["agent_id"] in live_ids]
        activity.sort(key=lambda x: x["at"] or "", reverse=True)
        out = {"agent_id": agent_id, "kind": "roster", **{k: roster[k] for k in ("name", "owner", "role", "job")},
               "live_roles": roster["live"], "run": rid, "subject": None, "parent": None,
               "children": [_brief(a) for a in sorted(live, key=lambda a: a.get("seq") or 0)][:limit],
               "children_total": len(live), "actions": len(log), "activity": activity[:limit],
               "activity_total": len(activity), "case_file": None}
    else:
        a = folded.get(agent_id)
        if a is None:
            raise KeyError(agent_id)
        subject = a.get("subject")
        events = [{"at": e["at"], "kind": "agent", "action": e["event"], "subject_id": subject,
                   "agent_id": agent_id, "summary": e.get("summary", "")} for e in mine if e["agent_id"] == agent_id]
        roster_of = LIVE_ROLES.get(a.get("role")) or next((r for r in AGENTS if a.get("role") in r["live"]), None)
        finding, log = None, []
        if isinstance(subject, str):
            try:
                finding = store.get(subject)
                log = store.log(subject)
            except KeyError:
                finding = None
        children = [x for x in folded.values() if x.get("parent_id") == agent_id]
        events += [{"at": e["at"], "kind": "audit", "action": e["action"], "subject_id": e["subject_id"],
                    "allowed": e["allowed"], "summary": e.get("reason", "")} for e in log]
        events.sort(key=lambda x: x["at"] or "", reverse=True)
        out = {"agent_id": agent_id, "kind": "live", "name": (roster_of or {}).get("name", a.get("role")),
               "role": a.get("role"), "job": (roster_of or {}).get("job"), "state": a.get("state"),
               "summary": a.get("summary"), "started": a.get("started"), "ended": a.get("ended"), "run": rid,
               "subject": subject,
               "parent": _brief(folded[a["parent_id"]]) if a.get("parent_id") in folded else (
                   {"agent_id": a["parent_id"]} if a.get("parent_id") else None),
               "children": [_brief(x) for x in children][:limit], "children_total": len(children),
               "activity": events[:limit], "activity_total": len(events),
               "case_file": ({"finding_id": subject, "url": f"/api/findings/{subject}",
                              "has_case_file": bool(finding.get("case_file")), "case_file": finding.get("case_file")}
                             if finding else None)}
        if a.get("pooled"):
            out["pooled"] = {k: a.get(k) for k in ("count", "done", "failed")}
    is_clicky = agent_id == "clicky" or out.get("role") == "clicky"
    out["has_screen"] = is_clicky
    out["screen_note"] = SCREEN_NOTE
    out["screens"] = clicky_screens() if is_clicky else None
    return out
