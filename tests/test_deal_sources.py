"""Deal changes scattered across email (.eml), Slack exports and call notes, read with one authority rule."""
import csv
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from flock import connectors, demo, llm
from flock.agents import collector, deal_keeper
from flock.store import JsonStore
from generator import generate as gen

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "raw"


def read(path):
    with Path(path).open(newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def bagel(tmp_path_factory):
    """The simulated bagel chain renders every deal source: .eml, a Slack export and call notes."""
    base = tmp_path_factory.mktemp("sources")
    w = gen.generate(seed=42, stores=6, days=14, out=base / "out", profile="ca_bagel_chain")
    collector.collect(base / "out", base / "raw")
    got = deal_keeper.read_sources(base / "raw" / "contracts", base / "raw" / "emails")
    return {"w": w, "base": base, "raw": base / "raw", "got": got, "key": read(base / "out" / "answer_key.csv")}


def by_medium(got, medium):
    return [c for c in got["claims"] if c.get("medium") == medium]


def text_of(source_file):
    p = Path(source_file)
    return (p if p.is_absolute() else ROOT / p).read_text()


def source_text(c):
    return text_of(c["source_file"])


# generator -------------------------------------------------------------------------------------------------------

def test_new_sources_only_where_asked(tmp_path, bagel):
    gen.generate(seed=42, stores=12, days=14, out=tmp_path / "d")
    assert not (tmp_path / "d" / "slack").exists() and not (tmp_path / "d" / "notes").exists()
    assert sorted(p.suffix for p in (tmp_path / "d" / "emails").iterdir()) == [".json"] * 3
    contract = json.loads((tmp_path / "d" / "contracts" / "ubereats_contract.json").read_text())
    assert "authorized_contacts" not in contract                     # default output stays byte-identical
    out = bagel["base"] / "out"
    assert {p.suffix for p in (out / "emails").iterdir()} == {".eml"}
    assert (out / "slack" / "users.json").exists() and (out / "slack" / "channels.json").exists()
    assert {p.name for p in (out / "slack").iterdir() if p.is_dir()} >= {"ubereats-partnership", "grubhub-partnership",
                                                                         "doordash-partnership", "ops-promos"}
    assert list((out / "notes").glob("*.md"))
    ue = json.loads((out / "contracts" / "ubereats_contract.json").read_text())
    assert "sam.takahashi@ubereats.example" in ue["authorized_contacts"] and "@sam.takahashi" in ue["authorized_contacts"]
    with pytest.raises(SystemExit, match="unknown --sources"):
        gen.resolve_sources({}, "email,fax")
    assert gen.resolve_sources({}, None) == ["json"] and gen.resolve_sources({}, "notes,email") == ["email", "notes"]


def test_new_traps_in_the_answer_key(bagel):
    per = Counter((r["trap_type"], r["expected_outcome"]) for r in bagel["key"])
    for no in gen.SOURCE_TRAPS:
        t, outcome = gen.TRAPS[no]
        assert per[(t, outcome)] > 0, t
    assert len({r["order_id"] for r in bagel["key"]}) == len(bagel["key"])   # one trap per order


def test_slack_export_layout(bagel):
    slack = bagel["base"] / "out" / "slack"
    users = {u["id"]: u for u in json.loads((slack / "users.json").read_text())}
    msgs = [m for f in slack.glob("*/*.json") for m in json.loads(f.read_text())]
    assert all(m["user"] in users and re.match(r"^\d+\.\d{6}$", m["ts"]) for m in msgs)
    replies = [m for m in msgs if m.get("thread_ts") and m["thread_ts"] != m["ts"]]
    assert replies and all(any(p["ts"] == r["thread_ts"] for p in msgs) for r in replies)
    assert any(m.get("reactions") for m in msgs) and any(m.get("edited") for m in msgs)
    shared = [c for c in json.loads((slack / "channels.json").read_text()) if c["is_ext_shared"]]
    assert shared and all("SIMULATED" in c["purpose"]["value"] for c in shared)


# readers -----------------------------------------------------------------------------------------------------------

def test_every_quote_is_verbatim_with_a_locator(bagel):
    msgs = [c for c in bagel["got"]["claims"] if c.get("medium")]
    assert {c["medium"] for c in msgs} == {"email", "slack", "call_note"}
    for c in msgs:
        assert c["quote"] and c["quote"] in source_text(c), c
        assert c["locator"].startswith(("line ", "ts="))


def test_eml_amendment_notice_and_quoted_history(bagel):
    eml = by_medium(bagel["got"], "email")
    notice = next(c for c in eml if c["term_type"] == "commission")
    am = bagel["w"]["contracts"]["doordash"]["amendment"]
    assert notice["authority"] == "auth_email" and notice["value"] == am["commission"]
    assert notice["effective_from"] == am["effective_from"].isoformat() and notice["channel"] == "doordash"
    assert "authorized contacts" in notice["authority_basis"]
    text = source_text(notice)
    assert "> Hi, our records show 23% commission" in text               # quoted history is in the file ...
    assert not any(c["value"] == 0.23 for c in eml)                     # ... but never read as a new claim
    assert int(notice["locator"].split()[1]) == text[:text.index(notice["quote"])].count("\n") + 1
    waivers = {c["channel"]: c["authority"] for c in eml if c["term_type"] == "fee"}
    assert waivers == {"ubereats": "auth_email", "grubhub": "unauth_email"}    # from the list, not a flag
    assert not any("newsletter" in c["source_file"] for c in eml)       # noise email: nothing


def test_slack_authority_comes_from_authorized_contacts(bagel):
    slack = by_medium(bagel["got"], "slack")
    by_auth = Counter((c["channel"], c["authority"]) for c in slack)
    assert by_auth[("ubereats", "auth_email")] >= 2 and by_auth[("grubhub", "unauth_email")] >= 1
    unauth = next(c for c in slack if c["authority"] == "unauth_email")
    assert unauth["edited"] and "(edited)" in unauth["locator"] and "riley.okafor@grubhub.example" in unauth["sender"]
    assert {c["slack_channel"] for c in slack} <= {"#ubereats-partnership", "#grubhub-partnership"}


def test_thread_correction_latest_binding_message_wins(bagel):
    got, w = bagel["got"], bagel["w"]
    assert len(got["superseded"]) >= 1
    for old in got["superseded"]:
        assert old["superseded_by"].startswith("ts=") and "thread_ts=" in old["superseded_by"]
        new = next(c for c in got["claims"] if c.get("locator") == old["superseded_by"])
        assert new["store_id"] == old["store_id"] and new["quote"].startswith("Correction:")
        assert new["effective_from"] > old["effective_from"]
        # the retracted day has no waiver in effect; the corrected day does
        on_old, _ = deal_keeper.effective(got["claims"], "ubereats", old["store_id"], "fee", old["effective_from"])
        on_new, _ = deal_keeper.effective(got["claims"], "ubereats", new["store_id"], "fee", new["effective_from"])
        assert on_old["authority"] == "contract" and on_old["value"] > 0
        assert on_new["value"] == 0.0 and on_new["authority"] == "auth_email"


def test_noise_ignored_and_nothing_unread(bagel):
    got = bagel["got"]
    assert got["unread"] == [] and got["ignored"] >= 10
    quotes = " ".join(c["quote"] for c in got["claims"])
    for noise in ("Who can cover", "menu boards", "revisit the commission", "Anything you need", "Tablet"):
        assert noise not in quotes


def test_call_note_is_never_binding(bagel):
    notes = by_medium(bagel["got"], "call_note")
    assert notes and all(c["authority"] == "call_note" and c["source_file"].endswith(".md") for c in notes)
    c = notes[0]
    winner, conflicts = deal_keeper.effective(bagel["got"]["claims"], "doordash", "S01", "commission",
                                              c["effective_from"])
    assert winner["authority"] == "amendment" and conflicts == []      # agrees with the signed amendment


def _slack_dir(tmp_path, users, msgs, channel="ubereats-partnership"):
    s = tmp_path / "slack"
    (s / channel).mkdir(parents=True)
    (s / "users.json").write_text(json.dumps(users))
    (s / "channels.json").write_text(json.dumps([{"id": "C1", "name": channel}]))
    (s / channel / "2026-10-02.json").write_text(json.dumps(msgs))
    return s


CONTRACT = {"doc_type": "contract", "channel": "ubereats", "effective_from": "2026-01-01",
            "authorized_contacts": ["marcus.lee@ubereats.example", "@marcus.lee"],
            "terms": [{"term_type": "fee", "store_id": "all", "value": 1.0, "quote": "Marketing Fee: $1.00 per order"}]}
USERS = [{"id": "U1", "name": "marcus.lee", "profile": {"email": "marcus.lee@ubereats.example"}},
         {"id": "U2", "name": "jo.rep", "profile": {"email": "jo.rep@ubereats.example"}},
         {"id": "U3", "name": "gm.s01", "profile": {"email": "gm@restaurant.example"}},
         {"id": "U4", "name": "marcus.lee"}]      # no email: matched by @handle
WAIVE = "we'll waive the Marketing Fee ($1.00/order) for Downtown (S01) on {}"


def test_authority_matrix_unknown_sender_is_a_conflict(tmp_path):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "ue.json").write_text(json.dumps(CONTRACT))
    msgs = [{"type": "message", "user": "U1", "ts": "1790900000.000001", "text": WAIVE.format("Monday, October 5")},
            {"type": "message", "user": "U2", "ts": "1790900100.000002", "text": WAIVE.format("Tuesday, October 6")},
            {"type": "message", "user": "U3", "ts": "1790900200.000003", "text": WAIVE.format("Wednesday, October 7")},
            {"type": "message", "user": "U4", "ts": "1790900300.000004", "text": WAIVE.format("Thursday, October 8")},
            {"type": "message", "user": "U9", "ts": "1790900400.000005", "text": WAIVE.format("Friday, October 9")}]
    _slack_dir(tmp_path, USERS, msgs)
    claims, unread = deal_keeper.load_claims(tmp_path / "contracts", tmp_path / "emails")
    auth = {c["effective_from"]: c["authority"] for c in claims if c.get("medium") == "slack"}
    assert auth == {"2026-10-05": "auth_email", "2026-10-06": "unauth_email", "2026-10-07": None,
                    "2026-10-08": "auth_email", "2026-10-09": None} and unread == []
    winner, conflicts = deal_keeper.effective(claims, "ubereats", "S01", "fee", "2026-10-07")
    assert winner["authority"] == "contract" and conflicts[0]["authority"] is None   # unknown -> a human sees it


