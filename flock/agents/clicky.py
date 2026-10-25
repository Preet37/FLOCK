"""Clicky (owner: Preet). Files APPROVED disputes into a FAKE merchant portal. Never a real one.

Two modes (FLOCK_CLICKY_MODE):
  file     (default) append each dispute to data/sandbox_portal/disputes.jsonl. Zero setup.
  browser  Playwright logs into the sandbox portal (flock/portal.py, localhost only), fills the form,
           submits, and reads back the reference number. Falls back to file mode if Playwright or a
           browser is missing. FLOCK_CLICKY_HEADED=1 shows the browser for the live demo.

Filing is gatekeeper-only in the state machine: Clicky does the clicking, the gatekeeper records the move.

Learned workflow (flock/recorder.py records, flock/workflow.py learns): Clicky learns the form by watching a human.
  python -m flock.agents.clicky record     # a person files ONE dispute in the portal; Clicky records and learns it
  python -m flock.agents.clicky show       # the learned steps, which finding field feeds each, its state
  python -m flock.agents.clicky --approve  # a human approves it (gatekeeper-logged with the workflow's hash)
In browser mode an APPROVED learned workflow replaces the hard-coded selectors below. A learned workflow that is
DRAFT (not approved yet) or STALE (a step's element is gone) stops browser filing: Clicky never guesses on a money
form. With no learned workflow at all (or a rejected one), the hard-coded path is the fallback.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flock import portal, workflow
from flock.gatekeeper import GATEKEEPER, Denied, transition
from flock.models import Actor, money
from flock.store import ROOT, Store, get_store

PORTAL = portal.DATA_DIR / "disputes.jsonl"
PORTAL_URL = os.environ.get("FLOCK_PORTAL_URL", f"http://{portal.HOST}:{portal.PORT}")
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
SCREENS_DIR = Path(os.environ.get("FLOCK_SCREENS_DIR") or ROOT / "data" / "screens")  # one screenshot per browser filing
ACTOR = Actor(id="clicky", role="agent")


class StaleWorkflow(Exception):
    """A learned step's element is not on the page (or is ambiguous). Clicky stops instead of guessing."""

    def __init__(self, step: dict[str, Any] | None, why: str, submitted: bool = False):
        super().__init__(why)
        self.step, self.why, self.submitted = step, why, submitted


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


