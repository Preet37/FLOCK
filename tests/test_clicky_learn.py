"""Clicky learns the dispute form from one human demonstration.

- demo.json -> workflow: typed values map back to the finding fields they came from; the rest stay constants;
  the password is never stored.
- Workflows move through the gatekeeper: an agent learns, only a human approves (with the hash), edits after
  learning or approval are refused.
- Replay against the real sandbox portal HTML (a tiny page driver over the Flask test client, no browser needed):
  files through gatekeeper.transition, reads back the SBX- reference; on the v2 layout it stops before typing,
  marks the workflow STALE and files nothing.
- Real browser, end to end (FLOCK_TEST_BROWSER=1): a scripted human is recorded, the workflow is learned, approved,
  replayed, goes stale on layout v2, and a second demonstration learns v2.
"""
import copy
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import pytest

from flock import portal, workflow
from flock.agents import clicky
from flock.gatekeeper import Denied, transition
from flock.models import Actor, Finding
from flock.store import JsonStore

ROOT = Path(__file__).resolve().parent.parent
DEMO = json.loads((ROOT / "fixtures" / "clicky_demo_v1.json").read_text())
AGENT = Actor("checkers", "agent")
HUMAN = Actor("controller", "human")


# ---------- helpers ----------
def seed_approved(store, fid, order_ids=("ORD-1",), gap=2.5, channel="doordash", check_type="commission",
                  draft="Please refund 2.50."):
    store.put_new(Finding(order_ids=list(order_ids), channel=channel, check_type=check_type, expected=4,
                          actual=4 + gap, gap=gap, finding_id=fid,
                          evidence=[{"source_file": "x.csv", "row": 1}]).to_dict())
    transition(store, fid, "check", AGENT)
    transition(store, fid, "judge", AGENT, patch={"outcome": "recoverable"})
    transition(store, fid, "draft", AGENT, patch={"dispute_draft": draft})
    transition(store, fid, "approve", HUMAN)


@pytest.fixture
def store(tmp_path):
    return JsonStore(tmp_path / "state")


@pytest.fixture
def ws(store, tmp_path):
    return workflow.WorkflowStore(store, tmp_path / "workflows")


def learned(ws, demo=None):
    return workflow.learn(copy.deepcopy(demo or DEMO), ws)


def lbl(item):
    return (item.get("target") or {}).get("label") or ""


def step_for(wf, label):
    return next(s for s in wf["steps"] if lbl(s) == label)


# ---------- demo.json -> workflow ----------
def test_values_map_back_to_finding_fields():
    steps = workflow.learn_steps(DEMO)
    src = {(s.get("target") or {}).get("label"): s.get("source") for s in steps}
    assert src["Sales channel"] == {"kind": "finding", "field": "channel", "format": {}}
    assert src["Order ID(s), comma separated"]["field"] == "order_ids"
    assert src["Amount disputed (USD)"] == {"kind": "finding", "field": "amount", "format": {"prefix": "$"}}
    assert src["Category"]["field"] == "category"
    assert src["Your internal reference (optional)"]["field"] == "finding_id"
    assert src["Message and evidence"]["field"] == "dispute_draft"
    assert src["Username"] == {"kind": "env", "name": "FLOCK_PORTAL_USER", "default": "sandbox"}
    assert src["Password"] == {"kind": "env", "name": "FLOCK_PORTAL_PASS", "secret": True}
    assert [s["phase"] for s in steps].count("login") == 4 and steps[-1]["action"] == "read_ref"
    assert steps[-1]["pattern"] == r"^SBX-[A-Z0-9]{6}$"
    assert "sandbox" not in json.dumps([s for s in steps if s["phase"] == "login" and s["action"] == "fill"
                                        and s["source"].get("secret")])


def test_password_never_stored_in_demo_or_workflow(ws):
    assert all(e.get("value") is None for e in DEMO["events"] if e.get("secret"))
    wf = learned(ws)
    assert "demo_value" not in step_for(wf, "Password")


