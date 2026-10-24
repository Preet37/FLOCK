"""CREAO provider: Flock's AI roles as hosted CREAO agents, with urlopen mocked. Nothing here hits a network."""
import importlib.util
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conftest import fake  # noqa: E402
from flock import llm  # noqa: E402
from flock.agents import deal_keeper, panel  # noqa: E402

VOTE = {"outcome": "recoverable", "confidence": 0.9, "reason": "Contract says 20%."}


def ndjson(*events) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode()


def stream_reply(text: str, credits: float = 0.3, model: str = "anthropic/claude-sonnet-5-5") -> bytes:
    half = len(text) // 2
    return ndjson({"type": "run.created", "run_id": "r1", "agent_id": "a1", "model": model, "render_protocol": 1},
                  {"type": "output_text.delta", "delta": text[:half]},
                  {"type": "output_text.delta", "delta": text[half:]},
                  {"type": "run.completed", "run_id": "r1", "status": "completed", "result": {"text": text},
                   "usage": {"credits": credits}})


class FakeCreao:
    """Stands in for urllib.request.urlopen against developer.creao.ai. routes: (method, path) -> bytes | Exception | callable."""

    def __init__(self, routes):
        self.routes, self.calls, self._lock = routes, [], threading.Lock()

    def __call__(self, req, timeout=None):
        path = req.full_url.split("developer.test", 1)[1] if "developer.test" in req.full_url else req.full_url
        call = {"method": req.get_method(), "path": path, "headers": {k.lower(): v for k, v in req.header_items()},
                "body": json.loads(req.data) if req.data else None}
        with self._lock:
            self.calls.append(call)
        key = (call["method"], path.split("?")[0])
        r = self.routes.get(key) or self.routes.get((call["method"], "*"))
        if r is None:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b'{"error":"RUN_NOT_FOUND"}'))
        r = r(call) if callable(r) else r
        if isinstance(r, BaseException):
            raise r
        return io.BytesIO(r)


@pytest.fixture
def creao(monkeypatch, tmp_path):
    monkeypatch.delenv("FLOCK_OFFLINE", raising=False)
    monkeypatch.setenv("CREAO_API_KEY", "cr_sk_test")
    monkeypatch.setenv("FLOCK_CREAO_URL", "https://developer.test")
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "0")
    agents = tmp_path / "creao_agents.json"
    agents.write_text(json.dumps({"agents": {"judge.strict": {"agent_id": "a-strict"}, "judge.operator": {"agent_id": "a-op"},
                                             "judge.skeptic": {"agent_id": "a-skep"}, "reviewer": "a-rev",
                                             "writer": "a-writer", "mapper": "a-map", "reader": "a-reader"}}))
    monkeypatch.setenv("FLOCK_CREAO_AGENTS", str(agents))
    monkeypatch.setattr(llm, "_client", lambda: pytest.fail("no Anthropic call expected"))
    monkeypatch.setattr(llm, "CREAO_POLL_SECONDS", 0.0)
    llm.reset_calls()

    def install(routes):
        f = FakeCreao(routes)
        monkeypatch.setattr(urllib.request, "urlopen", f)
        return f
    return install


# --- routing and availability ---------------------------------------------------------------------------------------

def test_roles_resolve_to_their_agents(creao, monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "creao")
    assert [llm.route("judge", i) for i in range(3)] == [("creao", "a-strict"), ("creao", "a-op"), ("creao", "a-skep")]
    monkeypatch.setenv("FLOCK_READER_PROVIDER", "creao")
    assert llm.route("reader") == ("creao", "a-reader")
    monkeypatch.setenv("FLOCK_WRITER_PROVIDER", "creao")
    monkeypatch.setenv("FLOCK_WRITER_MODEL", "a-custom")
    assert llm.route("writer") == ("creao", "a-custom"), "an explicit model id wins"


def test_without_a_key_or_agent_it_is_offline_with_a_reason(creao, monkeypatch):
    assert llm.available(("creao", "a-strict"))
    monkeypatch.delenv("CREAO_API_KEY")
    assert not llm.available(("creao", "a-strict")) and llm.offline_reason(("creao", "a-strict")) == "no CREAO_API_KEY"
    monkeypatch.setenv("CREAO_API_KEY", "cr_sk_test")
    monkeypatch.setenv("FLOCK_CREAO_AGENTS", "/nonexistent.json")
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "creao")
    assert llm.route("judge", 0) == ("creao", "")
    assert not llm.available(llm.route("judge", 0)) and "no CREAO agent" in llm.offline_reason(llm.route("judge", 0))


def test_airgap_keeps_creao_offline(creao, monkeypatch):
    monkeypatch.setenv("FLOCK_AIRGAP", "1")
    assert not llm.available(("creao", "a-strict"))
    assert "developer.test" in llm.offline_reason(("creao", "a-strict"))


