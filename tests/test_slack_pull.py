"""Slack pull: Web API (mocked) -> connector batches the Deal Keeper reads; cursor, threads, invoices, 429 retry."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from flock import connectors, slack_pull

TOKEN = "xoxb-test-secret"
CSV = b"invoice_id,store,amount,commission,total\nINV-1,S06,100.00,25.00,75.00\n"


def contract(d: Path) -> Path:
    c = d / "contracts"
    c.mkdir(parents=True)
    (c / "ubereats_contract.json").write_text(json.dumps({
        "doc_type": "contract", "channel": "ubereats", "effective_from": "2026-01-01",
        "authorized_contacts": ["sam.takahashi@ubereats.example"],
        "terms": [{"term_type": "fee", "store_id": "all", "value": 0.75, "quote": "Marketing Fee: $0.75 per order"}]}))
    return c


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSlack:
    def __init__(self):
        self.calls, self.auth, self.limited = [], set(), 1
        self.history = [
            {"ts": "1768907280.000011", "user": "U1", "thread_ts": "1768907280.000011", "reply_count": 1,
             "text": "Heads up: we'll waive the Marketing Fee ($0.75/order) for Ringtide Bagels Temescal (S06) on "
                     "Saturday, January 24 while the street is closed."},
            {"ts": "1768910000.000013", "user": "U2", "text": "",
             "files": [{"id": "F1", "name": "statement.csv", "filetype": "csv",
                        "url_private": "https://files.slack.com/files-pri/T/statement.csv"},
                       {"id": "F2", "name": "logo.png", "filetype": "png",
                        "url_private": "https://files.slack.com/files-pri/T/logo.png"}]},
            {"ts": "1768900000.000001", "subtype": "channel_join", "user": "U2", "text": "joined"},
        ]
        self.replies = [self.history[0],
                        {"ts": "1768909380.000012", "user": "U1", "thread_ts": "1768907280.000011",
                         "text": "Correction: we'll waive the Marketing Fee ($0.75/order) for Ringtide Bagels Temescal "
                                 "(S06) on Sunday, January 25, not Saturday. Saturday is billed as normal."}]

    def __call__(self, req, timeout=None):
        url = urllib.parse.urlparse(req.full_url)
        q = dict(urllib.parse.parse_qsl(url.query))
        self.auth.add(req.get_header("Authorization"))
        self.calls.append((url.path, q))
        if url.hostname == "files.slack.com":
            return Resp(CSV)
        method = url.path.rsplit("/", 1)[-1]
        if method == "conversations.list" and self.limited:
            self.limited -= 1
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "0"}, None)
        oldest = float(q.get("oldest") or 0)
        u = q.get("user", "U1")
        body = {
            "conversations.list": {"channels": [{"id": "C1", "name": "ubereats-partnership"}]},
            "conversations.history": {"messages": [m for m in self.history if float(m["ts"]) > oldest]},
            "conversations.replies": {"messages": self.replies},
            "users.info": {"user": {"name": {"U1": "sam.takahashi", "U2": "avery.quinn"}[u],
                                    "profile": {"real_name": "Sam", "email": {
                                        "U1": "sam.takahashi@ubereats.example",
                                        "U2": "avery.quinn@ringtide-bagels.example"}[u]}}},
        }[method]
        return Resp(json.dumps({"ok": True, **body}).encode())


@pytest.fixture
def slack(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("FLOCK_SLACK_CHANNELS", "#ubereats-partnership")
    monkeypatch.setattr(slack_pull.time, "sleep", lambda s: None)
    fake = FakeSlack()
    monkeypatch.setattr(slack_pull.urllib.request, "urlopen", fake)
    real = connectors.receive
    contracts = contract(tmp_path)
    monkeypatch.setattr(slack_pull.connectors, "receive",
                        lambda p, s: real(p, s, contracts=contracts, raw=tmp_path / "raw"))
    return fake


def test_pull_reads_deal_messages_threads_and_invoices(tmp_path, slack):
    state = tmp_path / "state"
    r = slack_pull.pull(state)
    assert r["channels"] == [{"id": "C1", "name": "ubereats-partnership", "messages": 4}]
    assert r["messages"] == 2, "two text messages (the file-only one becomes a note, joins are skipped)"
    claims = r["slack"]["claims"]
    assert len(claims) == 1 and claims[0]["authority"] == "auth_email" and claims[0]["binding"]
    assert "Sunday, January 25" in claims[0]["quote"] and r["slack"]["superseded"] == 1
    assert r["slack"]["connector"] == "slack-pull" and r["slack"]["simulated"] is False
    assert r["notes"]["items"] == 1 and len(r["files"]) == 1, "the PNG is not downloaded"
    saved = Path(r["files"][0])
    assert saved.read_bytes() == CSV and "slack_files/ubereats-partnership" in str(saved)
    assert r["attachments"][0]["name"] == "statement.csv"
    assert slack.auth == {f"Bearer {TOKEN}"}
    assert json.loads((state / slack_pull.CURSOR).read_text()) == {"C1": "1768910000.000013"}
    assert TOKEN not in "".join(p.read_text(errors="ignore") for p in state.rglob("*") if p.is_file()
                                and p.suffix in (".json", ".jsonl"))


def test_second_pull_only_fetches_new(tmp_path, slack):
    state = tmp_path / "state"
    slack_pull.pull(state)
    r = slack_pull.pull(state)
    assert r["messages"] == 0 and r["slack"] is None and r["notes"] is None
    hist = [q for p, q in slack.calls if p.endswith("conversations.history")]
    assert hist[-1]["oldest"] == "1768910000.000013"


def test_missing_config(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOCK_SLACK_CHANNELS", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    with pytest.raises(slack_pull.SlackError):
        slack_pull.pull(tmp_path)
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    with pytest.raises(slack_pull.SlackError, match="SLACK_BOT_TOKEN"):
        slack_pull.call("conversations.list")


def test_unknown_channel(tmp_path, slack, monkeypatch):
    monkeypatch.setenv("FLOCK_SLACK_CHANNELS", "nope")
    with pytest.raises(slack_pull.SlackError, match="not found"):
        slack_pull.pull(tmp_path)


def test_looks_like_invoice(tmp_path):
    p = tmp_path / "a.csv"
    p.write_bytes(CSV)
    assert slack_pull.looks_like_invoice(p)
    p.write_text("name,favorite_color\nsam,blue\n")
    assert not slack_pull.looks_like_invoice(p)
