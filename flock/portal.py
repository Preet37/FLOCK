"""Sandbox merchant dispute portal (owner: Preet). FAKE, localhost only. Clicky files disputes here.

  python -m flock.portal            # serves http://127.0.0.1:8765  (login: sandbox / sandbox)

Not a real platform and not connected to one. Filings land in data/sandbox_portal/disputes.jsonl.
Creds: FLOCK_PORTAL_USER / FLOCK_PORTAL_PASS. Port: FLOCK_PORTAL_PORT.
"""
from __future__ import annotations

import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, redirect, render_template_string, request, session, url_for

from flock.models import CHANNELS, money
from flock.store import ROOT

HOST = "127.0.0.1"
PORT = int(os.environ.get("FLOCK_PORTAL_PORT", "8765"))
DATA_DIR = ROOT / "data" / "sandbox_portal"
CATEGORIES = {
    "commission": "Incorrect commission",
    "promo": "Promotion charged incorrectly",
    "tax": "Tax withheld incorrectly",
    "error_charge": "Error charge / adjustment",
    "deposit": "Missing or short payout",
    "missing_order": "Missing order",
}

LAYOUT = """<!doctype html>
<html><head><meta charset="utf-8"><title>{{ title }} - Sandbox Merchant Portal</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f4f5f7;color:#1f2328}
 .banner{background:#b42318;color:#fff;text-align:center;padding:6px;font-weight:600;letter-spacing:.03em}
 header{background:#24292f;color:#fff;padding:12px 24px;display:flex;justify-content:space-between;align-items:center}
 header a{color:#c9d1d9;margin-left:16px;text-decoration:none}
 main{max-width:760px;margin:24px auto;background:#fff;padding:24px 28px;border-radius:8px;box-shadow:0 1px 3px #0002}
 label{display:block;margin-top:14px;font-weight:600;font-size:14px}
 input,select,textarea{width:100%;box-sizing:border-box;padding:8px;margin-top:4px;border:1px solid #d0d7de;border-radius:6px;font:inherit}
 textarea{min-height:140px}
 button{margin-top:18px;background:#1f6feb;color:#fff;border:0;padding:10px 18px;border-radius:6px;font-weight:600;cursor:pointer}
 table{width:100%;border-collapse:collapse;font-size:14px}
 th,td{text-align:left;padding:8px;border-bottom:1px solid #eaeef2;vertical-align:top}
 .err{background:#ffebe9;color:#82071e;padding:8px 12px;border-radius:6px}
 .ref{font-size:28px;font-family:ui-monospace,monospace;background:#ddf4ff;padding:8px 14px;border-radius:6px;display:inline-block}
 .muted{color:#656d76;font-size:13px}
</style></head><body>
<div class="banner">SANDBOX &mdash; not a real platform. Filings go nowhere.</div>
<header><strong>Sandbox Merchant Portal &middot; Dispute Center</strong>
<nav>{% if session.get('user') %}<a href="{{ url_for('new_dispute') }}">File a dispute</a>
<a href="{{ url_for('list_disputes') }}">My disputes</a><a href="{{ url_for('logout') }}">Sign out</a>{% endif %}</nav></header>
<main>{{ body|safe }}</main></body></html>"""

LOGIN = """<h2>Sign in</h2>
{% if error %}<p class="err" id="login-error">{{ error }}</p>{% endif %}
<form method="post" action="{{ url_for('login') }}">
<label for="username">Username</label><input id="username" name="username" autocomplete="off">
<label for="password">Password</label><input id="password" name="password" type="password">
<button id="login-btn" type="submit">Sign in</button></form>
<p class="muted">Sandbox credentials only. Default: sandbox / sandbox.</p>"""

FORM = """<h2>File a dispute</h2>
{% if error %}<p class="err" id="form-error">{{ error }}</p>{% endif %}
<form method="post" action="{{ url_for('new_dispute') }}">
<label for="channel">Sales channel</label>
<select id="channel" name="channel">{% for c in channels %}<option value="{{ c }}">{{ c }}</option>{% endfor %}</select>
<label for="order_ids">Order ID(s), comma separated</label><input id="order_ids" name="order_ids">
<label for="amount">Amount disputed (USD)</label><input id="amount" name="amount" inputmode="decimal">
<label for="category">Category</label>
<select id="category" name="category">{% for k, v in categories.items() %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select>
<label for="merchant_ref">Your internal reference (optional)</label><input id="merchant_ref" name="merchant_ref">
<label for="message">Message and evidence</label><textarea id="message" name="message"></textarea>
<button id="submit-btn" type="submit">Submit dispute</button></form>"""

