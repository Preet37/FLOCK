"""Air-gap mode (FLOCK_AIRGAP=1): only providers on this network get called, and every model call is counted by host.
urlopen is mocked; nothing here hits a network."""
import io
import json
import socket
import threading
import urllib.request
from urllib.parse import urlparse

import pytest

from conftest import fake
from dashboard.app import create_app
from flock import llm, pipeline
from flock.agents import panel
from flock.store import JsonStore

VOTE = '{"outcome": "recoverable", "confidence": 0.9, "reason": "Contract says 20%."}'
TUNNEL = "http://127.0.0.1:11435/v1"


class FakeHTTP:
    def __init__(self, reply=VOTE):
        self.reply, self.urls, self._lock = reply, [], threading.Lock()

    def __call__(self, req, timeout=None):
        with self._lock:
            self.urls.append(req.full_url)
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": self.reply}}]}).encode())


@pytest.fixture
def http(monkeypatch):
    monkeypatch.delenv("FLOCK_OFFLINE", raising=False)
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "0")
    monkeypatch.setattr(llm, "_client", lambda: pytest.fail("no Anthropic call expected"))
    fake_http = FakeHTTP()
    monkeypatch.setattr(urllib.request, "urlopen", fake_http)
    return fake_http


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost", "10.1.2.3", "172.16.0.9", "192.168.1.20",
                                  "169.254.1.1", "100.64.0.1", "100.101.102.103", "100.127.255.254",
                                  "gx10.tail1234.ts.net"])
def test_on_this_network(host):
    assert llm.on_this_network(host)


@pytest.mark.parametrize("host", ["api.anthropic.com", "api.groq.com", "integrate.api.nvidia.com", "8.8.8.8",
                                  "100.128.0.1", "100.63.255.255", "", "evil.localhost.example.com"])
def test_off_this_network(host, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("dotted names are never looked up"))
    assert not llm.on_this_network(host)


def test_one_word_name_is_resolved_and_must_be_private(monkeypatch):
    answers = {"gx10": "100.88.1.2", "router": "192.168.0.1", "sneaky": "52.1.2.3"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, *a, **k: [(0, 0, 0, "", (answers[h], 0))]
                        if h in answers else (_ for _ in ()).throw(socket.gaierror("no such host")))
    assert llm.on_this_network("gx10") and llm.on_this_network("router")
    assert not llm.on_this_network("sneaky") and not llm.on_this_network("nowhere")


def test_airgap_off_changes_nothing(http, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    r = ("groq", "llama-3.3-70b-versatile")
    assert llm.airgap_block(r) is None and llm.available(r)
    llm.call_json("s", "u", r=r)
    rep = llm.airgap_report()
    assert rep["airgap"] is False and rep["calls"] == 1 and rep["external_hosts"] == ["api.groq.com"]
    assert rep["line"].startswith("not air-gapped") and "api.groq.com: 1" in rep["line"]


def test_airgap_refuses_cloud_providers_with_a_reason(http, monkeypatch):
    monkeypatch.setenv("FLOCK_AIRGAP", "1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    for provider, host in (("groq", "api.groq.com"), ("nvidia", "integrate.api.nvidia.com"),
                           ("anthropic", "api.anthropic.com")):
        r = (provider, llm.default_model(provider))
        assert not llm.available(r)
        assert llm.offline_reason(r) == f"FLOCK_AIRGAP=1: {provider} would call {host}, which is off this network"
        assert llm.call_json("s", "u", r=r) == (None, None)  # offline on purpose: offline rules, no error
        with pytest.raises(PermissionError, match=host):
            llm.complete("s", "u", 10, r)                    # even a direct call is stopped before the network
    assert http.urls == []
    rep = llm.airgap_report()
    assert rep["calls"] == 0 and rep["external_calls"] == 0
    assert set(rep["refused"]) == {"groq/llama-3.3-70b-versatile", "nvidia/meta/llama-3.3-70b-instruct",
                                   "anthropic/claude-sonnet-5-5"}
    assert rep["line"].startswith("air-gapped: 0 model calls, 0 left this network; kept offline: ")


def test_airgap_allows_a_cloud_provider_pointed_at_a_local_server(http, monkeypatch):
    monkeypatch.setenv("FLOCK_AIRGAP", "1")
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    monkeypatch.setenv("FLOCK_NVIDIA_URL", "http://100.90.1.2:8000/v1")  # a NIM container on the GX10 over Tailscale
    r = ("nvidia", "meta/llama-3.3-70b-instruct")
    assert llm.available(r) and llm.offline_reason(r) is None
    assert llm.call_json("s", "u", r=r)[0]["outcome"] == "recoverable"
    assert llm.airgap_report()["by_host"] == {"100.90.1.2": 1}


def test_airgap_panel_mixed_routes_only_local_calls_leave_the_process(http, monkeypatch):
    monkeypatch.setenv("FLOCK_AIRGAP", "1")
    monkeypatch.setenv("FLOCK_LOCAL_URL", TUNNEL)
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "local,groq,anthropic")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    d = panel.decide(fake("F-0001"), rules=[])
    votes = {v["judge"]: v for v in d["votes"]}
    assert votes["judge.strict"]["model"] == "local/llama3.1:8b"
    assert "model" not in votes["judge.operator"] and "fallback" not in votes["judge.operator"]  # offline rules
    assert http.urls and {urlparse(u).netloc for u in http.urls} == {"127.0.0.1:11435"}
    rep = llm.airgap_report()
    assert rep["calls"] == len(http.urls) and rep["by_host"] == {"127.0.0.1": len(http.urls)}
    assert rep["line"].startswith(f"air-gapped: {len(http.urls)} model calls, 0 left this network")