def test_unmatched_values_stay_constants_and_are_flagged(ws):
    demo = copy.deepcopy(DEMO)
    for e in demo["events"]:
        if lbl(e) == "Your internal reference (optional)":
            e["value"] = "BATCH-7"
    wf = learned(ws, demo)
    assert step_for(wf, "Your internal reference (optional)")["source"] == {"kind": "constant", "value": "BATCH-7"}
    assert "finding_id" in wf["unused_fields"]
    assert any("same text every time" in w for w in wf["warnings"])


def test_order_ids_and_amount_formats():
    f = {**DEMO["shown"], "order_ids": ["ORD-2", "ORD-1"], "gap": -13.4}
    assert workflow.match_field("ORD-1,ORD-2", f) == ("order_ids", {"join": ","})
    assert workflow.match_field("13.40", f) == ("amount", {"prefix": ""})
    assert workflow.match_field("$13.4", f) == ("amount", {"prefix": "$"})
    assert workflow.match_field("13.41", f) is None
    assert workflow.value_for({"kind": "finding", "field": "amount", "format": {"prefix": "$"}}, f) == "$13.40"
    assert workflow.value_for({"kind": "finding", "field": "order_ids", "format": {"join": ","}}, f) == "ORD-2,ORD-1"


def test_last_value_wins_when_the_human_retypes_a_field():
    demo = copy.deepcopy(DEMO)
    i = next(i for i, e in enumerate(demo["events"]) if lbl(e).startswith("Amount"))
    demo["events"].insert(i, {**demo["events"][i], "value": "99.00"})
    steps = workflow.learn_steps(demo)
    amounts = [s for s in steps if lbl(s).startswith("Amount")]
    assert len(amounts) == 1 and amounts[0]["source"]["field"] == "amount"


def test_learned_example_fixture_matches_the_learner(ws):
    example = json.loads((ROOT / "fixtures" / "file_dispute_workflow.example.json").read_text())
    wf = learned(ws)
    assert [s["source"] for s in wf["steps"] if s.get("source")] == \
           [s["source"] for s in example["steps"] if s.get("source")]
    assert workflow.workflow_hash(example) == example["hash"]


# ---------- approval: agents propose, humans approve ----------
def test_learn_is_a_logged_draft_and_only_a_human_approves(ws, store):
    wf = learned(ws)
    assert wf["state"] == "DRAFT" and wf["hash"] == workflow.workflow_hash(wf)
    with pytest.raises(Denied):
        workflow.approve(ws, Actor("clicky", "agent"))
    assert ws.current()["state"] == "DRAFT"
    wf = workflow.approve(ws, HUMAN)
    assert wf["state"] == "APPROVED" and wf["approved_by"] == "controller" and wf["approved_hash"] == wf["hash"]
    assert workflow.usable(wf)
    log = ws.log(wf["workflow_id"])
    assert [(e["action"], e["allowed"]) for e in log] == [("learn", True), ("approve", False), ("approve", True)]
    assert wf["hash"] in log[-1]["reason"] and log[-1]["evidence"][0]["workflow_hash"] == wf["hash"]
    assert store.log(wf["workflow_id"])[-1]["action"] == "approve"  # also in the run's audit log


def test_steps_edited_after_learning_cannot_be_approved(ws):
    wf = learned(ws)
    path = ws.dir / "file_dispute.json"
    tampered = json.loads(path.read_text())
    step = next(s for s in tampered["steps"] if (s.get("source") or {}).get("field") == "amount")
    step["source"] = {"kind": "constant", "value": "999.00"}
    path.write_text(json.dumps(tampered))
    with pytest.raises(Denied, match="hash mismatch"):
        workflow.approve(ws, HUMAN)


def test_steps_edited_after_approval_are_not_used(ws, store):
    learned(ws)
    workflow.approve(ws, HUMAN)
    path = ws.dir / "file_dispute.json"
    tampered = json.loads(path.read_text())
    tampered["steps"][4]["target"]["label"] = "Something else"
    tampered["hash"] = workflow.workflow_hash(tampered)
    path.write_text(json.dumps(tampered))
    wf, stop = clicky.learned_status(ws)
    assert wf is None and "hash" in stop


