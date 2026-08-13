"""Pydantic validation: every field on PredictRequest is checked, not
trusted."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pulse_serve.schemas import PredictRequest


def _valid(**overrides) -> dict:
    base = {"route_id": "1", "direction_id": 0, "stop_id": "110", "trip_id": "trip-1"}
    base.update(overrides)
    return base


def test_valid_request_defaults_horizon_to_trained_regime():
    req = PredictRequest(**_valid())
    assert req.horizon_min == 10


def test_direction_id_must_be_0_or_1():
    with pytest.raises(ValidationError):
        PredictRequest(**_valid(direction_id=2))
    with pytest.raises(ValidationError):
        PredictRequest(**_valid(direction_id=-1))


def test_empty_route_id_rejected():
    with pytest.raises(ValidationError):
        PredictRequest(**_valid(route_id=""))


def test_horizon_min_out_of_range_rejected():
    with pytest.raises(ValidationError):
        PredictRequest(**_valid(horizon_min=0))
    with pytest.raises(ValidationError):
        PredictRequest(**_valid(horizon_min=61))


def test_missing_required_field_rejected():
    payload = _valid()
    del payload["trip_id"]
    with pytest.raises(ValidationError):
        PredictRequest(**payload)


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        PredictRequest(**_valid(extra_field="nope"))


def test_route_id_whitespace_is_stripped():
    req = PredictRequest(**_valid(route_id="  1  "))
    assert req.route_id == "1"
