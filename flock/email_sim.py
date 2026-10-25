"""Simulated accounting inbox for a generated month: the email side of what Thabhelo's Slack bots do for chat.

Built from the generated world (stores, contracts, authorized contacts, orders, payouts), so every number in it is
real for that month. All addresses are .example and every message says SIMULATED. Messages:

  1. An authorized rep (on the contract's authorized contacts) waives a per-order fee for one store on one day. The
     platform never applied it: those orders were still charged -> recoverable findings.
  2. Someone from the same platform who is NOT an authorized contact promises the same kind of waiver -> the Deal
     Keeper marks it non-binding; the charged orders go to a human (conflict).
  3. A thread: an authorized rep says a waiver is coming, then confirms the store and day in a reply (the reply quotes
     the first message, quoted lines are history) -> recoverable for the confirmed day only.
  4. A platform statement with a CSV attachment (per-store totals from that month's payouts).
  5. A promo commitment that restates the contract's promo split (agrees with the contract: no finding).
  6. A platform support reply to a dispute (no deal content).

inject(world, out, raw, state_dir) sends the messages through connectors.receive (source "email", connector
"email-sim") so they land in raw/emails before the pipeline runs and show on the Sources page; the expected outcomes are
added to the month's answer key (out/answer_key.csv) so the scoreboard grades them. Attachments are written to
<state_dir>/inbox/email_files/<month>/.
"""
from __future__ import annotations

import csv
import io
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flock import connectors

CONNECTOR = "email-sim"
NOTICE = "SIMULATED DATA (Flock email simulator, .example addresses)."
FILES_DIR = Path("inbox") / "email_files"
# same-company people who are NOT on any contract's authorized contacts
UNLISTED = {"ubereats": ("Dana Whitfield (simulated)", "dana.whitfield@ubereats.example", "Account Coordinator"),
            "grubhub": ("Taylor Brooks (simulated)", "taylor.brooks@grubhub.example", "Partner Success Associate"),
            "doordash": ("Chris Ndiaye (simulated)", "chris.ndiaye@doordash.example", "Merchant Onboarding")}
SUPPORT = {"doordash": "merchant-support@doordash.example", "ubereats": "restaurants-support@ubereats.example",
           "grubhub": "restaurant-care@grubhub.example"}


def _iso(d: datetime) -> str:
    return d.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, datetime.min.time()) + timedelta(hours=hour, minutes=minute)


def _sig(name: str, title: str, platform: str) -> str:
    return f"{name.replace(' (simulated)', '')}\n{title}, {platform}\n\n-- {NOTICE}"


def _slots(world: dict, ch: str, taken: set[str]) -> list[tuple[str, date, list[dict]]]:
    """(store, day, orders) where every order was charged the fee and no planted trap touched it."""
    by: dict[tuple[str, date], list[dict]] = {}
    for o in world["orders"]:
        if o["channel"] == ch and o.get("in_payout", True):
            by.setdefault((o["store_id"], o["day"]), []).append(o)
    first, last = world["start"] + timedelta(days=5), world["start"] + timedelta(days=world["days"] - 2)
    out = []
    for (sid, day), rows in sorted(by.items()):
        if not (first <= day <= last) or len(rows) < 2:
            continue
        if any(o["order_id"] in taken or o.get("platform_order_id") in taken for o in rows):
            continue
        if all(o["actual"].get("other_fees", 0) > 0 and all(o["actual"].get(k) == v for k, v in o["truth"].items())
               for o in rows):
            out.append((sid, day, rows))
    return out


def _waiver_line(c: dict, store: dict, day: date) -> str:
    # same wording as the generator's waiver emails, which the Deal Keeper's rules read
    return (f"we'll waive the {c['fee_name']} (${c['fee']:.2f}/order) for {store['name']} ({store['store_id']}) on "
            f"{day:%A, %B %d} for your local event")


