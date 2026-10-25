"""Simulated platform reps for the demo Slack workspace (Ringtide Bagels Finance, the made-up brand).

One script, three personas, each a separate Slack app (bot user) and the contract's authorized contact for its platform:
  priya    Priya Raman, DoorDash Partner Account Manager        #doordash-partnership   SLACK_REP_TOKEN
  marcus   Marcus Lee, Uber Eats Senior Account Executive       #ubereats-partnership   SLACK_MARCUS_TOKEN
  dana     Dana Whitfield, Grubhub Partner Success Manager      #grubhub-partnership    SLACK_DANA_TOKEN

  python scripts/slack_rep_bot.py respond --persona marcus              # answer whatever a human posts in that channel
  python scripts/slack_rep_bot.py respond --persona priya --every 6

respond: scripted replies when a seeded finance line is posted verbatim (see SCRIPTS), otherwise the `writer` model role
(flock/llm.py, e.g. a CREAO hosted agent with FLOCK_WRITER_PROVIDER=creao), and a short human holding line if no
model answers. A rep confirms and explains terms; the only new terms they ever state are the scripted ones.
Everything here is a simulation; the bots never say so in the channel, the workspace name does.
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

PERSONAS = {
    "priya": {
        "name": "Priya Raman", "token_env": "SLACK_REP_TOKEN", "channel": "doordash-partnership",
        "persona": ("You are Priya Raman, Partner Account Manager at DoorDash, chatting in a shared Slack channel with the "
                    "finance team of Ringtide Bagels, a 56-store bagel chain in California."),
        "script": [
            ("finance", "Priya, quick one. Our week 42 payout landed $412 short of what the statement says. Is there a holdback I'm not seeing?"),
            ("rep", "Hey Thabhelo, let me check with payments. Usually that's an error charge batch that posts a day after the statement. I'll come back to you by end of day."),
            ("rep", "Looked into it. $388.50 of that is error charges from the 15th and 16th, mostly missing item claims at the Cupertino store, and the other $23.50 is a tip adjustment. The error charges are disputable in the portal, you have 14 days."),
            ("finance", "Thanks. About half of those claims came in more than 72 hours after delivery, so by your own policy they shouldn't be on us. We'll dispute those."),
            ("rep", "Fair. Flag them and I'll make sure the team looks at them this week."),
            ("finance", "Separate thing. The DD-3OFF promo in October shows us funding 100% of it on the statement, but the signed terms say 50/50."),
            ("rep", "That one's a known reporting gap, the split is applied at payout, not on the statement line. If the payout itself doesn't reflect 50/50, send me the week and I'll escalate it."),
            ("rep", "One more, and this one's good news. Starting Monday, November 3, all of your stores move to the Plus plan at 22% commission (down from 25%). The countersigned amendment will follow by email, but I wanted you to have the date now."),
            ("finance", "Great. Does that cover all 56 stores, including the two that opened in September?"),
            ("rep", "Yes, every location under the master agreement, including the September openings."),
            ("finance", "Noted, we'll set the new rate from the 3rd in our checks. Thanks Priya."),
            ("rep", "Anytime. And no promises, but leadership might look at the rate again next quarter if volume keeps growing. I'll keep you posted."),
        ],
    },
    "marcus": {
        "name": "Marcus Lee", "token_env": "SLACK_MARCUS_TOKEN", "channel": "ubereats-partnership",
        "persona": ("You are Marcus Lee, Senior Account Executive at Uber Eats, chatting in a shared Slack channel with the "
                    "finance team of Ringtide Bagels, a 56-store bagel chain in California."),
        "script": [
            ("finance", "Marcus, the Marketing Fee on last week's statement is $1.00 an order across the board, but our Cupertino store had the street fair on the 21st and you said you'd cover that day."),
            ("rep", "You're right, that was agreed on our call. To confirm in writing: we'll waive the Marketing Fee ($1.00/order) for Ringtide Bagels Cupertino (S05) on Tuesday, October 21 for the local event. It'll come off as a credit on the next payout, you don't need to file anything."),
            ("finance", "Perfect, thank you. Second thing, we're seeing some orders charged at a higher rate than the plan rate. Is that the Uber One thing?"),
            ("rep", "Yes. Orders placed by Uber One members carry the member rate, which is 5 points above your plan rate, that's in section 3 of the agreement. The statement tags them with a member flag in the order source column, so your team can split them out."),
            ("finance", "Got it. We'll check the member-flagged orders against that rate rather than the base rate then."),
            ("rep", "Exactly. If any member-flagged order is above plan plus 5, send me the order IDs and I'll have it corrected."),
            ("finance", "One more. We had three refunds last week for the same order ID, UE-77812. Looks like a duplicate."),
            ("rep", "I see it. Two of those are duplicates from a retry on our side. I've raised it with payments, you'll see the two extra refunds reversed on next week's statement. Sorry about that."),
            ("finance", "Appreciate it, Marcus. We'll hold the dispute until we see next week's statement."),
            ("rep", "Sounds good. I'll ping you here when the reversal posts."),
        ],
    },
    "dana": {
        "name": "Dana Whitfield", "token_env": "SLACK_DANA_TOKEN", "channel": "grubhub-partnership",
        "persona": ("You are Dana Whitfield, Partner Success Manager at Grubhub, chatting in a shared Slack channel with the "
                    "finance team of Ringtide Bagels, a 56-store bagel chain in California."),
        "script": [
            ("finance", "Dana, quick check on the October statement. The Delivery Network Fee is showing as $1.25 on pickup orders too. Those should be delivery only, right?"),
            ("rep", "Correct, the Delivery Network Fee only applies to orders we deliver. Pickup orders shouldn't carry it. Can you send me a few order numbers? That sounds like a tagging issue on our side."),
            ("finance", "Sure, GH-20418, GH-20425 and GH-20431 to start. There are 60 or so in total across four stores."),
            ("rep", "Thanks. I've confirmed those three were pickup. I'll have the fee reversed on all affected pickup orders for October, you'll see it as one credit line on the next statement."),
            ("finance", "Great. Also, the processing fee looks like it's been applied twice on the week of the 13th."),
            ("rep", "Looking now. Yes, there was a double posting of the processing fee on the 13th through the 19th for a batch of accounts, yours included. Finance is reversing it this week, no action needed on your end."),
            ("finance", "Thanks Dana. Last one, is the 18% marketing commission still the rate for Q4? We want to make sure our checks use the right number."),
            ("rep", "Yes, 18% marketing commission stays through December 31. If anything changes for Q1 you'll get a signed amendment first, nothing changes on a Slack message alone."),
            ("finance", "That's exactly what we like to hear. Thanks."),
            ("rep", "Anytime. I'll confirm here once both reversals post."),
        ],
    },
}

HOLDING = [
    "Let me check on that and come back to you.",
    "Good question, I'll confirm with the team and get back to you today.",
    "On it. Give me a bit to pull the numbers.",
    "I'll look into it and let you know what I find.",
]

STYLE = (" You are friendly, concrete and brief, like a real account manager typing between meetings. One to three "
         "sentences. No bullet points, no headings, no sign-off. If they ask about a number you do not have, say you "
         "will check and come back. Never invent a new commission rate, fee waiver or promo split on your own; only "
         "confirm or explain terms. Everything here is a simulation for a hackathon demo, but never say so in the channel.")


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


def post(token: str, cid: str, text: str) -> str:
    return slack("chat.postMessage", token, {"channel": cid, "text": text})["ts"]


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def scripted(script: list[tuple[str, str]], text: str) -> list[str]:
    """The rep's scripted reply/replies when a finance line from the script is posted (ignoring punctuation/case)."""
    n = _norm(text)
    for i, (who, line) in enumerate(script):
        if who == "finance" and _norm(line) == n:
            out = []
            for who2, line2 in script[i + 1:]:
                if who2 != "rep":
                    break
                out.append(line2)
            return out
    return []


