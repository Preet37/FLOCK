"""Evidence viewer: a finding's source_file + row opens as a window with the proving cell highlighted; nothing outside
the evidence folders can be read."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flock import evidence  # noqa: E402


@pytest.fixture
def state(tmp_path):
    d = tmp_path / "state"
    (d / "raw").mkdir(parents=True)
    rows = ["payout_id,store_id,gross,commission"] + [f"P{i},S{i:02d},{10 + i}.00,{2 + i}.50" for i in range(1, 31)]
    (d / "raw" / "payouts.csv").write_text("\n".join(rows) + "\n")
    (d / "raw" / "deal.eml").write_text("From: rep@x.example\nSubject: fees\n\nWe will waive the Marketing Fee on Sunday.\nThanks\n")
    return d


def test_csv_row_and_cell(state):
    out = evidence.resolve({"file": "raw/payouts.csv", "row": 12, "what": "commission charged 13.50 on gross 21.00"},
                           evidence.roots(state))
    assert out["kind"] == "csv" and out["header"] == ["payout_id", "store_id", "gross", "commission"]
    assert out["line"] == 12 and out["highlight"]["row"] == 12
    assert out["highlight"]["column_name"] == "commission"
    nums = [r["row"] for r in out["rows"]]
    assert nums[0] == 4 and nums[-1] == 20 and 12 in nums
    assert next(r for r in out["rows"] if r["row"] == 12)["cells"][0] == "P11", "row 12 in Excel = 11th data row"


def test_string_ref_path_colon_row(state):
    out = evidence.resolve("raw/payouts.csv:2", evidence.roots(state))
    assert out["rows"][0]["row"] == 2 and out["highlight"]["row"] == 2


def test_quote_finds_the_line(state):
    out = evidence.resolve({"source_file": "raw/deal.eml", "quote": "waive the Marketing Fee"}, evidence.roots(state))
    assert out["kind"] == "eml" and out["line"] == 4


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", "flock/api.py", "raw/../../../secret.txt",
                                 "data/../flock/llm.py"])
def test_traversal_is_refused(state, bad):
    with pytest.raises(evidence.EvidenceError):
        evidence.resolve(bad, evidence.roots(state))


def test_row_out_of_range(state):
    with pytest.raises(evidence.EvidenceError):
        evidence.resolve("raw/payouts.csv:999", evidence.roots(state))


def test_index_groups_by_file():
    findings = [
        {"finding_id": "F-1", "evidence": [{"source_file": "a.csv", "row": 3, "what": "x"},
                                           {"source_file": "c.md", "quote": "20%", "authority": "contract"}]},
        {"finding_id": "F-2", "evidence": [{"source_file": "a.csv", "row": 9}]},
        {"finding_id": "F-3", "evidence": []},
    ]
    idx = evidence.index(findings)
    assert idx["count"] == 3 and [f["finding_id"] for f in idx["findings"]] == ["F-1", "F-2"]
    assert idx["files"][0] == {"file": "a.csv", "kind": "csv", "refs": 2, "findings": ["F-1", "F-2"]}