def test_workflow_without_the_amount_cannot_be_approved(ws):
    demo = copy.deepcopy(DEMO)
    for e in demo["events"]:
        if lbl(e).startswith("Amount"):
            e["value"] = "1.00"
    learned(ws, demo)
    with pytest.raises(Denied, match="dispute amount"):
        workflow.approve(ws, HUMAN)


def test_draft_workflow_blocks_browser_filing(ws, store, monkeypatch):
    seed_approved(store, "F-A")
    learned(ws)
    monkeypatch.setattr(clicky, "BrowserFiler", None)  # would crash if Clicky tried
    assert clicky.file_approved(store, mode="browser", wstore=ws) == []
    assert store.get("F-A")["state"] == "APPROVED"
    assert not clicky.PORTAL.exists()  # and no silent fallback to file mode


def test_relearning_retires_the_old_version(ws):
    learned(ws)
    workflow.approve(ws, HUMAN)
    v2 = learned(ws)
    assert v2["workflow_id"] == "WF-file_dispute-v2" and v2["state"] == "DRAFT"
    assert ws.get("WF-file_dispute-v1")["state"] == "RETIRED"
    assert (ws.dir / "archive" / "WF-file_dispute-v1.json").exists()


# ---------- replay against the real portal HTML (no browser) ----------
class _Doc(HTMLParser):
    """Just enough of the DOM: elements with attrs, text, and which label points at which id."""

    def __init__(self):
        super().__init__()
        self.els, self.stack, self.labels = [], [], {}

    def handle_starttag(self, tag, attrs):
        el = {"tag": tag, "attrs": dict(attrs), "text": "", "value": "", "options": []}
        self.els.append(el)
        if tag == "option" and self.stack and self.stack[-1]["tag"] == "select":
            self.stack[-1]["options"].append(el)
        if tag not in ("input", "meta", "br"):
            self.stack.append(el)

    def handle_endtag(self, tag):
        while self.stack:
            el = self.stack.pop()
            if el["tag"] == "label" and el["attrs"].get("for"):
                self.labels[el["attrs"]["for"]] = " ".join(el["text"].split())
            if el["tag"] == tag:
                break

    def handle_data(self, data):
        for el in self.stack:
            el["text"] += data


class FakeLocator:
    def __init__(self, page, pred):
        self.page, self.pred = page, pred

    def _all(self):
        return [e for e in self.page.doc.els if self.pred(e)]

    def count(self):
        return len(self._all())

    def element_handle(self):
        return self._all()[0]

    def evaluate(self, _js, other):
        return self._all()[0] is other

    def fill(self, v):
        self._all()[0]["value"] = v

    def select_option(self, value, timeout=None):
        el = self._all()[0]
        if value not in [o["attrs"].get("value") for o in el["options"]]:
            raise ValueError(f"no option {value}")
        el["value"] = value

    def inner_text(self):
        return " ".join(self._all()[0]["text"].split())

    def click(self):
        el = self._all()[0]
        if el["tag"] == "a":
            return self.page.goto(el["attrs"]["href"])
        form = {}
        for e in self.page.doc.els:
            if e["tag"] in ("input", "textarea", "select") and e["attrs"].get("name"):
                first = e["options"][0]["attrs"].get("value") if e["options"] else ""
                form[e["attrs"]["name"]] = e["value"] or (first if e["tag"] == "select" else "")
        action = next(e["attrs"].get("action") for e in self.page.doc.els if e["tag"] == "form")
        self.page._load(self.page.client.post(action, data=form, follow_redirects=True))


