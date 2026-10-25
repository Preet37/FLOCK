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


def stream_reply(text: str, tokens=(1000, 100), model: str = "anthropic/claude-sonnet-5-5") -> bytes:
    half = len(text) // 2
    return ndjson({"type": "run.created", "run_id": "r1", "agent_id": "a1", "model": model, "render_protocol": 1},
                  {"type": "output_text.delta", "delta": text[:half]},
                  {"type": "output_text.delta", "delta": text[half:]},
                  {"type": "run.completed", "run_id": "r1", "status": "completed", "result": {"text": text},
                   "usage": {"models": [{"model": model, "input_tokens": tokens[0], "output_tokens": tokens[1]}]}})


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
    f = creao({("POST", "/v2/realtime/runs"): stream_reply(json.dumps(VOTE), tokens=(1_000_000, 100_000))})
    text, served = llm.complete("SYSTEM PROMPT", '{"gap": 2.5}', 300, ("creao", "a-strict"))
    assert json.loads(text) == VOTE and served == "creao:anthropic/claude-sonnet-5-5"
    call = f.calls[0]
    assert call["headers"]["authorization"] == "Bearer cr_sk_test"
    assert call["body"]["agent_id"] == "a-strict"
    assert call["body"]["input"]["instructions"].startswith("SYSTEM PROMPT") and call["body"]["input"]["request"] == '{"gap": 2.5}'
    assert call["body"]["input"]["instructions"].endswith(llm.CREAO_ANSWER_RULE), "the marker rule rides along"
    assert "model" not in call["body"], "no FLOCK_CREAO_LLM: the agent's default model runs"
    u = llm.creao_usage()
    assert u["input_tokens"] == 1_000_000 and u["output_tokens"] == 100_000 and u["calls"] == 1
    assert u["usd"] == pytest.approx(2.60 + 1.30), "Sonnet 5.5 at CREAO's $2.60 in / $13 out per 1M tokens"
    assert set(u["by_agent"]) == {"a-strict"} and u["credits"] == 0
    assert llm.airgap_report()["by_host"] == {"developer.test": 1}


def test_model_choice_is_passed_through(creao, monkeypatch):
    monkeypatch.setenv("FLOCK_CREAO_LLM", "anthropic/claude-haiku-4-5")
    f = creao({("POST", "/v2/realtime/runs"): stream_reply("{}", model="anthropic/claude-haiku-4-5")})
    llm.complete("s", "u", 10, ("creao", "a-op"))
    assert f.calls[0]["body"]["model"] == "anthropic/claude-haiku-4-5"
    assert llm.creao_usage()["unpriced"] == 1, "a model not in the price table is counted, not priced"


def test_falls_back_to_async_polling_when_realtime_is_not_enabled(creao):
    polls = {"n": 0}

    def status(call):
        polls["n"] += 1
        if polls["n"] < 3:
            return json.dumps({"run_id": "r9", "status": "running"}).encode()
        return json.dumps({"run_id": "r9", "status": "completed", "model": "anthropic/claude-sonnet-5-5",
                           "result": {"text": json.dumps(VOTE)},
                           "usage": {"models": [{"model": "anthropic/claude-sonnet-5-5", "input_tokens": 10, "output_tokens": 5}]}}).encode()

    f = creao({("POST", "/v2/realtime/runs"): urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(b'{"error":"ACCOUNT_BLOCKED"}')),
               ("POST", "/v2/runs"): json.dumps({"run_id": "r9", "status": "pending"}).encode(),
               ("GET", "/v2/runs/r9"): status})
    text, served = llm.complete("s", "u", 10, ("creao", "a-skep"))
    assert json.loads(text) == VOTE and served == "creao:anthropic/claude-sonnet-5-5"
    assert [c["path"] for c in f.calls] == ["/v2/realtime/runs", "/v2/runs", "/v2/runs/r9", "/v2/runs/r9", "/v2/runs/r9"]
    assert llm.creao_usage()["input_tokens"] == 10