CONFIRM = """<h2>Dispute received</h2>
<p>Your reference number:</p><p class="ref" id="portal-ref">{{ d.portal_ref }}</p>
<table><tr><th>Channel</th><td>{{ d.channel }}</td></tr>
<tr><th>Orders</th><td>{{ d.order_ids|join(', ') }}</td></tr>
<tr><th>Amount</th><td>${{ '%.2f'|format(d.amount) }}</td></tr>
<tr><th>Category</th><td>{{ categories.get(d.category, d.category) }}</td></tr>
<tr><th>Your reference</th><td>{{ d.merchant_ref or '-' }}</td></tr>
<tr><th>Submitted</th><td>{{ d.at }}</td></tr></table>
<p><a href="{{ url_for('list_disputes') }}">View all disputes</a></p>"""

LIST = """<h2>My disputes</h2>
{% if not rows %}<p class="muted" id="empty">No disputes filed yet.</p>{% else %}
<table id="disputes"><tr><th>Reference</th><th>Channel</th><th>Orders</th><th>Amount</th><th>Category</th><th>Submitted</th></tr>
{% for d in rows %}<tr><td><a href="{{ url_for('show_dispute', ref=d.portal_ref) }}">{{ d.portal_ref }}</a></td>
<td>{{ d.channel }}</td><td>{{ d.order_ids|join(', ') }}</td><td>${{ '%.2f'|format(d.amount) }}</td>
<td>{{ categories.get(d.category, d.category or '-') }}</td><td>{{ d.at }}</td></tr>{% endfor %}</table>{% endif %}"""


def new_ref() -> str:
    return f"SBX-{uuid.uuid4().hex[:6].upper()}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_filing(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def read_filings(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def create_app(data_dir: Path | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLOCK_PORTAL_SECRET") or secrets.token_hex(16)
    filings = Path(data_dir or DATA_DIR) / "disputes.jsonl"
    user = os.environ.get("FLOCK_PORTAL_USER", "sandbox")
    password = os.environ.get("FLOCK_PORTAL_PASS", "sandbox")

    def page(title: str, body: str, **ctx: Any) -> str:
        ctx.update(channels=CHANNELS, categories=CATEGORIES)
        return render_template_string(LAYOUT, title=title, body=render_template_string(body, **ctx))

    @app.before_request
    def require_login():
        if request.endpoint not in ("login", "static") and not session.get("user"):
            return redirect(url_for("login"))

    @app.route("/")
    def index():
        return redirect(url_for("list_disputes"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            if (secrets.compare_digest(request.form.get("username", ""), user)
                    and secrets.compare_digest(request.form.get("password", ""), password)):
                session["user"] = user
                return redirect(url_for("new_dispute"))
            error = "Wrong username or password."
        return page("Sign in", LOGIN, error=error), (401 if error else 200)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/disputes/new", methods=["GET", "POST"])
    def new_dispute():
        if request.method == "GET":
            return page("File a dispute", FORM, error=None)
        form = request.form
        order_ids = [o.strip() for o in form.get("order_ids", "").split(",") if o.strip()]
        try:
            amount = money(form.get("amount", "").replace("$", ""))
        except ValueError:
            amount = 0.0
        error = None
        if form.get("channel") not in CHANNELS:
            error = "Pick a sales channel."
        elif not order_ids:
            error = "Enter at least one order ID."
        elif amount <= 0:
            error = "Amount must be a positive number."
        elif form.get("category") not in CATEGORIES:
            error = "Pick a category."
        elif not form.get("message", "").strip():
            error = "Describe the dispute."
        if error:
            return page("File a dispute", FORM, error=error), 400
        row = {"portal_ref": new_ref(), "at": now(), "channel": form["channel"], "order_ids": order_ids,
               "amount": amount, "category": form["category"], "merchant_ref": form.get("merchant_ref", "").strip(),
               "message": form["message"].strip(), "via": "portal"}
        append_filing(filings, row)
        return redirect(url_for("show_dispute", ref=row["portal_ref"]))

    @app.route("/disputes/<ref>")
    def show_dispute(ref: str):
        for d in read_filings(filings):
            if d["portal_ref"] == ref:
                return page("Dispute received", CONFIRM, d=d)
        return page("Not found", "<p>No dispute with that reference.</p>"), 404

    @app.route("/disputes")
    def list_disputes():
        rows = sorted(read_filings(filings), key=lambda d: d["at"], reverse=True)
        return page("My disputes", LIST, rows=rows)

    return app


if __name__ == "__main__":
    print(f"Sandbox portal (FAKE, localhost only): http://{HOST}:{PORT}")
    create_app().run(host=HOST, port=PORT, debug=False)
