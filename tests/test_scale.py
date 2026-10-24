"""Year scale, small: traps scale with the period, the fast paths give the same answers as the simple ones,
progress is reported, and the live demo never touches committed files."""
import csv
import hashlib
import importlib.util
import io
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from conftest import ROOT
from dashboard.app import create_app
from flock import demo, pipeline, scan
from flock.agents import collector, deal_keeper, formatter
from flock.progress import NullProgress, Progress, progress_path, read_progress
from flock.store import JsonStore
from generator import generate as gen

spec = importlib.util.spec_from_file_location("score_mod", ROOT / "scripts" / "score.py")
score_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(score_mod)


def read(path):
    with Path(path).open(newline="") as fh:
        return list(csv.DictReader(fh))


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# traps at scale ------------------------------------------------------------------------------------------------

def test_trap_counts_formula():
    prof = gen.load_profile("default")
    assert set(gen.trap_counts(prof, 12, 14, sorted(gen.TRAPS)).values()) == {1}          # the 2-week demo
    year = gen.trap_counts(prof, 12, 364, sorted(gen.TRAPS))
    assert year[7] == 12 and year[2] == 1 and year[14] == 3
    assert gen.trap_counts(prof, 56, 364, [7])[7] == 58
    assert gen.trap_counts(prof, 12, 364, [7], rate=0)[7] == 1


@pytest.fixture(scope="module")
def scaled(tmp_path_factory):
    """4 stores x 8 weeks at a high trap rate: several of every trap, collected, formatted, run, scored."""
    base = tmp_path_factory.mktemp("scaled")
    maps = base / "mappings.json"
    shutil.copy(ROOT / "mappings.json", maps)
    w = gen.generate(seed=21, stores=4, days=56, out=base / "out", trap_rate=40)
    collector.collect(base / "out", base / "raw")
    store = JsonStore(base / "state")
    pipeline.run(reset=True, forget=True, demo_approve=True, store=store, rules_path=base / "rules.json",
                 portal=base / "portal.jsonl", raw=base / "raw", std=base / "std", mappings=maps, confirm=True,
                 runs_path=base / "state" / "runs.jsonl", progress=NullProgress())
    return {"w": w, "base": base, "store": store, "key": read(base / "out" / "answer_key.csv")}


def test_traps_scale_with_the_period(scaled):
    counts = scaled["w"]["trap_counts"]
    assert counts[7] == round(40 * 4 * 8 / 100) == 13 and counts[2] == 1
    per = Counter(r["trap_type"] for r in scaled["key"])
    for no in (1, 3, 4, 7, 8, 9, 10, 11, 12, 13, 16, 17):
        assert per[gen.TRAPS[no][0]] == counts[no], no
    assert per["unseen_layout"] == 1
    assert per["deposit_covers_two_weeks"] == 4                  # capped: 8 weeks hold 4 non-overlapping pairs
    assert per["payout_deposit_mismatch"] == 8                   # capped: one per weekly deposit
    assert len({r["order_id"] for r in scaled["key"] if r["trap_type"] not in
                ("authorized_rep_waiver", "unauthorized_rep_promise")}) == \
        sum(1 for r in scaled["key"] if r["trap_type"] not in ("authorized_rep_waiver", "unauthorized_rep_promise"))
    days = {r["order_id"]: None for r in scaled["key"]}
    trapped = [o for o in scaled["w"]["orders"] if o["order_id"] in days]
    assert len({o["day"].isocalendar()[1] for o in trapped}) >= 5   # spread across the period, not one week


def test_every_scaled_trap_caught(scaled):
    s = score_mod.score(scaled["store"], verbose=False, runs_path=scaled["base"] / "state" / "runs.jsonl",
                        answer_key=scaled["base"] / "out" / "answer_key.csv")
    assert (s["caught"], s["false_alarms"]) == (s["total"], 0) and s["total"] == len(scaled["key"])


def test_split_detection_matches_single_process(scaled):
    std, raw = scaled["base"] / "std", scaled["base"] / "raw"
    tables = {n: std / f"{n}.csv" for n in ("orders", "payout_lines", "bank_deposits", "error_charges", "refunds")}
    terms = deal_keeper.extract_terms(raw / "contracts", raw / "emails")

    def sig(fs):
        return sorted((f["check_type"], f["channel"], tuple(f["order_ids"]), f["gap"], f["reason"], str(f["evidence"]))
                      for f in fs)

    one = scan.detect(tables, terms, split_rows=10**9)
    split = scan.detect(tables, terms, split_rows=0, workers=1)
    pooled = scan.detect(tables, terms, split_rows=0, workers=2)
    assert one and sig(one) == sig(split) == sig(pooled)