class FakePage:
    """The slice of Playwright's Page that Clicky's replay uses, over the Flask test client."""

    def __init__(self, client, base="http://127.0.0.1:8765"):
        self.client, self.base, self.url, self.doc = client, base, base, _Doc()

    def _load(self, resp):
        self.url = self.base + resp.request.path
        self.doc = _Doc()
        self.doc.feed(resp.get_data(as_text=True))

    def goto(self, url):
        self._load(self.client.get(urlparse(url).path or url, follow_redirects=True))

    def get_by_label(self, text, exact=True):
        return FakeLocator(self, lambda e: e["attrs"].get("id") and self.doc.labels.get(e["attrs"]["id"]) == text
                           and e["tag"] != "label")

    def get_by_role(self, role, name, exact=True):
        tags = {"button": ("button",), "link": ("a",)}[role]
        return FakeLocator(self, lambda e: e["tag"] in tags and " ".join(e["text"].split()) == name)

    def locator(self, css):
        m = re.fullmatch(r'(\w*)\[(name|id)="([^"]+)"\]', css) or re.fullmatch(r"()#()([\w-]+)", css)
        if not m:
            return FakeLocator(self, lambda e: False)
        tag, attr, val = m.group(1), m.group(2) or "id", m.group(3)
        return FakeLocator(self, lambda e: (not tag or e["tag"] == tag) and e["attrs"].get(attr) == val)

    @contextmanager
    def expect_navigation(self):
        yield

    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000)


def fake_filer(tmp_path, layout):
    app = portal.create_app(tmp_path / "portal", layout=layout)
    app.config["TESTING"] = True

    class Filer(clicky.BrowserFiler):
        def __enter__(self):
            self.page = FakePage(app.test_client())
            self.login()
            return self

        def __exit__(self, *exc):
            pass

    return Filer


def portal_rows(tmp_path):
    return portal.read_filings(tmp_path / "portal" / "disputes.jsonl")


def test_replay_files_through_the_gatekeeper(ws, store, tmp_path, monkeypatch):
    seed_approved(store, "F-A")
    seed_approved(store, "F-B", order_ids=("ORD-7", "ORD-8"), gap=-13.4, channel="ubereats", check_type="promo",
                  draft="Promo was ours to cover? No: contract says the platform funds it.")
    learned(ws)
    workflow.approve(ws, HUMAN)
    monkeypatch.setattr(clicky, "BrowserFiler", fake_filer(tmp_path, "v1"))
    refs = clicky.file_approved(store, mode="browser", wstore=ws)
    assert len(refs) == 2 and all(r.startswith("SBX-") for r in refs)
    rows = {r["merchant_ref"]: r for r in portal_rows(tmp_path)}
    assert rows["F-B"]["order_ids"] == ["ORD-7", "ORD-8"] and rows["F-B"]["amount"] == 13.4
    assert rows["F-B"]["channel"] == "ubereats" and rows["F-B"]["category"] == "promo"
    assert rows["F-B"]["message"].startswith("Promo was ours") and rows["F-B"]["via"] == "portal"
    wf = ws.current()
    for fid in ("F-A", "F-B"):
        f, last = store.get(fid), store.log(fid)[-1]
        assert f["state"] == "FILED" and f["portal_ref"] == rows[fid]["portal_ref"]
        assert last["actor"] == "gatekeeper" and last["action"] == "file" and wf["workflow_id"] in last["reason"]
        assert last["evidence"][-1]["workflow_hash"] == wf["hash"]
    assert not clicky.PORTAL.exists()


def test_layout_v2_stops_before_typing_and_marks_stale(ws, store, tmp_path, monkeypatch):
    seed_approved(store, "F-A")
    learned(ws)
    workflow.approve(ws, HUMAN)
    monkeypatch.setattr(clicky, "BrowserFiler", fake_filer(tmp_path, "v2"))
    assert clicky.file_approved(store, mode="browser", wstore=ws) == []
    assert store.get("F-A")["state"] == "APPROVED" and portal_rows(tmp_path) == []
    assert not clicky.PORTAL.exists()  # never guesses another way
    wf = ws.current()
    assert wf["state"] == "STALE" and wf["stale_step"] == 5 and "Sales channel" in wf["stale_reason"]
    assert wf["stale_submitted"] is False
    last = ws.log(wf["workflow_id"])[-1]
    assert last["action"] == "stale" and last["actor"] == "clicky" and "Nothing was submitted" in last["reason"]
    # and it stays stopped until a human demonstrates again
    assert clicky.file_approved(store, mode="browser", wstore=ws) == []
    assert "stale" in clicky.learned_status(ws)[1]


