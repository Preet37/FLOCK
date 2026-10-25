"""Benchmark harness: same findings for every contestant, scored on held-out seeds, results written for the dashboard."""
import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load():
    spec = importlib.util.spec_from_file_location("flock_benchmark", ROOT / "scripts" / "benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    bm = _load()
    base = tmp_path_factory.mktemp("bench")
    bm.SEEDS_DIR = base / "seeds"
    bm.RESULTS_JSON = base / "results.json"
    bm.RESULTS_CSV = base / "results.csv"
    bm.OUT_DIR = base
    return bm


def test_parse_seeds(bench):
    assert bench.parse_seeds("101-103,200") == [101, 102, 103, 200]


def test_prepare_is_cached_and_never_touches_the_repo(bench):
    before = (ROOT / "mappings.json").read_bytes(), (ROOT / "data" / "answer_key.csv").read_bytes()
    p1 = bench.prepare(7, "default", 2, 7, quiet=True)
    p2 = bench.prepare(7, "default", 2, 7, quiet=True)
    assert p1["findings"] and p1["key"].exists() and p1["orders"].exists()
    assert [f["finding_id"] for f in p1["findings"]] == [f["finding_id"] for f in p2["findings"]], "cached, same findings"
    assert all(f["state"] == "NEW" for f in p1["findings"]), "prepare stops before the panel: nothing is judged yet"
    assert ((ROOT / "mappings.json").read_bytes(), (ROOT / "data" / "answer_key.csv").read_bytes()) == before


def test_rules_contestant_scores_and_times_every_finding(bench):
    run = bench.run_benchmark([7], ["rules"], profile="default", stores=2, weeks=1, write=True, quiet=True)
    row = run["per_seed"]["rules"][0]
    assert row["status"] == "ok" and row["total"] > 0
    assert row["caught"] == row["total"] and row["false_alarms"] == 0, "the rules catch every planted trap offline"
    assert row["p50_s"] is not None and row["per_minute"] > 0 and row["cost_per_1000"] == 0
    assert row["offline"] is True
    agg = run["aggregate"]["rules"]
    assert agg["accuracy"] == 1.0 and agg["caught"] == row["caught"]
    data = json.loads(bench.RESULTS_JSON.read_text())
    assert data["current"]["rules"]["caught"] == row["caught"]
    rows = list(csv.DictReader(bench.RESULTS_CSV.open()))
    assert {r["seed"] for r in rows} == {"7", "all"}


def test_live_contestant_without_a_key_is_skipped_not_faked(bench, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run = bench.run_benchmark([7], ["claude"], profile="default", stores=2, weeks=1, write=False, quiet=True)
    row = run["per_seed"]["claude"][0]
    assert row["status"] == "skipped" and "no key" in row["note"]
    assert run["aggregate"]["claude"]["status"] == "skipped"


def test_tuned_contestant_needs_a_model_name(bench, monkeypatch):
    monkeypatch.delenv("FLOCK_TUNED_MODEL", raising=False)
    run = bench.run_benchmark([7], ["local-tuned"], profile="default", stores=2, weeks=1, write=False, quiet=True)
    assert run["per_seed"]["local-tuned"][0]["status"] == "skipped"


def test_contestant_env_is_scoped(bench, monkeypatch):
    monkeypatch.setenv("FLOCK_OFFLINE", "1")
    with bench.contestant_env({"FLOCK_JUDGE_PROVIDERS": "anthropic"}):
        import os
        assert "FLOCK_OFFLINE" not in os.environ and os.environ["FLOCK_JUDGE_PROVIDERS"] == "anthropic"
    import os
    assert os.environ["FLOCK_OFFLINE"] == "1" and "FLOCK_JUDGE_PROVIDERS" not in os.environ


def test_human_sheet_is_scored_on_its_store_week(bench, tmp_path):
    p = bench.prepare(7, "default", 2, 7, quiet=True)
    with p["key"].open(newline="") as fh:
        key = list(csv.DictReader(fh))
    with p["orders"].open(newline="") as fh:
        store_of = {r["order_id"]: r["store_id"] for r in csv.DictReader(fh)}
    trap = next(r for r in key if r["expected_outcome"] == "recoverable" and r["order_id"] in store_of)
    store = store_of[trap["order_id"]]
    innocent = next(o for o, s in store_of.items() if s == store and o not in {r["order_id"] for r in key})
    sheet = tmp_path / "sheet.csv"
    sheet.write_text("order_id,outcome,amount\n"
                     f"{trap['order_id']},recoverable,{trap['amount']}\n"
                     f"{innocent},recoverable,1.00\n")
    row = bench.judge_human(sheet, p, f"7:{store}", minutes=40)
    assert row["status"] == "ok"
    assert any(t["order_id"] == trap["order_id"] and t["caught"] for t in row["traps"])
    assert row["false_alarms"] == 1, "flagging a fair order as recoverable is a false alarm"
    assert row["human_minutes_per_store_week"] == 40 and row["per_minute"] == round(2 / 40, 2)


# ---- a live contestant against a fake OpenAI-compatible server: metering, model labels, fallbacks -----------------
import json as _json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _FakeJudge(BaseHTTPRequestHandler):
    reply = {"outcome": "recoverable", "confidence": 0.9, "reason": "fake model says so"}

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = _json.dumps({"model": "fake-judge-7b",
                            "choices": [{"message": {"content": _json.dumps(self.reply)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture
def fake_local(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeJudge)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("FLOCK_LOCAL_URL", f"http://127.0.0.1:{srv.server_port}/v1")
    monkeypatch.setenv("FLOCK_LOCAL_MODEL", "fake-judge-7b")
    yield srv
    srv.shutdown()


def test_local_contestant_is_metered_and_labelled(bench, fake_local):
    p = bench.prepare(7, "default", 2, 7, quiet=True)
    row = bench.judge("local", p, bench.resolve_env("local", None), bench.PRICES, workers=2, quiet=True)
    assert row["status"] == "ok"
    assert row["calls"] >= 3 * len(p["findings"]), "three judges voted on every finding through the model"
    assert set(row["models"]) == {"fake-judge-7b"} and row["fallbacks"] == 0
    assert row["cost_per_1000"] == 0, "a local model costs nothing per call"
    assert row["p50_s"] > 0 and row["per_minute"] > 0


def test_unreachable_local_model_is_reported_not_faked(bench, monkeypatch):
    monkeypatch.setenv("FLOCK_LOCAL_URL", "http://127.0.0.1:9/v1")  # nothing listens on port 9
    monkeypatch.setenv("FLOCK_LLM_TIMEOUT", "1")
    p = bench.prepare(7, "default", 2, 7, quiet=True)
    row = bench.judge("local", p, bench.resolve_env("local", None), bench.PRICES, workers=4, quiet=True)
    assert row["status"] == "unreachable"
    assert row["fallbacks"] == row["votes"] > 0


def test_cost_estimate_uses_list_prices(bench):
    m = bench.Meter()
    m.calls, m.in_chars, m.out_chars, m.models = 10, 4_000_000, 400_000, {"claude-sonnet-5-5": 10}
    # 1M input tokens at $2 + 100k output tokens at $10 = $3.00
    assert m.cost_usd(bench.PRICES) == pytest.approx(3.0)


def test_adhoc_local_panels():
    bm = _load()
    one = bm.contestant("local:qwen3:8b")
    assert one["env"]["FLOCK_JUDGE_MODELS"] == "qwen3:8b" and one["env"]["FLOCK_JUDGE_PROVIDERS"] == "local"
    three = bm.resolve_env("local:gpt-oss:20b+qwen3:30b+gemma3:27b", None)
    assert three["FLOCK_JUDGE_MODELS"] == "gpt-oss:20b,qwen3:30b,gemma3:27b"
    assert three["FLOCK_REVIEWER_PROVIDER"] == "offline"
    with pytest.raises(KeyError):
        bm.contestant("nope")
