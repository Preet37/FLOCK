"""Priya Raman, the simulated DoorDash account manager, in the demo Slack workspace.

Two jobs, both honest about being simulated (the workspace is Ringtide Bagels Finance, the made-up brand):
  seed     post a short, real-sounding finance conversation between Priya and the Ringtide finance team into a
           channel, one message every few seconds, so there is unstructured deal talk for Flock to read
  respond  watch the channel and answer, as Priya, whatever a human posts, so nobody has to play the rep live.
           Replies come from the `writer` model role (flock/llm.py, any provider incl. a CREAO hosted agent);
           if no model is reachable, a short human-sounding holding reply is used instead.

  export SLACK_REP_TOKEN=xoxb-...     # the "Priya Raman" Slack app (bot user @priyaraman, scope chat:write + history)
  python scripts/slack_rep_bot.py seed --channel doordash-partnership
  python scripts/slack_rep_bot.py respond --channel doordash-partnership --every 8

Priya is on the DoorDash demo contract (`authorized_contacts`: @priyaraman), so a term she states is binding to the
Deal Keeper; the same words from anyone else go to a human. Only Priya's lines are posted by this script; the finance
team's lines in `seed` are posted as Priya quoting them? No: they are posted by Priya's bot too, prefixed with the
speaker's name, unless SLACK_BOT_TOKEN (the watcher app) is present, in which case nothing changes either: the
finance side is meant to be typed by real teammates. Use `seed --with-finance` to have the script post both sides.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SLACK = "https://slack.com/api/"
PERSONA = ("You are Priya Raman, Partner Account Manager at DoorDash, chatting in a shared Slack channel with the finance "
           "team of Ringtide Bagels, a 56-store bagel chain in California. You are friendly, concrete and brief, like a "
           "real account manager typing between meetings. One to three sentences. No bullet points, no headings, no "
           "sign-off. If they ask about a number you do not have, say you will check and come back. Never invent a new "
           "commission rate, fee waiver or promo split on your own; only confirm or explain terms. Everything here is a "
           "simulation for a hackathon demo, but never say so in the channel.")

# (speaker, text). "priya" lines are the rep; the others are Ringtide finance staff.
SCRIPT = [
    ("thabhelo", "Priya, quick one. Our week 42 payout landed $412 short of what the statement says. Is there a holdback I'm not seeing?"),
    ("priya", "Hey Thabhelo, let me check with payments. Usually that's an error charge batch that posts a day after the statement. I'll come back to you by end of day."),
    ("priya", "Looked into it. $388.50 of that is error charges from the 15th and 16th, mostly missing item claims at the Cupertino store, and the other $23.50 is a tip adjustment. The error charges are disputable in the portal, you have 14 days."),
    ("thabhelo", "Thanks. About half of those claims came in more than 72 hours after delivery, so by your own policy they shouldn't be on us. We'll dispute those."),
    ("priya", "Fair. Flag them and I'll make sure the team looks at them this week."),
    ("preet", "Separate thing. The DD-3OFF promo in October shows us funding 100% of it on the statement, but the signed terms say 50/50."),
    ("priya", "That one's a known reporting gap, the split is applied at payout, not on the statement line. If the payout itself doesn't reflect 50/50, send me the week and I'll escalate it."),
    ("priya", "One more, and this one's good news. Starting Monday, November 3, all of your stores move to the Plus plan at 22% commission (down from 25%). The countersigned amendment will follow by email, but I wanted you to have the date now."),
    ("thabhelo", "Great. Does that cover all 56 stores, including the two that opened in September?"),
    ("priya", "Yes, every location under the master agreement, including the September openings."),
    ("preet", "Noted, we'll set the new rate from the 3rd in our checks. Thanks Priya."),
    ("priya", "Anytime. And no promises, but leadership might look at the rate again next quarter if volume keeps growing. I'll keep you posted."),
]

HOLDING = [
    "Let me check on that and come back to you.",
    "Good question, I'll confirm with the team and get back to you today.",
    "On it. Give me a bit to pull the numbers.",
    "I'll look into it and let you know what I find.",
]


def slack(method: str, token: str, body: dict | None = None, **params) -> dict:
    if body is None:
        url = SLACK + method + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "flock-rep-bot/1.0"})
    else:
        req = urllib.request.Request(SLACK + method, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8",
                                              "User-Agent": "flock-rep-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if not data.get("ok"):
        raise SystemExit(f"Slack {method} failed: {data.get('error')}")
    return data


def channel_id(token: str, name: str) -> str:
    cursor = None
    while True:
        d = slack("conversations.list", token, types="public_channel", limit=200, **({"cursor": cursor} if cursor else {}))
        for c in d["channels"]:
            if c["name"] == name.lstrip("#"):
                return c["id"]
        cursor = d.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            raise SystemExit(f"no channel #{name} visible to this token (invite the app to it)")


def post(token: str, cid: str, text: str, thread_ts: str | None = None) -> str:
    body = {"channel": cid, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    return slack("chat.postMessage", token, body)["ts"]


def seed(token: str, name: str, with_finance: bool, pause: float) -> None:
    cid = channel_id(token, name)
    for who, text in SCRIPT:
        if who == "priya":
            post(token, cid, text)
            print(f"priya: {text[:70]}...", flush=True)
        elif with_finance:
            post(token, cid, f"*{who.capitalize()} (Ringtide finance):* {text}")
            print(f"{who}: {text[:70]}...", flush=True)
        else:
            print(f"(skip {who}; a teammate types this line) {text[:60]}...", flush=True)
        time.sleep(pause)


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def scripted(text: str) -> list[str]:
    """Priya's scripted reply/replies when a finance line from SCRIPT is posted verbatim (ignoring punctuation/case)."""
    n = _norm(text)
    out: list[str] = []
    for i, (who, line) in enumerate(SCRIPT):
        if who != "priya" and _norm(line) == n:
            for who2, line2 in SCRIPT[i + 1:]:
                if who2 != "priya":
                    break
                out.append(line2)
            break
    return out