def test_failed_run_raises_with_the_stable_code(creao):
    creao({("POST", "/v2/realtime/runs"): ndjson({"type": "run.created", "run_id": "r1"},
                                                 {"type": "run.failed", "run_id": "r1", "status": "failed",
                                                  "error": {"code": "INSUFFICIENT_CREDITS"}})})
    with pytest.raises(RuntimeError, match="INSUFFICIENT_CREDITS"):
        llm.complete("s", "u", 10, ("creao", "a-strict"))
    creao({("POST", "/v2/realtime/runs"): urllib.error.HTTPError("u", 404, "x", {}, io.BytesIO(b'{"error":"AGENT_NOT_FOUND"}')),
           ("POST", "/v2/runs"): urllib.error.HTTPError("u", 404, "x", {}, io.BytesIO(b'{"error":"AGENT_NOT_FOUND"}'))})
    with pytest.raises(llm.CreaoHTTPError, match="AGENT_NOT_FOUND"):
        llm.complete("s", "u", 10, ("creao", "a-strict"))


def test_rate_limited_waits_for_retry_after_then_retries(creao, monkeypatch):
    waits = []
    monkeypatch.setattr(llm, "_sleep", lambda s: waits.append(s))
    hits = {"n": 0}

    def limited(call):
        hits["n"] += 1
        if hits["n"] == 1:
            return urllib.error.HTTPError("u", 429, "x", {"Retry-After": "7"}, io.BytesIO(b'{"error":"RATE_LIMITED"}'))
        return stream_reply(json.dumps(VOTE))

    creao({("POST", "/v2/realtime/runs"): limited})
    text, _ = llm.complete("s", "u", 10, ("creao", "a-strict"))
    assert json.loads(text) == VOTE and hits["n"] == 2 and waits == [7.0]


def test_run_creates_are_paced_under_the_per_minute_budget(creao, monkeypatch):
    monkeypatch.setenv("FLOCK_CREAO_RPM", "2")
    waits = []
    monkeypatch.setattr(llm, "_sleep", lambda s: (waits.append(s), llm._run_times.clear()))
    llm._run_times.clear()
    creao({("POST", "/v2/realtime/runs"): stream_reply("{}")})
    for _ in range(3):
        llm.complete("s", "u", 10, ("creao", "a-strict"))
    assert len(waits) == 1 and 0 < waits[0] <= 60.1, "the third create in the same minute waited"


# --- the roles, end to end ------------------------------------------------------------------------------------------

def test_the_panel_judges_on_creao(creao, monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "creao")
    monkeypatch.setenv("FLOCK_REVIEWER_PROVIDER", "offline")
    f = creao({("POST", "/v2/realtime/runs"): lambda call: stream_reply(json.dumps(VOTE))})
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "recoverable"
    assert [v["model"] for v in d["votes"]] == ["creao/a-strict", "creao/a-op", "creao/a-skep"]
    assert {c["body"]["agent_id"] for c in f.calls} == {"a-strict", "a-op", "a-skep"}
    assert all(panel.SYSTEM in c["body"]["input"]["instructions"] for c in f.calls), "Flock's own prompt is the instructions"
    assert llm.creao_usage()["calls"] == 3


def test_a_judge_that_fails_falls_back_to_the_rules_and_says_so(creao, monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "creao")
    monkeypatch.setenv("FLOCK_REVIEWER_PROVIDER", "offline")
    creao({("POST", "/v2/realtime/runs"): urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b'{"error":"SERVICE_UNAVAILABLE"}'))})
    d = panel.decide(fake("F-0001"), rules=[])
    assert all(v.get("fallback") for v in d["votes"]) and "offline rules" in d["reason"]


