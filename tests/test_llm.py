"""Live Claude path, with the client mocked. No test here ever reaches the real API."""
import threading
from types import SimpleNamespace

import pytest

from conftest import fake
from flock import llm
from flock.agents import dispute_writer, panel


class FakeClient:
    """Stands in for anthropic.Anthropic(). reply: str, Exception, or callable(kwargs) -> str."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self._lock = threading.Lock()
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        with self._lock:
            self.calls.append(kw)
        r = self.reply(kw) if callable(self.reply) else self.reply
        if isinstance(r, BaseException):
            raise r
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)])


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.delenv("FLOCK_OFFLINE", raising=False)
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "0")  # audit sampling has its own tests in test_panel.py

    def install(reply):
        client = FakeClient(reply)
        monkeypatch.setattr(llm, "_client", lambda: client)
        return client
    return install


# --- config ------------------------------------------------------------------

def test_model_defaults_to_sonnet(live):
    client = live('{"outcome": "correct", "confidence": 0.9, "reason": "x"}')
    llm.ask_json("s", "u")
    assert llm.model() == "claude-sonnet-5-5"
    assert client.calls[0]["model"] == "claude-sonnet-5-5"


def test_model_is_configurable_from_env(live, monkeypatch):
    monkeypatch.setenv("FLOCK_MODEL", "claude-test-model")
    client = live('{"outcome": "correct", "confidence": 0.9, "reason": "x"}')
    llm.ask_json("s", "u")
    assert client.calls[0]["model"] == "claude-test-model"


def test_timeout_is_configurable(monkeypatch):
    monkeypatch.setenv("FLOCK_LLM_TIMEOUT", "7.5")
    assert llm.timeout() == 7.5
    monkeypatch.setenv("FLOCK_LLM_TIMEOUT", "soon")
    assert llm.timeout() == 30.0


def test_offline_flag_wins_even_with_a_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("FLOCK_OFFLINE", "1")
    monkeypatch.setattr(llm, "_client", lambda: pytest.fail("must not build a client offline"))
    assert llm.call_json("s", "u") == (None, None)
    assert llm.ask_text("s", "u") is None


# --- tolerant JSON -----------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{"outcome": "correct", "confidence": 0.9}',
    '```json\n{"outcome": "correct", "confidence": 0.9}\n```',
    '```\n{"outcome": "correct", "confidence": 0.9}\n```',
    'Here is my call:\n{"outcome": "correct", "confidence": 0.9}\nHope that helps {really}.',
    'Thinking {not json} then {"outcome": "correct", "confidence": 0.9}',
    '{"outcome": "correct", "confidence": 0.9, "meta": {"why": "nested {braces} in a string"}} trailing',
])
def test_parse_json_object_is_tolerant(text):
    got = llm.parse_json_object(text)
    assert got["outcome"] == "correct" and got["confidence"] == 0.9


@pytest.mark.parametrize("text", [None, "", "no json here", "[1, 2, 3]", '{"outcome": "correct",'])
def test_parse_json_object_gives_up_cleanly(text):
    assert llm.parse_json_object(text) is None


# --- judge on the live path --------------------------------------------------

def test_live_judge_vote_is_used_and_normalized(live):
    live('```json\n{"outcome": " Recoverable ", "confidence": "85%", "reason": "Contract says 20%."}\n```')
    v = panel.vote("judge.strict", fake("F-0001"))
    assert v == {"outcome": "recoverable", "confidence": 0.85, "reason": "Contract says 20%.", "judge": "judge.strict",
                 "model": "anthropic/claude-sonnet-5-5"}


@pytest.mark.parametrize("conf,want", [(1.7, 0.017), (-3, 0.0), (40, 0.4), ("0.6", 0.6)])
def test_live_confidence_is_clamped(live, conf, want):
    live('{"outcome": "correct", "confidence": %s, "reason": "x"}' % (f'"{conf}"' if isinstance(conf, str) else conf))
    assert panel.vote("judge.strict", fake("F-0005"))["confidence"] == pytest.approx(want)


def test_timeout_falls_back_to_offline_rules_and_is_recorded(live):
    live(TimeoutError("request timed out"))
    d = panel.decide(fake("F-0001"), rules=[])
    assert d["outcome"] == "recoverable"  # same call the offline rules make
    assert all(v["fallback"].startswith("TimeoutError") for v in d["votes"])
    assert all("[offline fallback: TimeoutError" in v["reason"] for v in d["votes"])
    assert "3 of 3 judges used offline rules after an API error" in d["reason"]


def test_one_judge_failing_only_that_judge_falls_back(live):
    def reply(kw):
        if "skeptic" in kw["system"]:
            return RuntimeError("overloaded")
        return '{"outcome": "recoverable", "confidence": 0.95, "reason": "Clear breach."}'
    live(reply)
    d = panel.decide(fake("F-0001"), rules=[])
    by = {v["judge"]: v for v in d["votes"]}
    assert "fallback" not in by["judge.strict"] and by["judge.strict"]["reason"] == "Clear breach."
    assert by["judge.skeptic"]["fallback"].startswith("RuntimeError")
    assert "1 of 3 judges used offline rules" in d["reason"]


@pytest.mark.parametrize("reply", ["I think it's recoverable.", '{"outcome": "maybe", "confidence": 0.9}',
                                   '{"outcome": "correct", "confidence": "high"}'])
def test_unusable_judge_reply_falls_back_and_is_recorded(live, reply):
    live(reply)
    v = panel.vote("judge.strict", fake("F-0003"))
    assert v["outcome"] == "conflict"  # offline rule for an unauthorized-email finding
    assert v["fallback"] and v["reason"].startswith("[offline fallback:")


def test_live_judges_are_called_in_parallel(live):
    barrier = threading.Barrier(3, timeout=5)

    def reply(kw):
        barrier.wait()
        return '{"outcome": "recoverable", "confidence": 0.9, "reason": "ok"}'
    client = live(reply)
    d = panel.decide(fake("F-0001"), rules=[])
    assert len(client.calls) == 3 and d["outcome"] == "recoverable"
    assert all("fallback" not in v for v in d["votes"])


def test_live_judges_cannot_dispute_pennies_or_weak_evidence(live):
    live('{"outcome": "recoverable", "confidence": 0.99, "reason": "Dispute it."}')
    assert panel.decide(fake("A-0002"), rules=[])["outcome"] == "correct"
    assert panel.decide(fake("A-0005"), rules=[])["outcome"] == "conflict"


# --- dispute writer on the live path -----------------------------------------

def test_live_draft_missing_a_citation_gets_the_evidence_appended(live):
    live("Hello DoorDash — we were overcharged on ORD-1001 per payouts_wk1.csv. Please fix.")
    text = dispute_writer.draft(fake("F-0001"))
    assert "—" not in text
    assert "data/raw/contracts/doordash_msa.pdf" in text and "Evidence:" in text


def test_live_draft_citing_everything_is_left_alone(live):
    body = "Hello. Per payouts_wk1.csv row 14 and doordash_msa.pdf, commission should be 4.00. Please credit 2.50."
    live(body)
    assert dispute_writer.draft(fake("F-0001")) == body


def test_live_draft_error_falls_back_to_template(live):
    live(ConnectionError("no network"))
    text = dispute_writer.draft(fake("F-0001"))
    assert text.startswith("Hello DoorDash merchant support")


def test_draft_prompt_never_includes_panel_reasoning(live):
    client = live("Per payouts_wk1.csv and doordash_msa.pdf, please credit 2.50.")
    f = {**fake("F-0001"), "reason": "All 3 judges agree (83% avg confidence). internal note"}
    dispute_writer.draft(f)
    assert "judges" not in client.calls[0]["messages"][0]["content"]
