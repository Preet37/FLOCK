"""Data generator (owner: Maja). Builds a fake franchise group's weeks with planted traps and an answer key.

  python generator/generate.py --seed 42 --stores 12 --weeks 2            # full run -> generator/out/
  python generator/generate.py --mini                                     # first milestone: 1 store x 1 day, 3 traps
  python generator/generate.py --seed 7 --traps 7,10,16 --stores 2 --days 3
  python generator/generate.py --seed 42 --write-answer-key               # ALSO overwrite data/answer_key.csv
  python generator/generate.py --profile ca_bagel_chain --weeks 52        # another (simulated) company, a full year

Company profiles (profiles/<name>.json) hold every company-specific fact: name, stores and cities, channels,
orders per store per day by channel, menu (average ticket), seasonality, dayparts, catering, contracts per
channel. `default` is the original 12-store demo company; `ca_bagel_chain` is a fictional 56-store bagel chain.
Every profile is SIMULATED data.

Approach (see FLOCK_CONTEXT.md section 13):
  1. Build the TRUE world first: stores, channels, contracts (commission tier, promo split, fees, dates),
     one amendment, rep emails, and every order with what the company SHOULD be paid.
  2. Plant the traps by changing what the PLATFORM reports (never the truth), and record each one.
     Each trap is planted max(1, round(trap_rate x weight x store_weeks / 100)) times, spread across the period,
     so a 2-week demo gets one of each and a year gets a realistic number (--trap-rate to change it).
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
from itertools import chain
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flock.models import money  # noqa: E402

MARKER = ".flock_generated"
PROFILES = ROOT / "profiles"
DEFAULT_PROFILE = "default"

PLATFORMS = ["doordash", "ubereats", "grubhub"]
PO_PREFIX = {"doordash": "DD", "ubereats": "UE", "grubhub": "GH", "direct_app": "DA"}
BANK_MEMO = {"doordash": "DOORDASH, INC. DES:PAYOUT ID:{po}",
             "ubereats": "UBER USA 6787 DES:EDI PYMNTS ID:{po}",
             "grubhub": "GRUBHUB HOLDINGS DES:ACH ID:{po}",
             "direct_app": "PAYPROC SETTLEMENT DES:DIRECT APP ID:{po}"}
XLSX_STREAM_ROWS = 20000  # above this many Grubhub rows, stream the workbook with fast_xlsx()

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
    18: ("processing_fee_creep", "recoverable"),   # the direct app's processor quietly raises its rate for one store-week
}
MINI_TRAPS = [3, 7, 16]


# ---------------------------------------------------------------------------------------------------------------
# 0. Company profiles
# ---------------------------------------------------------------------------------------------------------------

def list_profiles() -> list[str]:
    return sorted(p.stem for p in PROFILES.glob("*.json"))


def load_profile(name: str | Path | dict | None = None) -> dict:
    """A profile by name (profiles/<name>.json), by path, or already loaded. None = the default profile."""
    if isinstance(name, dict):
        return name
    name = name or DEFAULT_PROFILE
    path = Path(name) if str(name).endswith(".json") else PROFILES / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"unknown profile {name!r}; pick one of: {', '.join(list_profiles())}")
    return json.loads(path.read_text())


def trap_counts(profile: dict, n_stores: int, days: int, traps: list[int], rate: float | None = None) -> dict:
    """How many times each trap is planted. trap_rate = instances per trap per 100 store-weeks (weights scale it;
    weight 0 = exactly once). A 12-store x 2-week demo plants every trap once."""
    rate = float(profile.get("trap_rate", 2.0) if rate is None else rate)
    weights = profile.get("trap_weights", {})
    store_weeks = n_stores * days / 7
    return {t: max(1, round(rate * float(weights.get(str(t), 1.0)) * store_weeks / 100)) for t in traps}


def summarize_profile(profile: dict | str | None = None, n_stores: int | None = None) -> dict:
    """Plain numbers about a profile, for the CLI / README: stores, orders per store-day, average ticket."""
    p = load_profile(profile)
    n = n_stores or p["default_stores"]
    per_day = {ch: (lo + hi) / 2 for ch, (lo, hi) in p["orders_per_day"].items() if ch in p["channels"]}
    wk, mo = p["seasonality"]["weekday"], p["seasonality"]["monthly"]
    season = (sum(wk) / len(wk)) * (sum(mo) / len(mo))
    size = sum(p.get("store_size", [1, 1])) / 2
    menu_avg = sum(x for _, x in p["menu"]) / len(p["menu"])
    items = sum(p["items_per_order"]) / 2
    return {"name": p["name"], "brand": p["brand"], "stores": n, "channels": p["channels"],
            "orders_per_store_day": {ch: round(v * season * size, 1) for ch, v in per_day.items()},
            "orders_per_year": int(sum(per_day.values()) * season * size * n * 364),
            "menu_ticket": round(menu_avg * items, 2), "average_ticket": p.get("average_ticket"),
            "catering_share": (p.get("catering") or {}).get("share", 0.0), "simulated": p.get("simulated", True)}


def make_stores(rng: random.Random, xrng: random.Random, profile: dict, n_stores: int) -> list[dict]:
    named = profile.get("stores") or [[f"{profile['brand']} {c}", c] for c in profile["cities"]]
    stores = []
    for i in range(n_stores):
        name, city = named[i % len(named)]
        if i >= len(named):
            name = f"{name} {i // len(named) + 1}"
        stores.append({"store_id": f"S{i + 1:02d}", "name": name, "city": city,
                       "tax_rate": rng.choice(profile["tax_rates"])})
    if profile.get("store_size"):  # own random stream so profiles without it keep the exact same orders
        lo, hi = profile["store_size"]
        for s in stores:
            s["size"] = round(xrng.uniform(lo, hi), 2)
    return stores


# ---------------------------------------------------------------------------------------------------------------
# 1. The TRUE world
# ---------------------------------------------------------------------------------------------------------------

def build_contracts(start: date, days: int, profile: dict) -> dict:
    """Deal terms per channel from the profile. Dates are relative to the period so every date-based trap fits."""
    end = start + timedelta(days=days - 1)
    year_start, year_end = date(start.year, 1, 1), date(start.year + 1, 12, 31)
    out = {}
    for ch, spec in profile["contracts"].items():
        c = {k: v for k, v in spec.items() if k not in ("promo", "amendment")}
        c["effective_from"], c["effective_to"] = year_start, year_end
        if "promo" in spec:
            p = dict(spec["promo"])
            window = p.pop("window", "period")
            if window == "first_half":
                if days >= 2:
                    p["from"], p["to"] = start, start + timedelta(days=days // 2 - 1)
                else:  # one-day run: the promo already ended, so any use of it is out of range
                    p["from"], p["to"] = start - timedelta(days=14), start - timedelta(days=1)
            else:
                p["from"], p["to"] = start, end
            c["promo"] = p
        if "amendment" in spec:
            am = dict(spec["amendment"])
            am["effective_from"] = start + timedelta(days=days // 2)
            am["signed"] = am["effective_from"] - timedelta(days=am.pop("signed_days_before", 5))
            am.pop("at", None)
            c["amendment"] = am
        out[ch] = c
    return out


def commission_rate(contracts: dict, channel: str, day: date) -> float:
    c = contracts[channel]
    am = c.get("amendment")
    if am and day >= am["effective_from"]:
        return am["commission"]
    return c["commission"]


def promo_active(promo: dict, day: date) -> bool:
    return promo["from"] <= day <= promo["to"]


def make_orders(rng: random.Random, xrng: random.Random, stores: list[dict], start: date, days: int,
                contracts: dict, profile: dict) -> list[dict]:
    orders, n = [], 10000
    menu, (ipo_lo, ipo_hi) = profile["menu"], profile["items_per_order"]
    open_m, close_m = profile.get("hours", [660, 1305])
    dayparts = profile.get("dayparts")
    part_weights = [w for _, _, w in dayparts] if dayparts else None
    cat = profile.get("catering")
    weekday, monthly = profile["seasonality"]["weekday"], profile["seasonality"]["monthly"]
    promo_rate = profile.get("promo_rate", 0.12)
    for d in range(days):
        day = start + timedelta(days=d)
        season = weekday[day.weekday()] * monthly[day.month - 1]
        midnight = datetime.combine(day, datetime.min.time())
        for st in stores:
            mult = season * st.get("size", 1)
            for ch in profile["channels"]:
                lo, hi = profile["orders_per_day"][ch]
                count = rng.randint(lo, hi)
                if mult != 1:
                    count = max(0, round(count * mult))
                for _ in range(count):
                    n += 1
                    catering = bool(cat) and ch in cat["channels"] and xrng.random() < cat["share"]
                    if catering:
                        minute = rng.randint(cat["earliest"], cat["latest"])
                    elif dayparts:
                        a, b, _w = rng.choices(dayparts, weights=part_weights)[0]
                        minute = rng.randint(a, b - 1)
                    else:
                        minute = rng.randint(open_m, close_m)
                    placed = midnight + timedelta(minutes=minute, seconds=rng.randint(0, 59))
                    if catering:
                        items = [rng.choice(cat["menu"]) for _ in range(rng.randint(*cat["items_per_order"]))]
                    else:
                        items = [rng.choice(menu) for _ in range(rng.randint(ipo_lo, ipo_hi))]
                    promo = None
                    if ch in PLATFORMS:
                        p = contracts[ch]["promo"]
                        if promo_active(p, day) and rng.random() < promo_rate:
                            promo = p["code"]
                    o = {"order_id": f"ORD-{n}", "store_id": st["store_id"], "channel": ch, "day": day,
                         "placed_at": placed, "items": items, "promo_code": promo}
                    if catering:
                        o["catering"] = True
                    orders.append(o)
    tax = {s["store_id"]: s["tax_rate"] for s in stores}
    adds = {ch: c["promo"].get("adds_items") for ch, c in contracts.items() if "promo" in c}
    for o in orders:
        if o["promo_code"] and adds.get(o["channel"]):  # e.g. BOGO needs two of the item on the ticket
            o["items"] += [tuple(x) for x in adds[o["channel"]]]
        o["subtotal"] = money(sum(p for _, p in o["items"]))
        o["tax"] = money(o["subtotal"] * tax[o["store_id"]])
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


def build_world(seed: int, n_stores: int, days: int, traps: list[int], profile: dict | str | None = None,
                trap_rate: float | None = None) -> dict:
    profile = load_profile(profile)
    rng = random.Random(seed)
    xrng = random.Random(f"{seed}:{profile['name']}")   # profile-only extras (store size, catering)
    start = date.fromisoformat(profile["base_monday"]) + timedelta(days=7 * (seed % 10))  # Monday; new seed = new week
    stores = make_stores(rng, xrng, profile, n_stores)
    contracts = build_contracts(start, days, profile)
    orders = make_orders(rng, xrng, stores, start, days, contracts, profile)
    seen_ids: dict[str, set] = {}
    lo, hi = profile.get("delivery_minutes", [28, 55])
    for o in orders:
        o["truth"] = true_payout(o, contracts)
        o["actual"] = dict(o["truth"])          # what the platform reports; traps edit this, never truth
        o["in_pos"], o["in_payout"] = True, True
        o["errors"] = []                        # error charge lines: amount, reason, delivered_at, reported_at
        seen = seen_ids.setdefault(o["channel"], set()) if o["channel"] in PLATFORMS else None  # direct ids never shown
        o["platform_order_id"] = platform_id(rng, o["channel"], seen)
        o["delivered_at"] = o["placed_at"] + timedelta(minutes=rng.randint(lo, hi))
    weeks = (days + 6) // 7
    world = {"seed": seed, "start": start, "days": days, "weeks": weeks, "stores": stores, "contracts": contracts,
             "orders": orders, "key": [], "emails": [], "refunds": [], "deposit_traps": {}, "dup_order_ids": [],
             "unseen_layout_week": None, "profile": profile,
             "trap_counts": trap_counts(profile, n_stores, days, traps, trap_rate)}
    plant_traps(rng, world, traps)
    if "doordash" in contracts and contracts["doordash"].get("amendment"):
        world["emails"].append(amendment_email(contracts["doordash"], profile))
    world["emails"].sort(key=lambda e: e["date"])
    build_payouts(rng, world)
    world["key"].sort(key=lambda r: (r["trap_no"], r["order_id"]))
    return world


def platform_id(rng: random.Random, channel: str, seen: set | None = None) -> str:
    """Platform's own order id. Re-drawn on a collision so ids stay unique at year scale."""
    while True:
        if channel == "doordash":
            pid = "".join(rng.choice("0123456789ABCDEF") for _ in range(8))
        elif channel == "ubereats":
            h = "".join(rng.choice("0123456789abcdef") for _ in range(12))
            pid = f"{h[:4]}-{h[4:8]}-{h[8:]}"
        elif channel == "grubhub":
            pid = str(rng.randint(100000000, 999999999))
        else:
            pid = f"DA{rng.randint(100000, 999999)}"
        if seen is None or pid not in seen:
            if seen is not None:
                seen.add(pid)
            return pid


