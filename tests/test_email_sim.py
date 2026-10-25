"""Email simulator: a month's inbox built from the generated world; the Deal Keeper reads authority from the sender;
the expected outcomes join the month's answer key; a live month turns them into the right findings."""
import csv
import json

import pytest

from flock import connectors, email_sim
from flock.agents import collector
from flock.demo import generator


@pytest.fixture(scope="module")
def month(tmp_path_factory):
    base = tmp_path_factory.mktemp("es")
    world = generator.generate(7, 6, out=base / "out", scenario="demo_month", month="2026-09")
    collector.collect(base / "out", base / "raw")
    return base, world


def test_inbox_is_simulated_valid_and_consistent(month):
    _, world = month
    inbox = email_sim.build(world)
    p = email_sim.payload(inbox["items"])
    assert connectors.validate(p) == [] and p["simulated"] is True
    assert all(a.split("@")[-1].rstrip(">").endswith(".example") for it in inbox["items"] for a in (it["from"], it["to"]))
    kinds = {r["trap_type"] for r in inbox["key"]}
    assert kinds == {"email_authorized_waiver", "email_unauthorized_promise", "email_thread_confirmed_waiver"}
    planted = {r["order_id"] for r in world["key"]}
    assert not planted & {r["order_id"] for r in inbox["key"]}, "never on an order another trap already uses"
    assert any(it.get("in_reply_to") for it in inbox["items"]), "one threaded reply"
    assert len(inbox["attachments"]) == 2 and inbox["attachments"][0][1].startswith("statement_month,store_id")
    assert email_sim.build(world)["items"] == inbox["items"], "deterministic for a seed"


def test_inject_reads_authority_and_extends_the_key(month):
    base, world = month
    before = len(world["key"])
    rec = email_sim.inject(world, base / "out", base / "raw", base / "state")
    auth = [c for c in rec["claims"] if c["binding"]]
    unauth = [c for c in rec["claims"] if not c["binding"]]
    assert len(auth) == 2 and all(c["authority"] == "auth_email" for c in auth)
    assert len(unauth) == 1 and unauth[0]["authority"] == "unauth_email"
    assert rec["connector"] == "email-sim" and rec["source"] == "email"
    rows = list(csv.DictReader((base / "out" / "answer_key.csv").open()))
    assert len(rows) == before + rec["key_rows"] and rec["key_rows"] > 0
    assert list((base / "raw" / "emails").glob("*.eml")), "in raw/emails for the pipeline"
    assert all(p.endswith(".csv") for p in rec["attachments"])
    assert connectors.received(base / "state")[0]["connector"] == "email-sim"


def test_live_month_turns_the_inbox_into_findings(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOCK_OFFLINE", "1")
    from flock import showtime
    from flock.store import JsonStore
    st = JsonStore(tmp_path / "state")
    showtime.live(seed=11, stores=4, month="2026-10", store=st, rules_path=tmp_path / "rules.json",
                  portal=tmp_path / "portal.jsonl", workdir=tmp_path / "ws")
    key = {r["order_id"]: r for r in csv.DictReader((tmp_path / "ws" / "2026-10" / "out" / "answer_key.csv").open())
           if r["trap_type"].startswith("email_")}
    assert key
    got = {}
    for f in st.all():
        for o in f["order_ids"]:
            if o in key:
                got.setdefault(key[o]["trap_type"], set()).add(f["outcome"])
    assert got.get("email_authorized_waiver") == {"recoverable"}
    assert got.get("email_unauthorized_promise") == {"conflict"}
    info = json.loads((tmp_path / "state" / showtime.LIVE_JSON).read_text())
    assert info["months"][-1]["false_alarms"] == 0


def test_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOCK_OFFLINE", "1")
    monkeypatch.setenv("FLOCK_EMAIL_SIM", "0")
    from flock import showtime
    from flock.store import JsonStore
    st = JsonStore(tmp_path / "state")
    showtime.live(seed=11, stores=3, month="2026-10", store=st, rules_path=tmp_path / "rules.json",
                  portal=tmp_path / "portal.jsonl", workdir=tmp_path / "ws")
    assert connectors.received(tmp_path / "state") == []