def reply_for(text: str, history: list[str]) -> str:
    try:
        from flock import llm
        convo = "\n".join(history[-8:])
        out = llm.ask_text(PERSONA, f"Recent channel messages:\n{convo}\n\nNewest message from the finance team:\n{text}\n\nReply as Priya.",
                           max_tokens=160, r=llm.route("writer"))
        if out and out.strip():
            return out.strip().strip('"')
    except Exception as e:  # no model reachable: a holding line beats silence
        print(f"model unavailable ({e}); holding reply", file=sys.stderr, flush=True)
    return random.choice(HOLDING)


def respond(token: str, name: str, every: float) -> None:
    cid = channel_id(token, name)
    me = slack("auth.test", token)["user_id"]
    since = slack("conversations.history", token, channel=cid, limit=1)["messages"][0]["ts"] if slack("conversations.history", token, channel=cid, limit=1)["messages"] else "0"
    history: list[str] = []
    print(f"Priya is listening in #{name} (as {me}); replies via the writer model role, holding lines if none", flush=True)
    while True:
        try:
            d = slack("conversations.history", token, channel=cid, oldest=since, limit=50)
            msgs = sorted((m for m in d["messages"] if m.get("type") == "message" and not m.get("subtype")), key=lambda m: float(m["ts"]))
            for m in msgs:
                since = m["ts"]
                if m.get("user") == me:
                    history.append(f"Priya: {m['text']}")
                    continue
                history.append(f"Finance: {m['text']}")
                lines = scripted(m["text"]) or [None]
                for line in lines:
                    time.sleep(random.uniform(4, 9))   # a person does not answer in 200 ms
                    text = line or reply_for(m["text"], history)
                    post(token, cid, text)
                    history.append(f"Priya: {text}")
                    print(f"replied: {text[:80]}", flush=True)
        except SystemExit:
            raise
        except Exception as e:
            print(f"responder: {e}", file=sys.stderr, flush=True)
        time.sleep(every)


def main() -> None:
    ap = argparse.ArgumentParser(description="Priya Raman, simulated DoorDash rep, for the demo Slack channel")
    ap.add_argument("mode", choices=["seed", "respond"])
    ap.add_argument("--channel", default="doordash-partnership")
    ap.add_argument("--with-finance", action="store_true", help="seed: also post the finance team's lines (prefixed with the name)")
    ap.add_argument("--pause", type=float, default=4.0, help="seed: seconds between messages")
    ap.add_argument("--every", type=float, default=8.0, help="respond: poll interval in seconds")
    a = ap.parse_args()
    token = os.environ.get("SLACK_REP_TOKEN", "")
    if not token.startswith("xox"):
        raise SystemExit("set SLACK_REP_TOKEN (the Priya Raman Slack app's bot token)")
    if a.mode == "seed":
        seed(token, a.channel, a.with_finance, a.pause)
    else:
        respond(token, a.channel, a.every)


if __name__ == "__main__":
    main()