def test_select_with_unknown_option_is_stale_not_a_guess(ws, store, tmp_path, monkeypatch):
    seed_approved(store, "F-A")
    demo = copy.deepcopy(DEMO)
    learned(ws, demo)
    workflow.approve(ws, HUMAN)
    f = store.get("F-A")
    filer = fake_filer(tmp_path, "v1")(wf=ws.current())
    with filer as bot:
        with pytest.raises(clicky.StaleWorkflow, match="no option"):
            bot.file({**f, "channel": "acme_eats"})
    assert portal_rows(tmp_path) == []


def test_portal_v2_serves_renamed_fields(tmp_path):
    c = portal.create_app(tmp_path, layout="v2").test_client()
    c.post("/login", data={"username": "sandbox", "password": "sandbox"})
    page = c.get("/disputes/new").get_data(as_text=True)
    assert 'name="claim_amount"' in page and 'name="amount"' not in page and "Send to platform" in page
    assert page.index("Reason for dispute") < page.index("Delivery platform")
    r = c.post("/disputes/new", data={"platform": "grubhub", "order_refs": "ORD-9", "claim_amount": "3.10",
                                      "reason_code": "fee", "case_details": "x"})
    assert r.status_code == 400  # 'fee' is not a portal category
    r = c.post("/disputes/new", data={"platform": "grubhub", "order_refs": "ORD-9", "claim_amount": "3.10",
                                      "reason_code": "deposit", "case_details": "short payout", "case_id": "F-9"})
    assert r.status_code == 302
    row = portal.read_filings(tmp_path / "disputes.jsonl")[0]
    assert row["channel"] == "grubhub" and row["amount"] == 3.1 and row["merchant_ref"] == "F-9"
    with pytest.raises(ValueError):
        portal.create_app(tmp_path, layout="v9")


# ---------- the real thing: record a scripted human, learn, replay, relearn (FLOCK_TEST_BROWSER=1) ----------
def scripted_human(f, layout):
    """Plays the person at the keyboard: finds fields by what they say on screen, like a person would."""
    def drive(page, tick):
        def do(action):
            action()
            page.wait_for_timeout(350)   # like a person pausing; lets the typed value reach the recorder
            tick()
        do(lambda: page.get_by_label("Username").fill("sandbox"))
        do(lambda: page.get_by_label("Password").fill("sandbox"))
        do(lambda: page.get_by_role("button", name="Sign in").click())
        page.wait_for_url("**/disputes/new")
        if layout == "v1":
            do(lambda: page.get_by_label("Sales channel").select_option(f["channel"]))
            do(lambda: page.get_by_label("Order ID(s), comma separated").fill(", ".join(f["order_ids"])))
            do(lambda: page.get_by_label("Amount disputed (USD)").fill("$%.2f" % abs(f["gap"])))
            do(lambda: page.get_by_label("Category").select_option(f["check_type"]))
            do(lambda: page.get_by_label("Your internal reference (optional)").fill(f["finding_id"]))
            do(lambda: page.get_by_label("Message and evidence").fill(f["dispute_draft"]))
            do(lambda: page.get_by_role("button", name="Submit dispute").click())
        else:
            do(lambda: page.get_by_label("Reason for dispute").select_option(f["check_type"]))
            do(lambda: page.get_by_label("Disputed amount ($)").fill("%.2f" % abs(f["gap"])))
            do(lambda: page.get_by_label("Delivery platform").select_option(f["channel"]))
            do(lambda: page.get_by_label("Affected order numbers").fill(",".join(f["order_ids"])))
            do(lambda: page.get_by_label("Explain the issue").fill(f["dispute_draft"]))
            do(lambda: page.get_by_label("Merchant case ID (optional)").fill(f["finding_id"]))
            do(lambda: page.get_by_role("button", name="Send to platform").click())
    return drive


