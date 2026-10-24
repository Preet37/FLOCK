"""Investigator (owner: Preet). A sub-agent spawned only when a finding is unexplained, to build a case file for the
human who will decide it. It never changes an outcome or a dollar: it adds evidence, through the gatekeeper.

When one is spawned (and never otherwise):
  deposit_gap_unsupported    a deposit gap (beyond the tolerance) backed by one source only, e.g. a payout whose
                             money never reached the bank: there is no second record to compare against
  missing_order_unsupported  a missing_order gap backed by one source only, e.g. a platform charged for an order the
                             register never rang up, or a register order with no statement read for that channel
  conflict_no_memo           the panel sent the finding to a human and nobody wrote a review memo (offline, or the
                             reviewer failed), so the human would start from the raw votes
The first two run right after the checkers (child of the store's worker); the third right after the panel (child of
that finding's judge). One case file per finding.

What it gathers, deterministically, from the std tables and the deal terms:
  - the payout the finding sits in: every line in it (count, net, a few sample lines with their source rows)
  - bank deposits for that channel within DEPOSIT_WINDOW days of the payout date, and how far each is from the payout
  - the store's history for that check type: other findings for the same store + channel + check type this run
  - every deal claim for that term, binding or not (contract, amendment, auth_email, unauth_email, call_note)
Then a short summary: the "writer" model route when one is configured (FLOCK_WRITER_PROVIDER), else a template.

The case file goes on the finding as `case_file` via gatekeeper.transition(..., "investigate", ...): an annotation
(models.ANNOTATIONS) whose patch may only write case_file (models.ANNOTATION_FIELDS), so it is logged and can't move
state, outcome or money. At most FLOCK_INVESTIGATORS (default 4) run at once.
"""
from __future__ import annotations

import csv
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path
from typing import Any

from flock import agents_live, llm
from flock.agents import matcher, panel
from flock.agents.deal_keeper import BINDING
from flock.gatekeeper import transition
from flock.models import Actor, money

ACTOR = Actor(id="investigator", role="agent")
TRIGGERS = {
    "deposit_gap_unsupported": "deposit gap with no second source",
    "missing_order_unsupported": "missing order with no second source",
    "conflict_no_memo": "sent to a human with no review memo",
}
DEFAULT_MAX_CONCURRENT = 4
DEPOSIT_WINDOW = 7          # days either side of the payout date
SAMPLE_LINES = 5            # payout lines kept per payout for the case file
CAP = 5                     # items per list in a case file
TERM_TYPE = {"commission": "commission", "promo": "promo_split", "tax": "tax_rule", "error_charge": "error_window",
             "fee": "fee"}

SUMMARY = (
    "You write a short case file summary for a person who must decide a delivery-payout discrepancy. You do NOT "
    "decide and you do NOT recompute numbers (they were computed in code). In under 90 words, plain text, no em "
    "dashes: what is unexplained, the most relevant facts from the case file, and the one thing to check next."
)


def max_concurrent() -> int:
    env = os.environ.get("FLOCK_INVESTIGATORS", "").strip()
    return int(env) if env.isdigit() and int(env) > 0 else DEFAULT_MAX_CONCURRENT


# triggers ------------------------------------------------------------------------------------------------------
def detection_trigger(f: dict[str, Any]) -> str | None:
    """Right after the checkers: a deposit or missing_order gap with only one source behind it."""
    if panel.within_tolerance(f) or f.get("case_file"):
        return None
    if f["check_type"] == "deposit" and panel.weak_evidence(f):
        return "deposit_gap_unsupported"
    if f["check_type"] == "missing_order" and panel.weak_evidence(f):
        return "missing_order_unsupported"
    return None


def conflict_trigger(f: dict[str, Any]) -> str | None:
    """Right after the panel: a conflict for a human with no review memo (and no case file yet)."""
    if f.get("outcome") == "conflict" and not f.get("review_memo") and not f.get("case_file"):
        return "conflict_no_memo"
    return None


