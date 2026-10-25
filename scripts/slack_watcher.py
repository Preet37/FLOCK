"""Slack -> Flock connector (fallback for the CREAO Slack connector; same payload, same door).

Reads one Slack channel with a bot token and hands every new message to Flock as a connector payload
(docs/connectors.md, version 1). Flock itself never calls Slack; this script is the outside connector, the same
role the CREAO Slack watcher plays. The payload's `connector` field says which one delivered it, so the audit log
never claims a CREAO run that did not happen.

  export SLACK_BOT_TOKEN=xoxb-...          # Slack app, scopes: channels:history channels:read users:read users:read.email
  python scripts/slack_watcher.py --channel doordash-partnership                  # once: new messages -> data/inbox/connectors/
  python scripts/slack_watcher.py --channel doordash-partnership --post http://127.0.0.1:8787   # ... -> POST /api/connectors
  python scripts/slack_watcher.py --channel doordash-partnership --post https://web-production-812b4.up.railway.app --token "$FLOCK_API_TOKEN"
  python scripts/slack_watcher.py --channel doordash-partnership --watch 10 --post http://127.0.0.1:8787   # every 10 s

State: data/state/slack_watcher.json remembers the last ts per channel, so a message is delivered once.
Needs the bot invited to the channel (/invite @Flock Watcher) and users:read.email so authority can be matched.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from flock import connectors  # noqa: E402

STATE = ROOT / "data" / "state" / "slack_watcher.json"
SLACK = "https://slack.com/api/"


def slack(method: str, token: str, **params) -> dict:
    url = SLACK + method + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "flock-slack-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if not data.get("ok"):
        raise SystemExit(f"Slack {method} failed: {data.get('error')}")
    return data


def channel_id(token: str, name: str) -> tuple[str, bool]:
    name = name.lstrip("#")
    cursor = None
    while True:
        data = slack("conversations.list", token, types="public_channel,private_channel", limit=200,
                     **({"cursor": cursor} if cursor else {}))
        for c in data["channels"]:
            if c["name"] == name:
                return c["id"], bool(c.get("is_ext_shared"))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            raise SystemExit(f"no channel named #{name} visible to this token (invite the bot to it)")


_users: dict[str, dict] = {}


def user(token: str, uid: str) -> dict:
    if uid not in _users:
        p = slack("users.info", token, user=uid)["user"]
        _users[uid] = {"id": uid, "team_id": p.get("team_id", ""), "name": p.get("name", uid),
                       "real_name": p.get("real_name") or p.get("profile", {}).get("real_name", ""),
                       "email": p.get("profile", {}).get("email", "")}
    return _users[uid]


def new_messages(token: str, cid: str, since: str) -> list[dict]:
    out, cursor = [], None
    while True:
        data = slack("conversations.history", token, channel=cid, oldest=since, limit=200, inclusive="false",
                     **({"cursor": cursor} if cursor else {}))
        out += [m for m in data["messages"] if m.get("type") == "message" and not m.get("subtype") and m.get("user")]
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not data.get("has_more") or not cursor:
            break
    return sorted(out, key=lambda m: float(m["ts"]))


def payload(token: str, name: str, shared: bool, msgs: list[dict], connector: str) -> dict:
    items = []
    for m in msgs:
        it = {"channel": name.lstrip("#"), "shared": shared, "user": user(token, m["user"]), "ts": m["ts"], "text": m["text"]}
        if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
            it["thread_ts"] = m["thread_ts"]
        if m.get("edited"):
            it["edited"] = m["edited"].get("ts", True)
        if m.get("reactions"):
            it["reactions"] = m["reactions"]
        items.append(it)
    return {"flock_connector": connectors.VERSION, "source": "slack", "connector": connector, "simulated": True, "items": items}


def deliver(p: dict, post: str | None, api_token: str) -> str:
    if post:
        req = urllib.request.Request(post.rstrip("/") + "/api/connectors", data=json.dumps(p).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "flock-slack-watcher/1.0",
                                              **({"Authorization": f"Bearer {api_token}"} if api_token else {})})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
        claims = body.get("claims", [])
        return f"POST {post} -> {len(claims)} claim(s): " + "; ".join(
            f"{c.get('term_type')} {c.get('store_id')} {c.get('value')} [{c.get('authority') or 'unknown'}{', binding' if c.get('binding') else ''}]"
            for c in claims)
    drop = connectors.DROP
    drop.mkdir(parents=True, exist_ok=True)
    tmp = drop / f"slack_{int(time.time())}.json.tmp"
    tmp.write_text(json.dumps(p, indent=2))
    final = tmp.with_suffix("")
    tmp.rename(final)
    return f"wrote {final.relative_to(ROOT)} (run: python -m flock.connectors ingest)"


def run_once(token: str, name: str, post: str | None, api_token: str, connector: str, state: dict) -> int:
    cid, shared = channel_id(token, name)
    since = state.get(name, "0")
    msgs = new_messages(token, cid, since)
    if not msgs:
        return 0
    p = payload(token, name, shared, msgs, connector)
    problems = connectors.validate(p)
    if problems:
        raise SystemExit("payload invalid: " + "; ".join(problems))
    print(f"#{name}: {len(msgs)} new message(s) -> {deliver(p, post, api_token)}", flush=True)
    state[name] = msgs[-1]["ts"]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))
    return len(msgs)


def main() -> None:
    ap = argparse.ArgumentParser(description="Slack channel -> Flock connector payload")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--post", help="Flock API base URL; default: write to the drop folder")
    ap.add_argument("--token", default=os.environ.get("FLOCK_API_TOKEN", ""), help="bearer token for a hosted API")
    ap.add_argument("--watch", type=float, default=0, help="poll every N seconds (0 = once)")
    ap.add_argument("--from-start", action="store_true", help="ignore the saved cursor and resend the whole channel")
    ap.add_argument("--connector", default="script:slack-watcher", help='who delivered it (never claim "creao:..." here)')
    a = ap.parse_args()
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token.startswith("xox"):
        raise SystemExit("set SLACK_BOT_TOKEN (a Slack app bot token, xoxb-...)")
    state = json.loads(STATE.read_text()) if STATE.exists() and not a.from_start else {}
    if a.from_start:
        state.pop(a.channel.lstrip("#"), None)
    while True:
        try:
            n = run_once(token, a.channel.lstrip("#"), a.post, a.token, a.connector, state)
            if not a.watch:
                print("nothing new" if not n else "done")
                break
        except SystemExit:
            raise
        except Exception as e:  # keep watching through a flaky network
            print(f"watcher: {e}", file=sys.stderr, flush=True)
            if not a.watch:
                raise
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