@contextmanager
def live_portal(data_dir, layout, port=0):
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", port, portal.create_app(data_dir, layout=layout))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server.server_port
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(os.environ.get("FLOCK_TEST_BROWSER") != "1", reason="set FLOCK_TEST_BROWSER=1 to drive a real browser")
def test_record_learn_replay_relearn_in_a_real_browser(ws, store, tmp_path, monkeypatch):
    from flock import recorder

    demos = tmp_path / "demos"
    seed_approved(store, "F-A")
    seed_approved(store, "F-B", order_ids=("ORD-7", "ORD-8"), gap=-13.4, channel="ubereats", check_type="promo",
                  draft="Contract says the platform funds this promo.\nPlease credit 13.40.")
    shown = clicky.demo_finding(JsonStore(tmp_path / "empty"))   # fixture F-0001 with its offline draft

    with live_portal(tmp_path / "portal", "v1") as (url, port):
        demo = recorder.record(shown, url, headed=False, driver=scripted_human(shown, "v1"), root=demos, linger_s=0.3)
        assert demo["finished"] == "reference_seen" and demo["portal_layout"] == "v1"
        out = demos / demo["demo_id"]
        assert (out / "video.webm").exists() and (out / "trace.zip").exists()
        assert all((out / e["screenshot"]).exists() for e in demo["events"] if e.get("screenshot"))
        assert sum(1 for e in demo["events"] if e.get("screenshot")) >= 10
        wf = workflow.learn(demo, ws)
        assert wf["fields"].keys() == set(workflow.FIELDS)
        if os.environ.get("FLOCK_WRITE_FIXTURES") == "1":  # refresh the committed examples
            keep = {k: v for k, v in demo.items()}
            (ROOT / "fixtures" / "clicky_demo_v1.json").write_text(json.dumps(keep, indent=2) + "\n")
            (ROOT / "fixtures" / "file_dispute_workflow.example.json").write_text(json.dumps(wf, indent=2) + "\n")
        workflow.approve(ws, HUMAN)
        monkeypatch.setattr(clicky, "PORTAL_URL", url)
        refs = clicky.file_approved(store, mode="browser", wstore=ws)
        assert len(refs) == 2
        assert {store.get(f)["state"] for f in ("F-A", "F-B")} == {"FILED"}
        rows = {r["merchant_ref"]: r for r in portal.read_filings(tmp_path / "portal" / "disputes.jsonl")}
        assert rows["F-B"]["amount"] == 13.4 and rows["F-B"]["order_ids"] == ["ORD-7", "ORD-8"]
        assert rows["F-B"]["message"].replace("\r\n", "\n") == "Contract says the platform funds this promo.\nPlease credit 13.40."
        assert store.log("F-B")[-1]["evidence"][-1]["workflow"] == "WF-file_dispute-v1"

    seed_approved(store, "F-C", order_ids=("ORD-9",), gap=3.1, channel="grubhub", check_type="deposit",
                  draft="Payout was short by 3.10.")
    before = len(portal.read_filings(tmp_path / "portal" / "disputes.jsonl"))
    with live_portal(tmp_path / "portal", "v2") as (url, port):   # the platform redesigned its form
        monkeypatch.setattr(clicky, "PORTAL_URL", url)
        assert clicky.file_approved(store, mode="browser", wstore=ws) == []
        assert ws.current()["state"] == "STALE" and store.get("F-C")["state"] == "APPROVED"
        assert len(portal.read_filings(tmp_path / "portal" / "disputes.jsonl")) == before

        demo2 = recorder.record(shown, url, headed=False, driver=scripted_human(shown, "v2"), root=demos,
                                linger_s=0.3)
        assert demo2["portal_layout"] == "v2"
        wf2 = workflow.learn(demo2, ws)
        assert wf2["workflow_id"] == "WF-file_dispute-v2" and wf2["fields"].keys() == set(workflow.FIELDS)
        assert step_for(wf2, "Disputed amount ($)")["source"]["format"] == {"prefix": ""}
        assert ws.get("WF-file_dispute-v1")["state"] == "RETIRED"
        workflow.approve(ws, HUMAN)
        refs = clicky.file_approved(store, mode="browser", wstore=ws)
        assert len(refs) == 1 and store.get("F-C")["state"] == "FILED"
        row = portal.read_filings(tmp_path / "portal" / "disputes.jsonl")[-1]
        assert row["merchant_ref"] == "F-C" and row["amount"] == 3.1 and row["channel"] == "grubhub"
        assert store.log("F-C")[-1]["evidence"][-1]["workflow"] == "WF-file_dispute-v2"