def test_contacts_list_beats_the_old_flag_and_flag_is_the_fallback(tmp_path):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "emails").mkdir()
    (tmp_path / "contracts" / "ue.json").write_text(json.dumps(CONTRACT))
    email = {"kind": "email", "from": "Jo <jo.rep@ubereats.example>", "sender_authorized": True, "channel": "ubereats",
             "body": "waived", "claims": [{"term_type": "fee", "store_id": "S01", "value": 0, "quote": "waived",
                                           "effective_from": "2026-10-05", "effective_to": "2026-10-05"}]}
    (tmp_path / "emails" / "jo.json").write_text(json.dumps(email))
    claims, _ = deal_keeper.load_claims(tmp_path / "contracts", tmp_path / "emails")
    jo = next(c for c in claims if c["source_file"].endswith("jo.json"))
    assert jo["authority"] == "unauth_email" and "not on" in jo["authority_basis"]
    # the committed fixtures have no list: the flag still decides, exactly as before
    old, _ = deal_keeper.load_claims(FIX / "contracts", FIX / "emails")
    flagged = {Path(c["source_file"]).name: c["authority"] for c in old if "emails" in c["source_file"]}
    assert flagged == {"grubhub_manager_0928.json": "auth_email", "rep_jordan_1001.txt": "unauth_email"}


