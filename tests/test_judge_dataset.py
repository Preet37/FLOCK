"""Training set for the tuned judge: exact panel prompts, answer-key labels, held-out seeds locked out."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flock.agents import panel  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("flock_judge_dataset", ROOT / "scripts" / "judge_dataset.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    ds = _load()
    base = tmp_path_factory.mktemp("judge_ds")
    ds.benchmark.SEEDS_DIR = base / "seeds"
    manifest = ds.build([3, 4], [4], profile="default", stores=2, weeks=2, out_dir=base / "out", quiet=True)
    return ds, manifest, base / "out"


def test_held_out_seeds_are_refused_before_anything_is_generated(tmp_path):
    ds = _load()
    ds.benchmark.SEEDS_DIR = tmp_path / "seeds"
    with pytest.raises(SystemExit, match="held-out"):
        ds.build([1, 2, 105], [2], profile="default", stores=2, weeks=1, out_dir=tmp_path / "out")
    assert not (tmp_path / "seeds").exists(), "refused before generating"
    with pytest.raises(SystemExit, match="held-out"):
        ds.build([1], [110], profile="default", stores=2, weeks=1, out_dir=tmp_path / "out")


def test_examples_use_the_panels_exact_prompts_and_shape(built):
    ds, manifest, out = built
    rows = [json.loads(l) for l in (out / "judge_train.jsonl").read_text().splitlines()]
    assert rows and manifest["examples"]["train"] == len(rows)
    r = rows[0]
    system, user, assistant = (m["content"] for m in r["messages"])
    assert any(system.startswith(persona) for persona in panel.JUDGES.values())
    assert panel.SYSTEM in system and system.endswith(ds.JSON_ONLY)
    payload = json.loads(user)
    assert set(payload) == {"check_type", "channel", "expected", "actual", "gap", "evidence", "reason"}
    assert "order_ids" not in payload and "finding_id" not in payload, "no ids: the model cannot learn the seed"
    assert "seeds/" not in user and all(e["source_file"].startswith("data/raw/") for e in payload["evidence"]), \
        "evidence paths look like production, never the seed's cache folder"
    target = json.loads(assistant)
    assert panel._parse_call(target) is not None, "the assistant JSON is what the panel parses at inference"
    assert target["outcome"] in panel.OUTCOMES and 0 < target["confidence"] <= 1 and target["reason"]


def test_every_finding_gets_one_example_per_judge_and_splits_by_seed(built):
    ds, manifest, out = built
    assert manifest["train_seeds"] == [3] and manifest["val_seeds"] == [4]
    assert manifest["examples"]["train"] % len(panel.JUDGES) == 0
    assert manifest["examples"]["val"] % len(panel.JUDGES) == 0
    assert manifest["held_out_locked"] == list(range(101, 111))


def test_labels_come_from_the_answer_key(built):
    ds, manifest, out = built
    p = ds.benchmark.prepare(3, "default", 2, 14, quiet=True)
    key = ds.load_key(p["key"])
    trap_types = set()
    for f in p["findings"]:
        outcome, trap = ds.label(f, key)
        planted = [key[o]["expected_outcome"] for o in f["order_ids"] if o in key]
        if planted:
            assert outcome in planted
            trap_types.add(trap)
        else:
            assert outcome == "correct" and trap is None
    assert {"recoverable", "correct", "conflict"} <= set(manifest["labels"]["train"]) | set(manifest["labels"]["val"]) \
        or sum(manifest["labels"]["train"].values()) > 0


def test_reasons_never_name_the_trap(built):
    ds, manifest, out = built
    text = (out / "judge_train.jsonl").read_text()
    for trap in manifest["by_trap_type"]:
        if trap != "no_trap":
            assert trap not in text, f"trap name {trap!r} leaked into the training text"