# --- the call -------------------------------------------------------------------------------------------------------

def test_realtime_run_sends_the_contract_and_returns_the_streamed_text(creao):
    f = creao({("POST", "/v1/realtime/runs"): stream_reply(json.dumps(VOTE), credits=0.25)})
    text, served = llm.complete("SYSTEM PROMPT", '{"gap": 2.5}', 300, ("creao", "a-strict"))
    assert json.loads(text) == VOTE and served == "creao:anthropic/claude-sonnet-5-5"
    call = f.calls[0]
    assert call["headers"]["authorization"] == "Bearer cr_sk_test"
    assert call["body"]["agent_id"] == "a-strict"
    assert call["body"]["input"]["instructions"] == "SYSTEM PROMPT" and call["body"]["input"]["request"] == '{"gap": 2.5}'
    assert "model" not in call["body"], "no FLOCK_CREAO_LLM: the agent's default model runs"
    assert llm.creao_credits() == {"credits": 0.25, "by_agent": {"a-strict": 0.25}}
    assert llm.airgap_report()["by_host"] == {"developer.test": 1}


def test_model_choice_is_passed_through(creao, monkeypatch):
    monkeypatch.setenv("FLOCK_CREAO_LLM", "anthropic/claude-haiku-5-5")
    f = creao({("POST", "/v1/realtime/runs"): stream_reply("{}")})
    llm.complete("s", "u", 10, ("creao", "a-op"))
    assert f.calls[0]["body"]["model"] == "anthropic/claude-haiku-5-5"


def test_falls_back_to_async_polling_when_realtime_is_not_enabled(creao):
    polls = {"n": 0}

    def status(call):
        polls["n"] += 1
        if polls["n"] < 3:
            return json.dumps({"run_id": "r9", "status": "running"}).encode()
        return json.dumps({"run_id": "r9", "status": "completed", "model": "anthropic/claude-sonnet-5-5",
                           "result": {"text": json.dumps(VOTE)}, "usage": {"credits": 1.5}}).encode()

    f = creao({("POST", "/v1/realtime/runs"): urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(b'{"error":"ACCOUNT_BLOCKED"}')),
               ("POST", "/v1/runs"): json.dumps({"run_id": "r9", "status": "pending"}).encode(),
               ("GET", "/v1/runs/r9"): status})
    text, served = llm.complete("s", "u", 10, ("creao", "a-skep"))
    assert json.loads(text) == VOTE and served == "creao:anthropic/claude-sonnet-5-5"
    assert [c["path"] for c in f.calls] == ["/v1/realtime/runs", "/v1/runs", "/v1/runs/r9", "/v1/runs/r9", "/v1/runs/r9"]
    assert llm.creao_credits()["credits"] == 1.5


def test_failed_run_raises_with_the_stable_code(creao):
    creao({("POST", "/v1/realtime/runs"): ndjson({"type": "run.created", "run_id": "r1"},
                                                 {"type": "run.failed", "run_id": "r1", "status": "failed",
                                                  "error": {"code": "INSUFFICIENT_CREDITS"}})})
    with pytest.raises(RuntimeError, match="INSUFFICIENT_CREDITS"):
        llm.complete("s", "u", 10, ("creao", "a-strict"))
    creao({("POST", "/v1/realtime/runs"): urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b'{"error":"RATE_LIMITED"}'))})
    with pytest.raises(llm.CreaoHTTPError, match="RATE_LIMITED"):
        llm.complete("s", "u", 10, ("creao", "a-strict"))


# --- the roles, end to end ------------------------------------------------------------------------------------------

def test_the_panel_judges_on_creao(creao, monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "creao")
    monkeypatch.setenv("FLOCK_REVIEWER_PROVIDER", "offline")
    f = creao({("POST", "/v1/realtime/runs"): lambda call: stream_reply(json.dumps(VOTE), credits=0.1)})
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "recoverable"
    assert [v["model"] for v in d["votes"]] == ["creao/a-strict", "creao/a-op", "creao/a-skep"]
    assert {c["body"]["agent_id"] for c in f.calls} == {"a-strict", "a-op", "a-skep"}
    assert all(panel.SYSTEM in c["body"]["input"]["instructions"] for c in f.calls), "Flock's own prompt is the instructions"
    assert llm.creao_credits()["credits"] == pytest.approx(0.3)


def test_a_judge_that_fails_falls_back_to_the_rules_and_says_so(creao, monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "creao")
    monkeypatch.setenv("FLOCK_REVIEWER_PROVIDER", "offline")
    creao({("POST", "/v1/realtime/runs"): urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b'{"error":"SERVICE_UNAVAILABLE"}'))})
    d = panel.decide(fake("F-0001"), rules=[])
    assert all(v.get("fallback") for v in d["votes"]) and "offline rules" in d["reason"]