def test_unparseable_message_goes_to_reader_model_never_invented(tmp_path, monkeypatch):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "ue.json").write_text(json.dumps(CONTRACT))
    vague = "Per our chat, S01 gets the Marketing Fee knocked off for the 10th, same as last time."
    _slack_dir(tmp_path, USERS, [{"type": "message", "user": "U1", "ts": "1790900000.000001", "text": vague},
                                 {"type": "message", "user": "U3", "ts": "1790900000.000002", "text": "lunch?"}])
    claims, unread = deal_keeper.load_claims(tmp_path / "contracts", tmp_path / "emails")      # offline
    assert [c for c in claims if c.get("medium")] == []
    assert len(unread) == 1 and unread[0].endswith("2026-10-02.json#ts=1790900000.000001")

    seen = []

    def fake_call_json(system, user, max_tokens=600, r=None):     # the 'reader' route, mocked
        seen.append((r, json.loads(user)["text"]))
        return {"claims": [
            {"term_type": "fee", "store_id": "S01", "value": 0, "effective_from": "2026-10-10",
             "effective_to": "2026-10-10", "quote": "S01 gets the Marketing Fee knocked off for the 10th",
             "authority": "contract"},                                          # the model can't grant authority
            {"term_type": "fee", "store_id": "S01", "value": 0, "quote": "Fee waived for S01 all month",
             "authority": "auth_email"}]}, None                                 # not in the message: dropped
    monkeypatch.setattr(llm, "available", lambda r=None: True)
    monkeypatch.setattr(llm, "call_json", fake_call_json)
    claims, unread = deal_keeper.load_claims(tmp_path / "contracts", tmp_path / "emails")
    got = [c for c in claims if c.get("medium")]
    assert seen and seen[0][0][0] == llm.route("reader")[0] and seen[0][1] == vague   # only the relevant message
    assert len(got) == 1 and unread == []
    c = got[0]
    assert c["quote"] in vague and c["authority"] == "auth_email"               # the sender's authority, not the model's
    assert c["locator"] == "ts=1790900000.000001" and c["channel"] == "ubereats"