# the context, read once per run ----------------------------------------------------------------------------------
def _rows(src: Any):
    if src is None:
        return
    if isinstance(src, (str, Path)):
        if not Path(src).exists():
            return
        with Path(src).open(newline="") as fh:
            for row in csv.DictReader(fh):
                yield matcher._clean(row)
    else:
        for row in src:
            yield matcher._clean(dict(row))


def _raw(src: Any):
    """At most one (header, rows) pair, rows as lists of strings: csv.reader for a file (fast at year scale), or the
    in-memory row dicts (tests) laid out the same way."""
    if src is None:
        return
    if isinstance(src, (str, Path)):
        if not Path(src).exists():
            return
        with Path(src).open(newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header:
                yield header, (r for r in reader if len(r) == len(header))
        return
    rows = [dict(r) for r in src]
    header = list(dict.fromkeys(k for r in rows for k in r))
    yield header, ([("" if r.get(k) is None else str(r.get(k))) for k in header] for r in rows)


class Casebook:
    """Indexes the std tables just enough for the findings being investigated: one streaming pass over the payout
    lines (per-payout totals for every payout, lines for the orders involved) and one over the orders. Cheap at year
    scale, and only paid for when an investigator is actually spawned."""

    def __init__(self, tables: dict[str, Any] | None = None, terms: list[dict[str, Any]] | None = None):
        self.tables = dict(tables or {})
        self.terms = list(terms or [])
        self.payouts: dict[tuple, dict[str, Any]] = {}
        self.lines_of: dict[str, list[dict[str, Any]]] = {}
        self.store_of: dict[str, str] = {}
        self.deposits: list[dict[str, Any]] = []
        self.kinds: set[tuple] = set()

    def prepare(self, targets: list[dict[str, Any]], findings: list[dict[str, Any]]) -> "Casebook":
        """Read the tables once for every channel x check type in `findings` (so the investigators spawned after the
        panel reuse it); a later call only re-reads if a target brings a kind that wasn't there."""
        kinds = {(f["channel"], f["check_type"]) for f in targets}
        if kinds <= self.kinds:
            return self
        kinds |= self.kinds | {(f["channel"], f["check_type"]) for f in findings}
        self.payouts, self.lines_of, self.store_of = {}, {}, {}
        wanted = {x for f in findings if (f["channel"], f["check_type"]) in kinds for x in f.get("order_ids", []) if x}
        for header, rows in _raw(self.tables.get("payout_lines")):
            ix = {c: i for i, c in enumerate(header)}
            need = ("channel", "payout_id", "store_id", "payout_date", "net")
            if any(c not in ix for c in need):
                break
            ch, pi, si, di, ni = (ix[c] for c in need)
            id_cols = [ix[c] for c in ("order_id", "platform_order_id", "payout_line_id") if c in ix]
            for r in rows:  # plain lists: a year of payout lines is millions of rows
                key = (r[ch].strip(), r[pi].strip() or None)
                p = self.payouts.get(key)
                store = r[si].strip()
                if p is None:
                    p = self.payouts[key] = {"payout_id": key[1], "channel": key[0], "store_id": store,
                                             "payout_date": r[di].strip(), "lines": 0, "cents": 0, "sample": []}
                elif p["store_id"] != store:
                    p["store_id"] = None   # one payout covering several stores: no single store
                p["lines"] += 1
                p["cents"] += round(float(r[ni] or 0) * 100)
                hit = [r[i].strip() for i in id_cols if r[i].strip() in wanted]
                if hit:
                    ln = matcher._clean(dict(zip(header, r)))
                    for x in hit:
                        self.lines_of.setdefault(x, []).append(ln)
                        if store:
                            self.store_of[x] = store
                elif len(p["sample"]) < SAMPLE_LINES:
                    p["sample"].append(_brief_line(matcher._clean(dict(zip(header, r)))))
        for header, rows in _raw(self.tables.get("orders")):
            if "order_id" not in header or "store_id" not in header:
                break
            oi, si = header.index("order_id"), header.index("store_id")
            for r in rows:
                if r[oi].strip() in wanted and r[si].strip():
                    self.store_of[r[oi].strip()] = r[si].strip()
        self.deposits = list(_rows(self.tables.get("bank_deposits")))
        for f in findings:  # a deposit finding's order_ids are payout ids: its store is the payout's store
            if f["check_type"] == "deposit":
                for pid in f.get("order_ids", []):
                    p = self.payouts.get((f["channel"], pid))
                    if p and p.get("store_id"):
                        self.store_of.setdefault(pid, p["store_id"])
        self.kinds = kinds
        return self

    def store(self, f: dict[str, Any]) -> str | None:
        return next((self.store_of[x] for x in f.get("order_ids", []) if x in self.store_of), None)

    def payouts_for(self, f: dict[str, Any]) -> list[dict[str, Any]]:
        if f["check_type"] == "deposit":
            pids = list(f.get("order_ids", []))
        else:
            pids = sorted({ln.get("payout_id") for x in f.get("order_ids", []) for ln in self.lines_of.get(x, [])}
                          - {None, ""})
        out = []
        for pid in pids[:CAP]:
            p = self.payouts.get((f["channel"], pid))
            if p:
                mine = {ln.get("payout_line_id") for x in f.get("order_ids", []) for ln in self.lines_of.get(x, [])}
                out.append({"payout_id": pid, "payout_date": p["payout_date"], "store_id": p["store_id"],
                            "lines": p["lines"], "net": money(p["cents"] / 100),
                            "other_lines": [s for s in p["sample"] if s["payout_line_id"] not in mine]})
        return out

    def nearby_deposits(self, f: dict[str, Any], payouts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        days = [matcher.parse_ts(p["payout_date"]) for p in payouts if p.get("payout_date")]
        days = [d for d in days if d]
        if not days:
            return []
        net = money(sum(p["net"] for p in payouts))
        pids = [p["payout_id"] for p in payouts]
        win = timedelta(days=DEPOSIT_WINDOW)
        near = []
        for dep in self.deposits:
            d = matcher.parse_ts(dep.get("deposit_date"))
            if dep.get("channel") != f["channel"] or d is None or not any(abs(d - x) <= win for x in days):
                continue
            near.append({"deposit_id": dep.get("deposit_id"), "deposit_date": str(dep.get("deposit_date"))[:10],
                         "amount": dep["amount"], "off_by": money(dep["amount"] - net),
                         "memo_names_payout": any(p and p in (dep.get("memo") or "") for p in pids),
                         "source_file": dep.get("source_file"), "row": dep.get("source_row")})
        return sorted(near, key=lambda d: abs(d["off_by"]))[:CAP]

    def claims(self, f: dict[str, Any], store: str | None) -> list[dict[str, Any]]:
        tt = TERM_TYPE.get(f["check_type"])
        if not tt:
            return []
        if not self.terms:  # no documents read this run (fixtures): the claims quoted in the finding's evidence
            return [{"term_id": None, "authority": e["authority"], "binding": e["authority"] in BINDING,
                     "value": None, "store_id": None, "effective_from": None, "effective_to": None,
                     "quote": e.get("quote"), "source_file": e.get("source_file")}
                    for e in f.get("evidence", []) if e.get("authority")][:2 * CAP]
        out = []
        for c in self.terms:
            if c.get("term_type") != tt or c.get("channel") not in (f["channel"], "all"):
                continue
            if store and c.get("store_id") not in (store, "all"):
                continue
            out.append({"term_id": c.get("term_id"), "authority": c.get("authority"),
                        "binding": c.get("authority") in BINDING, "value": c.get("value"),
                        "store_id": c.get("store_id"), "effective_from": c.get("effective_from"),
                        "effective_to": c.get("effective_to"), "quote": c.get("quote"),
                        "source_file": c.get("source_file")})
        return sorted(out, key=lambda c: (not c["binding"], str(c["effective_from"] or "")))[:2 * CAP]


def _brief_line(ln: dict[str, Any]) -> dict[str, Any]:
    return {"payout_line_id": ln.get("payout_line_id"), "order_id": ln.get("order_id") or ln.get("platform_order_id"),
            "gross": ln.get("gross"), "net": ln.get("net"), "source_file": ln.get("source_file"),
            "row": ln.get("source_row")}


def history(f: dict[str, Any], findings: list[dict[str, Any]], book: Casebook, store: str | None) -> dict[str, Any]:
    """Other findings this run for the same store (when known) + channel + check type."""
    same = [g for g in findings if g["finding_id"] != f["finding_id"] and g["channel"] == f["channel"]
            and g["check_type"] == f["check_type"] and (store is None or book.store(g) == store)]
    outcomes: dict[str, int] = {}
    for g in same:
        k = g.get("outcome") or "undecided"
        outcomes[k] = outcomes.get(k, 0) + 1
    return {"store_id": store, "count": len(same), "outcomes": outcomes,
            "total_gap": money(sum(g["gap"] for g in same)),
            "examples": [g["finding_id"] for g in sorted(same, key=lambda g: -abs(g["gap"]))[:CAP]]}


# the case file ----------------------------------------------------------------------------------------------------
def _usd(x: Any) -> str:
    return f"${money(x or 0):,.2f}"


def facts_and_next(f: dict[str, Any], trigger: str, cf: dict[str, Any]) -> tuple[list[str], str]:
    facts: list[str] = []
    for p in cf["payouts"]:
        facts.append(f"Payout {p['payout_id']} ({p['payout_date']}) has {p['lines']} lines, net {_usd(p['net'])}.")
    dep = cf["nearby_deposits"]
    if trigger == "deposit_gap_unsupported" or (f["check_type"] == "deposit" and cf["payouts"]):
        if dep:
            best = dep[0]
            facts.append(f"{len(dep)} {f['channel']} deposit(s) landed within {DEPOSIT_WINDOW} days; closest is "
                         f"{best['deposit_id']} {_usd(best['amount'])} on {best['deposit_date']} "
                         f"(off by {_usd(best['off_by'])}).")
        elif cf["payouts"]:
            facts.append(f"No {f['channel']} bank deposit within {DEPOSIT_WINDOW} days of the payout.")
    binding = [c for c in cf["claims"] if c["binding"]]
    weak = [c for c in cf["claims"] if not c["binding"]]

    def says(c: dict[str, Any]) -> str:
        return f"{c['authority']} says {c['value']}" if c["value"] is not None else f"{c['authority']}: \"{c['quote']}\""

    if cf["claims"]:
        facts.append(f"{len(binding)} binding and {len(weak)} non-binding claim(s) for this term"
                     + (f"; binding {says(binding[0])}" if binding else "")
                     + (f"; non-binding {says(weak[0])}" if weak else "") + ".")
    h = cf["history"]
    where = f"store {h['store_id']}" if h["store_id"] else "this run"
    if h["count"]:
        facts.append(f"{h['count']} other {f['channel']} {f['check_type'].replace('_', ' ')} finding(s) for {where}, "
                     f"{_usd(h['total_gap'])} in gaps ({', '.join(f'{v} {k}' for k, v in sorted(h['outcomes'].items()))}).")
    else:
        facts.append(f"First {f['channel']} {f['check_type'].replace('_', ' ')} finding for {where}.")

    if trigger == "deposit_gap_unsupported":
        nxt = ("Check whether this payout was folded into a later lump deposit or is still in transit before "
               "disputing; " + ("a deposit memo names it." if any(d["memo_names_payout"] for d in dep)
                                else "no deposit memo names it."))
    elif trigger == "missing_order_unsupported":
        nxt = ("Look for a register order under another id or time for this charge; if there is none, the "
               f"{_usd(f['gap'])} in charges is disputable." if f.get("actual") else
               "Read the missing statement for this channel first; without it there is no second source.")
    elif weak and binding:
        nxt = (f"Confirm whether the {weak[0]['authority'].replace('_', ' ')} sender could change the deal; only a "
               "contract, amendment or authorized email can.")
    elif weak:
        nxt = "Find a contract, amendment or authorized email for this term; nothing binding backs it yet."
    else:
        nxt = "Compare the judges' reasons against the sources above; the numbers were computed in code."
    return facts, nxt


def build(f: dict[str, Any], trigger: str, book: Casebook, findings: list[dict[str, Any]]) -> dict[str, Any]:
    """The case file for one finding (pure: reads the casebook, writes nothing)."""
    store = book.store(f)
    payouts = book.payouts_for(f)
    cf: dict[str, Any] = {
        "trigger": trigger, "why": TRIGGERS[trigger], "store_id": store, "payouts": payouts,
        "nearby_deposits": book.nearby_deposits(f, payouts), "history": history(f, findings, book, store),
        "claims": book.claims(f, store),
    }
    if f.get("votes"):
        cf["votes"] = [f"{v['judge'].replace('judge.', '')}: {v['outcome']} ({v['confidence']:.0%})"
                       for v in f["votes"] if v.get("round") in (None, 2)][:CAP]
    cf["facts"], cf["next_step"] = facts_and_next(f, trigger, cf)
    cf["summary"], cf["summary_by"] = summarize(f, cf)
    return cf


def summarize(f: dict[str, Any], cf: dict[str, Any]) -> tuple[str, str]:
    r = llm.route("writer")
    if llm.available(r):
        payload = {"finding": {k: f.get(k) for k in ("check_type", "channel", "expected", "actual", "gap", "reason")},
                   "case_file": {k: cf[k] for k in ("why", "facts", "next_step", "claims", "nearby_deposits")}}
        text = llm.ask_text(SUMMARY, json.dumps(payload, default=str), max_tokens=300, r=r)
        if text:
            return re.sub(r"\s*—\s*", ", ", text)[:800], llm.label(r)
    head = f"Unexplained: {cf['why']} ({f['channel']} {f['check_type'].replace('_', ' ')}, gap {_usd(f['gap'])})."
    return " ".join([head, *cf["facts"][:3], "Next: " + cf["next_step"]]), "template"


# running them -----------------------------------------------------------------------------------------------------
def run(store: Any, jobs: list[tuple[dict[str, Any], str, str | None]], book: Casebook,
        tracker: agents_live.Tracker | None = None, limit: int | None = None) -> list[str]:
    """jobs: (finding, trigger, parent agent id). Builds case files at most `limit` at a time (FLOCK_INVESTIGATORS),
    then attaches each through the gatekeeper in this thread. Returns the finding ids that got a case file."""
    if not jobs:
        return []
    tracker = agents_live.ensure(tracker)
    findings = store.all()
    book.prepare([f for f, _, _ in jobs], findings)
    agents = {f["finding_id"]: tracker.spawn("investigator", parent=parent, subject=f["finding_id"],
                                             agent_id=f"investigator:{f['finding_id']}", summary=TRIGGERS[trigger])
              for f, trigger, parent in jobs}
    tracker.flush()

    def work(f: dict[str, Any], trigger: str) -> dict[str, Any]:
        agents[f["finding_id"]].working(f"gathering context: {TRIGGERS[trigger]}")
        return build(f, trigger, book, findings)

    done: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, limit or max_concurrent())) as pool:
        futures = {pool.submit(work, f, trigger): (f, trigger) for f, trigger, _ in jobs}
        for fut in as_completed(futures):
            f, trigger = futures[fut]
            agent = agents[f["finding_id"]]
            try:
                cf = fut.result()
                transition(store, f["finding_id"], "investigate", ACTOR, patch={"case_file": cf},
                           reason=f"Investigator attached a case file ({TRIGGERS[trigger]}); outcome and money unchanged")
            except Exception as e:  # one failed case file never stops the run; the finding is untouched
                agent.failed(f"{type(e).__name__}: {e}"[:200])
                continue
            agent.done(cf["next_step"], trigger=trigger)
            done.append(f["finding_id"])
    return done
