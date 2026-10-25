"""Filing screens: hosted (file-mode) Clicky filings leave a confirmation page built from what was submitted."""
from flock import filing_screens
from flock.agents import clicky
from flock.gatekeeper import transition
from flock.models import Actor, Finding
from flock.store import JsonStore

F = {"finding_id": "F-6a673b72", "channel": "doordash", "order_ids": ["O1", "O2"], "gap": -549.33,
     "check_type": "commission", "dispute_draft": "Charged 25%, contract says 20%.", "evidence": [{}, {}]}


def test_capture_and_render(tmp_path):
    rec = filing_screens.capture(F, {"portal_ref": "SBX-42", "at": "2026-10-25T10:00:00+00:00"}, tmp_path)
    assert rec["portal_ref"] == "SBX-42" and rec["steps"][-1]["detail"] == "SBX-42"
    assert any("2 item" in s["detail"] for s in rec["steps"])
    page = filing_screens.render("F-6a673b72", tmp_path)
    assert "Filed by Clicky" in page and "SBX-42" in page and "549.33" in page and "Charged 25%" in page
    assert filing_screens.record("F-6a673b72", tmp_path)["fields"]["amount"] == "549.33"


def test_missing_and_escaped(tmp_path):
    assert filing_screens.render("F-none", tmp_path) is None
    assert filing_screens.record("F-none", tmp_path) is None
    filing_screens.capture(dict(F, finding_id="F-x", dispute_draft="<script>alert(1)</script>"), "SBX-1", tmp_path)
    assert "<script>alert" not in filing_screens.render("F-x", tmp_path)


def test_clicky_file_mode_saves_a_screen(tmp_path, monkeypatch):
    monkeypatch.setattr(clicky, "PORTAL", tmp_path / "portal" / "disputes.jsonl")
    s = JsonStore(tmp_path / "state")
    s.put_new(Finding(order_ids=["ORD-1"], channel="doordash", check_type="commission", expected=4, actual=6.5,
                      gap=2.5, finding_id="F-A", evidence=[{"source_file": "x.csv", "row": 1}]).to_dict())
    transition(s, "F-A", "check", Actor("checkers", "agent"))
    transition(s, "F-A", "judge", Actor("checkers", "agent"), patch={"outcome": "recoverable"})
    transition(s, "F-A", "draft", Actor("checkers", "agent"), patch={"dispute_draft": "Please refund 2.50."})
    transition(s, "F-A", "approve", Actor("controller", "human"))
    refs = clicky.file_approved(s, mode="file")
    assert len(refs) == 1
    page = filing_screens.render("F-A", s.dir)
    assert page and refs[0] in page and "Please refund 2.50." in page
    assert s.log("F-A")[-1]["evidence"][-1]["screen_page"] == "/api/filings/F-A/screen"
