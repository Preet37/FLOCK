"""Judging many findings at once (FLOCK_JUDGE_CONCURRENCY) and the per-run live-model budget (FLOCK_LIVE_JUDGE_BUDGET).
Concurrency must never change a result; the budget gives live judges to the riskiest findings and says so on the rest."""
import json
import re
import threading

import pytest

from conftest import ADVERSARIAL, FAKE
from flock import frontend, llm, pipeline
from flock.agents import panel
from flock.models import Finding
from flock.store import JsonStore


def findings():
    rows = [Finding(**f).to_dict() for f in FAKE + ADVERSARIAL]
    for i, f in enumerate(rows):
        f["finding_id"] = f"F-{i:04d}"
    return rows


def plan_for(todo, concurrency, monkeypatch, budget=None):
    monkeypatch.setenv("FLOCK_JUDGE_CONCURRENCY", str(concurrency))
    if budget is None:
        monkeypatch.delenv("FLOCK_LIVE_JUDGE_BUDGET", raising=False)
    else:
        monkeypatch.setenv("FLOCK_LIVE_JUDGE_BUDGET", str(budget))
    return pipeline.judge_plan(todo, [])


def test_concurrency_defaults(monkeypatch):
    monkeypatch.delenv("FLOCK_JUDGE_CONCURRENCY", raising=False)
    assert pipeline.judge_concurrency(live=False) == 1
    assert pipeline.judge_concurrency(live=True) == pipeline.DEFAULT_LIVE_CONCURRENCY == 6
    monkeypatch.setenv("FLOCK_JUDGE_CONCURRENCY", "3")
    assert pipeline.judge_concurrency(live=False) == 3
    monkeypatch.setenv("FLOCK_JUDGE_CONCURRENCY", "0")
    assert pipeline.judge_concurrency(live=True) == 1
    monkeypatch.setenv("FLOCK_JUDGE_CONCURRENCY", "lots")
    assert pipeline.judge_concurrency(live=False) == 1


def test_concurrent_judging_gives_identical_decisions_offline(monkeypatch):
    todo = findings()
    weights = {"judge.strict": 0.4, "judge.operator": 0.25, "judge.skeptic": 0.35}
    seq = list(pipeline.judge_all(todo, [], weights, plan_for(todo, 1, monkeypatch)))
    par = list(pipeline.judge_all(todo, [], weights, plan_for(todo, 6, monkeypatch)))
    assert [f["finding_id"] for f, _ in par] == [f["finding_id"] for f in todo]   # caller sees todo order
    assert json.dumps([d for _, d in par], sort_keys=True) == json.dumps([d for _, d in seq], sort_keys=True)


def test_panels_really_run_at_once(monkeypatch):
    todo = findings()[:4]
    barrier = threading.Barrier(4, timeout=5)   # deadlocks (BrokenBarrierError) unless 4 panels run together
    seen = []

    def decide(f, rules=None, weights=None, live=True):
        barrier.wait()
        seen.append(threading.current_thread().name)
        return {"outcome": "correct", "confidence": 1.0, "votes": [], "reason": f["finding_id"]}

    monkeypatch.setattr(panel, "decide", decide)
    out = list(pipeline.judge_all(todo, [], {}, plan_for(todo, 4, monkeypatch)))
    assert [d["reason"] for _, d in out] == [f["finding_id"] for f in todo] and len(set(seen)) == 4


def run_once(tmp_path, name, monkeypatch, concurrency):
    monkeypatch.setenv("FLOCK_JUDGE_CONCURRENCY", str(concurrency))
    store = pipeline.run(reset=True, forget=True, demo_approve=True, store=JsonStore(tmp_path / name / "state"),
                         rules_path=tmp_path / name / "rules.json", portal=tmp_path / name / "portal.jsonl")
    keep = ("finding_id", "outcome", "state", "confidence", "votes", "reason", "attention", "rank_score")
    return [{k: f.get(k) for k in keep} for f in sorted(store.all(), key=lambda f: f["finding_id"])], store


