"""Thin wrapper around Claude. If there's no ANTHROPIC_API_KEY, callers fall back to offline rules,
so the whole pipeline still runs on a laptop with no key (useful for tests and the demo backup).

Rule: never ask the model to do money math. Give it numbers already computed in Python.

Env:
  FLOCK_MODEL        model id (default claude-sonnet-5-5)
  FLOCK_LLM_TIMEOUT  seconds per request before we give up and fall back (default 30)
  FLOCK_OFFLINE=1    never call the API, even with a key
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from typing import Any

DEFAULT_MODEL = "claude-sonnet-5-5"
MODEL = os.environ.get("FLOCK_MODEL", DEFAULT_MODEL)  # kept for anyone importing it; model() is read per call

_client_lock = threading.Lock()
_client_obj = None


def model() -> str:
    return os.environ.get("FLOCK_MODEL") or DEFAULT_MODEL


def timeout() -> float:
    try:
        return float(os.environ.get("FLOCK_LLM_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("FLOCK_OFFLINE") != "1"


def _client():
    """One shared client (it is thread-safe), so parallel judges reuse the connection pool."""
    global _client_obj
    with _client_lock:
        if _client_obj is None:
            import anthropic

            _client_obj = anthropic.Anthropic(timeout=timeout(), max_retries=1)
        return _client_obj


def _create(system: str, user: str, max_tokens: int) -> str:
    msg = _client().messages.create(
        model=model(), max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def parse_json_object(text: str | None) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply. Tolerates code fences and chatter around it."""
    if not text:
        return None
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


def call_json(system: str, user: str, max_tokens: int = 600) -> tuple[dict[str, Any] | None, str | None]:
    """Like ask_json, but also says WHY it failed, so callers can record it in the audit trail.

    Returns (obj, None) on success, (None, None) when offline on purpose, (None, "error") on a real failure.
    """
    if not available():
        return None, None
    try:
        text = _create(system + "\nRespond with a single JSON object and nothing else.", user, max_tokens)
    except Exception as e:  # network, auth, timeout
        err = f"{type(e).__name__}: {e}"[:200]
        print(f"[llm] falling back to offline rules: {err}", file=sys.stderr)
        return None, err
    obj = parse_json_object(text)
    if obj is None:
        err = f"unparseable reply: {text[:80]!r}"
        print(f"[llm] falling back to offline rules: {err}", file=sys.stderr)
        return None, err
    return obj, None


def ask_json(system: str, user: str, max_tokens: int = 600) -> dict[str, Any] | None:
    """Ask for a JSON object back. Returns None on any failure so callers can fall back."""
    return call_json(system, user, max_tokens)[0]


def ask_text(system: str, user: str, max_tokens: int = 800) -> str | None:
    if not available():
        return None
    try:
        return _create(system, user, max_tokens) or None
    except Exception as e:
        print(f"[llm] falling back to template: {type(e).__name__}: {e}"[:200], file=sys.stderr)
        return None
