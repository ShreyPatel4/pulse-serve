"""Pydantic request/response models. Every field on every endpoint is
validated here -- the API never trusts a raw dict past this layer."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pulse_serve import config


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class PredictRequest(BaseModel):
    """The unit of prediction from pulse-mbta's ML spec: (route, direction,
    stop, trip) at a horizon. horizon_min accepts 1-60 so the API doesn't
    hard-reject a reasonable request, but the trained regime is a fixed 10
    minutes (config.TRAINED_HORIZON_MIN) -- see PredictResponse below."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    route_id: str = Field(min_length=1, max_length=64)
    direction_id: int = Field(ge=0, le=1, description="0 or 1, per MBTA V3's direction_id")
    stop_id: str = Field(min_length=1, max_length=64)
    trip_id: str = Field(min_length=1, max_length=64)
    horizon_min: int = Field(default=config.TRAINED_HORIZON_MIN, ge=1, le=60)


class PredictResult(BaseModel):
    """The prediction payload itself -- shared shape between the sync
    response and the async GET /v1/results/{request_id} response, so a
    caller who switches from sync to async doesn't have to parse a different
    schema for the number that actually matters."""

    probability_delay_gt_180s: float = Field(ge=0.0, le=1.0)
    predicted_label: bool = Field(description="probability_delay_gt_180s >= 0.5")
    model_version: str
    is_baseline: bool
    baseline_strategy: Literal["route_hour_table", "always_on_time"] | None = None
    horizon_in_trained_regime: bool = Field(
        description=f"False when horizon_min != {config.TRAINED_HORIZON_MIN} (the trained regime) -- "
        "the prediction is extrapolation outside what the model was fit on"
    )
    generated_at: dt.datetime


class PredictResponse(PredictResult):
    """Sync POST /v1/predict response: the request echoed back plus the
    result, so a caller can log one self-describing object."""

    route_id: str
    direction_id: int
    stop_id: str
    trip_id: str
    horizon_min: int


class AsyncEnqueueResponse(BaseModel):
    request_id: str
    status: Literal["queued"] = "queued"
    queued_at: dt.datetime = Field(default_factory=_utcnow)


class ResultResponse(BaseModel):
    """GET /v1/results/{request_id}. status="pending" covers both "still in
    the queue/being worked" and "this request_id was never issued" -- see
    the delivery-semantics section of README.md for why those two cases are
    deliberately not distinguished."""

    request_id: str
    status: Literal["pending", "done"]
    result: PredictResult | None = None
    completed_at: dt.datetime | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    model_version: str
    is_baseline: bool
    model_integrity_ok: bool
    uptime_seconds: float
    queue_depth: int | None = Field(
        description="Redis LLEN on the pending queue; null if redis is unreachable"
    )
    redis_connected: bool
    db_connected: bool