def test_the_document_reader_turns_free_text_into_claims(creao, monkeypatch, tmp_path):
    monkeypatch.setenv("FLOCK_READER_PROVIDER", "creao")
    reply = {"claims": [{"channel": "grubhub", "term_type": "fee", "store_id": "all", "value": 0,
                         "effective_from": "2026-08-01", "effective_to": "2026-08-31",
                         "quote": "We will waive the Delivery Network Fee for August.", "authority": "auth_email"},
                        {"channel": "grubhub", "term_type": "nonsense", "value": 1},
                        {"channel": "grubhub", "term_type": "commission", "value": 0.2, "quote": "x", "authority": "unknown"}]}
    f = creao({("POST", "/v2/realtime/runs"): stream_reply(json.dumps(reply))})
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
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)
    state = {"n": 0, "agents": [], "polls": {}}

    def create(call):
        state["n"] += 1
        rid = f"cr{state['n']}"
        state["agents"].append({"agent_id": f"agent-{rid}", "name": f"n{rid}", "created_at": f"2026-10-24T20:0{state['n']}:00Z"})
        return json.dumps({"run_id": rid, "status": "pending", "agent_id": None}).encode()   # v2: id not known yet

    def router(call):
        path = call["path"]
        if path.startswith("/v2/agents?"):
            return json.dumps({"agents": state["agents"], "limit": 100, "offset": 0, "has_more": False}).encode()
        if "/edits/" in path:
            agent_id, rid = path.split("/v2/agents/")[1].split("/edits/")
            state["polls"][rid] = state["polls"].get(rid, 0) + 1
            status = "running" if state["polls"][rid] == 1 else "succeeded"
            return json.dumps({"run_id": rid, "agent_id": agent_id, "status": status,
                               "completed_at": "2026-10-24T20:05:00Z"}).encode()
        raise AssertionError(path)

    f = creao({("POST", "/v2/agents"): create, ("GET", "*"): router})
    data = setup.create(list(setup.ROLES), model="anthropic/claude-haiku-4-5")
    assert set(data["agents"]) == set(setup.ROLES)
    assert len({a["agent_id"] for a in data["agents"].values()}) == 7, "each role got its own agent id from the list diff"
    creates = [c for c in f.calls if c["path"] == "/v2/agents"]
    assert len(creates) == 7 and all(c["body"]["model"] == "anthropic/claude-haiku-4-5" for c in creates)
    assert all("`instructions`" in c["body"]["input"] and "only the single JSON object" in c["body"]["input"] for c in creates)
    assert all(p == 2 for p in state["polls"].values()), "each creation run was polled on the edits route until succeeded"
    assert llm.creao_agents()["judge.operator"] == data["agents"]["judge.operator"]["agent_id"]
    env = setup.env_block()
    assert "FLOCK_JUDGE_PROVIDERS=creao" in env and "FLOCK_READER_PROVIDER=creao" in env
    assert setup.create(list(setup.ROLES), None)["agents"] == data["agents"], "second run keeps existing agents"


def test_setup_check_reports_usable_agents(creao, monkeypatch, capsys):
    setup = _setup()
    creao({("POST", "/v2/realtime/runs"): lambda call: stream_reply(json.dumps({"ok": True, "role": "x"}), tokens=(500, 20))})
    assert setup.check(["judge.strict", "reader"]) == 2
    out = capsys.readouterr().out
    assert "500 in / 20 out tokens" in out and "$" in out


def test_benchmark_has_a_creao_contestant():
    spec = importlib.util.spec_from_file_location("bm", ROOT / "scripts" / "benchmark.py")
    bm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bm)
    assert bm.CONTESTANTS["creao"]["env"]["FLOCK_JUDGE_PROVIDERS"] == "creao"
    assert bm.CONTESTANTS["creao"]["offline"] is False