def test_pipeline_run_is_identical_with_concurrency(tmp_path, monkeypatch):
    seq, s1 = run_once(tmp_path, "seq", monkeypatch, 1)
    par, s2 = run_once(tmp_path, "par", monkeypatch, 4)
    assert seq == par
    sbx = lambda s: re.sub(r"SBX-[0-9A-F]+", "SBX-?", s or "")   # the portal's confirmation ids are random
    strip = lambda log: [{**{k: e[k] for k in ("actor", "action", "subject_id", "allowed")}, "reason": sbx(e["reason"])}
                         for e in log]
    assert strip(s1.log()) == strip(s2.log())   # transitions in the same order
    assert json.loads((tmp_path / "par" / "state" / "last_run.json").read_text())["judging"]["concurrency"] == 4


# live-model budget ----------------------------------------------------------------------------------------------------
@pytest.fixture
def live_models(monkeypatch):
    """Pretend every route is live: judges answer 'recoverable' 95% from a fake model; count the calls."""
    calls = []
    lock = threading.Lock()

    def call_json(system, user, max_tokens=600, r=None):
        with lock:
            calls.append(json.loads(user).get("check_type") or json.loads(user)["finding"]["check_type"])
        return {"outcome": "recoverable", "confidence": 0.95, "reason": "fake live model"}, None

    monkeypatch.setattr(llm, "available", lambda r=None: True)
    monkeypatch.setattr(llm, "call_json", call_json)
    monkeypatch.setattr(llm, "ask_text", lambda *a, **k: None)
    return calls


def test_budget_picks_the_riskiest(monkeypatch, live_models):
    todo = findings()
    plan = plan_for(todo, 2, monkeypatch, budget=3)
    assert plan["live"] and plan["used"] == 3 and plan["outside"] == len(todo) - 3
    want = sorted(todo, key=lambda f: (-frontend.risk(f)["risk"], -abs(f["gap"]), f["finding_id"]))[:3]
    assert plan["chosen"] == {f["finding_id"] for f in want}


def test_budget_offline_is_not_applied(monkeypatch):
    plan = plan_for(findings(), 1, monkeypatch, budget=2)
    assert not plan["live"] and plan["budget"] is None and plan["outside"] == 0


def test_budget_run_marks_the_rest_honestly(tmp_path, monkeypatch, live_models, capsys):
    monkeypatch.setenv("FLOCK_LIVE_JUDGE_BUDGET", "2")
    monkeypatch.setenv("FLOCK_JUDGE_CONCURRENCY", "3")
    store = pipeline.run(reset=True, forget=True, demo_approve=False, store=JsonStore(tmp_path / "state"),
                         rules_path=tmp_path / "rules.json", portal=tmp_path / "portal.jsonl")
    rows = store.all()
    live = [f for f in rows if f.get("live_budget") != "outside"]
    outside = [f for f in rows if f.get("live_budget") == "outside"]
    assert len(live) == 2 and len(outside) == 4
    for f in outside:
        assert panel.BUDGET_NOTE in f["reason"]
        assert all(v["budget"] == "offline" and panel.BUDGET_NOTE in v["reason"] and "model" not in v for v in f["votes"])
    for f in live:
        assert all(v.get("model") and "budget" not in v for v in f["votes"] if v["judge"].startswith("judge."))
    assert len([c for c in live_models]) >= 6    # 3 judges x 2 live findings (+ any reviewer call); none for the rest
    run = json.loads((tmp_path / "state" / "last_run.json").read_text())
    assert run["judging"] == {"live": True, "concurrency": 3, "budget": 2, "used": 2, "outside": 4, "judged": 6,
                              "by_rule": 0}
    card = frontend.cost_card(store, run)
    assert card["value"]["live_judge_budget"]["used"] == 2
    assert any("FLOCK_LIVE_JUDGE_BUDGET=2" in n for n in card["notes"])
    assert "live models on 2 of 6 findings (FLOCK_LIVE_JUDGE_BUDGET=2; 4 on offline rules)" in capsys.readouterr().out
