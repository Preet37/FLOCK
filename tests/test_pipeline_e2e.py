"""End to end: the week 1 -> human approves R-001 -> week 2 demo, in a temp dir. Plus: nothing files without a human."""
import importlib.util
import json

import pytest

from conftest import ROOT
from flock import pipeline
from flock.agents import learner
from flock.models import Actor
from flock.store import JsonStore

spec = importlib.util.spec_from_file_location("score", ROOT / "scripts" / "score.py")
score_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(score_mod)

HUMAN = Actor("controller", "human")


@pytest.fixture
def paths(tmp_path):
    return {"store": JsonStore(tmp_path / "state"), "rules_path": tmp_path / "rules.json",
            "portal": tmp_path / "portal" / "disputes.jsonl"}


def portal_lines(p):
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_week1_approve_week2_demo(paths, capsys):
    repo_rules = ROOT / "rules.json"
    repo_rules_before = repo_rules.read_text() if repo_rules.exists() else None

    # WEEK 1: day one, no learned rules
    store = pipeline.run(reset=True, forget=True, demo_approve=True, **paths)
    s1 = score_mod.score(store)
    assert (s1["caught"], s1["total"], s1["to_human"], s1["false_alarms"], s1["by_rule"]) == (6, 6, 2, 0, 0)
    assert s1["recovered"] == pytest.approx(17.90)
    assert len(portal_lines(paths["portal"])) == 3
    assert {f["finding_id"]: f["state"] for f in store.all()} == {
        "F-0001": "FILED", "F-0002": "FILED", "F-0003": "REJECTED",
        "F-0004": "REJECTED", "F-0005": "JUDGED", "F-0006": "FILED"}
    [proposal] = json.loads((paths["store"].dir / "proposed_rules.json").read_text())
    assert proposal["rule_id"] == "R-001" and proposal["precedents"] == ["F-0003", "F-0004"]
    assert not paths["rules_path"].exists()  # proposed, not approved

    # the human approves the rule the Learner proposed
    learner.approve("R-001", HUMAN, store=store, rules_path=paths["rules_path"])

    # WEEK 2: same traps, fewer items for a human
    store = pipeline.run(reset=True, demo_approve=True, **paths)
    s2 = score_mod.score(store)
    assert (s2["caught"], s2["total"], s2["to_human"], s2["false_alarms"], s2["by_rule"]) == (6, 6, 0, 0, 2)
    assert len(portal_lines(paths["portal"])) == 3  # reset cleared week 1's portal
    for fid in ("F-0003", "F-0004"):
        assert store.get(fid)["reason"].startswith("Applied approved rule R-001")
    assert json.loads((paths["store"].dir / "proposed_rules.json").read_text()) == []

    out = capsys.readouterr().out
    assert "caught 6/6 traps" in out and "sent to a human: 2" in out and "sent to a human: 0" in out
    # the repo's own rules.json was never touched
    assert (repo_rules.read_text() if repo_rules.exists() else None) == repo_rules_before


def test_without_a_human_nothing_is_approved_filed_or_learned(paths):
    store = pipeline.run(reset=True, forget=True, demo_approve=False, **paths)
    states = {f["state"] for f in store.all()}
    assert states == {"JUDGED"}
    assert portal_lines(paths["portal"]) == []
    assert not paths["rules_path"].exists()
    assert all(e["role"] == "agent" for e in store.log())
    drafted = sorted(f["finding_id"] for f in store.all() if f.get("dispute_draft"))
    assert drafted == ["F-0001", "F-0002", "F-0006"]  # drafts wait for a human


def test_every_filed_dispute_was_approved_by_a_human_first(paths):
    store = pipeline.run(reset=True, forget=True, demo_approve=True, **paths)
    for f in store.all():
        if f["state"] != "FILED":
            continue
        moves = [(e["action"], e["role"]) for e in store.log(f["finding_id"]) if e["allowed"]]
        assert ("approve", "human") in moves and ("file", "gatekeeper") in moves
        assert moves.index(("approve", "human")) < moves.index(("file", "gatekeeper"))


def test_adversarial_week_files_only_what_it_should(paths):
    store = pipeline.run(reset=True, forget=True, demo_approve=True,
                         fixtures=ROOT / "fixtures" / "adversarial_findings.json", **paths)
    by = {f["finding_id"]: f for f in store.all()}
    filed = sorted(fid for fid, f in by.items() if f["state"] == "FILED")
    assert filed == ["A-0004"]
    assert by["A-0001"]["outcome"] == by["A-0002"]["outcome"] == "correct"
    assert {by[x]["state"] for x in ("A-0003", "A-0005", "A-0006")} == {"REJECTED"}
    assert [d["order_ids"] for d in portal_lines(paths["portal"])] == [["ORD-2004"]]


def test_small_recoverable_gap_is_judged_but_never_drafted(paths, tmp_path):
    small = {**json.loads((ROOT / "fixtures" / "fake_findings.json").read_text())[0],
             "finding_id": "S-1", "expected": 4.0, "actual": 4.9, "gap": 0.9}
    fx = tmp_path / "small.json"
    fx.write_text(json.dumps([small]))
    store = pipeline.run(reset=True, forget=True, demo_approve=True, fixtures=fx, **paths)
    f = store.get("S-1")
    assert f["outcome"] == "recoverable" and f["rank_score"] < 0
    assert f["state"] == "JUDGED" and not f.get("dispute_draft")
    assert portal_lines(paths["portal"]) == []
