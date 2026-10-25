"""Client login: hashed passwords only, 12h sessions in the state dir, roles, logout, light rate limiting."""
import json
import time

import pytest

from flock import auth


@pytest.fixture
def users(monkeypatch):
    rows = [{"username": "noahs-finance", "password_hash": auth.hash_password("pw-one", iterations=1000),
             "name": "Preet Karia", "role": "reviewer", "org": "Ringtide"},
            {"username": "viewer", "password_hash": auth.hash_password("pw-two", iterations=1000),
             "name": "Vi Ewer", "role": "viewer", "org": "Ringtide"}]
    monkeypatch.setenv("FLOCK_USERS", json.dumps(rows))
    auth._fails.clear()
    return rows


def test_hash_roundtrip():
    h = auth.hash_password("secret", iterations=1000)
    assert h.startswith("pbkdf2_sha256$1000$") and "secret" not in h
    assert auth.verify_password("secret", h) and not auth.verify_password("nope", h)
    assert not auth.verify_password("secret", "garbage")


def test_login_session_and_logout(tmp_path, users):
    r = auth.login("Noahs-Finance", "pw-one", tmp_path)
    assert r["user"] == {"username": "noahs-finance", "name": "Preet Karia", "role": "reviewer", "org": "Ringtide"}
    assert r["token"] not in (tmp_path / auth.SESSIONS).read_text(), "only the token's hash is stored"
    me = auth.user_for(r["token"], tmp_path)
    assert me["name"] == "Preet Karia" and auth.can_approve(me)
    assert auth.logout(r["token"], tmp_path) and auth.user_for(r["token"], tmp_path) is None
    assert auth.user_for(None, tmp_path) is None and auth.user_for("bogus", tmp_path) is None


def test_viewer_cannot_approve(tmp_path, users):
    r = auth.login("viewer", "pw-two", tmp_path)
    assert not auth.can_approve(auth.user_for(r["token"], tmp_path))


def test_wrong_password_and_rate_limit(tmp_path, users):
    for _ in range(auth.MAX_FAILS):
        with pytest.raises(auth.LoginError):
            auth.login("noahs-finance", "wrong", tmp_path)
    with pytest.raises(auth.RateLimited):
        auth.login("noahs-finance", "pw-one", tmp_path)
    with pytest.raises(auth.LoginError):
        auth.login("nobody", "x", tmp_path)


def test_session_expires(tmp_path, users, monkeypatch):
    r = auth.login("noahs-finance", "pw-one", tmp_path)
    real = time.time
    monkeypatch.setattr(auth.time, "time", lambda: real() + auth.TTL_SECONDS + 1)
    assert auth.user_for(r["token"], tmp_path) is None


def test_seeded_demo_user_file_is_hashed(monkeypatch):
    monkeypatch.delenv("FLOCK_USERS", raising=False)
    rows = auth.users()
    demo = next(u for u in rows if u["username"] == "noahs-finance")
    assert demo["role"] == "reviewer" and demo["name"] == "Preet Karia"
    assert demo["password_hash"].startswith("pbkdf2_sha256$")
    assert "password" not in demo
