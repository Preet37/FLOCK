"""Clicky (owner: Preet). Files APPROVED disputes into a FAKE merchant portal. Never a real one.

Two modes (FLOCK_CLICKY_MODE):
  file     (default) append each dispute to data/sandbox_portal/disputes.jsonl. Zero setup.
  browser  Playwright logs into the sandbox portal (flock/portal.py, localhost only), fills the form,
           submits, and reads back the reference number. Falls back to file mode if Playwright or a
           browser is missing. FLOCK_CLICKY_HEADED=1 shows the browser for the live demo.

Filing is gatekeeper-only in the state machine: Clicky does the clicking, the gatekeeper records the move.
"""
from __future__ import annotations

import os
import threading
import urllib.request
from typing import Any
from urllib.parse import urlparse

from flock import portal
from flock.gatekeeper import GATEKEEPER, transition
from flock.models import money
from flock.store import JsonStore

PORTAL = portal.DATA_DIR / "disputes.jsonl"
PORTAL_URL = os.environ.get("FLOCK_PORTAL_URL", f"http://{portal.HOST}:{portal.PORT}")
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def form_fields(f: dict[str, Any]) -> dict[str, str]:
    """What Clicky types into the portal form, built from the finding and its dispute draft."""
    return {
        "channel": f["channel"],
        "order_ids": ", ".join(f["order_ids"]),
        "amount": f"{money(abs(f['gap'])):.2f}",
        "category": f["check_type"] if f["check_type"] in portal.CATEGORIES else "commission",
        "merchant_ref": f["finding_id"],
        "message": f.get("dispute_draft") or f.get("reason", ""),
    }


def submit_to_sandbox(f: dict[str, Any]) -> str:
    fields = form_fields(f)
    ref = portal.new_ref()
    portal.append_filing(PORTAL, {
        "portal_ref": ref, "at": portal.now(), "channel": fields["channel"], "order_ids": f["order_ids"],
        "amount": float(fields["amount"]), "category": fields["category"], "merchant_ref": fields["merchant_ref"],
        "message": fields["message"], "via": "clicky-file",
    })
    return ref


def _portal_up(url: str) -> bool:
    try:
        urllib.request.urlopen(url + "/login", timeout=1)
        return True
    except OSError:
        return False


def _start_portal() -> None:
    from werkzeug.serving import WSGIRequestHandler, make_server

    class Quiet(WSGIRequestHandler):
        def log_request(self, *a):
            pass

    server = make_server(portal.HOST, portal.PORT, portal.create_app(), request_handler=Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Clicky started the sandbox portal at http://{portal.HOST}:{portal.PORT}")


def _launch(pw, headed: bool):
    opts = {"headless": not headed, "slow_mo": int(os.environ.get("FLOCK_CLICKY_SLOWMO", "350")) if headed else 0}
    channel = os.environ.get("FLOCK_CLICKY_CHANNEL")
    if channel:
        return pw.chromium.launch(channel=channel, **opts)
    try:
        return pw.chromium.launch(**opts)
    except Exception:
        return pw.chromium.launch(channel="chrome", **opts)  # installed Google Chrome


class BrowserFiler:
    """One browser session for the whole batch: log in once, file each dispute, read back the ref."""

    def __init__(self, url: str | None = None, headed: bool | None = None):
        url = url or PORTAL_URL
        if urlparse(url).hostname not in LOCAL_HOSTS:
            raise ValueError(f"Clicky only files into the localhost sandbox portal, not {url}")
        self.url = url.rstrip("/")
        self.headed = os.environ.get("FLOCK_CLICKY_HEADED") == "1" if headed is None else headed

    def __enter__(self) -> "BrowserFiler":
        from playwright.sync_api import sync_playwright

        if not _portal_up(self.url):
            _start_portal()
        self._pw = sync_playwright().start()
        try:
            self.browser = _launch(self._pw, self.headed)
            self.page = self.browser.new_page()
            self.login()
        except Exception:
            self._pw.stop()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.browser.close()
        self._pw.stop()

    def login(self) -> None:
        p = self.page
        p.goto(self.url + "/login")
        p.fill("#username", os.environ.get("FLOCK_PORTAL_USER", "sandbox"))
        p.fill("#password", os.environ.get("FLOCK_PORTAL_PASS", "sandbox"))
        p.click("#login-btn")
        p.wait_for_url("**/disputes/new")

    def file(self, f: dict[str, Any]) -> tuple[str, str]:
        p, fields = self.page, form_fields(f)
        p.goto(self.url + "/disputes/new")
        p.select_option("#channel", fields["channel"])
        p.fill("#order_ids", fields["order_ids"])
        p.fill("#amount", fields["amount"])
        p.select_option("#category", fields["category"])
        p.fill("#merchant_ref", fields["merchant_ref"])
        p.fill("#message", fields["message"])
        p.click("#submit-btn")
        p.wait_for_selector("#portal-ref")
        return p.inner_text("#portal-ref").strip(), p.url


def _record(store: JsonStore, f: dict[str, Any], ref: str, how: str, url: str | None = None) -> None:
    receipt = {"source": "sandbox_portal", "portal_ref": ref, "via": how}
    if url:
        receipt["url"] = url
    transition(store, f["finding_id"], "file", GATEKEEPER, patch={"portal_ref": ref},
               reason=f"Filed in sandbox portal as {ref} (via Clicky, {how})",
               evidence=list(f.get("evidence", [])) + [receipt])


def file_approved(store: JsonStore, mode: str | None = None) -> list[str]:
    approved = [f for f in store.all() if f["state"] == "APPROVED"]
    mode = mode or os.environ.get("FLOCK_CLICKY_MODE", "file")
    filed: list[str] = []
    if approved and mode == "browser":
        try:
            with BrowserFiler() as bot:
                for f in approved:
                    ref, url = bot.file(f)
                    _record(store, f, ref, "browser", url)
                    filed.append(ref)
            return filed
        except Exception as e:  # no Playwright, no browser, portal down: keep the demo running
            why = (str(e).splitlines() or [""])[0]
            print(f"Clicky browser mode unavailable ({type(e).__name__}: {why}); falling back to file mode")
            approved = [f for f in store.all() if f["state"] == "APPROVED"]
    for f in approved:
        ref = submit_to_sandbox(f)
        _record(store, f, ref, "file")
        filed.append(ref)
    return filed