def test_term_index_matches_terms_for(scaled):
    raw = scaled["base"] / "raw"
    terms = deal_keeper.extract_terms(raw / "contracts", raw / "emails")
    idx = deal_keeper.TermIndex(terms)
    for o in scaled["w"]["orders"][:400]:
        story = {"channel": o["channel"], "store_id": o["store_id"], "placed_at": o["placed_at"].isoformat(),
                 "pos": {"promo_code": o["promo_code"]}, "payout_lines": []}
        assert idx(story) == deal_keeper.terms_for(terms, story)


def test_fast_formatter_matches_per_row_reference(scaled):
    """The column-wise formatter gives exactly what standardize_row() gives, row by row."""
    raw, std = scaled["base"] / "raw", scaled["base"] / "std"
    maps = formatter.load_mappings(scaled["base"] / "mappings.json")
    path = raw / "doordash" / "doordash_payouts.csv"
    [(sheet, rows)] = formatter.read_sheets(path)
    name, hidx = formatter.find_layout(rows, maps)
    ref = formatter.apply_mapping("doordash/doordash_payouts.csv", rows, hidx, maps[name])
    got = [r for r in read(std / "payout_lines.csv") if r["source_file"] == "doordash/doordash_payouts.csv"]
    dup_rows = {int(r["source_row"]) for r in read(std / "duplicates_removed.csv")
                if r["source_file"] == "doordash/doordash_payouts.csv"}
    assert dup_rows and len(ref) == len(got) + len(dup_rows)     # minus the planted exact duplicates (trap 1)
    by_row = {int(r["source_row"]): r for r in got}
    for r in ref:
        if r["source_row"] in dup_rows:
            continue
        g = by_row[r["source_row"]]
        for c in formatter.PAYOUT_COLS:
            v = r.get(c)
            want = "" if v is None else (f"{v:.2f}" if c in formatter.MONEY and isinstance(v, float) else str(v))
            assert g[c] == want, (c, r["source_row"])


def test_fast_xlsx_reads_back_like_openpyxl(tmp_path, monkeypatch):
    def run(tag):
        gen.generate(seed=4, stores=3, days=14, out=tmp_path / tag / "out")
        collector.collect(tmp_path / tag / "out", tmp_path / tag / "raw")
        shutil.copy(ROOT / "mappings.json", tmp_path / tag / "m.json")
        formatter.format_all(tmp_path / tag / "raw", tmp_path / tag / "std", tmp_path / tag / "m.json", confirm=True)
        return [r for r in read(tmp_path / tag / "std" / "payout_lines.csv") if r["channel"] == "grubhub"], \
            read(tmp_path / tag / "std" / "payout_summaries.csv")

    slow = run("openpyxl")
    monkeypatch.setattr(gen, "XLSX_STREAM_ROWS", 0)
    fast = run("fast")
    assert (tmp_path / "openpyxl/out/grubhub_statement.xlsx").read_bytes() != \
        (tmp_path / "fast/out/grubhub_statement.xlsx").read_bytes()
    assert slow == fast and slow[0]


def test_formatter_reuses_tables_until_inputs_change(tmp_path, capsys):
    gen.generate(seed=4, stores=1, days=14, out=tmp_path / "out")
    collector.collect(tmp_path / "out", tmp_path / "raw")
    maps = tmp_path / "m.json"
    shutil.copy(ROOT / "mappings.json", maps)
    formatter.format_all(tmp_path / "raw", tmp_path / "std", maps, confirm=False)      # unseen layout pending
    assert formatter.pending_layouts(tmp_path / "std") and not (tmp_path / "std" / formatter.STAMP).exists()
    formatter.format_all(tmp_path / "raw", tmp_path / "std", maps, confirm=True)
    assert formatter.pending_layouts(tmp_path / "std") == []
    before = (tmp_path / "std" / "orders.csv").stat().st_mtime_ns
    formatter.format_all(tmp_path / "raw", tmp_path / "std", maps, verbose=True)
    assert "tables reused" in capsys.readouterr().out
    assert (tmp_path / "std" / "orders.csv").stat().st_mtime_ns == before


# progress ------------------------------------------------------------------------------------------------------

def test_progress_writes_state_and_bar(tmp_path):
    out = io.StringIO()
    p = Progress(tmp_path / "progress.json", stream=out, run="t", min_interval=0)
    p.stage("format", total=200)
    p.advance(50)
    st = read_progress(tmp_path / "progress.json")
    assert (st["status"], st["stage"], st["done"], st["total"]) == ("running", "format", 50, 200)
    p.stage("judge", total=3, unit="findings")
    p.update(3)
    p.finish()
    st = read_progress(tmp_path / "progress.json")
    assert st["status"] == "done" and [s["name"] for s in st["stages"]] == ["format", "judge"]
    assert st["stages"][0]["done"] == 200 and "rate" in st["stages"][0]
    assert "[format" in out.getvalue() and "200/200" in out.getvalue()      # one line per finished stage
    assert read_progress(tmp_path / "missing.json") == {"status": "idle"}
    NullProgress().stage("x", 1)                                             # does nothing, writes nothing