# ---------------------------------------------------------------------------------------------------------------
# 2. Traps: edit what the platform reports, record the correct outcome
# ---------------------------------------------------------------------------------------------------------------

def plant_traps(rng: random.Random, world: dict, traps: list[int]) -> None:
    orders, c, start = world["orders"], world["contracts"], world["start"]
    profile, counts = world["profile"], world["trap_counts"]
    reasons = profile["error_reasons"]
    tax = {s["store_id"]: s["tax_rate"] for s in world["stores"]}
    used: set[str] = set()
    by_ch: dict[str, list[dict]] = {}
    for o in orders:
        by_ch.setdefault(o["channel"], []).append(o)

    def key(no: int, order_id: str, amount: float) -> None:
        trap_type, outcome = TRAPS[no]
        world["key"].append({"trap_no": no, "order_id": order_id, "trap_type": trap_type,
                             "expected_outcome": outcome, "amount": f"{money(amount):.2f}"})

    def picks(no: int, channel: str, pred=lambda o: True):
        """Up to counts[no] distinct unused orders, drawn from one pool built once per trap."""
        if no not in traps:
            return
        pool = [o for o in by_ch.get(channel, []) if o["order_id"] not in used and pred(o)]
        for _ in range(counts.get(no, 1)):
            if not pool:
                return
            i = rng.randrange(len(pool))
            o = pool[i]
            pool[i] = pool[-1]
            pool.pop()
            used.add(o["order_id"])
            yield o

    def reprice(o: dict) -> None:
        o["actual"]["net"] = net_of(o["actual"])

    # 14 / 15 first: they cover every order of one store on one day
    by_slot: dict[tuple, list[dict]] = {}
    by_slot_week: dict[tuple, list[dict]] = {}
    for o in orders:
        by_slot.setdefault((o["channel"], o["store_id"], o["day"]), []).append(o)
        by_slot_week.setdefault((o["channel"], o["store_id"], week_index(start, o["day"])), []).append(o)
    for no, ch, auth in ((14, "ubereats", True), (15, "grubhub", False)):
        if no not in traps or ch not in by_ch:
            continue
        slots = sorted({(o["store_id"], o["day"]) for o in by_ch[ch] if o["order_id"] not in used})
        many = counts.get(no, 1) > 1
        for _ in range(counts.get(no, 1)):
            if not slots:
                break
            i = rng.randrange(len(slots))
            store_id, day = slots[i]
            slots[i] = slots[-1]
            slots.pop()
            for o in by_slot[(ch, store_id, day)]:
                used.add(o["order_id"])
                o["truth"]["other_fees"] = 0.0      # waived per the email ...
                key(no, o["order_id"], o["actual"]["other_fees"])  # ... but the platform still charged it
            world["emails"].append(waiver_email(rng, c[ch], ch, store_id, day, auth, world["stores"], profile, many))

    for o in picks(3, "doordash"):                       # sold, never paid
        o["in_payout"] = False
        key(3, o["order_id"], net_of(o["truth"]))
    for o in picks(4, "ubereats"):                       # paid, never rang up in POS
        o["in_pos"] = False
        key(4, o["platform_order_id"], net_of(o["truth"]))  # no POS id exists
    if "doordash" in c and c["doordash"].get("amendment"):
        amend_from = c["doordash"]["amendment"]["effective_from"]
        for o in picks(7, "doordash", lambda o: o["day"] >= amend_from and not o["promo_code"] and o["subtotal"] >= 25):
            o["actual"]["commission"] = money(o["subtotal"] * c["doordash"]["commission"])  # old tier
            key(7, o["order_id"], o["actual"]["commission"] - o["truth"]["commission"])
    for o in picks(8, "ubereats"):                       # BOGO charged 100% to the store, contract says 50/50
        p = c["ubereats"]["promo"]
        if o["promo_code"] != p["code"]:
            o["promo_code"] = p["code"]
            o["items"] += [tuple(x) for x in p.get("adds_items", [])]
            o["subtotal"] = money(sum(x for _, x in o["items"]))
            o["tax"] = money(o["subtotal"] * tax[o["store_id"]])
            o["truth"] = true_payout(o, c)
            o["actual"] = dict(o["truth"])
        o["actual"]["promo_merchant_funded"] = money(p["discount"])
        key(8, o["order_id"], o["actual"]["promo_merchant_funded"] - o["truth"]["promo_merchant_funded"])
    if "grubhub" in c:
        gh_end = c["grubhub"]["promo"]["to"]
        for o in picks(9, "grubhub", lambda o: o["day"] > gh_end):
            p = c["grubhub"]["promo"]
            o["promo_code"] = p["code"]                  # promo applied after it ended; store charged its share
            o["actual"]["promo_merchant_funded"] = money(p["discount"] * p["merchant_share"])
            key(9, o["order_id"], o["actual"]["promo_merchant_funded"])
    for o in picks(10, "doordash"):                      # complaint 72h+ after delivery
        amt = money(rng.uniform(5, 14))
        o["errors"].append(error_line(rng, o, amt, reasons["late"], hours=rng.randint(80, 120)))
        key(10, o["order_id"], amt)
    for o in picks(11, "doordash"):                      # the same (legit) error charge applied twice
        amt = money(rng.uniform(4, 10))
        first = error_line(rng, o, amt, reasons["duplicate"], hours=rng.randint(1, 20))
        dup = dict(first, adj_id=adj_id(rng))
        o["errors"] += [first, dup]
        o["truth"]["error_charge"] = amt
        key(11, o["order_id"], amt)
    for o in picks(12, "grubhub", lambda o: o["subtotal"] >= 20):   # platform collected the tax AND deducted it
        o["actual"]["tax_withheld_by_platform"] = o["tax"]
        key(12, o["order_id"], o["tax"])
    for o in picks(13, "direct_app"):                    # refund log says initiated; no money moved
        amt = money(min(o["subtotal"], rng.choice(profile["refund_amounts"])))
        world["refunds"].append({"refund_id": f"RF-{rng.randint(10000, 99999)}", "order_id": o["order_id"],
                                 "amount": amt, "status": "initiated",
                                 "initiated_at": o["placed_at"] + timedelta(hours=2), "completed_at": None})
        key(13, o["order_id"], amt)
    for o in picks(16, "doordash"):                      # legit error charge: must NOT be disputed
        amt = money(rng.uniform(4, 12))
        o["errors"].append(error_line(rng, o, amt, reasons["legit"], hours=rng.randint(1, 30)))
        o["truth"]["error_charge"] = amt
        key(16, o["order_id"], 0.0)
    for o in picks(17, "ubereats", lambda o: not o["promo_code"]):   # penny rounding
        o["actual"]["commission"] = money(o["actual"]["commission"] + 0.01)
        key(17, o["order_id"], 0.0)
    if 18 in traps and "direct_app" in by_ch and world["weeks"] >= 2:   # processing fee creep: no deal term covers it
        slots = sorted({(o["store_id"], week_index(start, o["day"])) for o in by_ch["direct_app"]
                        if week_index(start, o["day"]) >= 1 and o["order_id"] not in used})
        for _ in range(counts.get(18, 1)):
            if not slots:
                break
            i = rng.randrange(len(slots))
            store_id, wk = slots[i]
            slots[i] = slots[-1]
            slots.pop()
            bump_pct, bump_fixed = rng.choice([0.006, 0.008, 0.010]), rng.choice([0.05, 0.10])
            for o in by_slot_week[("direct_app", store_id, wk)]:
                if o["order_id"] in used:
                    continue
                used.add(o["order_id"])
                extra = money(o["subtotal"] * bump_pct + bump_fixed)
                o["actual"]["other_fees"] = money(o["actual"]["other_fees"] + extra)   # truth keeps the contract rate
                key(18, o["order_id"], extra)
    for o in picks(1, "doordash"):                       # export glitch: rows appear twice, byte for byte
        world["dup_order_ids"].append(o["order_id"])
        key(1, o["order_id"], 0.0)
    if 2 in traps and world["weeks"] >= 2:               # last Uber Eats week arrives in a layout nobody mapped
        world["unseen_layout_week"] = world["weeks"] - 1
        last = [o for o in by_ch.get("ubereats", []) if week_index(start, o["day"]) == world["weeks"] - 1
                and o["in_payout"] and o["order_id"] not in used]
        if last:
            first = min(last, key=lambda o: o["placed_at"])
            used.add(first["order_id"])
            key(2, first["order_id"], 0.0)
    if 5 in traps and world["weeks"] >= 2:               # pairs of Grubhub weeks land as one deposit
        free = list(range(2, world["weeks"] - 1))
        pairs = [0]
        for _ in range(min(counts.get(5, 1), world["weeks"] // 2) - 1):
            ok = [i for i in free if i - 1 not in pairs and i not in pairs and i + 1 not in pairs]
            if not ok:
                break
            pairs.append(ok[rng.randrange(len(ok))])
        world["deposit_traps"]["grubhub"] = [("combine", i) for i in pairs]
    if 6 in traps:                                        # Uber Eats deposits short of their statement
        idx = [0]
        free = list(range(1, world["weeks"]))
        for _ in range(min(counts.get(6, 1), world["weeks"]) - 1):
            idx.append(free.pop(rng.randrange(len(free))))
        world["deposit_traps"]["ubereats"] = [("short", i, money(rng.uniform(15, 60))) for i in idx]

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
        k = (o["channel"], w)
        if k not in payouts:
            wk_start = start + timedelta(days=7 * w)
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
    for ch, plan in world["deposit_traps"].items():
        mine = [d for d in deposits if d["channel"] == ch]
        combine = [(mine[t[1]], mine[t[1] + 1]) for t in plan if t[0] == "combine" and t[1] + 1 < len(mine)]
        for a, b in combine:                                 # trap 5: two weeks land as one deposit
            b["amount"] = money(a["amount"] + b["amount"])
            b["payout_ids"] = a["payout_ids"] + b["payout_ids"]
            deposits.remove(a)
            world["key"].append({"trap_no": 5, "order_id": a["payout_ids"][0], "trap_type": TRAPS[5][0],
                                 "expected_outcome": TRAPS[5][1], "amount": "0.00"})
        for t in plan:                                       # trap 6: deposit is less than the statement says
            if t[0] == "short" and t[1] < len(mine):
                d = mine[t[1]]
                d["amount"] = money(d["amount"] - t[2])
                world["key"].append({"trap_no": 6, "order_id": d["payout_ids"][0], "trap_type": TRAPS[6][0],
                                     "expected_outcome": TRAPS[6][1], "amount": f"{t[2]:.2f}"})
    world["payouts"] = payouts
    world["deposits"] = deposits


# ---------------------------------------------------------------------------------------------------------------
# 4. Documents: contracts, amendment, emails (templated text, numbers from the true world)
# ---------------------------------------------------------------------------------------------------------------

def contract_doc(ch: str, c: dict, profile: dict) -> dict:
    if ch == "direct_app":
        clauses = [
            {"section": "1. Services", "text": f"{c['platform_name']} processes card payments for orders placed in the "
             f"{profile['app_name']} and settles funds weekly to the Merchant's operating account."},
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
           "parties": [c["counterparty"], profile["legal_entity"]],
           "applies_to_stores": "all", "effective_from": c["effective_from"].isoformat(),
           "effective_to": c["effective_to"].isoformat(), "clauses": clauses, "terms": contract_terms(ch, c, clauses)}
    if ch != "direct_app":
        doc["schedule_b_authorized_contacts"] = [{"name": n, "email": e, "title": t} for n, e, t in c["authorized_contacts"]]
    if profile.get("notice"):
        doc["notice"] = profile["notice"]
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


def amendment_doc(c: dict, profile: dict) -> dict:
    am = c["amendment"]
    text = (f"Effective {am['effective_from']:%B %d, %Y}, Merchant moves from the {c['tier']} plan to the {am['tier']} "
            f"plan. Section 2 is amended so that {c['platform_name']} shall retain a commission of "
            f"{am['commission'] * 100:.0f}% of the food subtotal of each order. All other terms remain unchanged.")
    doc = {"doc_type": "amendment", "amendment_id": am["amendment_id"], "amends": c["contract_id"],
           "channel": "doordash", "applies_to_stores": "all", "signed_on": am["signed"].isoformat(),
           "effective_from": am["effective_from"].isoformat(), "effective_to": c["effective_to"].isoformat(),
           "clauses": [{"section": "A1. Commission", "text": text}],
           "terms": [{"term_type": "commission", "store_id": "all", "value": am["commission"], "quote": text,
                      "effective_from": am["effective_from"].isoformat(),
                      "effective_to": c["effective_to"].isoformat()}]}
    if profile.get("notice"):
        doc["notice"] = profile["notice"]
    return doc


def amendment_email(c: dict, profile: dict) -> dict:
    am = c["amendment"]
    name, email, title = c["authorized_contacts"][0]
    body = (f"Hi team,\n\nAttached is the countersigned amendment {am['amendment_id']}. Starting "
            f"{am['effective_from']:%A, %B %d}, all of your stores move to the {am['tier']} plan at "
            f"{am['commission'] * 100:.0f}% commission (down from {c['commission'] * 100:.0f}%).\n\n"
            f"Thanks for growing with us,\n{name}\n{title}, {c['platform_name']}")
    return {"date": datetime.combine(am["signed"], datetime.min.time()) + timedelta(hours=10, minutes=12),
            "from": f"{name} <{email}>", "to": f"accounting@{profile['email_domain']}",
            "subject": f"Signed: {am['amendment_id']} plan change", "body": body, "channel": "doordash",
            "slug": "doordash_amendment_notice", "sender_authorized": True,
            "claims": [{"term_type": "commission", "store_id": "all", "value": am["commission"],
                        "quote": body.split("\n\n")[1], "effective_from": am["effective_from"].isoformat(),
                        "effective_to": c["effective_to"].isoformat()}]}


def waiver_email(rng: random.Random, c: dict, ch: str, store_id: str, day: date, authorized: bool,
                 stores: list[dict], profile: dict, dated_slug: bool = False) -> dict:
    store = next(s for s in stores if s["store_id"] == store_id)
    if authorized:
        name, email, title = c["authorized_contacts"][0]
    else:
        name, email, title = profile["unauthorized_rep"]
    body = (f"Hey there,\n\nThanks for hopping on the call. As promised, we'll waive the {c['fee_name']} "
            f"(${c['fee']:.2f}/order) for {store['name']} ({store_id}) on {day:%A, %B %d} for your local "
            f"event. You don't need to do anything, it'll come off automatically.\n\nBest,\n{name}\n{title}")
    sent = datetime.combine(day - timedelta(days=rng.randint(2, 4)), datetime.min.time()) + timedelta(hours=15, minutes=rng.randint(0, 59))
    slug = f"{ch}_fee_waiver_{store_id.lower()}" + (f"_{day:%Y%m%d}" if dated_slug else "")
    return {"date": sent, "from": f"{name} <{email}>", "to": f"gm.{store_id.lower()}@{profile['email_domain']}",
            "subject": f"{c['fee_name']} waiver for {store_id}", "body": body, "channel": ch,
            "slug": slug, "sender_authorized": authorized,
            "claims": [{"term_type": "fee", "store_id": store_id, "value": 0.0, "quote": body.split("\n\n")[1],
                        "effective_from": day.isoformat(), "effective_to": day.isoformat()}]}


# ---------------------------------------------------------------------------------------------------------------
# 5. Render each source in its own messy format (CSV rows are streamed to disk, never built up in memory)
# ---------------------------------------------------------------------------------------------------------------

def usd(x: float) -> str:
    """Uber Eats style: "$12.50", negatives "($3.75)"."""
    x = money(x)
    return f"(${abs(x):,.2f})" if x < 0 else f"${x:,.2f}"


def neg(x: float) -> str:
    """DoorDash style: deductions as negative numbers, never "-0.00"."""
    return f"{money(-x):.2f}"


def write_csv(path: Path, header: list[str], rows) -> None:
    """rows can be any iterable (a generator streams straight to disk)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def render(world: dict, out: Path, progress=None) -> None:
    orders = sorted(world["orders"], key=lambda o: o["placed_at"])
    start = world["start"]
    chans: dict[str, list[dict]] = {ch: [] for ch in ("doordash", "ubereats", "grubhub", "direct_app")}
    for o in orders:
        chans[o["channel"]].append(o)
    by_ch = chans.__getitem__
    store_name = {s["store_id"]: s["name"] for s in world["stores"]}
    c, profile = world["contracts"], world["profile"]
    memo = {**BANK_MEMO, **profile.get("bank_memo", {})}
    dup_ids = set(world["dup_order_ids"])
    ptot = lambda o: f"{c[o['channel']]['promo']['discount'] if o['promo_code'] else 0:.2f}"  # full promo cost
    step = (lambda name: progress.advance(1)) if progress else (lambda name: None)  # one step per source

    # POS: marketplace orders only, clean-ish, local time
    step("render pos")
    channel_label = {"doordash": "DoorDash", "ubereats": "Uber Eats", "grubhub": "Grubhub"}
    write_csv(out / "pos_orders.csv",
              ["Check #", "Location", "Order Source", "Opened", "Net Sales", "Tax", "Discount Code", "Ext Ref"],
              ([o["order_id"], o["store_id"], channel_label[o["channel"]], o["placed_at"].strftime("%Y-%m-%d %H:%M:%S"),
                f"{o['subtotal']:.2f}", f"{o['tax']:.2f}", o["promo_code"] or "", o["platform_order_id"]]
               for o in orders if o["channel"] in PLATFORMS and o["in_pos"]))

    # DoorDash: one file, negative deductions, error charges as their own lines
    step("render doordash")

    def dd_rows():
        tx = 0
        for o in by_ch("doordash"):
            if not o["in_payout"]:
                continue
            a = o["actual"]
            tx += 1
            row = [f"TX{tx:07d}", "Order", o["store_id"], o["platform_order_id"], o["order_id"],
                   o["placed_at"].strftime("%Y-%m-%dT%H:%M:%S"), f"{a['gross']:.2f}", o["promo_code"] or "", ptot(o),
                   neg(a['commission']), neg(a['promo_merchant_funded']), "0.00", neg(a['tax_withheld_by_platform']),
                   neg(a['other_fees']), f"{a['net']:.2f}", o["payout_id"], o["payout_date"].isoformat(), "", "", ""]
            yield row
            if o["order_id"] in dup_ids:
                yield list(row)  # trap 1: exact duplicate row
            for e in o["errors"]:
                yield [e["adj_id"], "Error Charge", o["store_id"], o["platform_order_id"], o["order_id"],
                       o["placed_at"].strftime("%Y-%m-%dT%H:%M:%S"), "0.00", "", "0.00", "0.00", "0.00",
                       neg(e['amount']), "0.00", "0.00", neg(e['amount']), o["payout_id"],
                       o["payout_date"].isoformat(), e["reason"], e["delivered_at"].strftime("%Y-%m-%dT%H:%M:%S"),
                       e["reported_at"].strftime("%Y-%m-%dT%H:%M:%S")]
    write_csv(out / "doordash_payouts.csv",
              ["Transaction ID", "Transaction Type", "Store ID", "DoorDash Order ID", "Merchant Order Ref",
               "Order Placed", "Subtotal", "Promo Code", "Promo Total", "Commission", "Merchant Promo Contribution",
               "Error Charge",
               "Tax Withheld", "Other Fees", "Net Payout", "Payout ID", "Payout Date", "Error Reason",
               "Delivered At", "Reported At"], dd_rows())

    # Uber Eats: one statement per week; "$" text; last week in an unseen layout (trap 2)
    step("render ubereats")
    ue_weeks: dict[int, list[dict]] = {}
    for o in by_ch("ubereats"):
        if o["in_payout"]:
            ue_weeks.setdefault(week_index(start, o["day"]), []).append(o)
    for w in range(world["weeks"]):
        wk = ue_weeks.get(w, [])
        wk_start = start + timedelta(days=7 * w)
        name = f"ubereats_statement_{wk_start:%Y-%m-%d}.csv"
        if w == world["unseen_layout_week"]:
            write_csv(out / name,
                      ["order_reference", "pos_check_no", "location_code", "ordered_at", "gross_sales",
                       "promo_code", "total_promo_value", "platform_commission", "merchant_funded_promo",
                       "tax_withheld", "service_fees", "adjustment_total", "net_payout", "payout_ref", "paid_on"],
                      ([o["platform_order_id"], o["order_id"], o["store_id"], o["placed_at"].strftime("%Y/%m/%d %H:%M"),
                        f"{o['actual']['gross']:.2f}", o["promo_code"] or "", ptot(o),
                        f"{o['actual']['commission']:.2f}", f"{o['actual']['promo_merchant_funded']:.2f}",
                        f"{o['actual']['tax_withheld_by_platform']:.2f}", f"{o['actual']['other_fees']:.2f}", "0.00",
                        f"{o['actual']['net']:.2f}", o["payout_id"], o["payout_date"].strftime("%Y/%m/%d")] for o in wk))
        else:
            write_csv(out / name,
                      ["Order UUID", "Merchant Ref", "Restaurant", "Date/Time", "Food Sales", "Promotion",
                       "Promotion Value", "Uber Service Fee",
                       "Promotions on Items (Merchant)", "Tax Withheld", "Marketing Fee", "Adjustments",
                       "Total Payout", "Payout Reference", "Payout Date"],
                      ([o["platform_order_id"], o["order_id"], f"{store_name[o['store_id']]} ({o['store_id']})",
                        o["placed_at"].strftime("%m/%d/%Y %I:%M %p"), usd(o["actual"]["gross"]), o["promo_code"] or "",
                        usd(float(ptot(o))), usd(-o["actual"]["commission"]), usd(-o["actual"]["promo_merchant_funded"]),
                        usd(-o["actual"]["tax_withheld_by_platform"]), usd(-o["actual"]["other_fees"]), usd(0),
                        usd(o["actual"]["net"]), o["payout_id"], o["payout_date"].strftime("%m/%d/%Y")] for o in wk))

    step("render grubhub")
    if "grubhub" in c:
        render_grubhub(world, by_ch("grubhub"), out / "grubhub_statement.xlsx")

    # Direct app: ordering system export + processor settlements + refund log
    step("render direct app + bank")
    da = by_ch("direct_app")
    write_csv(out / "direct_app_orders.csv", ["id", "store", "created", "items_total", "tax_amt", "coupon"],
              ([o["order_id"], o["store_id"], o["placed_at"].strftime("%d-%b-%Y %H:%M"), f"{o['subtotal']:.2f}",
                f"{o['tax']:.2f}", o["promo_code"] or ""] for o in da))
    write_csv(out / "direct_app_settlements.csv",
              ["settlement_line", "order", "store", "charge_amount", "processing_fee", "net", "payout", "payout_date"],
              ([f"SL-{i + 1:06d}", o["order_id"], o["store_id"], f"{o['actual']['gross']:.2f}",
                f"{o['actual']['other_fees']:.2f}", f"{o['actual']['net']:.2f}", o["payout_id"],
                o["payout_date"].isoformat()] for i, o in enumerate(da) if o["in_payout"]))
    write_csv(out / "direct_app_refund_log.csv",
              ["refund_id", "order_id", "amount", "status", "initiated_at", "completed_at"],
              [[r["refund_id"], r["order_id"], f"{r['amount']:.2f}", r["status"],
                r["initiated_at"].strftime("%Y-%m-%d %H:%M"), ""] for r in world["refunds"]])

    # Bank: weekly lumps, no order detail
    write_csv(out / "bank_deposits.csv", ["Posting Date", "Description", "Amount", "Reference"],
              [[d["date"].strftime("%m/%d/%y"), memo[d["channel"]].format(po=" + ".join(d["payout_ids"])),
                f"{d['amount']:,.2f}", d["deposit_id"]] for d in sorted(world["deposits"], key=lambda d: (d["date"], d["channel"]))])

    # Contracts, amendment, emails
    for ch in ["doordash", "ubereats", "grubhub", "direct_app"]:
        if ch in c:
            write_json(out / "contracts" / f"{ch}_contract.json", contract_doc(ch, c[ch], profile))
    if "doordash" in c and c["doordash"].get("amendment"):
        write_json(out / "contracts" / "doordash_amendment_1.json", amendment_doc(c["doordash"], profile))
    for e in world["emails"]:
        doc = {"kind": "email", "from": e["from"], "to": e["to"], "date": e["date"].isoformat(),
               "subject": e["subject"], "sender_authorized": e["sender_authorized"], "channel": e["channel"],
               "body": e["body"], "claims": e["claims"]}
        if profile.get("notice"):
            doc["notice"] = profile["notice"]
        write_json(out / "emails" / f"{e['date']:%Y-%m-%d}_{e['slug']}.json", doc)


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def render_grubhub(world: dict, gh: list[dict], path: Path) -> None:
    rows = [o for o in gh if o["in_payout"]]
    summary = [["Grubhub for Restaurants - Payout Summary"], [],
               ["Payout ID", "Period Start", "Period End", "Orders", "Gross Sales", "Fees & Adjustments",
                "Net Payout", "Payout Date"]]
    for (ch, w), p in sorted(world["payouts"].items(), key=lambda kv: kv[0][1]):
        if ch != "grubhub":
            continue
        summary.append([p["payout_id"], p["period"][0], p["period"][1], p["count"], p["gross"],
                        money(p["gross"] - p["net"]), p["net"], p["payout_date"]])
    promo_val = world["contracts"]["grubhub"]["promo"]["discount"]
    detail_head = [["Grubhub for Restaurants - Transaction Detail"],
                   [f"Generated for {world['profile']['legal_short']}, period starting {world['start']:%m/%d/%Y}"], [],
                   ["GH Order #", "Restaurant ID", "POS Order No.", "Order Date", "Subtotal", "Promo Code", "Promo Total",
                    "Grubhub Commission",
                    "Promo - Restaurant Funded", "Sales Tax Withheld", "Delivery Network Fee", "Adjustment", "Net",
                    "Payout ID", "Payout Date"]]

    def detail_rows():
        for o in rows:
            a = o["actual"]
            yield [int(o["platform_order_id"]), f"GH-{o['store_id']}", o["order_id"], o["placed_at"], a["gross"],
                   o["promo_code"] or "", promo_val if o["promo_code"] else 0.0, a["commission"],
                   a["promo_merchant_funded"], a["tax_withheld_by_platform"], a["other_fees"], 0.0,
                   a["net"], o["payout_id"], o["payout_date"]]

    path.parent.mkdir(parents=True, exist_ok=True)
    if len(rows) > XLSX_STREAM_ROWS:   # year scale: stream the sheet XML ourselves (openpyxl is ~10x slower)
        fast_xlsx(path, [("Summary", summary, len(summary), 8),
                         ("Detail", chain(detail_head, detail_rows()), len(detail_head) + len(rows), 15)])
        return

    from openpyxl import Workbook

    wb = Workbook()
    summ = wb.active
    summ.title = "Summary"
    for r in summary:
        summ.append(r)
    det = wb.create_sheet("Detail")
    for r in chain(detail_head, detail_rows()):
        det.append(r)
    for row in det.iter_rows(min_row=5):
        row[3].number_format = "mm/dd/yyyy hh:mm"
        row[14].number_format = "mm/dd/yyyy"
    fixed = datetime(2026, 1, 1)
    wb.properties.creator = "grubhub-reports"
    wb.properties.created = fixed
    buf = io.BytesIO()
    wb.save(buf)
    pin_xlsx(buf.getvalue(), path)


XLSX_PARTS = {
    "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{sheets}</Types>',
    "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    "xl/styles.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="2"><numFmt numFmtId="164" formatCode="mm/dd/yyyy hh:mm"/><numFmt numFmtId="165" formatCode="mm/dd/yyyy"/></numFmts><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>',
}


def fast_xlsx(path: Path, sheets: list[tuple[str, object, int, int]]) -> None:
    """Minimal streaming xlsx writer for big generated statements: inline strings, numbers, and dates as Excel
    serials with a date format (openpyxl reads them back as datetimes, exactly as from its own writer).
    sheets: (name, rows, row count, column count); the counts go in <dimension> so readers needn't scan."""
    from xml.sax.saxutils import escape

    from openpyxl.utils import get_column_letter
    from openpyxl.utils.datetime import to_excel

    letters = [get_column_letter(i + 1) for i in range(64)]
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    stamp = (2026, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        def put(name: str, body: str) -> None:
            zi = zipfile.ZipInfo(name, date_time=stamp)
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, body)

        overrides = "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
                            f'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                            for i in range(1, len(sheets) + 1))
        put("[Content_Types].xml", XLSX_PARTS["[Content_Types].xml"].format(sheets=overrides))
        put("_rels/.rels", XLSX_PARTS["_rels/.rels"])
        put("xl/styles.xml", XLSX_PARTS["xl/styles.xml"])
        put("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="{ns}" '
            f'xmlns:r="{rel}"><sheets>' + "".join(f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>'
                                                   for i, (n, *_) in enumerate(sheets, 1)) + "</sheets></workbook>")
        put("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                      for i in range(1, len(sheets) + 1))
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
        for i, (_, rows, nrows, ncols) in enumerate(sheets, 1):
            zi = zipfile.ZipInfo(f"xl/worksheets/sheet{i}.xml", date_time=stamp)
            zi.compress_type = zipfile.ZIP_DEFLATED
            with z.open(zi, "w") as fh:
                fh.write(f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="{ns}">'
                         f'<dimension ref="A1:{letters[ncols - 1]}{max(1, nrows)}"/><sheetData>'.encode())
                buf = []
                for r, row in enumerate(rows, 1):
                    out = [f'<row r="{r}">']
                    for c, v in enumerate(row):
                        if v is None:
                            continue
                        ref = f"{letters[c]}{r}"
                        if isinstance(v, str):
                            out.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(v)}</t></is></c>')
                        elif isinstance(v, (datetime, date)):
                            style = 1 if isinstance(v, datetime) else 2
                            out.append(f'<c r="{ref}" s="{style}"><v>{to_excel(v)!r}</v></c>')
                        else:
                            out.append(f'<c r="{ref}"><v>{v!r}</v></c>')
                    out.append("</row>")
                    buf.append("".join(out))
                    if len(buf) >= 5000:
                        fh.write("".join(buf).encode())
                        buf = []
                fh.write(("".join(buf) + "</sheetData></worksheet>").encode())


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


def generate(seed: int = 42, stores: int | None = 12, days: int = 14, traps: list[int] | None = None,
             out: Path = ROOT / "generator" / "out", profile: str | dict | None = None,
             trap_rate: float | None = None, progress=None) -> dict:
    """stores=None = the profile's own store count. Returns the world (orders, key, ...)."""
    traps = sorted(TRAPS) if traps is None else traps
    prof = load_profile(profile)
    if progress:
        progress.stage("generate", total=6, unit="steps")
    world = build_world(seed, stores or prof["default_stores"], days, traps, prof, trap_rate)
    if progress:
        progress.advance(1)
    out = Path(out)
    prepare_out(out)
    render(world, out, progress)
    write_answer_key(world["key"], out / "answer_key.csv")
    return world


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a fake franchise group's payout data with planted traps.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--profile", default=DEFAULT_PROFILE, help=f"company profile: {', '.join(list_profiles())}")
    ap.add_argument("--stores", type=int, default=None, help="default: the profile's store count (12 for default)")
    ap.add_argument("--weeks", type=int, default=2)
    ap.add_argument("--days", type=int, default=None, help="overrides --weeks (e.g. --days 1)")
    ap.add_argument("--traps", default="all", help="comma list of trap numbers 1-18, or 'all'")
    ap.add_argument("--trap-rate", type=float, default=None,
                    help="instances of each trap per 100 store-weeks (default: the profile's, 2.0); min 1 each")
    ap.add_argument("--mini", action="store_true", help="first milestone: 1 store x 1 day, traps 3,7,16")
    ap.add_argument("--out", default=str(ROOT / "generator" / "out"))
    ap.add_argument("--write-answer-key", action="store_true",
                    help="ALSO overwrite data/answer_key.csv (the demo scoreboard uses it)")
    a = ap.parse_args()
    prof = load_profile(a.profile)
    stores, days = a.stores or prof["default_stores"], a.days or a.weeks * 7
    traps = sorted(TRAPS) if a.traps == "all" else [int(t) for t in a.traps.split(",") if t.strip()]
    if a.mini:
        stores, days, traps = 1, 1, MINI_TRAPS
    bad = [t for t in traps if t not in TRAPS]
    if bad:
        raise SystemExit(f"unknown trap numbers: {bad}")
    world = generate(a.seed, stores, days, traps, Path(a.out), prof, a.trap_rate)
    planted = sorted({r["trap_no"] for r in world["key"]})
    skipped = [t for t in traps if t not in planted]
    tag = "" if a.profile == DEFAULT_PROFILE else f" [{prof['name']}, simulated]"
    print(f"seed {a.seed}: {stores} stores x {days} days from {world['start']} -> {a.out}{tag}")
    print(f"  {len(world['orders'])} orders, {len(world['deposits'])} bank deposits, {len(world['emails'])} emails")
    print(f"  traps planted: {planted} ({len(world['key'])} answer key rows)")
    if skipped:
        print(f"  traps skipped (need more stores/days, e.g. 2 and 5 need --weeks 2): {skipped}")
    if a.write_answer_key:
        write_answer_key(world["key"], ROOT / "data" / "answer_key.csv")
        print("  wrote data/answer_key.csv (restore the demo key with: git checkout data/answer_key.csv)")


if __name__ == "__main__":
    main()
