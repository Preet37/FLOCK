"""Thin wrapper around the models Flock can talk to. Local-model-first: the judges, writer and mapper can run on
a local OpenAI-compatible server (vLLM or Ollama on an NVIDIA GX10), Groq or NVIDIA's hosted API, with Claude
kept as the reviewer. If a role's provider isn't configured, callers fall back to offline rules, so the whole
pipeline still runs on a laptop with no key (useful for tests and the demo backup).

Rule: never ask the model to do money math. Give it numbers already computed in Python.

Providers:
  local      OpenAI-compatible server. FLOCK_LOCAL_URL (default http://127.0.0.1:11434/v1, Ollama),
             FLOCK_LOCAL_MODEL (default llama3.1:8b), FLOCK_LOCAL_API_KEY (optional, e.g. vLLM --api-key)
  groq       GROQ_API_KEY, FLOCK_GROQ_MODEL (default llama-3.3-70b-versatile)
  nvidia     NVIDIA_API_KEY, FLOCK_NVIDIA_MODEL (default meta/llama-3.3-70b-instruct),
             FLOCK_NVIDIA_URL (default https://integrate.api.nvidia.com/v1, build.nvidia.com)
  anthropic  ANTHROPIC_API_KEY, FLOCK_MODEL (default claude-sonnet-5-5)
  offline    never call anything (same as leaving a key out)

Which provider each role uses:
  FLOCK_JUDGE_PROVIDERS    comma list, one per judge in panel order, e.g. "local,local,groq" (one entry = all judges)
  FLOCK_JUDGE_MODELS       optional comma list of model ids per judge, overriding that provider's model
  FLOCK_REVIEWER_PROVIDER  second opinions + escalation memos (default anthropic)
  FLOCK_WRITER_PROVIDER    dispute drafts
  FLOCK_MAPPER_PROVIDER    column mappings for unseen report layouts
  FLOCK_REVIEWER_MODEL / FLOCK_WRITER_MODEL / FLOCK_MAPPER_MODEL   optional model id for that role
  FLOCK_PROVIDER           default for judge/writer/mapper when their own variable is unset (default anthropic,
                           which is offline without ANTHROPIC_API_KEY: nothing set = offline rules)

Other env:
  FLOCK_LLM_TIMEOUT  seconds per request before we give up and fall back (default 30)
  FLOCK_OFFLINE=1    never call any provider, even with keys
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from typing import Any

DEFAULT_MODEL = "claude-sonnet-5-5"
MODEL = os.environ.get("FLOCK_MODEL", DEFAULT_MODEL)  # kept for anyone importing it; model() is read per call

# provider -> (base url env, default base url, key env, model env, default model). anthropic has its own client.
OPENAI_COMPATIBLE = {
    "local": ("FLOCK_LOCAL_URL", "http://127.0.0.1:11434/v1", "FLOCK_LOCAL_API_KEY", "FLOCK_LOCAL_MODEL", "llama3.1:8b"),
    "groq": ("FLOCK_GROQ_URL", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "FLOCK_GROQ_MODEL", "llama-3.3-70b-versatile"),
    "nvidia": ("FLOCK_NVIDIA_URL", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", "FLOCK_NVIDIA_MODEL",
               "meta/llama-3.3-70b-instruct"),
}
PROVIDERS = ("local", "groq", "nvidia", "anthropic", "offline")
ROLE_ENV = {"reviewer": "FLOCK_REVIEWER_PROVIDER", "writer": "FLOCK_WRITER_PROVIDER", "mapper": "FLOCK_MAPPER_PROVIDER"}

_client_lock = threading.Lock()
_client_obj = None


def model() -> str:
    return os.environ.get("FLOCK_MODEL") or DEFAULT_MODEL


def timeout() -> float:
    try:
        return float(os.environ.get("FLOCK_LLM_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",")]


def _pick(items: list[str], index: int, repeat_last: bool = True) -> str:
    """Entry `index` of a comma list. One entry applies to everyone ("local" = every judge local); past the end
    of a longer list, providers repeat the last entry, models fall back to the provider's default."""
    if index < len(items):
        return items[index]
    return items[-1] if items and (repeat_last or len(items) == 1) else ""


def default_model(provider: str) -> str:
    if provider == "anthropic":
        return model()
    if provider in OPENAI_COMPATIBLE:
        _, _, _, model_env, fallback = OPENAI_COMPATIBLE[provider]
        return os.environ.get(model_env) or fallback
    return ""


def route(role: str = "default", index: int = 0) -> tuple[str, str]:
    """(provider, model) for a role: 'judge' (index = which judge), 'reviewer', 'writer', 'mapper'."""
    fallback = (os.environ.get("FLOCK_PROVIDER") or "anthropic").strip().lower()
    override = ""
    if role == "judge":
        provider = _pick(_env_list("FLOCK_JUDGE_PROVIDERS"), index).lower() or fallback
        override = _pick(_env_list("FLOCK_JUDGE_MODELS"), index, repeat_last=False)
    elif role in ROLE_ENV:
        provider = (os.environ.get(ROLE_ENV[role]) or "").strip().lower() or ("anthropic" if role == "reviewer" else fallback)
        override = (os.environ.get(f"FLOCK_{role.upper()}_MODEL") or "").strip()
    else:
        provider = fallback
    return provider, override or default_model(provider)


