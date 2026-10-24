"""scripts/judge_load.py: timing, the JSON schema benchmark.py reads, fallbacks. urlopen mocked; no network."""
import importlib.util
import io
import json
import urllib.error
import urllib.request

import pytest

from conftest import ROOT
from flock.agents import panel

spec = importlib.util.spec_from_file_location("judge_load", ROOT / "scripts" / "judge_load.py")
jl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jl)

CORE = {"contestant", "models", "n", "p50", "p95", "per_min", "cost_per_1k", "offline", "errors"}
VOTE = '{"outcome": "recoverable", "confidence": 0.9, "reason": "Contract says 20%."}'


@pytest.fixture(autouse=True)
def restore_env(monkeypatch):
    """main() sets FLOCK_* variables in os.environ (it is a CLI); put them all back after each test."""
    import os
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def load(out_dir):
    [path] = list(out_dir.glob("judge_load_*.json"))
    return path, json.loads(path.read_text())


def test_percentile_matches_numpy_linear():
    assert jl.percentile([], 50) is None
    assert jl.percentile([3.0], 95) == 3.0
    assert jl.percentile([1, 2, 3, 4], 50) == 2.5
    assert jl.percentile(list(range(1, 11)), 95) == 9.55


def test_fixture_findings_cycle_with_unique_ids():
    fs = jl.fixture_findings("mixed", 40)
    ids = [f["finding_id"] for f in fs]
    assert len(ids) == 40 and len(set(ids)) == 40
    assert ids[0] == "F-0001" and any(i.endswith("-r2") for i in ids)


def test_rules_only_run_writes_core_keys(tmp_path, capsys):
    assert jl.main(["--n", "8", "--out", str(tmp_path)]) == 0
    path, out = load(tmp_path)
    assert path.name.startswith("judge_load_offline_")
    assert CORE <= set(out) and out["schema"] == jl.SCHEMA
    assert (out["contestant"], out["n"], out["offline"], out["cost_per_1k"], out["errors"]) == ("rules-only", 8, True, 0.0, 0)
    assert out["models"] == ["offline-rules"] and len(out["findings"]) == 8
    assert all(j["calls"] == 8 for j in out["judges"])


def test_local_run_times_each_judge_and_stays_on_network(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "0")
    monkeypatch.setenv("FLOCK_AIRGAP", "1")
    seen = []

    def fake(req, timeout=None):
        body = json.loads(req.data)
        seen.append((req.full_url, body["model"], timeout))
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": VOTE}}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setenv("FLOCK_LOCAL_URL", "http://127.0.0.1:11435/v1")
    rc = jl.main(["--provider", "local", "--models", "qwen3:8b", "--n", "6", "--concurrency", "3", "--out", str(tmp_path)])
    path, out = load(tmp_path)
    assert rc == 0 and path.name.startswith("judge_load_local_")
    assert out["contestant"] == "local/qwen3:8b" and out["offline"] and out["cost_per_1k"] == 0.0
    assert len(seen) == 18 and {s[1] for s in seen} == {"qwen3:8b"} and {s[2] for s in seen} == {120.0}
    assert all(j["calls"] == 6 and j["p50"] is not None for j in out["judges"])
    assert out["model_calls"]["by_host"] == {"127.0.0.1": 18} and out["model_calls"]["external_calls"] == 0
    assert out["concurrency"] == 3 and out["p95"] >= out["p50"] >= 0


def test_failures_are_counted_and_exit_nonzero(tmp_path, monkeypatch):
    def down(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", down)
    rc = jl.main(["--provider", "local", "--n", "2", "--timeout", "5", "--out", str(tmp_path)])
    _, out = load(tmp_path)
    assert rc == 1 and out["errors"] == 6 and out["fallbacks"] == 6
    assert "connection refused" in out["error_samples"][0] and out["timeout_seconds"] == 5.0


def test_hosted_contestant_is_not_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(
        json.dumps({"choices": [{"message": {"content": VOTE}}]}).encode()))
    jl.main(["--provider", "groq", "--n", "1", "--cost-per-1k", "1.25", "--out", str(tmp_path)])
    _, out = load(tmp_path)
    assert not out["offline"] and out["cost_per_1k"] == 1.25 and out["model_calls"]["external_hosts"] == ["api.groq.com"]


def test_timer_restores_panel(tmp_path):
    vote, revise = panel.vote, panel.revise
    jl.main(["--n", "1", "--out", str(tmp_path)])
    assert (panel.vote, panel.revise) == (vote, revise)
