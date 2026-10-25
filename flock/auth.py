"""Client login for the restaurant's finance team: who is approving, without pasting an API token.

Users come from FLOCK_USERS (a JSON list) or, if unset, data/users.example.json (the seeded demo reviewer). Each user:
  {"username": "...", "password_hash": "pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>", "name": "...",
   "role": "owner" | "reviewer" | "viewer", "org": "..."}
Passwords are stored hashed only (hashlib.pbkdf2_hmac). Make a hash: python -m flock.auth hash '<password>'.

login() hands out a random session token kept in <state_dir>/sessions.json for 12 hours. A reviewer's session may
approve and reject; the audit log then names the session user, never a name the browser sent. Failed logins are
rate-limited in memory (per username: 5 failures in 5 minutes locks it for the rest of that window).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

from flock.store import ROOT

USERS_FILE = ROOT / "data" / "users.example.json"
SESSIONS = "sessions.json"
TTL_SECONDS = 12 * 3600
ITERATIONS = 200_000
MAX_FAILS, FAIL_WINDOW = 5, 300
ROLES = ("owner", "reviewer", "viewer")

_lock = threading.Lock()
_fails: dict[str, list[float]] = {}


class LoginError(Exception):
    """Wrong username or password (the message never says which)."""


class RateLimited(LoginError):
    pass


def hash_password(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


def users() -> list[dict[str, Any]]:
    raw = os.environ.get("FLOCK_USERS")
    if raw:
        rows = json.loads(raw)
    elif USERS_FILE.exists():
        rows = json.loads(USERS_FILE.read_text())
    else:
        rows = []
    return [u for u in rows if u.get("username") and u.get("password_hash") and u.get("role") in ROLES]


def public(u: dict[str, Any]) -> dict[str, Any]:
    """A user without the password hash (safe to return to the browser)."""
    return {k: u.get(k) for k in ("username", "name", "role", "org")}


def _sessions_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / SESSIONS


def _load(state_dir: Path | str) -> dict[str, dict[str, Any]]:
    try:
        return json.loads(_sessions_path(state_dir).read_text())
    except (OSError, ValueError):
        return {}


def _save(state_dir: Path | str, data: dict[str, dict[str, Any]]) -> None:
    p = _sessions_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)


def _key(token: str) -> str:
    """Sessions are stored by the token's sha256, so the file alone can't be replayed."""
    return hashlib.sha256(token.encode()).hexdigest()


def _limited(username: str, now: float) -> bool:
    recent = [t for t in _fails.get(username, []) if now - t < FAIL_WINDOW]
    _fails[username] = recent
    return len(recent) >= MAX_FAILS


def login(username: str, password: str, state_dir: Path | str) -> dict[str, Any]:
    """{token, user, expires_at} for a correct username + password. Raises LoginError / RateLimited."""
    username = (username or "").strip().lower()
    now = time.time()
    with _lock:
        if _limited(username, now):
            raise RateLimited("too many failed logins; wait a few minutes")
    u = next((u for u in users() if u["username"].lower() == username), None)
    if u is None or not verify_password(password or "", u["password_hash"]):
        with _lock:
            _fails.setdefault(username, []).append(now)
        raise LoginError("wrong username or password")
    token = secrets.token_urlsafe(32)
    with _lock:
        _fails.pop(username, None)
        data = {k: v for k, v in _load(state_dir).items() if v.get("expires", 0) > now}
        data[_key(token)] = {"username": u["username"], "created": now, "expires": now + TTL_SECONDS}
        _save(state_dir, data)
    return {"token": token, "user": public(u), "expires_at": int(now + TTL_SECONDS)}


def user_for(token: str | None, state_dir: Path | str) -> dict[str, Any] | None:
    """The logged-in user (public fields) for a session token, or None (unknown, expired, user removed)."""
    if not token:
        return None
    s = _load(state_dir).get(_key(token))
    if not s or s.get("expires", 0) <= time.time():
        return None
    u = next((u for u in users() if u["username"] == s["username"]), None)
    return public(u) if u else None


def logout(token: str | None, state_dir: Path | str) -> bool:
    if not token:
        return False
    with _lock:
        data = _load(state_dir)
        gone = data.pop(_key(token), None) is not None
        if gone:
            _save(state_dir, data)
    return gone


def can_approve(user: dict[str, Any] | None) -> bool:
    return bool(user) and user.get("role") in ("owner", "reviewer")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "hash":
        print(hash_password(sys.argv[2]))
    else:
        sys.exit("usage: python -m flock.auth hash '<password>'")
