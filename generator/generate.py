"""Data generator (owner: Maja). Builds a fake franchise group's weeks with planted traps and an answer key.

  python generator/generate.py --seed 42 --stores 12 --weeks 2            # full run -> generator/out/
  python generator/generate.py --mini                                     # first milestone: 1 store x 1 day, 3 traps
  python generator/generate.py --seed 7 --traps 7,10,16 --stores 2 --days 3
  python generator/generate.py --seed 42 --write-answer-key               # ALSO overwrite data/answer_key.csv

Approach (see FLOCK_CONTEXT.md section 13):
  1. Build the TRUE world first: stores, channels, contracts (commission tier, promo split, fees, dates),
     one amendment, 3 rep emails, and every order with what Dave's SHOULD be paid.
  2. Plant the traps by changing what the PLATFORM reports (never the truth), and record each one.
  3. Render each source in its own messy format:
       pos_orders.csv                    cleanest; marketplace orders only (direct orders live in the app export)
       doordash_payouts.csv              own column names; error charges as separate lines; negative deductions
       ubereats_statement_<week>.csv     "$12.50" as text, "($3.75)" negatives, MM/DD/YYYY h:mm AM dates
                                         (last week switches to an unseen layout: trap 2)
       grubhub_statement.xlsx            Summary sheet + Detail sheet (title rows above the header, Excel dates)
       direct_app_orders.csv + direct_app_settlements.csv + direct_app_refund_log.csv
       bank_deposits.csv                 weekly lumps per platform, no order detail, MM/DD/YY, "1,234.56"
       contracts/*.json, emails/*.json   templated wording + machine-readable terms/claims (Deal Keeper format)
  4. Write generator/out/answer_key.csv: order_id, trap_type, expected_outcome (correct|recoverable|conflict), amount
     Deposit-level traps (5, 6) use the payout_id as order_id; trap 4 (no POS order) uses the platform_order_id.

Deterministic per seed: same seed + same args = byte-identical files (xlsx timestamps are pinned too).
No LLM here. Numbers are plain Python through flock.models.money().
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
import shutil
import sys
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flock.models import money  # noqa: E402

MARKER = ".flock_generated"

MENU = [("No.1 Two Sliders", 13.99), ("No.2 Slider + Tender", 13.99), ("No.3 Two Tenders", 13.99),
        ("Slider", 7.49), ("Tender", 6.99), ("Fries", 4.49), ("Cheese Fries", 6.49),
        ("Mac & Cheese", 4.99), ("Kale Slaw", 3.99), ("Shake", 6.49), ("Drink", 2.99)]
CITIES = ["Pasadena", "Glendale", "Burbank", "Torrance", "Irvine", "Anaheim", "Riverside", "Ontario",
          "Long Beach", "Santa Ana", "Fullerton", "Pomona", "Carson", "Downey", "Whittier", "Norwalk"]
TAX_RATES = [0.0725, 0.0875, 0.095, 0.1025]
ORDERS_PER_DAY = {"doordash": (5, 9), "ubereats": (3, 7), "grubhub": (2, 5), "direct_app": (2, 5)}
PLATFORMS = ["doordash", "ubereats", "grubhub"]
PO_PREFIX = {"doordash": "DD", "ubereats": "UE", "grubhub": "GH", "direct_app": "DA"}
BANK_MEMO = {"doordash": "DOORDASH, INC. DES:PAYOUT ID:{po}",
             "ubereats": "UBER USA 6787 DES:EDI PYMNTS ID:{po}",
             "grubhub": "GRUBHUB HOLDINGS DES:ACH ID:{po}",
             "direct_app": "PAYPROC SETTLEMENT DES:DIRECT APP ID:{po}"}

# The 17 traps from FLOCK_CONTEXT.md section 13: number -> (trap_type, expected_outcome)
TRAPS = {
    1: ("duplicate_rows", "correct"),
    2: ("unseen_layout", "correct"),
    3: ("sold_never_paid", "recoverable"),
    4: ("payout_without_pos_order", "conflict"),
    5: ("deposit_covers_two_weeks", "correct"),
    6: ("payout_deposit_mismatch", "recoverable"),
    7: ("commission_wrong_tier", "recoverable"),
    8: ("bogo_wrong_split", "recoverable"),
    9: ("promo_outside_dates", "recoverable"),
    10: ("error_charge_after_72h", "recoverable"),
    11: ("duplicate_error_charge", "recoverable"),
    12: ("tax_withheld_platform_collected", "recoverable"),
    13: ("refund_initiated_no_money", "conflict"),
    14: ("authorized_rep_waiver", "recoverable"),
    15: ("unauthorized_rep_promise", "conflict"),
    16: ("legit_error_charge", "correct"),
    17: ("penny_rounding", "correct"),
}
MINI_TRAPS = [3, 7, 16]


# ---------------------------------------------------------------------------------------------------------------
# 1. The TRUE world
# ---------------------------------------------------------------------------------------------------------------

def build_contracts(start: date, days: int) -> dict:
    """Deal terms per channel. Dates are chosen so every date-based trap can happen inside the period."""
    end = start + timedelta(days=days - 1)
    amend_from = start + timedelta(days=days // 2)
    if days >= 2:
        gh_promo = (start, start + timedelta(days=days // 2 - 1))
    else:  # one-day run: the Grubhub promo already ended, so any use of it is out of range
        gh_promo = (start - timedelta(days=14), start - timedelta(days=1))
    year_start, year_end = date(start.year, 1, 1), date(start.year + 1, 12, 31)
    return {
        "doordash": {
            "contract_id": "DD-MSA-2025-114", "counterparty": "DoorDash, Inc.",
            "effective_from": year_start, "effective_to": year_end,
            "tier": "Plus", "commission": 0.25, "fee_name": None, "fee": 0.0,
            "promo": {"code": "DD-3OFF", "discount": 3.00, "merchant_share": 0.5, "from": start, "to": end},
            "error_window_hours": 72, "tax_rule": "platform_remits",
            "authorized_contacts": [("Priya Raman", "priya.raman@doordash.example", "Partner Account Manager")],
            "amendment": {"amendment_id": "DD-MSA-2025-114-A1", "tier": "Basic", "commission": 0.20,
                          "effective_from": amend_from, "signed": amend_from - timedelta(days=5)},
        },
        "ubereats": {
            "contract_id": "UE-RSA-2025-0387", "counterparty": "Uber Eats US LLC",
            "effective_from": year_start, "effective_to": year_end,
            "tier": "Standard", "commission": 0.22, "fee_name": "Marketing Fee", "fee": 1.00,
            "promo": {"code": "UE-BOGO-SLIDER", "discount": 7.49, "merchant_share": 0.5, "from": start, "to": end},
            "error_window_hours": 72, "tax_rule": "platform_remits",
            "authorized_contacts": [("Marcus Lee", "marcus.lee@ubereats.example", "Senior Account Executive")],
        },
        "grubhub": {
            "contract_id": "GH-PA-2025-2290", "counterparty": "Grubhub Holdings Inc.",
            "effective_from": year_start, "effective_to": year_end,
            "tier": "Marketing Basic", "commission": 0.20, "fee_name": "Delivery Network Fee", "fee": 1.25,
            "promo": {"code": "GH-5OFF", "discount": 5.00, "merchant_share": 1.0, "from": gh_promo[0], "to": gh_promo[1]},
            "error_window_hours": 72, "tax_rule": "platform_remits",
            "authorized_contacts": [("Dana Whitfield", "dana.whitfield@grubhub.example", "Director, Restaurant Partnerships")],
        },
        "direct_app": {
            "contract_id": "PP-MA-2024-019", "counterparty": "PayProc Payments LLC",
            "effective_from": year_start, "effective_to": year_end,
            "processing_pct": 0.029, "processing_fixed": 0.30,
        },
    }


def commission_rate(contracts: dict, channel: str, day: date) -> float:
    c = contracts[channel]
    am = c.get("amendment")
    if am and day >= am["effective_from"]:
        return am["commission"]
    return c["commission"]


def promo_active(promo: dict, day: date) -> bool:
    return promo["from"] <= day <= promo["to"]


def make_orders(rng: random.Random, stores: list[dict], start: date, days: int, contracts: dict) -> list[dict]:
    orders, n = [], 10000
    for d in range(days):
        day = start + timedelta(days=d)
        for st in stores:
            for ch in ["doordash", "ubereats", "grubhub", "direct_app"]:
                lo, hi = ORDERS_PER_DAY[ch]
                for _ in range(rng.randint(lo, hi)):
                    n += 1
                    minute = rng.randint(11 * 60, 21 * 60 + 45)
                    placed = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minute, seconds=rng.randint(0, 59))
                    items = [rng.choice(MENU) for _ in range(rng.randint(1, 4))]
                    promo = None
                    if ch in PLATFORMS:
                        p = contracts[ch]["promo"]
                        if promo_active(p, day) and rng.random() < 0.12:
                            promo = p["code"]
                    orders.append({"order_id": f"ORD-{n}", "store_id": st["store_id"], "channel": ch, "day": day,
                                   "placed_at": placed, "items": items, "promo_code": promo})
    for o in orders:
        if o["promo_code"] == "UE-BOGO-SLIDER":  # BOGO needs two sliders on the ticket
            o["items"] += [("Slider", 7.49), ("Slider", 7.49)]
        o["subtotal"] = money(sum(p for _, p in o["items"]))
        tax_rate = next(s["tax_rate"] for s in stores if s["store_id"] == o["store_id"])
        o["tax"] = money(o["subtotal"] * tax_rate)
    return orders


def true_payout(o: dict, contracts: dict) -> dict:
    """What the platform SHOULD deduct, per the deal in effect. Plain code, money() everywhere."""
    ch, c = o["channel"], contracts[o["channel"]]
    if ch == "direct_app":
        fee = money(o["subtotal"] * c["processing_pct"] + c["processing_fixed"])
        return {"gross": o["subtotal"], "commission": 0.0, "promo_merchant_funded": 0.0, "error_charge": 0.0,
                "tax_withheld_by_platform": 0.0, "other_fees": fee}
    promo = 0.0
    if o["promo_code"] and promo_active(c["promo"], o["day"]):
        promo = money(c["promo"]["discount"] * c["promo"]["merchant_share"])
    return {"gross": o["subtotal"], "commission": money(o["subtotal"] * commission_rate(contracts, ch, o["day"])),
            "promo_merchant_funded": promo, "error_charge": 0.0, "tax_withheld_by_platform": 0.0,
            "other_fees": c["fee"]}


def net_of(p: dict) -> float:
    return money(p["gross"] - p["commission"] - p["promo_merchant_funded"] - p["error_charge"]
                 - p["tax_withheld_by_platform"] - p["other_fees"])


def week_index(start: date, day: date) -> int:
    return (day - start).days // 7


def build_world(seed: int, n_stores: int, days: int, traps: list[int]) -> dict:
    rng = random.Random(seed)
    start = date(2026, 8, 3) + timedelta(days=7 * (seed % 10))  # always a Monday; new seed = new week
    stores = [{"store_id": f"S{i + 1:02d}", "name": f"Dave's Hot Chicken {CITIES[i % len(CITIES)]}",
               "tax_rate": rng.choice(TAX_RATES)} for i in range(n_stores)]
    contracts = build_contracts(start, days)
    orders = make_orders(rng, stores, start, days, contracts)
    for o in orders:
        o["truth"] = true_payout(o, contracts)
        o["actual"] = dict(o["truth"])          # what the platform reports; traps edit this, never truth
        o["in_pos"], o["in_payout"] = True, True
        o["errors"] = []                        # error charge lines: amount, reason, delivered_at, reported_at
        o["platform_order_id"] = platform_id(rng, o["channel"])
        o["delivered_at"] = o["placed_at"] + timedelta(minutes=rng.randint(28, 55))
    weeks = (days + 6) // 7
    world = {"seed": seed, "start": start, "days": days, "weeks": weeks, "stores": stores, "contracts": contracts,
             "orders": orders, "key": [], "emails": [], "refunds": [], "deposit_traps": {}, "dup_order_ids": [],
             "unseen_layout_week": None}
    plant_traps(rng, world, traps)
    world["emails"].append(amendment_email(contracts["doordash"]))
    world["emails"].sort(key=lambda e: e["date"])
    build_payouts(rng, world)
    world["key"].sort(key=lambda r: (r["trap_no"], r["order_id"]))
    return world


def platform_id(rng: random.Random, channel: str) -> str:
    if channel == "doordash":
        return "".join(rng.choice("0123456789ABCDEF") for _ in range(8))
    if channel == "ubereats":
        h = "".join(rng.choice("0123456789abcdef") for _ in range(12))
        return f"{h[:4]}-{h[4:8]}-{h[8:]}"
    if channel == "grubhub":
        return str(rng.randint(100000000, 999999999))
    return f"DA{rng.randint(100000, 999999)}"


# ---------------------------------------------------------------------------------------------------------------
# 2. Traps: edit what the platform reports, record the correct outcome
# ---------------------------------------------------------------------------------------------------------------

def plant_traps(rng: random.Random, world: dict, traps: list[int]) -> None:
    orders, c, start, days = world["orders"], world["contracts"], world["start"], world["days"]
    used: set[str] = set()

    def key(no: int, order_id: str, amount: float) -> None:
        trap_type, outcome = TRAPS[no]
        world["key"].append({"trap_no": no, "order_id": order_id, "trap_type": trap_type,
                             "expected_outcome": outcome, "amount": f"{money(amount):.2f}"})

    def pick(channel: str, pred=lambda o: True) -> dict | None:
        pool = [o for o in orders if o["channel"] == channel and o["order_id"] not in used and pred(o)]
        if not pool:
            return None
        o = rng.choice(pool)
        used.add(o["order_id"])
        return o

    def reprice(o: dict) -> None:
        o["actual"]["net"] = net_of(o["actual"])

    # 14 / 15 first: they cover every order of one store on one day
    for no, ch, auth in ((14, "ubereats", True), (15, "grubhub", False)):
        if no not in traps:
            continue
        slots = sorted({(o["store_id"], o["day"]) for o in orders if o["channel"] == ch and o["order_id"] not in used})
        store_id, day = rng.choice(slots)
        hit = [o for o in orders if o["channel"] == ch and o["store_id"] == store_id and o["day"] == day]
        for o in hit:
            used.add(o["order_id"])
            o["truth"]["other_fees"] = 0.0      # waived per the email ...
            key(no, o["order_id"], o["actual"]["other_fees"])  # ... but the platform still charged it
        world["emails"].append(waiver_email(rng, c[ch], ch, store_id, day, auth, world["stores"]))

    if 3 in traps and (o := pick("doordash")):           # sold, never paid
        o["in_payout"] = False
        key(3, o["order_id"], net_of(o["truth"]))
    if 4 in traps and (o := pick("ubereats")):           # paid, never rang up in POS
        o["in_pos"] = False
        key(4, o["platform_order_id"], net_of(o["truth"]))  # no POS id exists
    if 7 in traps and (o := pick("doordash", lambda o: o["day"] >= c["doordash"]["amendment"]["effective_from"]
                                  and not o["promo_code"] and o["subtotal"] >= 25)):
        o["actual"]["commission"] = money(o["subtotal"] * c["doordash"]["commission"])  # old Plus tier
        key(7, o["order_id"], o["actual"]["commission"] - o["truth"]["commission"])
    if 8 in traps and (o := pick("ubereats")):           # BOGO charged 100% to the store, contract says 50/50
        p = c["ubereats"]["promo"]
        if o["promo_code"] != p["code"]:
            o["promo_code"] = p["code"]
            o["items"] += [("Slider", 7.49), ("Slider", 7.49)]
            o["subtotal"] = money(sum(x for _, x in o["items"]))
            o["tax"] = money(o["subtotal"] * next(s["tax_rate"] for s in world["stores"] if s["store_id"] == o["store_id"]))
            o["truth"] = true_payout(o, c)
            o["actual"] = dict(o["truth"])
        o["actual"]["promo_merchant_funded"] = money(p["discount"])
        key(8, o["order_id"], o["actual"]["promo_merchant_funded"] - o["truth"]["promo_merchant_funded"])
    if 9 in traps and (o := pick("grubhub", lambda o: o["day"] > c["grubhub"]["promo"]["to"])):
        p = c["grubhub"]["promo"]
        o["promo_code"] = p["code"]                      # promo applied after it ended; store charged its share
        o["actual"]["promo_merchant_funded"] = money(p["discount"] * p["merchant_share"])
        key(9, o["order_id"], o["actual"]["promo_merchant_funded"])
    if 10 in traps and (o := pick("doordash")):          # complaint 72h+ after delivery
        amt = money(rng.uniform(5, 14))
        o["errors"].append(error_line(rng, o, amt, "Missing item: Cheese Fries", hours=rng.randint(80, 120)))
        key(10, o["order_id"], amt)
    if 11 in traps and (o := pick("doordash")):          # the same (legit) error charge applied twice
        amt = money(rng.uniform(4, 10))
        first = error_line(rng, o, amt, "Wrong item: Slider instead of Tender", hours=rng.randint(1, 20))
        dup = dict(first, adj_id=adj_id(rng))
        o["errors"] += [first, dup]
        o["truth"]["error_charge"] = amt
        key(11, o["order_id"], amt)
    if 12 in traps and (o := pick("grubhub", lambda o: o["subtotal"] >= 20)):           # platform collected the tax AND deducted it from Dave's
        o["actual"]["tax_withheld_by_platform"] = o["tax"]
        key(12, o["order_id"], o["tax"])
    if 13 in traps and (o := pick("direct_app")):        # refund log says initiated; no money moved
        amt = money(min(o["subtotal"], rng.choice([4.49, 6.49, 6.99, 7.49])))
        world["refunds"].append({"refund_id": f"RF-{rng.randint(10000, 99999)}", "order_id": o["order_id"],
                                 "amount": amt, "status": "initiated",
                                 "initiated_at": o["placed_at"] + timedelta(hours=2), "completed_at": None})
        key(13, o["order_id"], amt)
    if 16 in traps and (o := pick("doordash")):          # legit error charge: must NOT be disputed
        amt = money(rng.uniform(4, 12))
        o["errors"].append(error_line(rng, o, amt, "Missing item: Mac & Cheese", hours=rng.randint(1, 30)))
        o["truth"]["error_charge"] = amt
        key(16, o["order_id"], 0.0)
    if 17 in traps and (o := pick("ubereats", lambda o: not o["promo_code"])):   # penny rounding
        o["actual"]["commission"] = money(o["actual"]["commission"] + 0.01)
        key(17, o["order_id"], 0.0)
    if 1 in traps and (o := pick("doordash")):           # export glitch: rows appear twice, byte for byte
        world["dup_order_ids"] = [o["order_id"]]
        key(1, o["order_id"], 0.0)
    if 2 in traps and world["weeks"] >= 2:               # last Uber Eats week arrives in a layout nobody mapped
        world["unseen_layout_week"] = world["weeks"] - 1
        last = [o for o in orders if o["channel"] == "ubereats" and week_index(start, o["day"]) == world["weeks"] - 1
                and o["in_payout"]]
        if last:
            key(2, min(last, key=lambda o: o["placed_at"])["order_id"], 0.0)
    if 5 in traps and world["weeks"] >= 2:
        world["deposit_traps"]["grubhub"] = ("combine", 0)
    if 6 in traps:
        world["deposit_traps"]["ubereats"] = ("short", 0, money(rng.uniform(15, 60)))

    for o in orders:
        reprice(o)


def adj_id(rng: random.Random) -> str:
    return "ADJ-" + "".join(rng.choice("0123456789ABCDEF") for _ in range(8))


def error_line(rng: random.Random, o: dict, amount: float, reason: str, hours: int) -> dict:
    return {"adj_id": adj_id(rng), "amount": amount, "reason": reason, "delivered_at": o["delivered_at"],
            "reported_at": o["delivered_at"] + timedelta(hours=hours, minutes=rng.randint(0, 59))}


# ---------------------------------------------------------------------------------------------------------------
# 3. Payouts and bank deposits (weekly lumps)
# ---------------------------------------------------------------------------------------------------------------

def build_payouts(rng: random.Random, world: dict) -> None:
    start = world["start"]
    payouts = {}  # (channel, week) -> {payout_id, payout_date, net}
    for o in world["orders"]:
        w = week_index(start, o["day"])
        wk_start = start + timedelta(days=7 * w)
        k = (o["channel"], w)
        if k not in payouts:
            payouts[k] = {"channel": o["channel"], "week": w, "payout_id": f"{PO_PREFIX[o['channel']]}-PO-{wk_start:%Y%m%d}",
                          "payout_date": wk_start + timedelta(days=8), "period": (wk_start, wk_start + timedelta(days=6)),
                          "net": 0.0, "count": 0, "gross": 0.0}
        o["payout_id"], o["payout_date"] = payouts[k]["payout_id"], payouts[k]["payout_date"]
        if o["in_payout"]:
            p = payouts[k]
            p["net"] = money(p["net"] + o["actual"]["net"] - sum(e["amount"] for e in o["errors"]))
            p["gross"] = money(p["gross"] + o["actual"]["gross"])
            p["count"] += 1
    deposits = []
    for (ch, w), p in sorted(payouts.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        deposits.append({"deposit_id": f"BNK-{rng.randint(1000000, 9999999)}", "channel": ch, "amount": p["net"],
                         "date": p["payout_date"] + timedelta(days=1), "payout_ids": [p["payout_id"]], "week": w})
    for ch, trap in world["deposit_traps"].items():
        mine = [d for d in deposits if d["channel"] == ch]
        if trap[0] == "combine" and len(mine) >= 2:      # trap 5: weeks 1 and 2 land as one deposit
            a, b = mine[0], mine[1]
            b["amount"] = money(a["amount"] + b["amount"])
            b["payout_ids"] = a["payout_ids"] + b["payout_ids"]
            deposits.remove(a)
            world["key"].append({"trap_no": 5, "order_id": a["payout_ids"][0], "trap_type": TRAPS[5][0],
                                 "expected_outcome": TRAPS[5][1], "amount": "0.00"})
        elif trap[0] == "short" and mine:                # trap 6: deposit is less than the statement says
            d = mine[0]
            d["amount"] = money(d["amount"] - trap[2])
            world["key"].append({"trap_no": 6, "order_id": d["payout_ids"][0], "trap_type": TRAPS[6][0],
                                 "expected_outcome": TRAPS[6][1], "amount": f"{trap[2]:.2f}"})
    world["payouts"] = payouts
    world["deposits"] = deposits


# ---------------------------------------------------------------------------------------------------------------
# 4. Documents: contracts, amendment, emails (templated text, numbers from the true world)
# ---------------------------------------------------------------------------------------------------------------

def contract_doc(ch: str, c: dict) -> dict:
    if ch == "direct_app":
        clauses = [
            {"section": "1. Services", "text": "PayProc processes card payments for orders placed in the Dave's Hot "
             "Chicken mobile app and website and settles funds weekly to the Merchant's operating account."},
            {"section": "3. Fees", "text": f"Merchant shall pay a processing fee of {c['processing_pct'] * 100:.1f}% "
             f"of the order subtotal plus ${c['processing_fixed']:.2f} per transaction, deducted from settlements."},
            {"section": "4. Refunds", "text": "Refunds initiated by Merchant are returned to the cardholder within 5 "
             "business days and deducted from the next settlement."},
        ]
    else:
        p = c["promo"]
        clauses = [
            {"section": "2. Commission", "text": f"Merchant is enrolled in the {c['tier']} plan. {c['counterparty']} "
             f"shall retain a commission of {c['commission'] * 100:.0f}% of the food subtotal of each order, "
             "excluding taxes and tips."},
            {"section": "3. Promotions", "text": f"For promotion {p['code']} (value ${p['discount']:.2f} per redemption), "
             f"Merchant funds {p['merchant_share'] * 100:.0f}% of the promotion value and {c['counterparty']} funds the "
             f"remainder. The promotion runs from {p['from']:%B %d, %Y} through {p['to']:%B %d, %Y}. Redemptions "
             "outside these dates are not Merchant-funded."},
            {"section": "4. Error Charges", "text": f"Merchant is responsible for verified order errors. Merchant is not "
             f"responsible for customer complaints reported more than {c['error_window_hours']} hours after delivery. "
             "Each error shall be charged no more than once."},
            {"section": "5. Taxes", "text": "Where required by marketplace facilitator law, the platform collects and "
             "remits sales tax on the Merchant's behalf. Such tax shall not be deducted from Merchant payouts."},
        ]
        if c["fee_name"]:
            clauses.append({"section": "6. Fees", "text": f"Merchant shall pay a {c['fee_name']} of ${c['fee']:.2f} per "
                            "completed order. Fee changes or waivers are effective only when confirmed in writing by an "
                            "Authorized Contact listed in Schedule B."})
        else:
            clauses.append({"section": "6. Fees", "text": "No per-order fees apply beyond the commission in Section 2. "
                            "Changes are effective only when confirmed in writing by an Authorized Contact (Schedule B)."})
    doc = {"doc_type": "contract", "contract_id": c["contract_id"], "channel": ch,
           "parties": [c["counterparty"], "Hot Chicken Franchise Group LLC (Dave's Hot Chicken franchisee)"],
           "applies_to_stores": "all", "effective_from": c["effective_from"].isoformat(),
           "effective_to": c["effective_to"].isoformat(), "clauses": clauses, "terms": contract_terms(ch, c, clauses)}
    if ch != "direct_app":
        doc["schedule_b_authorized_contacts"] = [{"name": n, "email": e, "title": t} for n, e, t in c["authorized_contacts"]]
    return doc


def contract_terms(ch: str, c: dict, clauses: list[dict]) -> list[dict]:
    """Deal terms the Deal Keeper reads: value semantics per term_type are in CONTRACT.md / deal_keeper."""
    if ch == "direct_app":  # percentage + fixed processing fee: no flat per-order fee term to compare
        return []
    q = {cl["section"].split(".")[0]: cl["text"] for cl in clauses}
    p = c["promo"]
    frm, to = c["effective_from"].isoformat(), c["effective_to"].isoformat()
    am = c.get("amendment")
    return [
        {"term_type": "commission", "store_id": "all", "value": c["commission"], "quote": q["2"],
         "effective_from": frm, "effective_to": (am["effective_from"] - timedelta(days=1)).isoformat() if am else to},
        {"term_type": "promo_split", "store_id": "all", "value": p["merchant_share"], "quote": q["3"],
         "promo_code": p["code"], "effective_from": p["from"].isoformat(), "effective_to": p["to"].isoformat()},
        {"term_type": "error_window", "store_id": "all", "value": c["error_window_hours"], "quote": q["4"],
         "effective_from": frm, "effective_to": to},
        {"term_type": "tax_rule", "store_id": "all", "value": c["tax_rule"], "quote": q["5"],
         "effective_from": frm, "effective_to": to},
        {"term_type": "fee", "store_id": "all", "value": c["fee"], "quote": q["6"], "effective_from": frm,
         "effective_to": to},
    ]


def amendment_doc(c: dict) -> dict:
    am = c["amendment"]
    text = (f"Effective {am['effective_from']:%B %d, %Y}, Merchant moves from the {c['tier']} plan to the {am['tier']} "
            f"plan. Section 2 is amended so that DoorDash shall retain a commission of {am['commission'] * 100:.0f}% of "
            "the food subtotal of each order. All other terms remain unchanged.")
    return {"doc_type": "amendment", "amendment_id": am["amendment_id"], "amends": c["contract_id"],
            "channel": "doordash", "applies_to_stores": "all", "signed_on": am["signed"].isoformat(),
            "effective_from": am["effective_from"].isoformat(), "effective_to": c["effective_to"].isoformat(),
            "clauses": [{"section": "A1. Commission", "text": text}],
            "terms": [{"term_type": "commission", "store_id": "all", "value": am["commission"], "quote": text,
                       "effective_from": am["effective_from"].isoformat(),
                       "effective_to": c["effective_to"].isoformat()}]}


def amendment_email(c: dict) -> dict:
    am = c["amendment"]
    name, email, title = c["authorized_contacts"][0]
    body = (f"Hi team,\n\nAttached is the countersigned amendment {am['amendment_id']}. Starting "
            f"{am['effective_from']:%A, %B %d}, all of your stores move to the {am['tier']} plan at "
            f"{am['commission'] * 100:.0f}% commission (down from {c['commission'] * 100:.0f}%).\n\n"
            f"Thanks for growing with us,\n{name}\n{title}, DoorDash")
    return {"date": datetime.combine(am["signed"], datetime.min.time()) + timedelta(hours=10, minutes=12),
            "from": f"{name} <{email}>", "to": "accounting@hotchickenfg.example",
            "subject": f"Signed: {am['amendment_id']} plan change", "body": body, "channel": "doordash",
            "slug": "doordash_amendment_notice", "sender_authorized": True,
            "claims": [{"term_type": "commission", "store_id": "all", "value": am["commission"],
                        "quote": body.split("\n\n")[1], "effective_from": am["effective_from"].isoformat(),
                        "effective_to": c["effective_to"].isoformat()}]}


def waiver_email(rng: random.Random, c: dict, ch: str, store_id: str, day: date, authorized: bool,
                 stores: list[dict]) -> dict:
    store = next(s for s in stores if s["store_id"] == store_id)
    if authorized:
        name, email, title = c["authorized_contacts"][0]
    else:
        name, email, title = ("Kyle Brandt", "kyle.brandt@grubhub.example", "Sales Representative")
    body = (f"Hey there,\n\nThanks for hopping on the call. As promised, we'll waive the {c['fee_name']} "
            f"(${c['fee']:.2f}/order) for {store['name']} ({store_id}) on {day:%A, %B %d} for your local "
            f"event. You don't need to do anything, it'll come off automatically.\n\nBest,\n{name}\n{title}")
    sent = datetime.combine(day - timedelta(days=rng.randint(2, 4)), datetime.min.time()) + timedelta(hours=15, minutes=rng.randint(0, 59))
    return {"date": sent, "from": f"{name} <{email}>", "to": f"gm.{store_id.lower()}@hotchickenfg.example",
            "subject": f"{c['fee_name']} waiver for {store_id}", "body": body, "channel": ch,
            "slug": f"{ch}_fee_waiver_{store_id.lower()}", "sender_authorized": authorized,
            "claims": [{"term_type": "fee", "store_id": store_id, "value": 0.0, "quote": body.split("\n\n")[1],
                        "effective_from": day.isoformat(), "effective_to": day.isoformat()}]}


# ---------------------------------------------------------------------------------------------------------------
# 5. Render each source in its own messy format
# ---------------------------------------------------------------------------------------------------------------

def usd(x: float) -> str:
    """Uber Eats style: "$12.50", negatives "($3.75)"."""
    x = money(x)
    return f"(${abs(x):,.2f})" if x < 0 else f"${x:,.2f}"


def neg(x: float) -> str:
    """DoorDash style: deductions as negative numbers, never "-0.00"."""
    return f"{money(-x):.2f}"


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    path.write_text(buf.getvalue())


def render(world: dict, out: Path) -> None:
    orders = sorted(world["orders"], key=lambda o: o["placed_at"])
    start = world["start"]
    by_ch = lambda ch: [o for o in orders if o["channel"] == ch]
    store_name = {s["store_id"]: s["name"] for s in world["stores"]}
    c = world["contracts"]
    ptot = lambda o: f"{c[o['channel']]['promo']['discount'] if o['promo_code'] else 0:.2f}"  # full promo cost

    # POS: marketplace orders only, clean-ish, local time
    channel_label = {"doordash": "DoorDash", "ubereats": "Uber Eats", "grubhub": "Grubhub"}
    write_csv(out / "pos_orders.csv",
              ["Check #", "Location", "Order Source", "Opened", "Net Sales", "Tax", "Discount Code", "Ext Ref"],
              [[o["order_id"], o["store_id"], channel_label[o["channel"]], o["placed_at"].strftime("%Y-%m-%d %H:%M:%S"),
                f"{o['subtotal']:.2f}", f"{o['tax']:.2f}", o["promo_code"] or "", o["platform_order_id"]]
               for o in orders if o["channel"] in PLATFORMS and o["in_pos"]])

    # DoorDash: one file, negative deductions, error charges as their own lines
    dd_rows, rng_tx = [], 0
    for o in by_ch("doordash"):
        if not o["in_payout"]:
            continue
        a = o["actual"]
        rng_tx += 1
        row = [f"TX{rng_tx:07d}", "Order", o["store_id"], o["platform_order_id"], o["order_id"],
               o["placed_at"].strftime("%Y-%m-%dT%H:%M:%S"), f"{a['gross']:.2f}", o["promo_code"] or "", ptot(o),
               neg(a['commission']), neg(a['promo_merchant_funded']), "0.00", neg(a['tax_withheld_by_platform']),
               neg(a['other_fees']), f"{a['net']:.2f}", o["payout_id"], o["payout_date"].isoformat(), "", "", ""]
        dd_rows.append(row)
        if o["order_id"] in world["dup_order_ids"]:
            dd_rows.append(list(row))  # trap 1: exact duplicate row
        for e in o["errors"]:
            dd_rows.append([e["adj_id"], "Error Charge", o["store_id"], o["platform_order_id"], o["order_id"],
                            o["placed_at"].strftime("%Y-%m-%dT%H:%M:%S"), "0.00", "", "0.00", "0.00", "0.00",
                            neg(e['amount']), "0.00", "0.00", neg(e['amount']), o["payout_id"],
                            o["payout_date"].isoformat(), e["reason"], e["delivered_at"].strftime("%Y-%m-%dT%H:%M:%S"),
                            e["reported_at"].strftime("%Y-%m-%dT%H:%M:%S")])
    write_csv(out / "doordash_payouts.csv",
              ["Transaction ID", "Transaction Type", "Store ID", "DoorDash Order ID", "Merchant Order Ref",
               "Order Placed", "Subtotal", "Promo Code", "Promo Total", "Commission", "Merchant Promo Contribution",
               "Error Charge",
               "Tax Withheld", "Other Fees", "Net Payout", "Payout ID", "Payout Date", "Error Reason",
               "Delivered At", "Reported At"], dd_rows)

    # Uber Eats: one statement per week; "$" text; last week in an unseen layout (trap 2)
    for w in range(world["weeks"]):
        wk = [o for o in by_ch("ubereats") if o["in_payout"] and week_index(start, o["day"]) == w]
        wk_start = start + timedelta(days=7 * w)
        name = f"ubereats_statement_{wk_start:%Y-%m-%d}.csv"
        if w == world["unseen_layout_week"]:
            write_csv(out / name,
                      ["order_reference", "pos_check_no", "location_code", "ordered_at", "gross_sales",
                       "promo_code", "total_promo_value", "platform_commission", "merchant_funded_promo",
                       "tax_withheld", "service_fees", "adjustment_total", "net_payout", "payout_ref", "paid_on"],
                      [[o["platform_order_id"], o["order_id"], o["store_id"], o["placed_at"].strftime("%Y/%m/%d %H:%M"),
                        f"{o['actual']['gross']:.2f}", o["promo_code"] or "", ptot(o),
                        f"{o['actual']['commission']:.2f}", f"{o['actual']['promo_merchant_funded']:.2f}",
                        f"{o['actual']['tax_withheld_by_platform']:.2f}", f"{o['actual']['other_fees']:.2f}", "0.00",
                        f"{o['actual']['net']:.2f}", o["payout_id"], o["payout_date"].strftime("%Y/%m/%d")] for o in wk])
        else:
            write_csv(out / name,
                      ["Order UUID", "Merchant Ref", "Restaurant", "Date/Time", "Food Sales", "Promotion",
                       "Promotion Value", "Uber Service Fee",
                       "Promotions on Items (Merchant)", "Tax Withheld", "Marketing Fee", "Adjustments",
                       "Total Payout", "Payout Reference", "Payout Date"],
                      [[o["platform_order_id"], o["order_id"], f"{store_name[o['store_id']]} ({o['store_id']})",
                        o["placed_at"].strftime("%m/%d/%Y %I:%M %p"), usd(o["actual"]["gross"]), o["promo_code"] or "",
                        usd(float(ptot(o))), usd(-o["actual"]["commission"]), usd(-o["actual"]["promo_merchant_funded"]),
                        usd(-o["actual"]["tax_withheld_by_platform"]), usd(-o["actual"]["other_fees"]), usd(0),
                        usd(o["actual"]["net"]), o["payout_id"], o["payout_date"].strftime("%m/%d/%Y")] for o in wk])

    render_grubhub(world, by_ch("grubhub"), out / "grubhub_statement.xlsx")

    # Direct app: ordering system export + processor settlements + refund log
    da = by_ch("direct_app")
    write_csv(out / "direct_app_orders.csv", ["id", "store", "created", "items_total", "tax_amt", "coupon"],
              [[o["order_id"], o["store_id"], o["placed_at"].strftime("%d-%b-%Y %H:%M"), f"{o['subtotal']:.2f}",
                f"{o['tax']:.2f}", o["promo_code"] or ""] for o in da])
    write_csv(out / "direct_app_settlements.csv",
              ["settlement_line", "order", "store", "charge_amount", "processing_fee", "net", "payout", "payout_date"],
              [[f"SL-{i + 1:06d}", o["order_id"], o["store_id"], f"{o['actual']['gross']:.2f}",
                f"{o['actual']['other_fees']:.2f}", f"{o['actual']['net']:.2f}", o["payout_id"],
                o["payout_date"].isoformat()] for i, o in enumerate(da) if o["in_payout"]])
    write_csv(out / "direct_app_refund_log.csv",
              ["refund_id", "order_id", "amount", "status", "initiated_at", "completed_at"],
              [[r["refund_id"], r["order_id"], f"{r['amount']:.2f}", r["status"],
                r["initiated_at"].strftime("%Y-%m-%d %H:%M"), ""] for r in world["refunds"]])

    # Bank: weekly lumps, no order detail
    write_csv(out / "bank_deposits.csv", ["Posting Date", "Description", "Amount", "Reference"],
              [[d["date"].strftime("%m/%d/%y"), BANK_MEMO[d["channel"]].format(po=" + ".join(d["payout_ids"])),
                f"{d['amount']:,.2f}", d["deposit_id"]] for d in sorted(world["deposits"], key=lambda d: (d["date"], d["channel"]))])

    # Contracts, amendment, emails
    for ch in ["doordash", "ubereats", "grubhub", "direct_app"]:
        write_json(out / "contracts" / f"{ch}_contract.json", contract_doc(ch, c[ch]))
    write_json(out / "contracts" / "doordash_amendment_1.json", amendment_doc(c["doordash"]))
    for e in world["emails"]:
        write_json(out / "emails" / f"{e['date']:%Y-%m-%d}_{e['slug']}.json",
                   {"kind": "email", "from": e["from"], "to": e["to"], "date": e["date"].isoformat(),
                    "subject": e["subject"], "sender_authorized": e["sender_authorized"], "channel": e["channel"],
                    "body": e["body"], "claims": e["claims"]})


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def render_grubhub(world: dict, gh: list[dict], path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    summ = wb.active
    summ.title = "Summary"
    summ.append(["Grubhub for Restaurants - Payout Summary"])
    summ.append([])
    summ.append(["Payout ID", "Period Start", "Period End", "Orders", "Gross Sales", "Fees & Adjustments",
                 "Net Payout", "Payout Date"])
    for (ch, w), p in sorted(world["payouts"].items(), key=lambda kv: kv[0][1]):
        if ch != "grubhub":
            continue
        summ.append([p["payout_id"], p["period"][0], p["period"][1], p["count"], p["gross"],
                     money(p["gross"] - p["net"]), p["net"], p["payout_date"]])
    det = wb.create_sheet("Detail")
    det.append(["Grubhub for Restaurants - Transaction Detail"])
    det.append([f"Generated for Hot Chicken Franchise Group LLC, period starting {world['start']:%m/%d/%Y}"])
    det.append([])
    promo_val = world["contracts"]["grubhub"]["promo"]["discount"]
    det.append(["GH Order #", "Restaurant ID", "POS Order No.", "Order Date", "Subtotal", "Promo Code", "Promo Total",
                "Grubhub Commission",
                "Promo - Restaurant Funded", "Sales Tax Withheld", "Delivery Network Fee", "Adjustment", "Net",
                "Payout ID", "Payout Date"])
    for o in gh:
        if not o["in_payout"]:
            continue
        a = o["actual"]
        det.append([int(o["platform_order_id"]), f"GH-{o['store_id']}", o["order_id"], o["placed_at"], a["gross"],
                    o["promo_code"] or "", promo_val if o["promo_code"] else 0.0, a["commission"], a["promo_merchant_funded"], a["tax_withheld_by_platform"], a["other_fees"], 0.0,
                    a["net"], o["payout_id"], o["payout_date"]])
    for row in det.iter_rows(min_row=5):
        row[3].number_format = "mm/dd/yyyy hh:mm"
        row[14].number_format = "mm/dd/yyyy"
    fixed = datetime(2026, 1, 1)
    wb.properties.creator = "grubhub-reports"
    wb.properties.created = fixed
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    wb.save(buf)
    pin_xlsx(buf.getvalue(), path)


def pin_xlsx(data: bytes, path: Path) -> None:
    """openpyxl stamps 'now' into the file; rewrite the zip with fixed times so a seed gives identical bytes."""
    src = zipfile.ZipFile(io.BytesIO(data))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            body = src.read(info.filename)
            if info.filename == "docProps/core.xml":
                body = re.sub(rb"(<dcterms:modified[^>]*>)[^<]*", rb"\g<1>2026-01-01T00:00:00Z", body)
            zi = zipfile.ZipInfo(info.filename, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(zi, body)


# ---------------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------------

KEY_COLS = ["order_id", "trap_type", "expected_outcome", "amount"]


def write_answer_key(rows: list[dict], path: Path) -> None:
    write_csv(path, KEY_COLS, [[r[c] for c in KEY_COLS] for r in rows])


def prepare_out(out: Path) -> None:
    """Wipe a previous generator run, but never a folder we didn't create."""
    if out.exists() and any(out.iterdir()):
        if not (out / MARKER).exists():
            raise SystemExit(f"{out} has files the generator didn't make; pick an empty --out")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("made by generator/generate.py; safe to delete\n")


