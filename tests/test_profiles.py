"""Company profiles: the default company is unchanged, the bagel chain is its own (simulated) business."""
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from generator import generate as gen


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_bytes(d: Path) -> dict:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_profiles_listed_and_loadable():
    assert {"default", "ca_bagel_chain"} <= set(gen.list_profiles())
    for name in gen.list_profiles():
        p = gen.load_profile(name)
        assert p["name"] == name and p["simulated"] is True
        assert set(p["orders_per_day"]) == set(p["channels"]) and set(p["contracts"]) >= set(p["channels"])
        assert len(p["seasonality"]["weekday"]) == 7 and len(p["seasonality"]["monthly"]) == 12
    with pytest.raises(SystemExit, match="unknown profile"):
        gen.load_profile("nope")


def test_default_profile_output_is_unchanged(tmp_path):
    """--seed 42 --stores 12 --weeks 2 gives the same bytes as before profiles existed."""
    gen.generate(seed=42, stores=12, days=14, out=tmp_path)
    assert sha(tmp_path / "answer_key.csv") == "131b4dfd9f5549099c9293a3b30febabf3cd813a9b2d35b5775f93211a47e605"
    assert sha(tmp_path / "pos_orders.csv") == "394937d79b4da4c5ae5d30b6293de084b7934f5def4a4a6ed8177b562000d77e"
    assert sha(tmp_path / "grubhub_statement.xlsx") == "a9ebc2bc5f3fafeabf95767dfc3e25ad2a0f810d1a4bd9d02602117669e0e7ce"


@pytest.fixture(scope="module")
def bagel(tmp_path_factory):
    out = tmp_path_factory.mktemp("bagel")
    w = gen.generate(seed=8, stores=8, days=28, out=out, profile="ca_bagel_chain")
    return w, out


def test_bagel_chain_is_its_own_simulated_company(bagel):
    w, out = bagel
    prof = gen.load_profile("ca_bagel_chain")
    assert prof["default_stores"] == 56 and len(prof["stores"]) == 56
    assert all("Ringtide Bagels" in s["name"] for s in w["stores"])
    texts = [p.read_text() for p in out.rglob("*") if p.suffix in (".csv", ".json")]
    assert not any("Dave" in t or "Hot Chicken" in t for t in texts)       # nothing from the default company
    for f in list((out / "contracts").glob("*.json")) + list((out / "emails").glob("*.json")):
        assert json.loads(f.read_text())["notice"].startswith("SIMULATED")
    assert {r["trap_type"] for r in csv.DictReader((out / "answer_key.csv").open())} == {t for t, _ in gen.TRAPS.values()}


def test_bagel_chain_shape(bagel):
    """Morning-heavy, weekend peaks, catering tickets, average ticket close to the profile's."""
    w, _ = bagel
    prof = gen.load_profile("ca_bagel_chain")
    orders = w["orders"]
    morning = sum(o["placed_at"].hour < 11 for o in orders) / len(orders)
    assert morning > 0.6
    per_day = Counter(o["day"] for o in orders)
    weekend = [n for d, n in per_day.items() if d.weekday() >= 5]
    weekday = [n for d, n in per_day.items() if d.weekday() < 5]
    assert sum(weekend) / len(weekend) > 1.2 * sum(weekday) / len(weekday)
    catering = [o for o in orders if o.get("catering")]
    assert catering and all(o["channel"] in prof["catering"]["channels"] for o in catering)
    avg = sum(o["subtotal"] for o in orders) / len(orders)
    assert abs(avg - prof["average_ticket"]) / prof["average_ticket"] < 0.25
    cat_avg = sum(o["subtotal"] for o in catering) / len(catering)
    assert cat_avg > 3 * avg


def test_same_seed_same_bytes_per_profile(tmp_path):
    for prof in ("default", "ca_bagel_chain"):
        gen.generate(seed=5, stores=3, days=9, out=tmp_path / prof / "a", profile=prof)
        gen.generate(seed=5, stores=3, days=9, out=tmp_path / prof / "b", profile=prof)
        gen.generate(seed=6, stores=3, days=9, out=tmp_path / prof / "c", profile=prof)
        a, b, c = (tree_bytes(tmp_path / prof / x) for x in "abc")
        assert a == b, prof
        assert a["pos_orders.csv"] != c["pos_orders.csv"], prof


def test_summarize_profile():
    s = gen.summarize_profile("ca_bagel_chain")
    assert s["stores"] == 56 and s["simulated"]
    assert 800_000 < s["orders_per_year"] < 2_000_000
    assert gen.summarize_profile()["stores"] == 12


def test_cli_profile_flag(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["generate.py", "--profile", "ca_bagel_chain", "--stores", "2", "--days", "3",
                                     "--out", str(tmp_path)])
    gen.main()
    out = capsys.readouterr().out
    assert "2 stores x 3 days" in out and "ca_bagel_chain, simulated" in out
    first = next(csv.DictReader((tmp_path / "direct_app_orders.csv").open()))
    assert datetime.strptime(first["created"], "%d-%b-%Y %H:%M").hour < 15     # bagel shops close mid-afternoon
