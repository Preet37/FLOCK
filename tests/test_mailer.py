"""Mailer: outbox by default, SMTP / Resend by env, demo redirect, never crashes an approval, no secrets kept."""
import json

from flock import mailer

ENV = ("FLOCK_SMTP_HOST", "FLOCK_SMTP_PORT", "FLOCK_SMTP_USER", "FLOCK_SMTP_PASSWORD", "RESEND_API_KEY",
       "FLOCK_MAIL_FROM", "FLOCK_VENDOR_EMAIL_TO")


def _clean(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


def test_no_backend_goes_to_outbox(tmp_path, monkeypatch):
    _clean(monkeypatch)
    r = mailer.send("a@vendor.example", "Hi", "body", state_dir=tmp_path)
    assert r["status"] == "outbox" and r["via"] == "outbox" and r["to"] == "a@vendor.example"
    assert mailer.outbox(tmp_path)[0]["id"] == r["id"]
    assert mailer.outbox(tmp_path / "nothing") == []


def test_redirect_notes_the_intended_vendor(tmp_path, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("FLOCK_VENDOR_EMAIL_TO", "presenter@example.com")
    r = mailer.send("a@vendor.example", "Hi", "body", state_dir=tmp_path)
    assert r["to"] == "presenter@example.com" and r["intended_to"] == "a@vendor.example"
    assert "a@vendor.example" in r["body"]


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append((self.host, self.port, msg["To"], msg["Subject"]))


def test_smtp_sends_and_keeps_no_password(tmp_path, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("FLOCK_SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("FLOCK_SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("FLOCK_SMTP_PASSWORD", "s3cret-app-pw")
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent.clear()
    r = mailer.send("x@y.example", "Subj", "Body", state_dir=tmp_path)
    assert r["status"] == "sent" and r["via"] == "smtp" and r["from"] == "me@gmail.com"
    assert FakeSMTP.sent == [("smtp.gmail.com", 587, "x@y.example", "Subj")]
    assert "s3cret-app-pw" not in (tmp_path / mailer.OUTBOX).read_text()


def test_smtp_failure_falls_back_to_outbox(tmp_path, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("FLOCK_SMTP_HOST", "smtp.gmail.com")

    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(mailer.smtplib, "SMTP", boom)
    r = mailer.send("x@y.example", "S", "B", state_dir=tmp_path)
    assert r["status"] == "outbox" and "network down" in r["error"]


def test_resend(tmp_path, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"id": "abc"}'

    def fake_urlopen(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data)
        return Resp()

    monkeypatch.setattr(mailer.urllib.request, "urlopen", fake_urlopen)
    r = mailer.send("x@y.example", "S", "B", state_dir=tmp_path)
    assert r["status"] == "sent" and r["via"] == "resend" and r["provider_id"] == "abc"
    assert seen["auth"] == "Bearer re_test_key" and seen["body"]["to"] == ["x@y.example"]
    assert "re_test_key" not in (tmp_path / mailer.OUTBOX).read_text()


def test_send_follow_up_uses_the_platform_address(tmp_path, monkeypatch):
    _clean(monkeypatch)
    f = {"finding_id": "F-1", "channel": "doordash", "gap": -549.33, "portal_ref": "SBX-1",
         "follow_up": {"claim_ref": "SBX-1", "amount": 549.33, "draft": "Please credit $549.33."}}
    r = mailer.send_follow_up(f, state_dir=tmp_path)
    assert r["to"] == mailer.VENDOR_EMAIL["doordash"] and "SBX-1" in r["subject"] and "$549.33" in r["subject"]
    assert r["finding_id"] == "F-1" and r["body"] == "Please credit $549.33."