def generate(seed: int = 42, stores: int = 12, days: int = 14, traps: list[int] | None = None,
             out: Path = ROOT / "generator" / "out") -> dict:
    traps = sorted(TRAPS) if traps is None else traps
    world = build_world(seed, stores, days, traps)
    out = Path(out)
    prepare_out(out)
    render(world, out)
    write_answer_key(world["key"], out / "answer_key.csv")
    return world


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a fake franchise group's payout data with planted traps.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--stores", type=int, default=12)
    ap.add_argument("--weeks", type=int, default=2)
    ap.add_argument("--days", type=int, default=None, help="overrides --weeks (e.g. --days 1)")
    ap.add_argument("--traps", default="all", help="comma list of trap numbers 1-17, or 'all'")
    ap.add_argument("--mini", action="store_true", help="first milestone: 1 store x 1 day, traps 3,7,16")
    ap.add_argument("--out", default=str(ROOT / "generator" / "out"))
    ap.add_argument("--write-answer-key", action="store_true",
                    help="ALSO overwrite data/answer_key.csv (the demo scoreboard uses it)")
    a = ap.parse_args()
    stores, days = a.stores, a.days or a.weeks * 7
    traps = sorted(TRAPS) if a.traps == "all" else [int(t) for t in a.traps.split(",") if t.strip()]
    if a.mini:
        stores, days, traps = 1, 1, MINI_TRAPS
    bad = [t for t in traps if t not in TRAPS]
    if bad:
        raise SystemExit(f"unknown trap numbers: {bad}")
    world = generate(a.seed, stores, days, traps, Path(a.out))
    planted = sorted({r["trap_no"] for r in world["key"]})
    skipped = [t for t in traps if t not in planted]
    print(f"seed {a.seed}: {stores} stores x {days} days from {world['start']} -> {a.out}")
    print(f"  {len(world['orders'])} orders, {len(world['deposits'])} bank deposits, {len(world['emails'])} emails")
    print(f"  traps planted: {planted} ({len(world['key'])} answer key rows)")
    if skipped:
        print(f"  traps skipped (need more stores/days, e.g. 2 and 5 need --weeks 2): {skipped}")
    if a.write_answer_key:
        write_answer_key(world["key"], ROOT / "data" / "answer_key.csv")
        print("  wrote data/answer_key.csv (restore the demo key with: git checkout data/answer_key.csv)")


if __name__ == "__main__":
    main()
