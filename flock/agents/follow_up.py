"""Vendor follow-up (owner: Preet). A claim Flock filed that the platform never paid gets a polite reminder.

For a finding that is FILED but not RECOVERED, this agent drafts: "we filed claim SBX-... on <date> for $X; it remains
unpaid; please credit", with the platform, the claim reference, the amount and a short evidence summary. Every number
comes from code (the finding's |gap|, the portal reference, the audit log's filing date); no model writes this.
Same rule as a dispute: the agent drafts, a human approves (or rejects), and only the gatekeeper sends it, with
Clicky typing it into the sandbox portal (clicky.send_follow_ups). Each step is a gatekeeper annotation on the
FILED finding, so it is logged like every other move and the finding carries it as `follow_up`:

  follow_up = {state: DRAFT | APPROVED | REJECTED | SENT, claim_ref, filed_on, amount, channel, platform,
               evidence_summary, draft, drafted_by, drafted_at, approved_by?, approved_at?, rejected_by?,
               sent_ref?, sent_at?}

  python -m flock.agents.follow_up                          # filed claims still unpaid, and their follow-ups
  python -m flock.agents.follow_up draft                    # draft for claims filed FLOCK_FOLLOW_UP_DAYS (14) ago
  python -m flock.agents.follow_up draft --finding F-1234   # draft for this claim now
  python -m flock.agents.follow_up approve F-1234 --actor preet --send   # a human approves; Clicky sends it
  python -m flock.agents.follow_up reject F-1234 --actor preet
  python -m flock.agents.follow_up send                     # send every approved follow-up

Over HTTP (flock/api.py, dashboard/app.py): POST /api/findings/<id>/follow_up (draft), .../follow_up/approve,
.../follow_up/reject ({actor, reason}), .../follow_up/send (Clicky, returns the portal reference).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from typing import Any

from flock.agents.dispute_writer import PLATFORM_NAME, no_em_dashes
from flock.gatekeeper import Denied, transition
from flock.models import Actor, money
from flock.store import Store, get_store

ACTOR = Actor(id="follow_up", role="agent")
DEFAULT_DAYS = 14   # how long a filed claim may sit unpaid before the agent drafts a reminder


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def wait_days() -> int:
    try:
        return max(0, int(os.environ.get("FLOCK_FOLLOW_UP_DAYS", DEFAULT_DAYS)))
    except ValueError:
        return DEFAULT_DAYS


def filed_entry(store: Store, f: dict[str, Any]) -> dict[str, Any] | None:
    """The allowed 'file' move in the audit log: who filed it, when, and with which reference."""
    rows = [e for e in store.log(f["finding_id"]) if e.get("allowed") and e.get("action") == "file"]
    return rows[-1] if rows else None


def unpaid(store: Store) -> list[dict[str, Any]]:
    """Filed claims the platform hasn't paid yet (state FILED; RECOVERED means it paid)."""
    return sorted((f for f in store.all() if f["state"] == "FILED"), key=lambda f: f["finding_id"])


def _orders(f: dict[str, Any]) -> str:
    ids = f.get("order_ids") or []
    if f.get("rollup"):
        return f"{f['rollup']['orders']:,} orders in payout {f['rollup']['payout_id']}"
    if f["check_type"] == "deposit":
        return f"payout {', '.join(ids)}"
    return f"order {', '.join(ids)}" if len(ids) == 1 else (
        f"orders {', '.join(ids[:5])}" + (f" and {len(ids) - 5} more" if len(ids) > 5 else ""))


