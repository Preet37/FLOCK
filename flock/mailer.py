"""Mailer: sends the vendor follow-up email after a human approves it. One function, three backends, chosen by env.

  FLOCK_SMTP_HOST / FLOCK_SMTP_PORT / FLOCK_SMTP_USER / FLOCK_SMTP_PASSWORD   any SMTP server with STARTTLS
      (Gmail: smtp.gmail.com, 587, the Gmail address, a Google *app password*; see docs/email.md)
  RESEND_API_KEY (+ FLOCK_MAIL_FROM)                                           Resend's HTTP API
  neither                                                                      append to <state_dir>/outbox.jsonl

FLOCK_VENDOR_EMAIL_TO redirects every vendor email to one inbox (the demo: the presenter's own address); the body then
says which vendor address it was meant for. FLOCK_MAIL_FROM sets the From (default: the SMTP user).

Every message, sent or not, is also kept in outbox.jsonl so the UI can show what went out. Secrets never reach it.
A failed send falls back to the outbox with the error, so an approval never crashes on a mail problem.
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.request
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

OUTBOX = "outbox.jsonl"
RESEND_URL = "https://api.resend.com/emails"

# Simulated vendor support addresses (.example: never deliverable). FLOCK_VENDOR_EMAIL_TO redirects them for the demo.
VENDOR_EMAIL = {
    "doordash": "merchant-support@doordash.example",
    "ubereats": "restaurants-support@ubereats.example",
    "grubhub": "restaurant-care@grubhub.example",
    "direct": "payments@direct-app.example",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def backend() -> str:
    if os.environ.get("FLOCK_SMTP_HOST"):
        return "smtp"
    if os.environ.get("RESEND_API_KEY"):
        return "resend"
    return "outbox"


def sender() -> str:
    return (os.environ.get("FLOCK_MAIL_FROM") or os.environ.get("FLOCK_SMTP_USER")
            or "Flock <flock@flock.example>")


def _smtp(msg: EmailMessage) -> None:
    host = os.environ["FLOCK_SMTP_HOST"]
    port = int(os.environ.get("FLOCK_SMTP_PORT") or 587)
    user, password = os.environ.get("FLOCK_SMTP_USER"), os.environ.get("FLOCK_SMTP_PASSWORD")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            if user:
                s.login(user, password or "")
            s.send_message(msg)
        return
    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls(context=ctx)
        if user:
            s.login(user, password or "")
        s.send_message(msg)


def _resend(to: str, subject: str, body: str) -> str | None:
    req = urllib.request.Request(RESEND_URL, method="POST",
                                 data=json.dumps({"from": sender(), "to": [to], "subject": subject,
                                                  "text": body}).encode(),
                                 headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
                                          "Content-Type": "application/json", "User-Agent": "flock-mailer/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return (json.loads(r.read() or b"{}") or {}).get("id")
    except urllib.error.HTTPError as e:   # keep Resend's reason (unverified recipient, bad from, ...) for the outbox row
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}") from None


def _keep(state_dir: Path, row: dict[str, Any]) -> None:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / OUTBOX).open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def send(to: str, subject: str, body: str, *, state_dir: Path | str,
         meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send one plain-text email. Returns {status: sent|outbox, via: smtp|resend|outbox, to, intended_to, id, at,
    subject, body, error?}. meta (e.g. finding_id, claim_ref) is stored with the outbox row."""
    intended = to
    redirect = os.environ.get("FLOCK_VENDOR_EMAIL_TO")
    if redirect and redirect != to:
        to = redirect
        body = f"[Demo: this email was addressed to {intended}; redirected to {to}.]\n\n{body}"
    row: dict[str, Any] = {"id": "MAIL-" + uuid.uuid4().hex[:10], "at": _now(), "to": to, "intended_to": intended,
                           "from": sender(), "subject": subject, "body": body, **(meta or {})}
    via = backend()
    try:
        if via == "smtp":
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender(), to, subject
            msg["Message-ID"] = f"<{row['id']}@flock>"
            msg.set_content(body)
            _smtp(msg)
        elif via == "resend":
            row["provider_id"] = _resend(to, subject, body)
        row.update(status="sent" if via != "outbox" else "outbox", via=via)
    except Exception as e:  # never let a mail problem break an approval; keep it visible instead
        row.update(status="outbox", via="outbox", error=f"{via} failed: {type(e).__name__}: {str(e)[:200]}")
    _keep(Path(state_dir), row)
    return row


def outbox(state_dir: Path | str, limit: int = 50) -> list[dict[str, Any]]:
    """Messages kept in the outbox (sent and unsent), newest first."""
    p = Path(state_dir) / OUTBOX
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(rows))[:limit]


def send_follow_up(f: dict[str, Any], *, state_dir: Path | str) -> dict[str, Any]:
    """Email the approved vendor follow-up of finding f (flock/agents/follow_up.py) to the platform's support
    address. Call after the gatekeeper recorded the send (the human approval is the gate, not this function)."""
    fu = f.get("follow_up") or {}
    to = VENDOR_EMAIL.get(f.get("channel", ""), f"support@{f.get('channel', 'vendor')}.example")
    subject = (f"Unpaid claim {fu.get('claim_ref') or f.get('portal_ref')}: "
               f"${float(fu.get('amount') or abs(f.get('gap', 0))):,.2f} ({f['finding_id']})")
    return send(to, subject, fu.get("draft") or "", state_dir=state_dir,
                meta={"finding_id": f["finding_id"], "claim_ref": fu.get("claim_ref"), "kind": "vendor_follow_up"})