def run_pipeline(tmp_path):
    store = JsonStore(tmp_path / "state")
    pipeline.run(reset=True, forget=True, demo_approve=True, store=store, rules_path=tmp_path / "rules.json",
                 portal=tmp_path / "portal.jsonl", runs_path=tmp_path / "runs.jsonl")
    return store


def test_pipeline_reports_airgap_and_writes_it_for_the_dashboard(http, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FLOCK_AIRGAP", "1")
    monkeypatch.setenv("FLOCK_LOCAL_URL", TUNNEL)
    monkeypatch.setenv("FLOCK_PROVIDER", "local")       # judges, writer, mapper local; reviewer stays anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    store = run_pipeline(tmp_path)
    n = len(http.urls)
    assert n >= 18  # 6 findings x 3 judges, plus any debate rounds and dispute drafts
    out = capsys.readouterr().out
    assert f"air-gapped: {n} model calls, 0 left this network\n" in out  # unanimous: the reviewer was never asked
    saved = json.loads((store.dir / pipeline.AIRGAP_NAME).read_text())
    assert saved["airgap"] and saved["calls"] == n and saved["external_calls"] == 0

    api = create_app(store, runs_path=tmp_path / "runs.jsonl").test_client()
    gap = api.get("/api/airgap").get_json()
    assert gap["enforced"] and gap["line"] == saved["line"] and gap["last_run"]["by_host"] == {"127.0.0.1": n}
    roles = {x["role"]: x for x in gap["routes"]}
    assert roles["judge.strict"]["on_this_network"] and roles["judge.strict"]["offline_reason"] is None
    assert not roles["reviewer"]["on_this_network"] and "api.anthropic.com" in roles["reviewer"]["offline_reason"]
    assert api.get("/api/meta").get_json()["airgap"] == {"enforced": True, "line": saved["line"]}


def test_offline_quickstart_output_unchanged_but_still_recorded(tmp_path, capsys):
    store = run_pipeline(tmp_path)  # conftest: FLOCK_OFFLINE=1, no FLOCK_AIRGAP
    assert "model calls" not in capsys.readouterr().out
    saved = json.loads((store.dir / pipeline.AIRGAP_NAME).read_text())
    assert saved["calls"] == 0 and not saved["airgap"]


def test_pipeline_counts_resets_each_run(http, monkeypatch, tmp_path):
    monkeypatch.setenv("FLOCK_PROVIDER", "local")
    run_pipeline(tmp_path)
    first = llm.airgap_report()["calls"]
    run_pipeline(tmp_path)
    assert first > 0 and llm.airgap_report()["calls"] == first