def test_slack_message_changed_events_apply_the_edit(tmp_path):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "ue.json").write_text(json.dumps(CONTRACT))
    msgs = [{"type": "message", "user": "U1", "ts": "1790900000.000001", "text": WAIVE.format("Monday, October 5")},
            {"type": "message", "subtype": "message_changed", "ts": "1790900090.000009",
             "message": {"user": "U1", "ts": "1790900000.000001", "text": WAIVE.format("Tuesday, October 6"),
                         "edited": {"user": "U1", "ts": "1790900090.000009"}}}]
    _slack_dir(tmp_path, USERS, msgs)
    claims, _ = deal_keeper.load_claims(tmp_path / "contracts", tmp_path / "emails")
    slack = [c for c in claims if c.get("medium") == "slack"]
    assert [c["effective_from"] for c in slack] == ["2026-10-06"] and slack[0]["edited"]


def test_old_sources_still_read_the_same():
    claims, unread = deal_keeper.load_claims(FIX / "contracts", FIX / "emails")
    assert unread == [] and len(claims) >= 8
    assert all(not c.get("medium") for c in claims)                  # nothing new invented from old files


# collector + connectors ------------------------------------------------------------------------------------------

def test_collector_keeps_the_slack_tree_and_routes_eml_and_md(tmp_path, bagel):
    raw = bagel["raw"]
    rows = collector.read_manifest(raw)
    srcs = Counter(r["source"] for r in rows)
    assert srcs["slack"] >= 6 and srcs["notes"] == 1 and srcs["emails"] >= 4
    assert any(r["file"].startswith("slack/ubereats-partnership/") for r in rows)
    assert collector.collect(bagel["base"] / "out", raw) == []       # idempotent
    inbox = tmp_path / "inbox"
    (inbox / "misc").mkdir(parents=True)
    (inbox / "misc" / "fwd.eml").write_text("From: a@b.example\n\nhi\n")
    (inbox / "call.md").write_text("# notes\n")
    (inbox / "slack" / "doordash").mkdir(parents=True)               # a Slack channel that happens to be "doordash"
    (inbox / "slack" / "doordash" / "2026-01-05.json").write_text("[]")
    got = {p.relative_to(tmp_path / "raw").as_posix() for p in collector.collect(inbox, tmp_path / "raw")}
    assert got == {"emails/fwd.eml", "notes/call.md", "slack/doordash/2026-01-05.json"}


