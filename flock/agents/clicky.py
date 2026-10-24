"""Clicky (owner: Preet). Files APPROVED disputes into a FAKE merchant portal. Never a real one.

Today: writes each filed dispute to data/sandbox_portal/disputes.jsonl (stands in for the fake portal).
TODO: a small fake portal web page + browser automation that logs in and submits the form, so the demo
shows an agent actually clicking. Keep it on localhost.

Filing is gatekeeper-only in the state machine: Clicky does the clicking, the gatekeeper records the move.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flock.gatekeeper import GATEKEEPER, transition
from flock.store import ROOT, JsonStore

PORTAL = ROOT / "data" / "sandbox_portal" / "disputes.jsonl"


def submit_to_sandbox(f: dict[str, Any], portal: Path | None = None) -> str:
    portal = Path(portal or PORTAL)
    portal.parent.mkdir(parents=True, exist_ok=True)
    ref = f"SBX-{uuid.uuid4().hex[:6].upper()}"
    with portal.open("a") as fh:
        fh.write(json.dumps({
            "portal_ref": ref, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "channel": f["channel"], "order_ids": f["order_ids"], "amount": abs(f["gap"]),
            "message": f.get("dispute_draft"),
        }) + "\n")
    return ref


def file_approved(store: JsonStore, portal: Path | None = None) -> list[str]:
    """Files only APPROVED findings (a human approved them). Never files an empty message."""
    filed = []
    for f in store.all():
        if f["state"] != "APPROVED":
            continue
        if not f.get("dispute_draft"):
            print(f"[clicky] {f['finding_id']} is approved but has no draft; not filing", file=sys.stderr)
            continue
        ref = submit_to_sandbox(f, portal)
        transition(store, f["finding_id"], "file", GATEKEEPER, patch={"portal_ref": ref},
                   reason=f"Filed in sandbox portal as {ref} (via Clicky)")
        filed.append(ref)
    return filed