def build(world: dict, seed: int | None = None) -> dict[str, Any]:
    """The month's simulated inbox: {"items": [email items], "key": [answer-key rows], "attachments": [(name, csv)]}."""
    rng = random.Random(f"email-sim:{seed if seed is not None else world.get('seed')}:{world['start']}")
    c, prof = world["contracts"], world["profile"]
    domain = prof["email_domain"]
    stores = {s["store_id"]: s for s in world["stores"]}
    taken = {r["order_id"] for r in world["key"]}
    items: list[dict[str, Any]] = []
    key: list[dict[str, str]] = []
    month = f"{world['start']:%Y-%m}"

    def plant(ch: str, outcome: str, trap: str) -> tuple[str, date] | None:
        slots = _slots(world, ch, taken)
        if not slots:
            return None
        sid, day, rows = slots[rng.randrange(len(slots))]
        for o in rows:
            taken.add(o["order_id"])
            key.append({"order_id": o["order_id"], "trap_type": trap, "expected_outcome": outcome,
                        "amount": f"{o['actual']['other_fees']:.2f}"})
        return sid, day

    def fee_channel(*prefer: str) -> str | None:
        return next((ch for ch in prefer if ch in c and c[ch].get("fee_name") and c[ch].get("authorized_contacts")),
                    None)

    # 1. authorized waiver the platform never applied -> recoverable
    ch = fee_channel("ubereats", "grubhub")
    if ch and (hit := plant(ch, "recoverable", "email_authorized_waiver")):
        sid, day = hit
        name, email, title = c[ch]["authorized_contacts"][0]
        items.append({"from": f"{name} <{email}>", "to": f"gm.{sid.lower()}@{domain}",
                      "date": _iso(_at(day - timedelta(days=rng.randint(2, 4)), 15, rng.randint(0, 59))),
                      "subject": f"{c[ch]['fee_name']} waiver for {sid}",
                      "body": f"Hi team,\n\nThanks for the call. As promised, {_waiver_line(c[ch], stores[sid], day)}. "
                              f"You don't need to do anything, it'll come off automatically.\n\n"
                              f"{_sig(name, title, c[ch]['platform_name'])}"})

    # 2. unauthorized same-company promise -> needs a human
    ch2 = fee_channel("grubhub", "ubereats")
    if ch2 and ch2 in UNLISTED and (hit := plant(ch2, "conflict", "email_unauthorized_promise")):
        sid, day = hit
        name, email, title = UNLISTED[ch2]
        items.append({"from": f"{name} <{email}>", "to": f"gm.{sid.lower()}@{domain}",
                      "date": _iso(_at(day - timedelta(days=rng.randint(2, 5)), 11, rng.randint(0, 59))),
                      "subject": f"Re: {c[ch2]['fee_name']} during the street fair",
                      "body": f"Hey!\n\nGood news, {_waiver_line(c[ch2], stores[sid], day)}. I'll let billing know.\n\n"
                              f"{_sig(name, title, c[ch2]['platform_name'])}"})

    # 3. thread: a waiver announced, then confirmed (store + day) in a reply -> recoverable for that day
    ch3 = fee_channel("grubhub", "ubereats")
    if ch3 and (hit := plant(ch3, "recoverable", "email_thread_confirmed_waiver")):
        sid, day = hit
        name, email, title = c[ch3]["authorized_contacts"][0]
        first_at = _at(day - timedelta(days=6), 9, rng.randint(0, 59))
        first = (f"Hi team,\n\nWe're arranging a fee break for one of your stores during the neighborhood festival. "
                 f"I'll confirm the store and the exact day in writing before then.\n\n"
                 f"{_sig(name, title, c[ch3]['platform_name'])}")
        subject = f"Festival fee break ({c[ch3]['platform_name']})"
        mid = f"<{first_at:%Y%m%d%H%M}.festival.{ch3}@mail.simulated.example>"
        items.append({"from": f"{name} <{email}>", "to": f"accounting@{domain}", "date": _iso(first_at),
                      "subject": subject, "body": first, "message_id": mid})
        quoted = "\n".join("> " + ln for ln in first.splitlines())
        items.append({"from": f"{name} <{email}>", "to": f"accounting@{domain}",
                      "date": _iso(first_at + timedelta(days=3, hours=5)), "subject": f"Re: {subject}",
                      "in_reply_to": mid,
                      "body": f"Hi team,\n\nConfirming: {_waiver_line(c[ch3], stores[sid], day)}.\n\n"
                              f"{_sig(name, title, c[ch3]['platform_name'])}\n\n"
                              f"On {first_at:%a, %b %d, %Y at %I:%M %p}, {name.replace(' (simulated)', '')} wrote:\n"
                              f"{quoted}"})

    # 4. platform statement with a CSV attachment: per-store totals from this month's payouts
    attachments = []
    for chs in [x for x in ("doordash", "ubereats", "grubhub") if x in c][:2]:
        per: dict[str, list[float]] = {}
        for o in world["orders"]:
            if o["channel"] == chs and o.get("in_payout", True):
                a = o["actual"]
                row = per.setdefault(o["store_id"], [0, 0.0, 0.0, 0.0, 0.0])
                row[0] += 1
                row[1] += a["gross"]
                row[2] += a["commission"]
                row[3] += a.get("other_fees", 0) + a.get("error_charge", 0) + a.get("promo_merchant_funded", 0)
                row[4] += a.get("net", 0)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["statement_month", "store_id", "store_name", "orders", "gross", "commission", "fees_and_adjustments",
                    "net_payout"])
        for sid in sorted(per):
            n, g, cm, f, net = per[sid]
            w.writerow([month, sid, stores[sid]["name"], n, f"{g:.2f}", f"{cm:.2f}", f"{f:.2f}", f"{net:.2f}"])
        fname = f"{chs}_statement_{month}.csv"
        attachments.append((fname, buf.getvalue()))
        totals = [sum(v[i] for v in per.values()) for i in (0, 4)]
        end = world["start"] + timedelta(days=world["days"])
        items.append({"from": f"{c[chs]['platform_name']} Billing <billing@{chs}.example>", "to": f"accounting@{domain}",
                      "date": _iso(_at(end, 7, 30)), "subject": f"Your {c[chs]['platform_name']} statement for {month}",
                      "body": f"Hello,\n\nYour monthly statement is attached ({fname}): {int(totals[0]):,} orders across "
                              f"{len(per)} stores, net payout ${totals[1]:,.2f}.\n\nAttachment: {fname}\n\n"
                              f"{c[chs]['platform_name']} Merchant Billing\n\n-- {NOTICE}"})

    # 5. promo commitment that matches the contract (no finding expected)
    chp = next((x for x in ("doordash", "ubereats") if x in c and c[x].get("promo") and c[x].get("authorized_contacts")),
               None)
    if chp:
        p, (name, email, title) = c[chp]["promo"], c[chp]["authorized_contacts"][0]
        items.append({"from": f"{name} <{email}>", "to": f"marketing@{domain}",
                      "date": _iso(_at(world["start"], 10, 5)), "subject": f"Promo {p['code']} this month",
                      "body": f"Hi team,\n\nQuick reminder that promotion {p['code']} stays on the terms in our agreement "
                              f"(Section 3); nothing changes this month. Let me know if you want to extend it.\n\n"
                              f"{_sig(name, title, c[chp]['platform_name'])}"})

    # 6. a platform support reply to a dispute (no deal content)
    chs = next((x for x in ("doordash", "ubereats", "grubhub") if x in c), None)
    if chs:
        items.append({"from": f"{c[chs]['platform_name']} Merchant Support <{SUPPORT[chs]}>",
                      "to": f"accounting@{domain}",
                      "date": _iso(_at(world["start"] + timedelta(days=world["days"] // 2), 13, 40)),
                      "subject": "Re: Dispute received",
                      "body": "Hello,\n\nThanks for reaching out. We received your dispute and it is under review. Most "
                              "reviews are completed within 10 business days; we'll reply on this thread with the "
                              f"outcome.\n\n{c[chs]['platform_name']} Merchant Support\n\n-- {NOTICE}"})
    items.sort(key=lambda it: it["date"])
    return {"items": items, "key": key, "attachments": attachments}


def payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"flock_connector": connectors.VERSION, "source": "email", "connector": CONNECTOR, "simulated": True,
            "items": items}


def inject(world: dict, out: Path, raw: Path, state_dir: Path, seed: int | None = None) -> dict[str, Any]:
    """Send the month's inbox through connectors.receive into raw/ (before the pipeline runs), add its expected
    outcomes to out/answer_key.csv, save attachments. Returns the receive record plus counts."""
    inbox = build(world, seed)
    if not inbox["items"]:
        return {"items": 0}
    rec = connectors.receive(payload(inbox["items"]), Path(state_dir), contracts=Path(raw) / "contracts", raw=Path(raw))
    if inbox["key"]:
        from flock.demo import generator
        world["key"].extend(inbox["key"])
        generator.write_answer_key(world["key"], Path(out) / "answer_key.csv")
    files = []
    dest = Path(state_dir) / FILES_DIR / f"{world['start']:%Y-%m}"
    for name, text in inbox["attachments"]:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / name).write_text(text)
        files.append(str(dest / name))
    return {**rec, "key_rows": len(inbox["key"]), "attachments": files}
