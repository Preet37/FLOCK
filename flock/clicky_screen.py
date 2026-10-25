"""Clicky's live screen (frontend/agent-screen.html): the viewer's browser shows Clicky driving the sandbox portal
(frontend/portal/) with a visible cursor, and the portal's Submit lands here.

Hosted has no browser, so the cursor runs in the page that watches it. The money stays on the server: plan() says
what Clicky will type (built from the human-approved finding, same fields as clicky.form_fields), and file() checks
that what was typed matches that plan before writing the dispute to the sandbox portal. The state change goes through
the gatekeeper exactly like Clicky's other modes (clicky._record), so only an APPROVED finding can be filed and the
audit log says "via Clicky, screen".
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from flock import portal
from flock.agents import clicky
from flock.store import Store

PLATFORMS = {
    "doordash": "DoorDash", "ubereats": "Uber Eats", "grubhub": "Grubhub", "direct": "Direct app",
}
CHECKED = ("channel", "amount", "category", "merchant_ref")   # typed values that must match the plan exactly


class Mismatch(ValueError):
    """What was typed into the portal is not what the approved finding says. Nothing is filed."""


def _payout(f: dict[str, Any]) -> str:
    return next((o for o in f.get("order_ids", []) if "-PO-" in o), "")


def _evidence(f: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for e in f.get("evidence", []):
        src = e.get("source_file") or e.get("file")
        if not src:
            continue
        out.append({"name": Path(str(src)).name, "row": e.get("row") or e.get("line"), "ref": src,
                    "what": e.get("what", "")})
    return out[:6]


def item(f: dict[str, Any]) -> dict[str, Any]:
    return {"finding_id": f["finding_id"], "channel": f["channel"], "platform": PLATFORMS.get(f["channel"], f["channel"]),
            "gap": abs(float(f.get("gap") or 0)), "check_type": f.get("check_type"), "state": f["state"],
            "portal_ref": f.get("portal_ref")}


def queue(store: Store) -> list[dict[str, Any]]:
    """APPROVED findings Clicky can file (a human approved them and they have a draft), biggest first."""
    ready = [f for f in store.all() if f["state"] == "APPROVED" and f.get("dispute_draft")]
    return [item(f) for f in sorted(ready, key=lambda f: -abs(float(f.get("gap") or 0)))]


def plan(store: Store, fid: str) -> dict[str, Any]:
    """Everything the agent screen needs to file one finding: the fields to type, the evidence to attach."""
    f = store.get(fid)
    fields = clicky.form_fields(f)
    fields["payout_id"] = _payout(f)
    fields["category_label"] = portal.CATEGORIES.get(fields["category"], fields["category"])
    return {**item(f), "fields": fields, "evidence": _evidence(f), "order_count": len(f.get("order_ids", [])),
            "fileable": f["state"] == "APPROVED" and bool(f.get("dispute_draft")),
            "screen": f"/api/filings/{fid}/screen" if f.get("portal_ref") else None}


def file(store: Store, fid: str, typed: dict[str, Any] | None, portal_path: Path | None = None) -> dict[str, Any]:
    """The portal's Submit. Checks the typed form against the plan, files into the sandbox portal, then the
    gatekeeper moves APPROVED -> FILED (it refuses anything else, and logs the refusal)."""
    f = store.get(fid)
    want = clicky.form_fields(f)
    typed = typed or {}
    for k in CHECKED:
        got = str(typed.get(k, "")).strip()
        if k == "amount":
            try:
                same = abs(float(got.replace("$", "").replace(",", "")) - float(want[k])) < 0.005
            except ValueError:
                same = False
        else:
            same = got == want[k]
        if not same:
            raise Mismatch(f"the portal form says {k}={got!r} but the approved finding says {want[k]!r}; nothing filed")
    if f["state"] != "APPROVED":
        # let the gatekeeper refuse it (and log the refusal) instead of writing to the portal first
        clicky._record(store, f, "NOT-FILED", "screen")
    ref = clicky.submit_to_sandbox(f, portal_path)
    clicky._record(store, f, ref, "screen", page=clicky._screen_page(store, f, ref))
    return {"portal_ref": ref, "finding": item(store.get(fid)), "screen": f"/api/filings/{fid}/screen",
            "at": portal.now()}
