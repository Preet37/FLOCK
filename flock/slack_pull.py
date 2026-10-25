"""Pull deal messages and invoices straight from a Slack workspace (no CREAO in between).

  SLACK_BOT_TOKEN        xoxb-... bot token; scopes channels:history, channels:read, users:read, users:read.email,
                         files:read. The bot must be a member of each channel (/invite @bot).
  FLOCK_SLACK_CHANNELS   comma list of channel names or IDs, e.g. "doordash-partnership,ubereats-partnership"

pull(state_dir) reads every new message since the last pull (cursor per channel in state_dir/slack_cursor.json),
including thread replies, looks up each poster's email and real name, and hands Flock a connector batch
(flock/connectors.py, contract version 1): source "slack", connector "slack-pull". The Deal Keeper decides authority
from the poster's email, exactly as for a CREAO-posted batch.

Files on a message (PDF / CSV invoices and statements) are downloaded with the bot token into
state_dir/inbox/slack_files/<channel>/ and reported as a note batch ("Invoice attached: <name>"); CSVs that look like an
invoice or statement are listed under "attachments" for the Collector.

  python -m flock.slack_pull [--since <unix ts>] [--state-dir data/state]

urllib only. The token is sent only in the Authorization header to slack.com and never logged or stored.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flock import connectors

API = "https://slack.com/api/"
CURSOR = "slack_cursor.json"
FILES_DIR = Path("inbox") / "slack_files"
CONNECTOR = "slack-pull"
MAX_RETRIES = 4
INVOICE_WORDS = ("invoice", "statement", "payout", "settlement", "amount", "total", "commission", "fee")
FILE_TYPES = {"pdf", "csv"}


class SlackError(Exception):
    pass


def _token() -> str:
    tok = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not tok:
        raise SlackError("SLACK_BOT_TOKEN is not set")
    return tok


def _open(req: urllib.request.Request, timeout: int = 30) -> bytes:
    """urlopen with Slack's rate limiting: on 429 wait Retry-After seconds and try again."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES:
                time.sleep(min(float(e.headers.get("Retry-After") or 1), 30))
                continue
            raise SlackError(f"{urllib.parse.urlparse(req.full_url).path}: HTTP {e.code}") from None
    raise SlackError("rate limited by Slack")


def call(method: str, **params: Any) -> dict[str, Any]:
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(f"{API}{method}?{q}", headers={"Authorization": f"Bearer {_token()}"})
    data = json.loads(_open(req) or b"{}")
    if not data.get("ok"):
        raise SlackError(f"{method}: {data.get('error', 'unknown error')}")
    return data


