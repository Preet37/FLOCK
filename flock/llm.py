"""Thin wrapper around Claude. If there's no ANTHROPIC_API_KEY, callers fall back to offline rules,
so the whole pipeline still runs on a laptop with no key (useful for tests and the demo backup).

Rule: never ask the model to do money math. Give it numbers already computed in Python.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

MODEL = os.environ.get("FLOCK_MODEL", "claude-sonnet-5-5")


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("FLOCK_OFFLINE") != "1"


def ask_json(system: str, user: str, max_tokens: int = 600) -> dict[str, Any] | None:
    """Ask for a JSON object back. Returns None on any failure so callers can fall back."""
    if not available():
        return None
    try:
        import anthropic

        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system + "\nRespond with a single JSON object and nothing else.",
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        match = re.search(r"\{.*\}", text, re.S)
        return json.loads(match.group(0)) if match else None
    except Exception as e:  # network, auth, parse: fall back quietly but say so
        print(f"[llm] falling back to offline rules: {e}")
        return None


def ask_text(system: str, user: str, max_tokens: int = 800) -> str | None:
    if not available():
        return None
    try:
        import anthropic

        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        print(f"[llm] falling back to template: {e}")
        return None