def test_the_document_reader_turns_free_text_into_claims(creao, monkeypatch, tmp_path):
    monkeypatch.setenv("FLOCK_READER_PROVIDER", "creao")
    reply = {"claims": [{"channel": "grubhub", "term_type": "fee", "store_id": "all", "value": 0,
                         "effective_from": "2026-08-01", "effective_to": "2026-08-31",
                         "quote": "We will waive the Delivery Network Fee for August.", "authority": "auth_email"},
                        {"channel": "grubhub", "term_type": "nonsense", "value": 1},
                        {"channel": "grubhub", "term_type": "commission", "value": 0.2, "quote": "x", "authority": "unknown"}]}
    f = creao({("POST", "/v1/realtime/runs"): stream_reply(json.dumps(reply))})
    doc = tmp_path / "grubhub_waiver.txt"
    doc.write_text("Hi, we will waive the Delivery Network Fee for August. Dana, Director of Restaurant Partnerships")
    got = deal_keeper.read_with_local_model(doc, doc.read_text())
    assert len(got) == 2, "a term type Flock does not know is dropped"
    assert got[0]["term_type"] == "fee" and got[0]["authority"] == "auth_email"
    assert got[1]["authority"] is None, "'unknown' authority becomes None, which the deal keeper treats as a conflict"
    assert f.calls[0]["body"]["agent_id"] == "a-reader"
    assert json.loads(f.calls[0]["body"]["input"]["request"])["file"] == "grubhub_waiver.txt"


def test_reader_is_silent_offline(creao, monkeypatch, tmp_path):
    monkeypatch.setenv("FLOCK_OFFLINE", "1")
    doc = tmp_path / "x.txt"
    doc.write_text("anything")
    assert deal_keeper.read_with_local_model(doc, "anything") == []


# --- the setup script -----------------------------------------------------------------------------------------------

def _setup():
    spec = importlib.util.spec_from_file_location("creao_setup", ROOT / "scripts" / "creao_setup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_setup_creates_one_agent_per_role_and_writes_the_file(creao, monkeypatch, tmp_path):
    setup = _setup()
    out = tmp_path / "fresh_agents.json"   # not the fixture's file: this test starts with no agents
    monkeypatch.setattr(setup, "AGENTS_FILE", out)
    monkeypatch.setenv("FLOCK_CREAO_AGENTS", str(out))
    n = {"i": 0}

    def create(call):
        n["i"] += 1
        return json.dumps({"run_id": f"cr{n['i']}", "status": "pending", "agent_id": None}).encode()

    def status(call):
        rid = call["path"].rsplit("/", 1)[1]
        return json.dumps({"run_id": rid, "status": "completed", "agent_id": f"agent-{rid}",
                           "result": {"agent_id": f"agent-{rid}"}, "completed_at": "2026-10-24T20:00:00Z"}).encode()

    f = creao({("POST", "/v1/agents"): create, ("GET", "*"): status})
    data = setup.create(list(setup.ROLES), model="anthropic/claude-haiku-5-5")
    assert set(data["agents"]) == set(setup.ROLES) and len(set(a["agent_id"] for a in data["agents"].values())) == 7
    creates = [c for c in f.calls if c["path"] == "/v1/agents"]
    assert len(creates) == 7 and all(c["body"]["model"] == "anthropic/claude-haiku-5-5" for c in creates)
    assert all("`instructions`" in c["body"]["input"] and "only the single JSON object" in c["body"]["input"] for c in creates)
    assert llm.creao_agents()["judge.operator"] == data["agents"]["judge.operator"]["agent_id"]
    env = setup.env_block()
    assert "FLOCK_JUDGE_PROVIDERS=creao" in env and "FLOCK_READER_PROVIDER=creao" in env
    assert setup.create(list(setup.ROLES), None)["agents"] == data["agents"], "second run keeps existing agents"


def test_setup_check_reports_usable_agents(creao, monkeypatch, capsys):
    setup = _setup()
    creao({("POST", "/v1/realtime/runs"): lambda call: stream_reply(json.dumps({"ok": True, "role": "x"}), credits=0.05)})
    assert setup.check(["judge.strict", "reader"]) == 2
    assert "0.05 credits" in capsys.readouterr().out


def test_benchmark_has_a_creao_contestant():
    spec = importlib.util.spec_from_file_location("bm", ROOT / "scripts" / "benchmark.py")
    bm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bm)
    assert bm.CONTESTANTS["creao"]["env"]["FLOCK_JUDGE_PROVIDERS"] == "creao"
    assert bm.CONTESTANTS["creao"]["offline"] is False
