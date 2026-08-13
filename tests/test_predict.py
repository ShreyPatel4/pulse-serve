"""run_prediction: the one code path shared by the sync endpoint and the
worker."""

from __future__ import annotations

from pulse_serve.predict import run_prediction
from pulse_serve.schemas import PredictRequest
from tests.conftest import write_model_artifact
from tests.fixtures.fake_model import ConstantModel


def test_baseline_always_on_time_predicts_zero_and_labels_false(tmp_path):
    from pulse_serve.model import load_model

    bundle = load_model(tmp_path)
    req = PredictRequest(route_id="1", direction_id=0, stop_id="110", trip_id="trip-1")

    result = run_prediction(bundle, req)

    assert result.probability_delay_gt_180s == 0.0
    assert result.predicted_label is False
    assert result.model_version == "baseline-fallback"
    assert result.is_baseline is True
    assert result.baseline_strategy == "always_on_time"


def test_real_model_result_reports_semver_and_label_threshold(tmp_path):
    from pulse_serve.model import load_model

    write_model_artifact(tmp_path, ConstantModel(0.6), semver="2.0.0")
    bundle = load_model(tmp_path)
    req = PredictRequest(route_id="1", direction_id=0, stop_id="110", trip_id="trip-1")

    result = run_prediction(bundle, req)

    assert result.probability_delay_gt_180s == 0.6
    assert result.predicted_label is True
    assert result.model_version == "2.0.0"
    assert result.is_baseline is False


def test_horizon_in_trained_regime_flag(tmp_path):
    from pulse_serve.model import load_model

    bundle = load_model(tmp_path)

    on_regime = run_prediction(
        bundle, PredictRequest(route_id="1", direction_id=0, stop_id="110", trip_id="trip-1", horizon_min=10)
    )
    off_regime = run_prediction(
        bundle, PredictRequest(route_id="1", direction_id=0, stop_id="110", trip_id="trip-1", horizon_min=30)
    )

    assert on_regime.horizon_in_trained_regime is True
    assert off_regime.horizon_in_trained_regime is False
