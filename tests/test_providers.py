"""Provider layer: local (vLLM/Ollama), Groq, NVIDIA over plain HTTP, with urlopen mocked. Nothing here hits a network."""
import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from conftest import fake
from flock import llm
from flock.agents import dispute_writer, formatter, panel


class FakeHTTP:
    """Stands in for urllib.request.urlopen. reply: str, Exception, or callable(request dict) -> str | Exception."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, req, timeout=None):
        call = {"url": req.full_url, "headers": {k.lower(): v for k, v in req.header_items()},
                "body": json.loads(req.data), "timeout": timeout}
        with self._lock:
            self.calls.append(call)
        r = self.reply(call) if callable(self.reply) else self.reply
        if isinstance(r, BaseException):
            raise r
        return io.BytesIO(json.dumps({"choices": [{"message": {"role": "assistant", "content": r}}]}).encode())


@pytest.fixture
def http(monkeypatch):
    monkeypatch.delenv("FLOCK_OFFLINE", raising=False)
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "0")
    monkeypatch.setattr(llm, "_client", lambda: pytest.fail("no Anthropic call expected"))

    def install(reply):
        fake_http = FakeHTTP(reply)
        monkeypatch.setattr(urllib.request, "urlopen", fake_http)
        return fake_http
    return install


VOTE = '{"outcome": "recoverable", "confidence": 0.9, "reason": "Contract says 20%."}'


# --- routing -----------------------------------------------------------------

def test_nothing_set_is_offline_exactly_as_before(http):
    calls = http(VOTE)
    assert llm.route("judge", 0) == ("anthropic", "claude-sonnet-5-5")
    assert llm.call_json("s", "u", r=llm.route("judge", 0)) == (None, None)
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "recoverable" and set(d) == {"outcome", "confidence", "votes", "reason"}
    assert calls.calls == []


def test_judge_providers_and_models_per_judge(monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "local, local ,groq")
    monkeypatch.setenv("FLOCK_JUDGE_MODELS", "qwen3:32b,,")
    monkeypatch.setenv("FLOCK_LOCAL_MODEL", "llama3.1:70b")
    assert [llm.route("judge", i) for i in range(3)] == [
        ("local", "qwen3:32b"), ("local", "llama3.1:70b"), ("groq", "llama-3.3-70b-versatile")]


def test_one_judge_provider_applies_to_every_judge(monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "nvidia")
    assert {llm.route("judge", i) for i in range(3)} == {("nvidia", "meta/llama-3.3-70b-instruct")}


def test_role_defaults(monkeypatch):
    assert llm.route("reviewer") == ("anthropic", "claude-sonnet-5-5")
    monkeypatch.setenv("FLOCK_PROVIDER", "local")
    assert llm.route("writer")[0] == llm.route("mapper")[0] == llm.route("judge", 2)[0] == "local"
    assert llm.route("reviewer")[0] == "anthropic"  # the reviewer stays Claude unless set on purpose
    monkeypatch.setenv("FLOCK_WRITER_PROVIDER", "groq")
    monkeypatch.setenv("FLOCK_REVIEWER_PROVIDER", "nvidia")
    monkeypatch.setenv("FLOCK_REVIEWER_MODEL", "nvidia/llama-3.1-nemotron-70b-instruct")
    assert llm.route("writer")[0] == "groq"
    assert llm.route("reviewer") == ("nvidia", "nvidia/llama-3.1-nemotron-70b-instruct")


@pytest.mark.parametrize("provider,key_env,ok", [
    ("local", None, True), ("groq", None, False), ("groq", "GROQ_API_KEY", True),
    ("nvidia", None, False), ("nvidia", "NVIDIA_API_KEY", True), ("anthropic", None, False),
    ("anthropic", "ANTHROPIC_API_KEY", True), ("offline", None, False),
])
def test_available_needs_the_providers_key(monkeypatch, provider, key_env, ok):
    monkeypatch.delenv("FLOCK_OFFLINE", raising=False)
    if key_env:
        monkeypatch.setenv(key_env, "k")
    assert llm.available((provider, "m")) is ok


def test_offline_flag_wins_over_local(http, monkeypatch):
    calls = http(VOTE)
    monkeypatch.setenv("FLOCK_OFFLINE", "1")
    assert llm.call_json("s", "u", r=("local", "m")) == (None, None)
    assert llm.ask_text("s", "u", r=("local", "m")) is None
    assert calls.calls == []


# --- each provider over HTTP -------------------------------------------------

def test_local_posts_openai_chat_to_the_local_url(http, monkeypatch):
    monkeypatch.setenv("FLOCK_LOCAL_MODEL", "gpt-oss:120b")
    monkeypatch.setenv("FLOCK_LLM_TIMEOUT", "12")
    calls = http(VOTE)
    got, err = llm.call_json("sys", "usr", r=("local", llm.default_model("local")))
    assert err is None and got["outcome"] == "recoverable"
    c = calls.calls[0]
    assert c["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert c["body"]["model"] == "gpt-oss:120b" and c["body"]["temperature"] == 0
    assert [m["role"] for m in c["body"]["messages"]] == ["system", "user"]
    assert c["body"]["messages"][1]["content"] == "usr"
    assert "authorization" not in c["headers"] and c["timeout"] == 12


def test_local_url_and_optional_key_from_env(http, monkeypatch):
    monkeypatch.setenv("FLOCK_LOCAL_URL", "http://gx10.tailnet.ts.net:8000/v1/")
    monkeypatch.setenv("FLOCK_LOCAL_API_KEY", "vllm-key")
    calls = http("hello")
    assert llm.ask_text("s", "u", r=("local", "m")) == "hello"
    assert calls.calls[0]["url"] == "http://gx10.tailnet.ts.net:8000/v1/chat/completions"
    assert calls.calls[0]["headers"]["authorization"] == "Bearer vllm-key"


def test_groq(http, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("FLOCK_GROQ_MODEL", "llama-3.1-8b-instant")
    calls = http(VOTE)
    assert llm.ask_json("s", "u", r=("groq", llm.default_model("groq")))["confidence"] == 0.9
    c = calls.calls[0]
    assert c["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert c["headers"]["authorization"] == "Bearer gsk-test" and c["body"]["model"] == "llama-3.1-8b-instant"


def test_nvidia(http, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    calls = http(VOTE)
    assert llm.ask_json("s", "u", r=("nvidia", llm.default_model("nvidia")))["outcome"] == "recoverable"
    c = calls.calls[0]
    assert c["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert c["headers"]["authorization"] == "Bearer nvapi-test"
    assert c["body"]["model"] == "meta/llama-3.3-70b-instruct"


def test_groq_without_key_never_calls(http, monkeypatch):
    calls = http(VOTE)
    assert llm.call_json("s", "u", r=("groq", "m")) == (None, None)
    assert calls.calls == []


def test_http_error_is_reported(http):
    calls = http(urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b'{"error": "model loading"}')))
    got, err = llm.call_json("s", "u", r=("local", "m"))
    assert got is None and err.startswith("RuntimeError: HTTP 503 from local") and "model loading" in err
    assert len(calls.calls) == 1


def test_unreachable_local_server_falls_back_and_is_recorded(http, monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "local")
    http(urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")))
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "recoverable"  # same call the offline rules make
    assert all(v["fallback"].startswith("URLError") for v in d["votes"])
    assert "3 of 3 judges used offline rules" in d["reason"]


def test_unknown_provider_is_an_error_not_silence(http):
    got, err = llm.call_json("s", "u", r=("olama", "m"))
    assert got is None and "unknown provider 'olama'" in err


def test_reasoning_model_think_block_is_stripped(http):
    http('<think>maybe {"outcome": "conflict"} hmm</think>\n{"outcome": "correct", "confidence": 0.8}')
    assert llm.ask_json("s", "u", r=("local", "m"))["outcome"] == "correct"
    http("<think>let me draft</think>\nHello Grubhub.")
    assert llm.ask_text("s", "u", r=("local", "m")) == "Hello Grubhub."


# --- roles wired to their providers ------------------------------------------

def test_mixed_judge_panel_hits_each_provider(http, monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "local,local,groq")
    monkeypatch.setenv("FLOCK_JUDGE_MODELS", "qwen3:32b,llama3.1:70b")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    calls = http(VOTE)
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "recoverable"
    assert sorted(c["url"].split("/")[2] for c in calls.calls) == ["127.0.0.1:11434", "127.0.0.1:11434", "api.groq.com"]
    assert [v["model"] for v in d["votes"]] == ["local/qwen3:32b", "local/llama3.1:70b", "groq/llama-3.3-70b-versatile"]


def test_writer_uses_its_provider(http, monkeypatch):
    monkeypatch.setenv("FLOCK_WRITER_PROVIDER", "local")
    calls = http("Per payouts_wk1.csv and doordash_msa.pdf, please credit 2.50.")
    assert dispute_writer.draft(fake("F-0001")).startswith("Per payouts_wk1.csv")
    assert len(calls.calls) == 1


def test_mapper_uses_its_provider(http, monkeypatch):
    monkeypatch.setenv("FLOCK_MAPPER_PROVIDER", "local")
    calls = http('{"table": "bank_deposits", "columns": {"Amt": "amount", "Memo": "memo"}}')
    m = formatter.llm_mapping(["Amt", "Memo"], [["10.00", "DOORDASH"]], "bank")
    assert m == {"table": "bank_deposits", "columns": {"Amt": "amount", "Memo": "memo"}}
    assert len(calls.calls) == 1


# --- debate, memo, audit over real HTTP shapes --------------------------------

def test_live_debate_round_records_both_rounds(http, monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "local")

    def reply(call):
        system = call["body"]["messages"][0]["content"]
        if "skeptic" in system and "debate round" not in system:
            return '{"outcome": "conflict", "confidence": 0.6, "reason": "Not sure the contract applies."}'
        if "skeptic" in system:
            other = json.loads(call["body"]["messages"][1]["content"])["other_judges"]
            assert {o["judge"] for o in other} == {"judge.strict", "judge.operator"}
            return '{"outcome": "recoverable", "confidence": 0.85, "reason": "The others cite the contract; convinced."}'
        return VOTE
    calls = http(reply)
    d = panel.decide(fake("F-0001"), rules=[])
    assert len(calls.calls) == 6  # 3 first votes + 3 debate replies
    assert d["outcome"] == "recoverable"
    assert [v["round"] for v in d["votes"]] == [1, 1, 1, 2, 2, 2]
    assert d["votes"][2]["outcome"] == "conflict" and d["votes"][5]["outcome"] == "recoverable"
    assert [v["revised"] for v in d["votes"][3:]] == [False, False, True]
    assert d["reason"].startswith("After one debate round (judge.skeptic conflict -> recoverable): All 3 judges agree")
    assert "review_memo" not in d  # reviewer has no key: no-op


def test_live_split_gets_a_memo_from_the_reviewer_not_a_verdict(http, monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "local")
    monkeypatch.setenv("FLOCK_REVIEWER_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")

    def reply(call):
        system = call["body"]["messages"][0]["content"]
        if "escalation memo" in system:
            return "Strict and operator say recoverable — contract is clear. Skeptic says conflict. A signed rate sheet settles it."
        if "skeptic" in system:
            return '{"outcome": "conflict", "confidence": 0.8, "reason": "Rate sheet unsigned."}'
        return VOTE
    calls = http(reply)
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "conflict" and "sending to a human" in d["reason"]
    assert "nobody changed their call" in d["reason"]
    assert d["review_memo"].startswith("Strict and operator say recoverable, contract is clear")
    assert "—" not in d["review_memo"]
    memo_calls = [c for c in calls.calls if "api.groq.com" in c["url"]]
    assert len(memo_calls) == 1 and len(d["votes"]) == 6


def test_live_audit_reviewer_disagreement_sends_to_a_human(http, monkeypatch):
    monkeypatch.setenv("FLOCK_JUDGE_PROVIDERS", "local")
    monkeypatch.setenv("FLOCK_REVIEWER_PROVIDER", "nvidia")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "1")

    def reply(call):
        if "integrate.api.nvidia.com" in call["url"]:
            assert json.loads(call["body"]["messages"][1]["content"])["panel_outcome"] == "recoverable"
            return '{"outcome": "conflict", "confidence": 0.7, "reason": "The amendment may supersede this rate."}'
        return VOTE
    http(reply)
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "conflict"
    assert d["reason"].startswith("Audit sample: the reviewer (nvidia/meta/llama-3.3-70b-instruct) disagrees")
    assert "Sending to a human" in d["reason"]
    assert d["votes"][-1]["judge"] == "reviewer" and d["votes"][-1]["round"] == "audit"