def _doc_payload():
    text = (ROOT / "docs" / "connectors.md").read_text()
    return json.loads(re.search(r"```json\n(.*?)\n```", text, re.S).group(1))


def test_connector_example_payload_is_ingested_and_read(tmp_path):
    payload = _doc_payload()
    assert connectors.validate(payload) == []
    raw = tmp_path / "raw"
    (raw / "contracts").mkdir(parents=True)
    contract = dict(CONTRACT, authorized_contacts=["sam.takahashi@ubereats.example"])
    (raw / "contracts" / "ue.json").write_text(json.dumps(contract))
    new = connectors.write_payload(payload, raw)
    assert {p.relative_to(raw).as_posix() for p in new} >= {"slack/users.json", "slack/channels.json",
                                                             "slack/ubereats-partnership/2026-01-20.json"}
    assert connectors.write_payload(payload, raw) == []               # same batch again: nothing new
    got = deal_keeper.read_sources(raw / "contracts", raw / "emails")
    msgs = [c for c in got["claims"] if c.get("medium")]
    assert len(msgs) == 1 and len(got["superseded"]) == 1 and got["unread"] == []
    c = msgs[0]
    assert (c["store_id"], c["effective_from"], c["authority"]) == ("S06", "2026-01-25", "auth_email")
    assert c["locator"] == "ts=1768909380.000012 thread_ts=1768907280.000011"


def test_connector_email_and_note_payloads(tmp_path):
    raw = tmp_path / "raw"
    (raw / "contracts").mkdir(parents=True)
    (raw / "contracts" / "ue.json").write_text(json.dumps(CONTRACT))
    connectors.write_payload({"flock_connector": "1", "source": "email", "connector": "creao:gmail-watcher",
                              "items": [{"from": "Marcus Lee <marcus.lee@ubereats.example>", "to": "ap@r.example",
                                         "date": "2026-10-01T09:00:00", "subject": "Fee",
                                         "body": f"Hi,\n\n{WAIVE.format('Monday, October 5')}.\n\n> old thread"}]},
                             raw)
    connectors.write_payload({"flock_connector": "1", "source": "note", "connector": "creao:notes",
                              "items": [{"title": "Uber Eats sync", "date": "2026-10-01", "channel": "ubereats",
                                         "body": "- Jo said we'll waive the Marketing Fee ($1.00/order) for "
                                                 "Downtown (S01) on Tuesday, October 6\n- weather was nice"}]}, raw)
    claims, unread = deal_keeper.load_claims(raw / "contracts", raw / "emails")
    got = {(c["medium"], c["effective_from"], c["authority"]) for c in claims if c.get("medium")}
    assert got == {("email", "2026-10-05", "auth_email"), ("call_note", "2026-10-06", "call_note")} and unread == []