def _paged(method: str, key: str, **params: Any) -> list[dict[str, Any]]:
    out, cursor = [], None
    while True:
        data = call(method, cursor=cursor, limit=200, **params)
        out.extend(data.get(key) or [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return out


def resolve_channels(wanted: list[str]) -> list[dict[str, str]]:
    """[{id, name}] for each wanted name or ID (public channels the bot can see)."""
    chans = _paged("conversations.list", "channels", types="public_channel", exclude_archived="true")
    by_name = {c["name"]: c for c in chans}
    by_id = {c["id"]: c for c in chans}
    out = []
    for w in wanted:
        w = w.strip().lstrip("#")
        c = by_id.get(w) or by_name.get(w)
        if c is None:
            raise SlackError(f"channel {w!r} not found (is the bot invited, and is it public?)")
        out.append({"id": c["id"], "name": c["name"]})
    return out


def _load_cursor(state_dir: Path) -> dict[str, str]:
    try:
        return json.loads((state_dir / CURSOR).read_text())
    except (OSError, ValueError):
        return {}


def _save_cursor(state_dir: Path, cur: dict[str, str]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / CURSOR).write_text(json.dumps(cur, indent=2))


def _user(uid: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if uid not in cache:
        try:
            u = call("users.info", user=uid)["user"]
            prof = u.get("profile") or {}
            cache[uid] = {"id": uid, "name": u.get("name") or uid, "real_name": prof.get("real_name") or u.get("real_name"),
                          "email": prof.get("email"), "title": prof.get("title") or None}
        except SlackError:
            cache[uid] = {"id": uid, "name": uid}
    return {k: v for k, v in cache[uid].items() if v}


def _messages(chan_id: str, oldest: str | None) -> list[dict[str, Any]]:
    """New top-level messages and their new thread replies, oldest first."""
    top = _paged("conversations.history", "messages", channel=chan_id, oldest=oldest, inclusive="false")
    out = []
    for m in top:
        out.append(m)
        if m.get("reply_count") and m.get("thread_ts") == m.get("ts"):
            for r in _paged("conversations.replies", "messages", channel=chan_id, ts=m["ts"]):
                if r["ts"] != m["ts"] and (oldest is None or float(r["ts"]) > float(oldest)):
                    out.append(r)
    return sorted(out, key=lambda m: float(m["ts"]))


def looks_like_invoice(path: Path) -> bool:
    try:
        head = path.read_text(errors="ignore")[:4000].lower()
    except OSError:
        return False
    return "," in head and sum(w in head for w in INVOICE_WORDS) >= 2


def _download(f: dict[str, Any], dest_dir: Path) -> Path | None:
    url = f.get("url_private_download") or f.get("url_private")
    if not url or urllib.parse.urlparse(url).hostname not in ("files.slack.com", "slack-files.com"):
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", f.get("name") or f.get("id") or "file")
    dest = dest_dir / f"{f.get('id', 'F')}_{name}"
    if not dest.exists():
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_token()}"})
        dest.write_bytes(_open(req, timeout=60))
    return dest


def _day(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).date().isoformat()


def pull(state_dir: Path | str, since: str | float | None = None, channels: list[str] | None = None) -> dict[str, Any]:
    """Fetch new messages from FLOCK_SLACK_CHANNELS (or channels), send them to connectors.receive, advance the cursor.
    since: ignore the cursor and start from this unix ts. Returns {channels, messages, slack, notes, attachments}."""
    state_dir = Path(state_dir)
    wanted = channels or [c for c in os.environ.get("FLOCK_SLACK_CHANNELS", "").split(",") if c.strip()]
    if not wanted:
        raise SlackError("FLOCK_SLACK_CHANNELS is not set (comma list of channel names or IDs)")
    cursor = _load_cursor(state_dir)
    users: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    downloaded: list[str] = []
    per_channel = []
    simulated = True
    for ch in resolve_channels(wanted):
        oldest = str(since) if since is not None else cursor.get(ch["id"])
        msgs = _messages(ch["id"], oldest)
        simulated = simulated and "sim" in ch["name"]
        for m in msgs:
            if m.get("subtype") in ("channel_join", "channel_leave", "bot_add", "channel_topic", "channel_purpose"):
                continue
            uid = m.get("user") or m.get("bot_id") or "unknown"
            poster = _user(uid, users) if m.get("user") else {"id": uid, "name": m.get("username") or uid}
            if (m.get("text") or "").strip():
                it = {"channel": ch["name"], "user": poster, "ts": m["ts"], "text": m["text"]}
                if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
                    it["thread_ts"] = m["thread_ts"]
                if m.get("edited"):
                    it["edited"] = m["edited"].get("ts") or True
                if m.get("reactions"):
                    it["reactions"] = m["reactions"]
                items.append(it)
            for f in m.get("files") or []:
                if (f.get("filetype") or "").lower() not in FILE_TYPES:
                    continue
                try:
                    path = _download(f, state_dir / FILES_DIR / ch["name"])
                except SlackError as e:
                    print(f"[slack_pull] could not download {f.get('name')}: {e}", file=sys.stderr)
                    path = None
                notes.append({"title": f"Invoice attached: {f.get('name') or f.get('id')}", "date": _day(m["ts"]),
                              "body": (f"Posted in #{ch['name']} by {poster.get('real_name') or poster['name']} "
                                       f"({poster.get('email') or 'no email'}) at ts {m['ts']}.\n\n"
                                       + (f"Saved to {path}." if path else "Not downloaded.")),
                              "channel": next((c for c in ("doordash", "ubereats", "grubhub") if c in ch["name"]), None)})
                if path:
                    downloaded.append(str(path))
                    if path.suffix.lower() == ".csv" and looks_like_invoice(path):
                        attachments.append({"path": str(path), "channel": ch["name"], "ts": m["ts"],
                                            "name": f.get("name")})
        if msgs:
            cursor[ch["id"]] = max((m["ts"] for m in msgs), key=float)
        per_channel.append({"id": ch["id"], "name": ch["name"], "messages": len(msgs)})
    result: dict[str, Any] = {"channels": per_channel, "messages": len(items), "files": downloaded,
                              "attachments": attachments, "slack": None, "notes": None}
    if items:
        result["slack"] = connectors.receive({"flock_connector": connectors.VERSION, "source": "slack",
                                              "connector": CONNECTOR, "simulated": simulated, "items": items},
                                             state_dir)
    if notes:
        for n in notes:
            if n["channel"] is None:
                n.pop("channel")
        result["notes"] = connectors.receive({"flock_connector": connectors.VERSION, "source": "note",
                                              "connector": CONNECTOR, "simulated": simulated, "items": notes},
                                             state_dir)
    _save_cursor(state_dir, cursor)   # only after Flock accepted the batches: a failed receive re-pulls next time
    return result


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m flock.slack_pull", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="unix ts to start from (ignores the saved cursor)")
    ap.add_argument("--state-dir", default=os.environ.get("FLOCK_STATE_DIR") or "data/state")
    a = ap.parse_args(argv)
    try:
        r = pull(Path(a.state_dir), since=a.since)
    except (SlackError, connectors.PayloadError) as e:
        sys.exit(f"slack_pull: {e}")
    print(json.dumps({k: r[k] for k in ("channels", "messages", "files", "attachments")}, indent=2))
    if r["slack"]:
        for c in r["slack"]["claims"]:
            print(f"  claim {c.get('term_type')} {c.get('store_id')} = {c.get('value')}  {c.get('authority')}  "
                  f"binding={c.get('binding')}  \"{c.get('quote')}\"")


if __name__ == "__main__":
    main()
