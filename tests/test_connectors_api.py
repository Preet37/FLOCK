"""POST /api/connectors: a connector batch (a CREAO agent watching Slack) is kept, read next to the contracts, and the
response says what Flock understood, including whether the sender may change the deal."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from flock import connectors

DOC = Path(__file__).resolve().parent.parent / "docs" / "connectors.md"


def example_payload() -> dict:
    block = re.search(r"## Example payload.*?```json\n(.*?)```", DOC.read_text(), re.S).group(1)
    return json.loads(block)


def contract(dirpath: Path, contacts: list[str]) -> Path:
    d = dirpath / "contracts"
    d.mkdir(parents=True)
    (d / "ubereats_contract.json").write_text(json.dumps({
        "doc_type": "contract", "channel": "ubereats", "effective_from": "2026-01-01",
        "authorized_contacts": contacts,
        "terms": [{"term_type": "fee", "store_id": "all", "value": 0.75, "quote": "Marketing Fee: $0.75 per order"}]}))
    return d


def test_authorized_sender_is_binding_and_correction_wins(tmp_path):
    rec = connectors.receive(example_payload(), tmp_path / "state",
                             contract(tmp_path, ["sam.takahashi@ubereats.example"]), raw=tmp_path / "raw")
    assert len(rec["claims"]) == 1
    c = rec["claims"][0]
    assert c["authority"] == "auth_email" and c["binding"] and c["store_id"] == "S06" and float(c["value"]) == 0
    assert "Sunday, January 25" in c["quote"] and rec["superseded"] == 1
    assert connectors.received(tmp_path / "state")[0]["claims"] == rec["claims"]
    assert list((tmp_path / "raw").rglob("*.json")), "messages also land in raw/ for the next run"


def test_unlisted_sender_from_the_platform_is_not_binding(tmp_path):
    rec = connectors.receive(example_payload(), tmp_path / "state",
                             contract(tmp_path, ["someone.else@ubereats.example"]), raw=tmp_path / "raw")
    assert rec["claims"] and all(not c["binding"] for c in rec["claims"])
    assert {c["authority"] for c in rec["claims"]} == {"unauth_email"}


def test_bad_payload_lists_problems(tmp_path):
    with pytest.raises(connectors.PayloadError) as e:
        connectors.receive({"source": "fax"}, tmp_path / "state", None, raw=tmp_path / "raw")
    assert e.value.problems
    assert connectors.received(tmp_path / "state") == []


def test_api_route_requires_token_and_records_agent(tmp_path, monkeypatch):
    from flock import api as flock_api
    from flock.store import JsonStore
    monkeypatch.setattr(connectors, "contracts_dir", lambda *_a: contract(tmp_path, ["sam.takahashi@ubereats.example"]))
    monkeypatch.setattr(connectors.collector, "RAW", tmp_path / "raw")
    a = flock_api.Api(store=JsonStore(tmp_path / "state"))
    rec = a.connector(example_payload())
    assert rec["claims"][0]["authority"] == "auth_email"
    with pytest.raises(flock_api.BadRequest):
        a.connector({"source": "fax"})


def test_contracts_dir_prefers_state_copy(tmp_path):
    kept = contract(tmp_path / "state", ["sam.takahashi@ubereats.example"])
    assert connectors.contracts_dir(tmp_path / "state") == kept



def test_received_batches_are_rechecked_against_the_contracts_now(tmp_path, monkeypatch):
    """A batch kept before the contracts were on file shows the same authority as one received after."""
    state = tmp_path / "state"
    monkeypatch.setattr(connectors, "contracts_dir", lambda state_dir=None: None)
    early = connectors.receive(example_payload(), state, raw=tmp_path / "raw")
    assert early["contracts_from"] is None and not any(c.get("binding") for c in early["claims"])
    good = contract(tmp_path, ["sam.takahashi@ubereats.example"])
    monkeypatch.setattr(connectors, "contracts_dir", lambda state_dir=None: good)
    [rec] = connectors.received(state)
    assert "payload" not in rec and rec["rechecked_against"] == str(good)
    assert any(c["binding"] and c["authority"] == "auth_email" for c in rec["claims"])