def test_connector_rejects_bad_payloads_and_ingest_moves_files(tmp_path):
    bad = {"flock_connector": "2", "source": "fax", "items": [{}]}
    probs = connectors.validate(bad)
    assert any("flock_connector" in p for p in probs) and any("source" in p for p in probs)
    assert any("connector" in p for p in probs)
    with pytest.raises(connectors.PayloadError):
        connectors.write_payload(bad, tmp_path / "raw")
    slack_bad = {"flock_connector": "1", "source": "slack", "connector": "x",
                 "items": [{"channel": "#Bad Name", "user": {"id": "U1"}, "ts": "yesterday", "text": ""}]}
    assert len(connectors.validate(slack_bad)) == 4
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "good.json").write_text(json.dumps(_doc_payload()))
    (drop / "bad.json").write_text(json.dumps(bad))
    (drop / "half.json.tmp").write_text("{")
    r = connectors.ingest(drop, tmp_path / "raw")
    assert r["processed"] == ["good.json"] and r["rejected"] == ["bad.json"] and r["copied"]
    assert (drop / "processed" / "good.json").exists() and (drop / "rejected" / "bad.errors.txt").exists()
    assert (drop / "half.json.tmp").exists()                          # still being written: left alone


def test_second_connector_batch_merges_users(tmp_path):
    raw = tmp_path / "raw"
    (raw / "contracts").mkdir(parents=True)
    (raw / "contracts" / "ue.json").write_text(json.dumps(CONTRACT))
    hi = {"flock_connector": "1", "source": "slack", "connector": "x", "items": [
        {"channel": "ubereats-partnership", "user": {"id": "U7", "name": "pat"}, "ts": "1790900000.000001",
         "text": "hello"}]}
    connectors.write_payload(hi, raw)
    later = {"flock_connector": "1", "source": "slack", "connector": "x", "items": [
        {"channel": "ubereats-partnership", "user": {"id": "U1", "name": "marcus.lee",
                                                     "email": "marcus.lee@ubereats.example"},
         "ts": "1790900500.000002", "text": WAIVE.format("Monday, October 5")}]}
    connectors.write_payload(later, raw)
    assert len(list((raw / "slack").glob("users*.json"))) == 2
    claims, _ = deal_keeper.load_claims(raw / "contracts", raw / "emails")
    assert [c["authority"] for c in claims if c.get("medium")] == ["auth_email"]


# through the whole pipeline --------------------------------------------------------------------------------------

def test_demo_catches_the_new_traps(tmp_path, capsys):
    store = JsonStore(tmp_path / "state")
    r = demo.run_demo(seed=2025, profile="ca_bagel_chain", stores=4, weeks=2, confirm=True, store=store,
                      portal=tmp_path / "portal.jsonl", interactive=False, forget=True)
    assert r["caught"] == r["total"] and r["false_alarms"] == 0
    per = Counter(t["trap_type"] for t in r["traps"])
    caught = Counter(t["trap_type"] for t in r["traps"] if t["caught"])
    for no in gen.SOURCE_TRAPS:
        t = gen.TRAPS[no][0]
        assert per[t] > 0 and caught[t] == per[t], t


def test_findings_cite_the_slack_message(tmp_path):
    from flock import pipeline

    out, raw = tmp_path / "out", tmp_path / "raw"
    gen.generate(seed=3, stores=3, days=14, out=out, profile="ca_bagel_chain")
    collector.collect(out, raw)
    found = pipeline.detect_real(raw=raw, std=tmp_path / "std", mappings=_maps(tmp_path), confirm=True)
    key = {r["order_id"]: r for r in read(out / "answer_key.csv")}
    slack_hits = [f for f in found if any(key.get(o, {}).get("trap_type", "").startswith("slack_") and
                                          key[o]["expected_outcome"] != "correct" for o in f["order_ids"])]
    assert slack_hits
    for f in slack_hits:
        ev = [e for e in f["evidence"] if "slack/" in str(e.get("source_file"))]
        assert ev and all(e["locator"].startswith("ts=") and e["quote"] in text_of(e["source_file"]) for e in ev)


def _maps(tmp_path):
    import shutil
    m = tmp_path / "mappings.json"
    shutil.copy(ROOT / "mappings.json", m)
    return m