def label(r: tuple[str, str]) -> str:
    return f"{r[0]}/{r[1]}" if r[1] else r[0]


def available(r: tuple[str, str] | None = None) -> bool:
    """Would a call on this route reach a model? False offline, or when the provider's key is missing."""
    provider = (r or route())[0]
    if os.environ.get("FLOCK_OFFLINE") == "1" or provider == "offline":
        return False
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if provider == "local":
        return True  # chosen on purpose; no key needed. If it's unreachable, the call fails and says so.
    if provider in OPENAI_COMPATIBLE:
        return bool(os.environ.get(OPENAI_COMPATIBLE[provider][2]))
    return True  # unknown name: let the call fail loudly so the audit trail records the typo


def _client():
    """One shared Anthropic client (it is thread-safe), so parallel judges reuse the connection pool."""
    global _client_obj
    with _client_lock:
        if _client_obj is None:
            import anthropic

            _client_obj = anthropic.Anthropic(timeout=timeout(), max_retries=1)
        return _client_obj


def base_url(provider: str) -> str:
    url_env, url_default, _, _, _ = OPENAI_COMPATIBLE[provider]
    return (os.environ.get(url_env) or url_default).rstrip("/")


def _openai_chat(provider: str, model_id: str, system: str, user: str, max_tokens: int) -> tuple[str, str]:
    """POST {base}/chat/completions with plain urllib. Works for vLLM, Ollama, Groq and build.nvidia.com."""
    key_env = OPENAI_COMPATIBLE[provider][2]
    body = json.dumps({
        "model": model_id, "max_tokens": max_tokens, "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if os.environ.get(key_env):
        headers["Authorization"] = f"Bearer {os.environ[key_env]}"
    req = urllib.request.Request(base_url(provider) + "/chat/completions", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout()) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:120] if e.fp else ""
        raise RuntimeError(f"HTTP {e.code} from {provider}: {detail}") from None
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    return (msg.get("content") or "").strip(), str(data.get("model") or model_id)


def complete(system: str, user: str, max_tokens: int, r: tuple[str, str] | None = None) -> tuple[str, str]:
    """One raw call: (reply text, the model id the server says answered). Raises on any failure."""
    provider, model_id = r or route()
    if provider in OPENAI_COMPATIBLE:
        return _openai_chat(provider, model_id, system, user, max_tokens)
    if provider != "anthropic":
        raise ValueError(f"unknown provider {provider!r} (use one of {', '.join(PROVIDERS)})")
    msg = _client().messages.create(
        model=model_id, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    return text, str(getattr(msg, "model", None) or model_id)


def _create(system: str, user: str, max_tokens: int, r: tuple[str, str] | None = None) -> str:
    return complete(system, user, max_tokens, r)[0]


def parse_json_object(text: str | None) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply. Tolerates code fences, chatter and <think> blocks."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # reasoning models (DeepSeek-R1, Qwen3) think out loud
    candidates = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)] + [text]
    decoder = json.JSONDecoder()
    for chunk in candidates:
        chunk = chunk.strip()
        for start in [i for i, ch in enumerate(chunk) if ch == "{"]:
            try:
                obj, _ = decoder.raw_decode(chunk, start)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def call_json(system: str, user: str, max_tokens: int = 600,
              r: tuple[str, str] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Like ask_json, but also says WHY it failed, so callers can record it in the audit trail.

    r is a (provider, model) route from route(); None means the default provider.
    Returns (obj, None) on success, (None, None) when offline on purpose, (None, "error") on a real failure.
    """
    r = r or route()
    if not available(r):
        return None, None
    try:
        text = _create(system + "\nRespond with a single JSON object and nothing else.", user, max_tokens, r)
    except Exception as e:  # network, auth, timeout
        err = f"{type(e).__name__}: {e}"[:200]
        print(f"[llm:{label(r)}] falling back to offline rules: {err}", file=sys.stderr)
        return None, err
    obj = parse_json_object(text)
    if obj is None:
        err = f"unparseable reply: {text[:80]!r}"
        print(f"[llm:{label(r)}] falling back to offline rules: {err}", file=sys.stderr)
        return None, err
    return obj, None


def ask_json(system: str, user: str, max_tokens: int = 600, r: tuple[str, str] | None = None) -> dict[str, Any] | None:
    """Ask for a JSON object back. Returns None on any failure so callers can fall back."""
    return call_json(system, user, max_tokens, r)[0]


def ask_text(system: str, user: str, max_tokens: int = 800, r: tuple[str, str] | None = None) -> str | None:
    r = r or route()
    if not available(r):
        return None
    try:
        text = _create(system, user, max_tokens, r)
    except Exception as e:
        print(f"[llm:{label(r)}] falling back to template: {type(e).__name__}: {e}"[:200], file=sys.stderr)
        return None
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip() or None