# ---------- CLI: record -> learn -> approve; the demonstration's own filing is recorded once ----------
def test_cli_record_learns_and_marks_the_demo_filing(store, tmp_path, monkeypatch, capsys):
    from flock import recorder

    seed_approved(store, "F-0001", order_ids=("ORD-1001",), draft=DEMO["shown"]["dispute_draft"])
    monkeypatch.setattr(clicky, "get_store", lambda: store)
    monkeypatch.setattr(recorder, "record", lambda f, url, headed=True: {**copy.deepcopy(DEMO), "shown": f})
    clicky.main(["record"])
    out = capsys.readouterr().out
    assert "WF-file_dispute-v1  state DRAFT" in out and "won't refile it" in out
    ref = next(e["value"] for e in DEMO["events"] if e["kind"] == "reference")
    f = store.get("F-0001")
    assert f["state"] == "FILED" and f["portal_ref"] == ref
    assert store.log("F-0001")[-1]["actor"] == "gatekeeper" and "while Clicky watched" in store.log("F-0001")[-1]["reason"]
    clicky.main(["--approve", "--actor", "preet"])
    assert "Approved WF-file_dispute-v1" in capsys.readouterr().out
    assert workflow.get_workflow_store(store).current()["approved_by"] == "preet"


# ---------- dashboard: the Clicky view ----------
def test_dashboard_clicky_view_and_human_only_approval(ws, store, tmp_path, monkeypatch):
    from dashboard.app import create_app

    monkeypatch.setattr(workflow, "DEMOS_DIR", tmp_path / "demos")
    c = create_app(store, runs_path=tmp_path / "runs.json", workflow_store=ws).test_client()
    assert c.get("/api/clicky").get_json()["workflow"] is None
    wf = learned(ws)
    shots = tmp_path / "demos" / wf["learned_from"]
    shots.mkdir(parents=True)
    (shots / "step-05.png").write_bytes(b"\x89PNG fake")
    d = c.get("/api/clicky").get_json()
    assert d["needs_approval"] and d["uses"] == "stopped" and d["workflow"]["workflow_id"] == "WF-file_dispute-v1"
    assert d["workflow"]["steps"][4]["describe"] == "Pick the finding's sales channel in “Sales channel”"
    assert c.get(d["demo"]["base"] + "step-05.png").status_code == 200
    assert c.get(d["demo"]["base"] + "demo.json").status_code == 404           # only recording files
    assert c.get("/api/clicky/demos/..%2Fstate/step-05.png").status_code == 404

    r = c.post("/api/clicky/approve", json={"actor": "clicky", "role": "agent"})
    assert r.status_code == 403 and not r.get_json()["log_entry"]["allowed"]
    assert c.post("/api/clicky/approve", json={}).status_code == 400
    r = c.post("/api/clicky/approve", json={"actor": "maja"})
    assert r.status_code == 200 and r.get_json()["workflow"]["approved_by"] == "maja"
    d = c.get("/api/clicky").get_json()
    assert d["uses"] == "learned" and not d["needs_approval"]
    assert [e["action"] for e in d["log"]] == ["learn", "approve", "approve"]
    assert any(e["subject_type"] == "workflow" for e in c.get("/api/summary").get_json()["activity"])
