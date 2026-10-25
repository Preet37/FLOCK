"""Filing screens: what Clicky "saw" when it filed a dispute on a host with no browser.

On a laptop Clicky drives a real Chrome (FLOCK_CLICKY_MODE=browser) and saves a PNG of the portal's confirmation page.
On the hosted server it files through the sandbox portal's records directly (file mode), so there is no screenshot.
capture() writes the equivalent: the portal's confirmation page as HTML plus the step list Clicky went through, built
only from what was actually submitted (the form fields and the portal reference). Nothing here invents a filing.

  state_dir/screens/<finding_id>.html   the confirmation page (GET /api/filings/<id>/screen)
  state_dir/screens/<finding_id>.json   {finding_id, portal_ref, at, via, fields, steps}
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCREENS = "screens"


def _safe(fid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", fid or "unknown")


def _paths(finding_id: str, state_dir: Path | str) -> tuple[Path, Path]:
    d = Path(state_dir) / SCREENS
    return d / f"{_safe(finding_id)}.html", d / f"{_safe(finding_id)}.json"


def steps_for(fields: dict[str, str], ref: str, evidence_n: int) -> list[dict[str, str]]:
    """The steps a person would take on the portal, with the value Clicky used at each one."""
    return [
        {"step": "Opened the merchant portal", "detail": "sandbox portal, logged in as the merchant"},
        {"step": "Started a new dispute", "detail": "Disputes > New"},
        {"step": "Filled the claim form", "detail": f"{fields.get('channel')} · {fields.get('category')} · "
                                                    f"${fields.get('amount')} · ref {fields.get('merchant_ref')}"},
        {"step": "Listed the orders", "detail": fields.get("order_ids", "")[:200]},
        {"step": "Attached the evidence", "detail": f"{evidence_n} item(s): data rows and the deal quote"},
        {"step": "Submitted", "detail": "Submit dispute"},
        {"step": "Read back the confirmation number", "detail": ref},
    ]


def _page(rec: dict[str, Any]) -> str:
    e = html.escape
    f = rec["fields"]
    rows = "".join(f"<tr><th>{e(k.replace('_', ' ').title())}</th><td>{e(str(v))}</td></tr>"
                   for k, v in f.items() if k != "message")
    steps = "".join(f"<li><b>{e(s['step'])}</b><span>{e(s['detail'])}</span></li>" for s in rec["steps"])
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dispute {e(rec['portal_ref'])} submitted</title>
<style>
 body{{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:#f4f5f7;color:#16181d}}
 .bar{{background:#16181d;color:#fff;padding:10px 20px;display:flex;justify-content:space-between;align-items:center}}
 .bar b{{letter-spacing:.02em}} .tag{{background:#d4fb54;color:#16181d;border-radius:99px;padding:3px 10px;font-weight:600;font-size:12px}}
 main{{max-width:760px;margin:24px auto;padding:0 16px}}
 .card{{background:#fff;border-radius:14px;padding:20px 22px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.06)}}
 .ok{{color:#127a3a;font-weight:700;font-size:18px}} .ref{{font:600 22px ui-monospace,Menlo,monospace}}
 table{{width:100%;border-collapse:collapse}} th{{text-align:left;color:#666;font-weight:500;width:34%;padding:6px 0;vertical-align:top}}
 td{{padding:6px 0}} pre{{white-space:pre-wrap;background:#f7f7f9;border-radius:8px;padding:12px;font:13px/1.45 ui-monospace,Menlo,monospace}}
 ol{{padding-left:20px;margin:0}} li{{margin:6px 0}} li span{{display:block;color:#666;font-size:13px}}
 .muted{{color:#777;font-size:12px}}
</style></head><body>
<div class="bar"><b>Merchant Portal · sandbox</b><span class="tag">Filed by Clicky</span></div>
<main>
 <div class="card"><div class="ok">✓ Dispute submitted</div>
  <div class="muted">Confirmation number</div><div class="ref" id="portal-ref">{e(rec['portal_ref'])}</div>
  <div class="muted">{e(rec['at'])} · via {e(rec['via'])} · finding {e(rec['finding_id'])}</div></div>
 <div class="card"><h3>What was submitted</h3><table>{rows}</table>
  <h4>Message</h4><pre>{e(f.get('message', ''))}</pre></div>
 <div class="card"><h3>What Clicky did</h3><ol>{steps}</ol>
  <p class="muted">Simulated portal. Hosted runs have no browser, so this page is rebuilt from the exact fields
  submitted and the reference the portal returned.</p></div>
</main></body></html>"""


def capture(finding: dict[str, Any], portal_response: dict[str, Any] | str, state_dir: Path | str,
            fields: dict[str, str] | None = None) -> dict[str, Any]:
    """Save the confirmation screen for one filing. portal_response: the portal row (dict with portal_ref, at, via)
    or just the reference string. fields: what was typed (default: clicky.form_fields(finding)). Returns the record."""
    if fields is None:
        from flock.agents.clicky import form_fields
        fields = form_fields(finding)
    resp = {"portal_ref": portal_response} if isinstance(portal_response, str) else dict(portal_response)
    ref = str(resp.get("portal_ref") or "")
    rec = {"finding_id": finding["finding_id"], "portal_ref": ref,
           "at": resp.get("at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "via": resp.get("via") or "clicky-file", "fields": {k: str(v) for k, v in fields.items()},
           "steps": steps_for(fields, ref, len(finding.get("evidence") or []))}
    page, meta = _paths(finding["finding_id"], state_dir)
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(_page(rec))
    meta.write_text(json.dumps(rec, indent=2))
    return rec


def render(finding_id: str, state_dir: Path | str) -> str | None:
    """The saved confirmation page, or None if Clicky hasn't filed this finding here."""
    page, _ = _paths(finding_id, state_dir)
    return page.read_text() if page.exists() else None


def record(finding_id: str, state_dir: Path | str) -> dict[str, Any] | None:
    """The saved step list and fields (JSON), or None."""
    _, meta = _paths(finding_id, state_dir)
    try:
        return json.loads(meta.read_text())
    except (OSError, ValueError):
        return None
