"""The web UI (frontend/, served by flock/api.py at /) driven in a real browser through the whole demo click path in
docs/DEMO.md, against a showtime state prepared into a temp dir:

  open the UI -> lump sums -> the headline payout's waterfall -> the headline finding -> "try as an agent" (the
  gatekeeper refuses, logged) -> approve (Clicky files, SBX- ref) -> reject the conflict -> vendor follow-up approve +
  send -> theater replay -> metrics / scoreboard / judge market -> connectors panel (after POSTing the example batch).

Plus: 401 opens settings, 409 during a run is shown, no page errors. Screenshots go to results/ui/ (gitignored).

  FLOCK_TEST_BROWSER=1 pytest -q tests/test_frontend_e2e.py
  FLOCK_E2E_STORES=56 FLOCK_E2E_MONTHS=2 (defaults) size the prepared state; FLOCK_E2E_HEADED=1 shows the browser.
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("FLOCK_TEST_BROWSER") != "1",
                                reason="set FLOCK_TEST_BROWSER=1 to drive the UI in a real browser")

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "results" / "ui"
TOKEN = "e2e-token"


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A showtime state in tmp_path, served by flock.api's real handler (UI + API) on a free port, token required."""
    from flock import api, connectors, showtime
    from flock.store import JsonStore

    store = JsonStore(tmp_path / "state")
    portal = tmp_path / "portal.jsonl"
    info = showtime.prepare(months=int(os.environ.get("FLOCK_E2E_MONTHS", "2")), seed=2025,
                            stores=int(os.environ.get("FLOCK_E2E_STORES", "56")), store=store,
                            rules_path=tmp_path / "rules.json", portal=portal, workdir=tmp_path / "work", quiet=True)
    monkeypatch.setattr(api, "TOKEN", TOKEN)
    monkeypatch.setattr(connectors.collector, "RAW", tmp_path / "raw")
    a = api.Api(store=store, portal=portal)
    monkeypatch.setattr(api.Handler, "api", a)
    api.install_stdout()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", a, info
    finally:
        srv.shutdown()