def submit_to_sandbox(f: dict[str, Any], portal_path: Path | None = None) -> str:
    fields = form_fields(f)
    ref = portal.new_ref()
    portal.append_filing(Path(portal_path or PORTAL), {
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
    print(f"Clicky started the sandbox portal at http://{portal.HOST}:{portal.PORT} (layout {portal.layout_name()})")


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
    """One browser session for the whole batch: log in once, file each dispute, read back the ref.
    With wf (an approved learned workflow) it replays the human's demonstrated steps instead of fixed selectors."""

    def __init__(self, url: str | None = None, headed: bool | None = None, wf: dict[str, Any] | None = None):
        url = url or PORTAL_URL
        if urlparse(url).hostname not in LOCAL_HOSTS:
            raise ValueError(f"Clicky only files into the localhost sandbox portal, not {url}")
        self.url = url.rstrip("/")
        self.headed = os.environ.get("FLOCK_CLICKY_HEADED") == "1" if headed is None else headed
        self.wf = wf

    def __enter__(self) -> "BrowserFiler":
        from playwright.sync_api import sync_playwright

        if not _portal_up(self.url):
            _start_portal()
        self._pw = sync_playwright().start()
        try:
            self.browser = _launch(self._pw, self.headed)
            self.page = self.browser.new_page()
        except Exception:
            self._pw.stop()
            raise
        try:
            self.login()
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.browser.close()
        self._pw.stop()

    def login(self) -> None:
        if self.wf:
            self._replay([st for st in self.wf["steps"] if st["phase"] == "login"], None)
            return
        p = self.page
        p.goto(self.url + "/login")
        p.fill("#username", os.environ.get("FLOCK_PORTAL_USER", "sandbox"))
        p.fill("#password", os.environ.get("FLOCK_PORTAL_PASS", "sandbox"))
        p.click("#login-btn")
        p.wait_for_url("**/disputes/new")

    def file(self, f: dict[str, Any]) -> tuple[str, str]:
        ref, url = self._file(f)
        self.last_screen = self.snap(ref)
        return ref, url

    def snap(self, ref: str) -> str | None:
        """Screenshot of the portal's confirmation page, data/screens/filing-<ref>.png (GET /api/screens/<file>).
        Best effort: a failed screenshot never stops a filing."""
        name = "filing-" + re.sub(r"[^A-Za-z0-9_.-]", "_", ref or "unknown") + ".png"
        try:
            SCREENS_DIR.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(SCREENS_DIR / name), full_page=True)
            return name
        except Exception:
            return None

    def _file(self, f: dict[str, Any]) -> tuple[str, str]:
        if self.wf:
            ref = self._replay([st for st in self.wf["steps"] if st["phase"] == "file"], f)
            return ref, self.page.url
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

    # learned workflow replay ----------------------------------------------------------------------------------
    def _candidates(self, t: dict[str, Any]) -> list[tuple[str, Any]]:
        """Ways to find the element, most human-meaningful first: label, role + visible text, name; id, CSS last."""
        p, out = self.page, []
        if t.get("label"):
            out.append((f"label “{t['label']}”", p.get_by_label(t["label"], exact=True)))
        if t.get("role") in ("button", "link") and t.get("text"):
            out.append((f"{t['role']} “{t['text']}”", p.get_by_role(t["role"], name=t["text"], exact=True)))
        if t.get("name"):
            out.append((f"name={t['name']}", p.locator(f"{t.get('tag') or ''}[name=\"{t['name']}\"]")))
        if t.get("id"):
            out.append((f"id={t['id']}", p.locator(f"[id=\"{t['id']}\"]")))
        elif t.get("css"):
            out.append((f"css {t['css']}", p.locator(t["css"])))
        return out

    def _resolve(self, step: dict[str, Any], wait_ms: int = 2500):
        """Exactly one element, agreed on by every strategy that finds anything. Otherwise StaleWorkflow."""
        t = step.get("target") or {}
        deadline = time.monotonic() + wait_ms / 1000
        while True:
            found = [(how, loc) for how, loc in self._candidates(t) if loc.count() == 1]
            if found or time.monotonic() >= deadline:
                break
            self.page.wait_for_timeout(100)
        where = workflow._target_name(t)
        if not found:
            raise StaleWorkflow(step, f"step {step['n']}: can't find “{where}” on "
                                      f"{urlparse(self.page.url).path} (tried its label, role, name and id)")
        first = found[0][1].element_handle()
        for how, loc in found[1:]:
            if not loc.evaluate("(e, o) => e === o", first):
                raise StaleWorkflow(step, f"step {step['n']}: “{where}” is ambiguous "
                                          f"({found[0][0]} and {how} point at different elements)")
        return found[0][1]

    def _replay(self, steps: list[dict[str, Any]], f: dict[str, Any] | None) -> str | None:
        """Run learned steps. Values are computed and every element on the first page is located BEFORE anything is
        typed, so a changed form stops Clicky with nothing typed and nothing submitted."""
        if not steps:
            return None
        p = self.page
        values = {st["n"]: workflow.value_for(st["source"], f or {}) for st in steps if st.get("source")}
        for st in steps:
            src = st.get("source") or {}
            if src.get("kind") == "finding" and not values[st["n"]].strip():
                raise ValueError(f"the finding has no {workflow.FIELDS[src['field']]} for step {st['n']}")
        p.goto(self.url + steps[0]["path"])
        if steps[0]["action"] == "goto":
            steps = steps[1:]
        first_page: list[dict[str, Any]] = []
        for st in steps:
            if st["action"] == "goto":
                break
            first_page.append(st)
            if st["action"] == "click" and st.get("navigates"):
                break
        submitted, ref = False, None
        try:
            for st in first_page:  # preflight: everything Clicky will touch on this page exists, exactly once
                self._resolve(st)
            for st in steps:
                a = st["action"]
                if a == "goto":
                    p.goto(self.url + st["path"])
                    continue
                loc = self._resolve(st)
                if a == "fill":
                    loc.fill(values[st["n"]])
                elif a == "select":
                    try:
                        loc.select_option(value=values[st["n"]], timeout=2000)
                    except Exception:
                        raise StaleWorkflow(st, f"step {st['n']}: “{workflow._target_name(st['target'])}” has "
                                                f"no option {values[st['n']]!r}")
                elif a == "click" and st.get("navigates"):
                    with p.expect_navigation():
                        loc.click()
                    submitted = submitted or st["phase"] == "file"
                elif a == "click":
                    loc.click()
                elif a == "read_ref":
                    ref = loc.inner_text().strip()
                    if not re.match(st.get("pattern") or r"^\S+$", ref):
                        raise StaleWorkflow(st, f"step {st['n']}: expected a reference number, read {ref[:40]!r}")
            if f is not None and not ref:
                raise StaleWorkflow(None, "the workflow has no step that reads back the portal's reference")
        except StaleWorkflow as e:
            e.submitted = e.submitted or submitted
            raise
        return ref


def _record(store: Store, f: dict[str, Any], ref: str, how: str, url: str | None = None,
            wf: dict[str, Any] | None = None, screen: str | None = None) -> None:
    receipt = {"source": "sandbox_portal", "portal_ref": ref, "via": how}
    if url:
        receipt["url"] = url
    if screen:
        receipt["screenshot"] = f"/api/screens/{screen}"
    learned = ""
    if wf:
        receipt.update(workflow=wf["workflow_id"], workflow_hash=wf["hash"])
        learned = f", learned workflow {wf['workflow_id']} {wf['hash'][:12]} approved by {wf.get('approved_by')}"
    transition(store, f["finding_id"], "file", GATEKEEPER, patch={"portal_ref": ref},
               reason=f"Filed in sandbox portal as {ref} (via Clicky, {how}{learned})",
               evidence=list(f.get("evidence", [])) + [receipt])


def _ready(store: Store) -> list[dict[str, Any]]:
    """APPROVED findings (a human approved them) that have a draft. Never files an empty message."""
    ready = []
    for f in store.all():
        if f["state"] != "APPROVED":
            continue
        if not f.get("dispute_draft"):
            print(f"[clicky] {f['finding_id']} is approved but has no draft; not filing", file=sys.stderr)
            continue
        ready.append(f)
    return ready


def learned_status(wstore: workflow.WorkflowStore) -> tuple[dict[str, Any] | None, str | None]:
    """(workflow to replay or None, why browser filing must stop or None)."""
    wf = wstore.current()
    if wf is None or wf["state"] in ("REJECTED", "RETIRED"):
        return None, None  # nothing learned, or a human said no: the hard-coded path
    if workflow.usable(wf):
        return wf, None
    if wf["state"] == "DRAFT":
        return None, (f"learned workflow {wf['workflow_id']} is waiting for a human to approve it "
                      f"(python -m flock.agents.clicky --approve, or the dashboard's Clicky view)")
    if wf["state"] == "STALE":
        return None, (f"learned workflow {wf['workflow_id']} is stale ({wf.get('stale_reason')}); record one new "
                      f"demonstration: python -m flock.agents.clicky record")
    return None, f"learned workflow {wf['workflow_id']} no longer matches what the human approved (hash changed)"


def file_approved(store: Store, portal_path: Path | None = None, mode: str | None = None,
                  wstore: workflow.WorkflowStore | None = None) -> list[str]:
    approved = _ready(store)
    mode = mode or os.environ.get("FLOCK_CLICKY_MODE", "file")
    filed: list[str] = []
    if approved and mode == "browser":
        wstore = wstore or workflow.get_workflow_store(store)
        wf, stop = learned_status(wstore)
        if stop:
            print(f"Clicky did not file: {stop}")
            return filed
        started = False
        try:
            with BrowserFiler(wf=wf) as bot:
                started = True
                for f in approved:
                    ref, url = bot.file(f)
                    _record(store, f, ref, "browser", url, wf, screen=getattr(bot, "last_screen", None))
                    filed.append(ref)
            return filed
        except StaleWorkflow as e:
            workflow.mark_stale(wstore, wf, e.step, e.why, ACTOR, submitted=e.submitted)
            sent = "A dispute may have been submitted; check the portal." if e.submitted else "Nothing was submitted."
            print(f"Clicky stopped: {e.why}. {sent} {wf['workflow_id']} is now STALE; "
                  f"{len(approved) - len(filed)} approved dispute(s) wait for one new demonstration "
                  f"(python -m flock.agents.clicky record).")
            return filed
        except Exception as e:  # no Playwright, no browser, portal down: keep the demo running
            why = (str(e).splitlines() or [""])[0]
            if wf and started:  # mid-replay of a learned workflow: never retry a money form another way
                print(f"Clicky stopped mid-filing ({type(e).__name__}: {why}); not falling back. Check the portal.")
                return filed
            print(f"Clicky browser mode unavailable ({type(e).__name__}: {why}); falling back to file mode")
            approved = _ready(store)
    for f in approved:
        ref = submit_to_sandbox(f, portal_path)
        _record(store, f, ref, "file")
        filed.append(ref)
    return filed


# CLI: record a demonstration, learn, show, approve -------------------------------------------------------------
def demo_finding(store: Store, finding_id: str | None = None) -> dict[str, Any]:
    """The finding the human files while Clicky watches: the one asked for, else an approved or drafted dispute
    from the last run, else fixture F-0001 with an offline draft."""
    rows = store.all()
    if finding_id:
        hit = next((f for f in rows if f["finding_id"] == finding_id), None)
        if hit is None:
            raise KeyError(f"no finding {finding_id}")
        return hit
    for ok in (lambda f: f["state"] == "APPROVED", lambda f: f["state"] == "JUDGED" and f.get("outcome") == "recoverable"):
        hit = next((f for f in sorted(rows, key=lambda f: f["finding_id"]) if ok(f) and f.get("dispute_draft")), None)
        if hit:
            return hit
    from flock.agents import dispute_writer
    from flock.models import Finding

    fixture = json.loads((ROOT / "fixtures" / "fake_findings.json").read_text())[0]
    f = Finding(**fixture).to_dict()
    f["dispute_draft"] = dispute_writer.draft(f)
    return f


def record_demo_filing(store: Store, demo: dict[str, Any]) -> str | None:
    """The demonstration was a real filing in the sandbox portal. If the finding was approved, the gatekeeper records
    it as FILED with the reference the person got, so Clicky never files the same dispute a second time."""
    ref = next((e.get("value") for e in demo["events"] if e.get("kind") == "reference"), None)
    fid = demo["shown"].get("finding_id")
    try:
        f = store.get(fid)
    except KeyError:
        return None  # a fixture finding, not part of this run
    if not ref:
        return None
    if f["state"] != "APPROVED":
        print(f"[clicky] {fid} was filed in the sandbox portal during the demonstration ({ref}) but is {f['state']} in "
              f"Flock, so it is not marked filed", file=sys.stderr)
        return None
    transition(store, fid, "file", GATEKEEPER, patch={"portal_ref": ref},
               reason=f"Filed in sandbox portal as {ref} by a person while Clicky watched (demonstration "
                      f"{demo['demo_id']})",
               evidence=list(f.get("evidence", [])) + [{"source": "sandbox_portal", "portal_ref": ref,
                                                        "via": "human-demonstration", "demo_id": demo["demo_id"]}])
    return ref


def show(wf: dict[str, Any] | None) -> str:
    if wf is None:
        return "No learned workflow yet. Record one: python -m flock.agents.clicky record"
    lines = [f"{wf['workflow_id']}  state {wf['state']}  hash {wf['hash'][:12]}  learned from {wf['learned_from']}"
             f"  (portal layout {wf.get('portal_layout') or '?'})"]
    for st in wf["steps"]:
        src = st.get("source") or {}
        feed = {"finding": f"<- finding.{src.get('field')}", "env": f"<- ${src.get('name')}",
                "constant": "<- constant"}.get(src.get("kind"), "")
        lines.append(f"  {st['n']:>2}. [{st['phase']:<5}] {st['describe']}  {feed}")
    for w in wf.get("warnings", []):
        lines.append(f"  warning: {w}")
    if wf["state"] == "STALE":
        lines.append(f"  STALE: {wf.get('stale_reason')}. Record one new demonstration.")
    if wf["state"] == "DRAFT":
        lines.append("  Approve (human): python -m flock.agents.clicky --approve")
    if wf.get("approved_by"):
        lines.append(f"  approved by {wf['approved_by']} at {wf.get('approved_at')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m flock.agents.clicky", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="show", choices=["record", "learn", "show", "approve", "reject", "file"])
    ap.add_argument("--approve", action="store_true", help="a human approves the current learned workflow")
    ap.add_argument("--finding", help="record: which finding the human files (default: an approved/drafted one)")
    ap.add_argument("--demo", help="learn: demo id (default: the latest)")
    ap.add_argument("--actor", default="cli-human", help="approve / reject: who you are")
    ap.add_argument("--headless", action="store_true", help="record without showing the browser (scripts only)")
    a = ap.parse_args(argv)
    store = get_store()
    ws = workflow.get_workflow_store(store)
    cmd = "approve" if a.approve else a.command
    try:
        if cmd == "record":
            from flock import recorder

            f = demo_finding(store, a.finding)
            print(f"Recording. File this ONE dispute in the sandbox portal the way you normally would:\n"
                  f"  finding {f['finding_id']} ({f['state']}): {f['channel']} {', '.join(f['order_ids'])} "
                  f"${abs(f['gap']):.2f} {f['check_type']}\n"
                  f"  (the finding and its message are in the panel on the page, with copy buttons)")
            demo = recorder.record(f, PORTAL_URL, headed=not a.headless)
            print(f"Saved demonstration {demo['demo_id']}: {len(demo['events'])} events "
                  f"({demo['finished']}) in {workflow.demo_dir(demo['demo_id'])}")
            ref = record_demo_filing(store, demo)
            if ref:
                print(f"{f['finding_id']} was filed by you as {ref}; the gatekeeper recorded it, Clicky won't refile it")
            print(show(workflow.learn(demo, ws)))
        elif cmd == "learn":
            print(show(workflow.learn(workflow.load_demo(a.demo), ws)))
        elif cmd == "approve":
            wf = workflow.approve(ws, Actor(id=a.actor, role="human"))
            print(f"Approved {wf['workflow_id']} (hash {wf['hash'][:12]}) as {a.actor}. Logged by the gatekeeper.")
            print(show(wf))
        elif cmd == "reject":
            wf = ws.current()
            if wf is None:
                raise KeyError("no learned workflow")
            print(show(transition(ws, wf["workflow_id"], "reject", Actor(id=a.actor, role="human"),
                                  reason="Rejected via the CLI", machine=workflow.WORKFLOW)))
        elif cmd == "file":
            refs = file_approved(store, mode="browser")
            print(f"Clicky filed {len(refs)}: {', '.join(refs) or '-'}")
        else:
            print(show(ws.current()))
    except (Denied, KeyError, ValueError, FileNotFoundError) as e:
        sys.exit(f"clicky: {e}")


if __name__ == "__main__":
    main()