def test_pipeline_reports_progress_and_dashboard_serves_it(tmp_path):
    store = JsonStore(tmp_path / "state")
    client = create_app(store=store, runs_path=tmp_path / "runs.jsonl").test_client()
    assert client.get("/api/progress").get_json() == {"status": "idle"}
    pipeline.run(reset=True, forget=True, demo_approve=True, store=store, rules_path=tmp_path / "rules.json",
                 portal=tmp_path / "portal.jsonl", runs_path=tmp_path / "runs.jsonl")
    st = client.get("/api/progress").get_json()
    assert st["status"] == "done" and progress_path(store) == tmp_path / "state" / "progress.json"
    assert {"log findings", "judge", "rank"} <= {s["name"] for s in st["stages"]}
    assert "run-progress" in (ROOT / "dashboard" / "static" / "app.js").read_text()


def test_store_batch_writes_once_and_stays_readable(tmp_path):
    from flock.models import Finding

    s = JsonStore(tmp_path)
    with s.batch(flush_every=3600):
        for i in range(50):
            s.put_new(Finding(order_ids=[f"O{i}"], channel="doordash", check_type="commission", expected=1,
                              actual=2, gap=1, finding_id=f"F-{i}").to_dict())
            s.commit(None, {"subject_id": f"F-{i}", "allowed": True})
        assert len(s.all()) == 50 and not s.findings_path.exists()        # nothing written yet
        assert len(s.log()) == 50                                         # log() flushes what it shows
    assert len(JsonStore(tmp_path).all()) == 50


# the live demo ---------------------------------------------------------------------------------------------------

@pytest.fixture
def committed():
    paths = [ROOT / "mappings.json", ROOT / "data" / "answer_key.csv"]
    before = {p: sha(p) for p in paths}
    yield
    assert {p: sha(p) for p in paths} == before, "the demo changed a committed file"


def test_demo_tiny_run_scores_and_restores(tmp_path, committed, capsys):
    store = JsonStore(tmp_path / "state")
    pipeline.run(reset=True, forget=True, store=store, rules_path=tmp_path / "rules.json",
                 portal=tmp_path / "portal.jsonl", runs_path=tmp_path / "state" / "runs.jsonl",
                 progress=NullProgress())                                 # the fixtures demo state
    before = store.findings_path.read_bytes()
    r = demo.run_demo(seed=77, stores=2, weeks=2, confirm=True, store=store, portal=tmp_path / "portal.jsonl",
                      interactive=False)
    assert r["caught"] == r["total"] > 10 and r["false_alarms"] == 0
    out = capsys.readouterr().out
    assert "live demo: seed 77" in out and f"caught {r['total']}/{r['total']} traps" in out
    assert store.findings_path.read_bytes() == before                    # fixtures demo state is back
    assert read_progress(progress_path(store))["status"] == "done"
    assert not Path(r["workdir"]).exists()


def test_demo_unseen_layout_still_needs_a_human(tmp_path, committed, capsys):
    store = JsonStore(tmp_path / "state")
    r = demo.run_demo(seed=77, stores=2, weeks=2, confirm=False, store=store, portal=tmp_path / "portal.jsonl",
                      interactive=False)
    out = capsys.readouterr().out
    assert "skipped until a human confirms" in out and "held back" in out
    assert "saved as" not in out
    assert r["false_alarms"] == 0 and r["caught"] < r["total"]          # the unread week's traps can't be caught


def test_demo_keep_leaves_the_run_and_its_key(tmp_path, committed):
    store = JsonStore(tmp_path / "state")
    r = demo.run_demo(seed=78, stores=1, weeks=2, confirm=True, keep=True, store=store,
                      portal=tmp_path / "portal.jsonl", interactive=False)
    live = tmp_path / "state" / "answer_key.csv"
    assert live.exists() and store.all()
    assert score_mod.answer_key_for(store) == live
    assert score_mod.score(store, verbose=False, runs_path=tmp_path / "state" / "runs.jsonl")["total"] == r["total"]
    pipeline.run(reset=True, forget=True, store=store, rules_path=tmp_path / "rules.json",
                 portal=tmp_path / "portal.jsonl", runs_path=tmp_path / "state" / "runs.jsonl",
                 progress=NullProgress())
    assert not live.exists() and score_mod.answer_key_for(store) == score_mod.ANSWER_KEY


def test_demo_cli_parses(monkeypatch):
    seen = {}
    monkeypatch.setattr(demo, "run_demo", lambda *a, **kw: seen.update(a=a, kw=kw) or {"total": 1})
    with pytest.raises(SystemExit) as e:
        demo.main(["--seed", "9", "--profile", "ca_bagel_chain", "--weeks", "1", "--keep", "--confirm"])
    assert e.value.code == 0
    assert seen["a"][:3] == (9, "ca_bagel_chain", 1) and seen["kw"]["keep"] and seen["kw"]["confirm"]
