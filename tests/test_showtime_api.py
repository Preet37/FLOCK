"""Hosted showtime: POST /api/run {"dataset": "showtime"} runs flock.showtime prepare as a job; GET /api/showtime."""
from __future__ import annotations

import pytest

from flock import api as flock_api
from flock.store import JsonStore


def test_showtime_params_validated():
    assert flock_api.Api.showtime_params({})["months"] == 4
    with pytest.raises(flock_api.BadRequest):
        flock_api.Api.showtime_params({"months": 9})
    with pytest.raises(flock_api.BadRequest):
        flock_api.Api.showtime_params({"seed": "x"})


def test_showtime_sheet_empty_note(tmp_path):
    a = flock_api.Api(store=JsonStore(tmp_path / "state"))
    sheet = a.showtime_sheet()
    assert sheet["json"] is None and "showtime" in sheet["note"]


def test_unknown_dataset_lists_showtime(tmp_path):
    a = flock_api.Api(store=JsonStore(tmp_path / "state"))
    with pytest.raises(flock_api.BadRequest, match="showtime"):
        a.run({"dataset": "nope"})
