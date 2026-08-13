"""Prediction logic shared between the sync endpoint (app.py) and the async
worker (worker.py) -- one code path, so sync and async can never silently
disagree about how a request turns into a probability."""

from __future__ import annotations

import datetime as dt

from pulse_serve import config
from pulse_serve.model import ModelBundle
from pulse_serve.schemas import PredictRequest, PredictResult


def run_prediction(bundle: ModelBundle, request: PredictRequest) -> PredictResult:
    features = {
        "route_id": request.route_id,
        "direction_id": request.direction_id,
        "stop_id": request.stop_id,
        "trip_id": request.trip_id,
        "horizon_min": request.horizon_min,
    }
    probability = bundle.predict_proba(features)
    return PredictResult(
        probability_delay_gt_180s=probability,
        predicted_label=probability >= 0.5,
        model_version=bundle.version,
        is_baseline=bundle.is_baseline,
        baseline_strategy=bundle.baseline_strategy,
        horizon_in_trained_regime=request.horizon_min == config.TRAINED_HORIZON_MIN,
        generated_at=dt.datetime.now(dt.UTC),
    )