def test_the_verdict_is_found_in_the_harness_narration(creao):
    marked = ('I\'ll run the agent. Inputs: {"instructions": "x", "request": "y", "max_tokens": 10}. '
              'The case is {"check_type": "commission", "gap": 2.5}. Done.\n'
              'FLOCK_ANSWER: {"outcome": "recoverable", "confidence": 0.9, "reason": "Contract says 20%."}\n'
              '{"status": "success", "summary": "Flock Judge executed successfully"}')
    f = creao({("POST", "/v2/realtime/runs"): stream_reply(marked)})
    text, _ = llm.complete("SYS", "u", 10, ("creao", "a-strict"))
    assert json.loads(text) == VOTE, "the marked object wins over echoes and the session summary"
    assert f.calls[0]["body"]["input"]["instructions"].endswith(llm.CREAO_ANSWER_RULE)
    unmarked = ('Let me validate {"instructions": "x", "request": "y"} first. '
                '{"outcome": "recoverable", "confidence": 0.9, "reason": "Contract says 20%."} '
                '{"status": "success", "summary": "done"}')
    creao({("POST", "/v2/realtime/runs"): stream_reply(unmarked)})
    text, _ = llm.complete("SYS", "u", 10, ("creao", "a-strict"))
    assert json.loads(text) == VOTE, "without a marker: skip the echo and the summary, take the real object"
    assert llm._creao_answer(["plain words only"], None) == "plain words only"


# --- an agent built on agent.creao.ai, through its app-scoped API Trigger -------------------------------------------

@pytest.fixture
def creaoapp(creao, monkeypatch, tmp_path):
    monkeypatch.setenv("CREAO_APP_KEY", "capi_test")
    monkeypatch.setattr(llm, "CREAOAPP_POLL_SECONDS", 0.0)
    llm._app_times.clear()
    monkeypatch.setenv("FLOCK_CREAOAPP_URL", "https://developer.test")   # the fake answers any host
    agents = tmp_path / "creao_agents.json"
    agents.write_text(json.dumps({"agents": {}, "apps": {"judge.strict": {"app_id": "app-judge"}, "judge.operator": "app-judge",
                                                          "judge.skeptic": "app-judge"}}))
    monkeypatch.setenv("FLOCK_CREAO_AGENTS", str(agents))
    return creao


def test_app_agent_routes_runs_and_polls(creaoapp, monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "creaoapp")
    assert llm.route("judge", 0) == ("creaoapp", "app-judge") and llm.available(llm.route("judge", 0))
    polls = {"n": 0}

    def status(call):
        polls["n"] += 1
        if polls["n"] < 2:
            return json.dumps({"id": "run1", "status": "running"}).encode()
        return json.dumps({"id": "run1", "status": "completed", "appName": "Flock Judge",
                           "result": {"text": "Working... FLOCK_ANSWER: " + json.dumps(VOTE) + "\n"}}).encode()

    f = creaoapp({("POST", "/api/v1/apps/app-judge/runs"): json.dumps({"id": "run1", "status": "pending", "appId": "app-judge"}).encode(),
                  ("GET", "/api/v1/apps/app-judge/runs/run1"): status})
    text, served = llm.complete("SYS", "u", 10, ("creaoapp", "app-judge"))
    assert json.loads(text) == VOTE and served == "creaoapp:Flock Judge"
    first = f.calls[0]
    assert first["headers"]["authorization"] == "Bearer capi_test"
    assert set(first["body"]["inputs"]) == {"instructions", "request"} and first["body"]["inputs"]["instructions"].startswith("SYS")
    assert [c["path"] for c in f.calls] == ["/api/v1/apps/app-judge/runs", "/api/v1/apps/app-judge/runs/run1",
                                           "/api/v1/apps/app-judge/runs/run1"]
    assert llm.creao_usage()["by_agent"]["app:app-judge"]["calls"] == 1


def test_app_agent_without_its_key_is_offline_with_a_reason(creaoapp, monkeypatch):
    monkeypatch.delenv("CREAO_APP_KEY")
    assert not llm.available(("creaoapp", "app-judge"))
    assert "CREAO_APP_KEY" in llm.offline_reason(("creaoapp", "app-judge"))


def test_app_agent_failed_run_raises(creaoapp):
    creaoapp({("POST", "/api/v1/apps/app-judge/runs"): json.dumps({"id": "r2", "status": "pending"}).encode(),
              ("GET", "/api/v1/apps/app-judge/runs/r2"): json.dumps({"id": "r2", "status": "failed", "errorCode": "INSUFFICIENT_CREDITS"}).encode()})
    with pytest.raises(RuntimeError, match="INSUFFICIENT_CREDITS"):
        llm.complete("s", "u", 10, ("creaoapp", "app-judge"))