def _json(url: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _browser(p):
    headless = os.environ.get("FLOCK_E2E_HEADED") != "1"
    try:
        return p.chromium.launch(headless=headless)
    except Exception:  # no bundled chromium: use the installed Chrome
        return p.chromium.launch(headless=headless, channel="chrome")


def test_demo_click_path(served):
    sync_api = pytest.importorskip("playwright.sync_api")
    base, a, info = served
    SHOTS.mkdir(parents=True, exist_ok=True)
    headline = info["headline"]["finding_id"]
    payout_id = info["lump_sum"]["payout_id"]
    fu_id = info["follow_up"]["finding_id"]
    conflict = next(f["finding_id"] for f in a.findings() if f["needs_human"] and f["outcome"] == "conflict")
    errors: list[str] = []

    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={"width": 1440, "height": 1600})
        page.on("pageerror", lambda e: errors.append(str(e)))
        # the 401 / 409s below are on purpose; the browser logs each as "Failed to load resource": anything else counts
        page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
                and errors.append(m.text))
        expect = sync_api.expect

        def shot(name: str) -> None:
            page.wait_for_timeout(300)
            page.screenshot(path=str(SHOTS / f"{name}.png"))

        # 1. open the UI from the API's own origin: same-origin API, live, metrics + showtime banner
        page.goto(base + "/")
        expect(page.locator("#liveStatus")).to_contain_text("Live")
        expect(page.locator("#apiBase")).to_contain_text(base)
        expect(page.locator("#metrics [data-how]")).to_have_count(4)
        metrics = page.locator("#metrics").inner_text()
        assert "—" not in metrics and "100%" in metrics and "$" in metrics and "ASSUMPTION" in metrics
        expect(page.locator("#showtimeBanner")).to_contain_text(payout_id)
        expect(page.locator("#showtimeBanner")).to_contain_text(f"${info['lump_sum']['short']:,.2f}")
        expect(page.locator("#agentChain [data-agent]")).to_have_count(len(a.agents()["agents"]))
        expect(page.locator("#logList button").first).to_be_visible()
        page.locator("[data-how='agent_time']").click()
        expect(page.locator("#modalBody")).to_contain_text("ASSUMPTION")
        expect(page.locator("#modalBody")).to_contain_text("Formula")
        shot("01_overview_how_we_got_this")
        page.locator("[data-close-modal]").click()
        shot("01_overview")

        # 2. lump sums -> the headline payout
        page.locator("aside [data-nav='lumpsums']").click()
        rows = page.locator("[data-payout]")
        expect(rows).to_have_count(_json(base + "/api/payouts")["total"])
        shot("02_lump_sums")
        page.locator(f"[data-payout='{payout_id}']").click()

        # 3. the waterfall: gross -> deductions (statement vs contract) -> expected net -> received -> shortfall
        expect(page.locator("#waterfall svg")).to_be_visible()
        for step in ("gross", "commission", "statement_net", "expected_net", "deposit", "shortfall"):
            expect(page.locator(f"[data-step='{step}']")).to_have_count(1)
        expect(page.locator("[data-test='gap-callouts']")).to_contain_text(f"${info['lump_sum']['short']:,.2f}")
        expect(page.locator(f"#payout [data-finding='{headline}']")).to_be_visible()
        shot("03_payout_waterfall")
        page.locator(f"#payout [data-finding='{headline}']").click()

        # 4. finding detail: rollup label (not hundreds of ids), evidence, votes, audit trail
        drawer = page.locator("#drawerBody")
        expect(page.locator("#drawer")).to_be_visible()
        expect(page.locator("[data-test='orders-label']")).to_contain_text("orders at")
        assert len(page.locator("[data-test='orders-label']").inner_text()) < 80
        expect(drawer).to_contain_text("Judge votes")
        expect(drawer.locator("[data-evidence]").first).to_be_visible()
        shot("04_finding_headline")

        # 5. try as an agent without a token: 401 -> settings open; then set name + token in the UI
        page.locator("[data-try-agent]").click()
        expect(page.locator("#settingsModal")).to_be_visible()
        expect(page.locator("#toast")).to_contain_text("token")
        page.fill("#actorInput", "presenter")
        page.fill("#tokenInput", TOKEN)
        page.locator("#settingsForm button[type=submit]").click()
        expect(page.locator("#settingsModal")).to_be_hidden()

        # 6. try as an agent with the token: the gatekeeper refuses (409) and the refusal is logged
        page.locator("[data-try-agent]").click()
        refusal = page.locator("[data-test='refusal']")
        expect(refusal).to_contain_text("refused")
        expect(refusal).to_contain_text("allowed false")
        expect(page.locator("[data-test='audit-trail']")).to_contain_text("Refused")
        assert a.store.log(headline)[-1]["allowed"] is False and a.store.log(headline)[-1]["role"] == "agent"
        assert a.store.get(headline)["state"] == "JUDGED"
        shot("05_try_as_agent_refused")

        # 7. approve: the API approves and Clicky files it (SBX- reference)
        page.fill("#decisionReason", "amendment says 20%")
        page.locator("[data-decide='approve']").click()
        expect(page.locator("[data-test='decision-result']")).to_contain_text("SBX-")
        expect(page.locator("[data-test='portal-ref']")).to_contain_text("SBX-")
        assert a.store.get(headline)["state"] == "FILED"
        shot("06_approved_filed")
        page.locator("#drawer [data-close-drawer]").last.click()
        expect(page.locator("#drawer")).to_be_hidden()

        # 8. a 409 during a run is shown, not swallowed (the conflict, opened from the overview's Needs review filter)
        page.locator("aside [data-nav='overview']").click()
        page.locator("[data-ffilter='review']").click()
        page.locator(f"#findingRows [data-finding='{conflict}']").click()
        a.running = "JOB-e2e"
        page.locator("[data-decide='reject']").click()
        expect(page.locator("#toast")).to_contain_text("run is in progress")
        a.running = None

        # 9. reject the conflict (rep not on the contract)
        page.fill("#decisionReason", "not binding: rep not on the contract")
        page.locator("[data-decide='reject']").click()
        expect(page.locator("[data-test='decision-result']")).to_contain_text("rejected")
        assert a.store.get(conflict)["state"] == "REJECTED"
        shot("07_conflict_rejected")

        # 10. vendor follow-up on the carried, unpaid claim: approve, then send via Clicky
        page.goto(f"{base}/#/finding/{fu_id}")
        expect(drawer).to_contain_text("Vendor follow-up")
        expect(drawer).to_contain_text("DRAFT")
        shot("08_follow_up_draft")
        page.locator("[data-fu='approve']").click()
        expect(page.locator("[data-fu='send']")).to_be_visible()
        page.locator("[data-fu='send']").click()
        expect(page.locator("[data-test='fu-sent-ref']")).to_contain_text("SBX-")
        assert a.store.get(fu_id)["follow_up"]["state"] == "SENT"
        shot("09_follow_up_sent")
        page.keyboard.press("Escape")

        # 11. theater: replay the last run at 4x
        page.locator("aside [data-nav='theater']").click()
        page.locator("[data-th='replay']").click()
        page.locator("[data-speed='4']").click()
        page.wait_for_function("Number(document.querySelector('[data-test=th-count]')?.textContent) > 25", timeout=30000)
        th = page.locator("#theater")
        expect(th).to_contain_text("Replay of the last run at 4")
        expect(th).to_contain_text("%")              # judges' confidence on the vote cards
        shot("10_theater_replay")
        page.locator("[data-th='live']").click()
        expect(th).to_contain_text("Live")

        # 12. scoreboard + learning curve with month labels + judge market balances
        page.locator("aside [data-nav='scoreboard']").click()
        sb = page.locator("#scoreboard")
        expect(sb.locator("[data-judge]")).to_have_count(3)
        expect(sb).to_contain_text("to a human")
        assert re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d\d", sb.inner_text())
        shot("11_scoreboard_market")

        # 13. connectors: POST the example batch, the Sources panel shows the claim, quote, sender, binding badge
        _json(base + "/api/connectors", json.loads((ROOT / "docs" / "connector_example.json").read_text()))
        page.locator("aside [data-nav='sources']").click()
        claim = page.locator("[data-claim]").first
        expect(claim).to_be_visible(timeout=10000)
        expect(claim).to_contain_text("Binding")
        expect(claim).to_contain_text("“")
        shot("12_sources")

        # 14. the static files are served with the right types, traversal refused
        for path, ctype in (("/", "text/html"), ("/dashboard.js", "javascript"), ("/frontend/dashboard.html", "text/html")):
            r = page.request.get(base + path)
            assert r.status == 200 and ctype in r.headers["content-type"]
        assert page.request.get(base + "/frontend/%2e%2e/flock/api.py").status == 404

        browser.close()
    assert not errors, errors