def reply_for(persona: dict, text: str, history: list[str]) -> str:
    try:
        from flock import llm
        convo = "\n".join(history[-8:])
        out = llm.ask_text(persona["persona"] + STYLE,
                           f"Recent channel messages:\n{convo}\n\nNewest message from the finance team:\n{text}\n\nReply as {persona['name']}.",
                           max_tokens=160, r=llm.route("writer"))
        if out and out.strip():
            return out.strip().strip('"')
    except Exception as e:  # no model reachable: a holding line beats silence
        print(f"model unavailable ({e}); holding reply", file=sys.stderr, flush=True)
    return random.choice(HOLDING)


def respond(persona: dict, token: str, name: str, every: float) -> None:
    cid = channel_id(token, name)
    me = slack("auth.test", token)["user_id"]
    last = slack("conversations.history", token, channel=cid, limit=1)["messages"]
    since = last[0]["ts"] if last else "0"
    history: list[str] = []
    print(f"{persona['name']} is listening in #{name} (as {me}); scripted lines, then the writer model role, then holding lines", flush=True)
    while True:
        try:
            d = slack("conversations.history", token, channel=cid, oldest=since, limit=50)
            msgs = sorted((m for m in d["messages"] if m.get("type") == "message" and not m.get("subtype")), key=lambda m: float(m["ts"]))
            for m in msgs:
                since = m["ts"]
                if m.get("user") == me:
                    history.append(f"{persona['name']}: {m['text']}")
                    continue
                if m.get("bot_id"):      # another rep bot or the watcher: reps do not talk to each other
                    continue
                history.append(f"Finance: {m['text']}")
                for line in scripted(persona["script"], m["text"]) or [None]:
                    time.sleep(random.uniform(4, 9))   # a person does not answer in 200 ms
                    text = line or reply_for(persona, m["text"], history)
                    post(token, cid, text)
                    history.append(f"{persona['name']}: {text}")
                    print(f"replied: {text[:80]}", flush=True)
        except SystemExit:
            raise
        except Exception as e:
            print(f"responder: {e}", file=sys.stderr, flush=True)
        time.sleep(every)


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulated platform reps for the demo Slack channels")
    ap.add_argument("mode", choices=["respond", "lines"])
    ap.add_argument("--persona", choices=sorted(PERSONAS), default="priya")
    ap.add_argument("--channel", help="default: the persona's channel")
    ap.add_argument("--every", type=float, default=6.0, help="respond: poll interval in seconds")
    a = ap.parse_args()
    p = PERSONAS[a.persona]
    if a.mode == "lines":   # print the finance team's lines, for whoever types them
        for who, line in p["script"]:
            if who == "finance":
                print(line)
        return
    token = os.environ.get(p["token_env"], "")
    if not token.startswith("xox"):
        raise SystemExit(f"set {p['token_env']} (the {p['name']} Slack app's bot token)")
    respond(p, token, a.channel or p["channel"], a.every)


if __name__ == "__main__":
    main()