def test_the_panel_judges_on_an_agent_built_in_agent_creao_ai(creaoapp, monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "creaoapp")
    monkeypatch.setenv("FLOCK_REVIEWER_PROVIDER", "offline")
    n = {"i": 0}

    def start(call):
        n["i"] += 1
        return json.dumps({"id": f"r{n['i']}", "status": "pending"}).encode()

    def status(call):
        return json.dumps({"id": call["path"].rsplit("/", 1)[1], "status": "completed",
                           "result": {"text": "FLOCK_ANSWER: " + json.dumps(VOTE)}}).encode()

    creaoapp({("POST", "/api/v1/apps/app-judge/runs"): start, ("GET", "*"): status})
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "recoverable" and {v["model"] for v in d["votes"]} == {"creaoapp/app-judge"}


def test_app_answer_merged_with_the_echoed_inputs_is_stripped(creaoapp):
    merged = {"instructions": "long prompt", "request": "{...}", **VOTE}
    creaoapp({("POST", "/api/v1/apps/app-judge/runs"): json.dumps({"id": "r3", "status": "pending"}).encode(),
              ("GET", "/api/v1/apps/app-judge/runs/r3"): json.dumps({"id": "r3", "status": "completed",
                                                                     "result": {"text": json.dumps(merged)}}).encode()})
    text, _ = llm.complete("s", "u", 10, ("creaoapp", "app-judge"))
    assert json.loads(text) == VOTE, "the chat product merges inputs and answer into one object; keep only the answer"


def test_app_polls_survive_a_rate_limit_and_count_against_the_budget(creaoapp, monkeypatch):
    waits = []
    monkeypatch.setattr(llm, "_sleep", lambda s: waits.append(s))
    hits = {"n": 0}

    def status(call):
        hits["n"] += 1
        if hits["n"] == 1:
            return urllib.error.HTTPError("u", 429, "x", {"Retry-After": "3"}, io.BytesIO(b'{"error":"RATE_LIMITED"}'))
        return json.dumps({"id": "r4", "status": "completed", "result": {"text": "FLOCK_ANSWER: " + json.dumps(VOTE)}}).encode()

    f = creaoapp({("POST", "/api/v1/apps/app-judge/runs"): json.dumps({"id": "r4", "status": "pending"}).encode(),
                  ("GET", "/api/v1/apps/app-judge/runs/r4"): status})
    text, _ = llm.complete("s", "u", 10, ("creaoapp", "app-judge"))
    assert json.loads(text) == VOTE and 3.0 in waits, "a 429 on a poll waits Retry-After and polls again"
    assert len(llm._app_times) == len(f.calls), "every create and poll was counted against the per-key budget"


def test_app_reason_comes_from_the_marked_line_in_chat_text(creaoapp):
    compact = {"request": "{...}", "check_type": "commission", "channel": "doordash", "outcome": "recoverable", "confidence": 0.86}
    chat = ("I'll start by reading the skill. The contract says 20% and the payout shows 32.5%.\n"
            "FLOCK_ANSWER: " + json.dumps({"outcome": "recoverable", "confidence": 0.86, "reason": "Contract says 20%; 6.50 on 20.00 is 32.5%."}))
    creaoapp({("POST", "/api/v1/apps/app-judge/runs"): json.dumps({"id": "r5", "status": "pending"}).encode(),
              ("GET", "/api/v1/apps/app-judge/runs/r5"): json.dumps({"id": "r5", "status": "completed",
                                                                     "result": {"text": json.dumps(compact), "chatText": chat}}).encode()})
    text, _ = llm.complete("s", "u", 10, ("creaoapp", "app-judge"))
    got = json.loads(text)
    assert got["outcome"] == "recoverable" and got["confidence"] == 0.86
    assert got["reason"].startswith("Contract says 20%"), "the reason lives in chatText's FLOCK_ANSWER line"
    assert "request" not in got, "echoed inputs are stripped"