def evidence_summary(f: dict[str, Any], limit: int = 3) -> str:
    """A few lines a platform agent can check: the data rows and the deal quote behind the claim."""
    parts = []
    for e in f.get("evidence", []):
        if e.get("source", "") == "sandbox_portal":
            continue
        if e.get("quote"):
            q = e["quote"] if len(e["quote"]) <= 160 else e["quote"][:157].rstrip() + "..."
            parts.append(f'{e.get("source_file")} ({e.get("authority") or "unknown authority"}): "{q}"')
        elif e.get("source_file"):
            rows = e.get("rows") or ([e["row"]] if e.get("row") is not None else [])
            where = (f"rows {rows[0]}-{rows[-1]} ({len(rows):,} rows)" if len(rows) > 1
                     else f"row {rows[0]}" if rows else "")
            parts.append(f"{e['source_file']} {where}".strip() + (f": {e['what']}" if e.get("what") else ""))
        if len(parts) >= limit:
            break
    more = len([e for e in f.get("evidence", []) if e.get("source") != "sandbox_portal"]) - len(parts)
    return "; ".join(parts) + (f"; plus {more} more item(s) in the original claim" if more > 0 else "")


def compose(store: Store, f: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """The follow-up for one filed claim, numbers from code."""
    entry = filed_entry(store, f)
    filed_on = (entry or {}).get("at", "")[:10] or None
    platform = PLATFORM_NAME.get(f["channel"], f["channel"])
    amount = money(abs(f["gap"]))
    ref = f.get("portal_ref")
    summary = evidence_summary(f)
    today = today or datetime.now(timezone.utc).date()
    text = no_em_dashes(
        f"Hello {platform} merchant support,\n\n"
        f"We filed claim {ref} on {filed_on or 'an earlier date'} for ${amount:.2f} "
        f"({f['check_type'].replace('_', ' ')}, {_orders(f)}). As of {today.isoformat()} it remains unpaid: no credit "
        f"for it has been recorded on our side.\n\n"
        f"Please credit ${amount:.2f} on our next payout, or tell us what else you need to close it.\n\n"
        f"Evidence summary: {summary}\n\nThank you.")
    return {"state": "DRAFT", "claim_ref": ref, "filed_on": filed_on, "amount": amount, "channel": f["channel"],
            "platform": platform, "evidence_summary": summary, "draft": text, "drafted_by": ACTOR.id,
            "drafted_at": _now()}


def due(store: Store, days: int | None = None, today: date | None = None) -> list[dict[str, Any]]:
    """Unpaid claims with no follow-up yet, filed at least `days` ago."""
    days = wait_days() if days is None else days
    today = today or datetime.now(timezone.utc).date()
    out = []
    for f in unpaid(store):
        if f.get("follow_up"):
            continue
        entry = filed_entry(store, f)
        try:
            age = (today - date.fromisoformat(entry["at"][:10])).days if entry else None
        except ValueError:
            age = None
        if age is not None and age >= days:
            out.append(f)
    return out


def draft(store: Store, finding_ids: list[str] | None = None, days: int | None = None,
          today: date | None = None) -> list[str]:
    """Draft follow-ups (agent move, logged). Named findings are drafted now; otherwise the ones that are due."""
    todo = [store.get(i) for i in finding_ids] if finding_ids else due(store, days, today)
    done = []
    for f in todo:
        fu = compose(store, f, today)
        transition(store, f["finding_id"], "follow_up", ACTOR, patch={"follow_up": fu},
                   reason=f"Claim {fu['claim_ref']} filed {fu['filed_on']} is still unpaid; follow-up drafted, "
                          "waiting for a human")
        done.append(f["finding_id"])
    return done


def approve(store: Store, finding_id: str, actor: Actor, text: str | None = None, reason: str = "") -> dict[str, Any]:
    """A human approves the follow-up (optionally with edited wording). The gatekeeper refuses anyone else."""
    fu = dict(store.get(finding_id).get("follow_up") or {})
    fu.update(state="APPROVED", approved_by=actor.id, approved_at=_now())
    if text:
        fu["draft"] = no_em_dashes(text)
    return transition(store, finding_id, "approve_follow_up", actor, patch={"follow_up": fu},
                      reason=reason or "Follow-up approved; Clicky may send it")


def reject(store: Store, finding_id: str, actor: Actor, reason: str = "") -> dict[str, Any]:
    fu = dict(store.get(finding_id).get("follow_up") or {})
    fu.update(state="REJECTED", rejected_by=actor.id, rejected_at=_now())
    return transition(store, finding_id, "reject_follow_up", actor, patch={"follow_up": fu},
                      reason=reason or "Follow-up rejected; not sent")


def send(store: Store, finding_id: str, portal_path: Any = None) -> str:
    """Clicky sends this one approved follow-up; the gatekeeper records SENT. Returns the portal reference. Not
    approved yet (DRAFT, REJECTED, none) or not FILED: the gatekeeper refuses the send, logs it, raises Denied."""
    from flock.agents import clicky

    f = store.get(finding_id)
    fu = f.get("follow_up") or {}
    if f["state"] != "FILED" or fu.get("state") != "APPROVED":   # let the gatekeeper say no, on the record
        transition(store, finding_id, "send_follow_up", clicky.GATEKEEPER,
                   patch={"follow_up": {**fu, "state": "SENT"}}, reason="Send follow-up (not approved by a human)")
    refs = clicky.send_follow_ups(store, portal_path, finding_ids=[finding_id])
    if not refs:   # transition() above always raises in that case; this is a guard for odd stores
        raise Denied(f"follow-up on {finding_id} was not sent")
    return refs[0]


def show(store: Store) -> str:
    rows = unpaid(store)
    if not rows:
        return "No filed claims waiting on the platform."
    lines = []
    for f in rows:
        fu = f.get("follow_up") or {}
        lines.append(f"{f['finding_id']}  {f['channel']:<10} {f['check_type']:<13} ${abs(f['gap']):>9,.2f}  "
                     f"claim {f.get('portal_ref')}  follow-up: {fu.get('state', 'none')}"
                     + (f" ({fu['sent_ref']})" if fu.get("sent_ref") else ""))
        if fu.get("state") == "DRAFT":
            lines.append("    " + fu["draft"].replace("\n", "\n    "))
            lines.append(f"    approve: python -m flock.agents.follow_up approve {f['finding_id']} --actor <you> --send")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m flock.agents.follow_up", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="show", choices=["show", "draft", "approve", "reject", "send"])
    ap.add_argument("finding", nargs="?", help="approve / reject: the finding id")
    ap.add_argument("--finding", dest="finding_opt", help="draft: draft for this claim now")
    ap.add_argument("--days", type=int, default=None, help=f"draft: days a claim may sit unpaid (default {DEFAULT_DAYS})")
    ap.add_argument("--actor", default="cli-human", help="approve / reject: who you are")
    ap.add_argument("--reason", default="")
    ap.add_argument("--send", action="store_true", help="approve: Clicky sends it right away")
    a = ap.parse_args(argv)
    store = get_store()
    try:
        if a.command == "draft":
            ids = draft(store, [a.finding_opt or a.finding] if (a.finding_opt or a.finding) else None, a.days)
            print(f"drafted {len(ids)} follow-up(s): {', '.join(ids) or '-'}")
            print(show(store))
        elif a.command in ("approve", "reject"):
            if not a.finding:
                raise KeyError("which finding? e.g. approve F-1234 --actor preet")
            who = Actor(id=a.actor, role="human")
            if a.command == "approve":
                approve(store, a.finding, who, reason=a.reason)
                print(f"{a.finding}: follow-up approved by {a.actor}. Logged by the gatekeeper.")
            else:
                reject(store, a.finding, who, reason=a.reason)
                print(f"{a.finding}: follow-up rejected by {a.actor}. Logged by the gatekeeper.")
        if a.command == "send" or (a.command == "approve" and a.send):
            from flock.agents import clicky

            refs = clicky.send_follow_ups(store)
            print(f"Clicky sent {len(refs)} follow-up(s) to the sandbox portal: {', '.join(refs) or '-'}")
        if a.command == "show":
            print(show(store))
    except (Denied, KeyError) as e:
        sys.exit(f"follow_up: {e}")


if __name__ == "__main__":
    main()
