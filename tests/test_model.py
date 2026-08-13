"""Model loading: baseline states, real-model happy path, and every
integrity-failure mode -- sha256 mismatch, one file missing its counterpart,
a bad manifest, an artifact that doesn't satisfy the predict_proba contract.
"Model serialization safety" is a headline syllabus objective; each of these
exercises the actual code path, not just the happy one."""

from __future__ import annotations

import json

from pulse_serve import model
from tests.conftest import write_model_artifact
from tests.fixtures.fake_model import ConstantModel, NoContractModel


def test_no_model_file_is_clean_baseline_always_on_time(tmp_path):
    bundle = model.load_model(tmp_path)

    assert bundle.is_baseline is True
    assert bundle.version == model.BASELINE_VERSION
    assert bundle.baseline_strategy == "always_on_time"
    assert bundle.integrity_ok is True
    assert bundle.predict_proba({"route_id": "1"}) == 0.0


def test_no_model_file_but_route_hour_table_present_is_clean_baseline(tmp_path):
    table = {"1": {"12": 0.4}, "_all": 0.1}
    (tmp_path / "baseline_route_hour.json").write_text(json.dumps(table))

    bundle = model.load_model(tmp_path)

    assert bundle.is_baseline is True
    assert bundle.baseline_strategy == "route_hour_table"
    assert bundle.integrity_ok is True
    assert bundle.version == model.BASELINE_VERSION


def test_real_model_happy_path_loads_and_reports_semver(tmp_path):
    write_model_artifact(tmp_path, ConstantModel(0.42), semver="1.2.3")

    bundle = model.load_model(tmp_path)

    assert bundle.is_baseline is False
    assert bundle.baseline_strategy is None
    assert bundle.integrity_ok is True
    assert bundle.version == "1.2.3"
    assert bundle.predict_proba({"route_id": "1"}) == 0.42


def test_sha256_mismatch_refuses_to_unpickle_and_falls_back_to_baseline(tmp_path):
    write_model_artifact(tmp_path, ConstantModel(0.9), sha256_override="0" * 64)

    bundle = model.load_model(tmp_path)

    assert bundle.is_baseline is True
    assert bundle.version == model.BASELINE_VERSION
    assert bundle.integrity_ok is False, "a hash mismatch must NOT look like the clean no-model-file state"


def test_artifact_present_manifest_missing_refuses_and_falls_back(tmp_path):
    write_model_artifact(tmp_path, ConstantModel(0.9))
    (tmp_path / "manifest.json").unlink()

    bundle = model.load_model(tmp_path)

    assert bundle.is_baseline is True
    assert bundle.integrity_ok is False


def test_manifest_present_artifact_missing_refuses_and_falls_back(tmp_path):
    write_model_artifact(tmp_path, ConstantModel(0.9))
    (tmp_path / "current.joblib").unlink()

    bundle = model.load_model(tmp_path)

    assert bundle.is_baseline is True
    assert bundle.integrity_ok is False


def test_manifest_missing_required_keys_refuses_and_falls_back(tmp_path):
    write_model_artifact(tmp_path, ConstantModel(0.9))
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["trained_at"]
    manifest_path.write_text(json.dumps(manifest))

    bundle = model.load_model(tmp_path)

    assert bundle.is_baseline is True
    assert bundle.integrity_ok is False


def test_manifest_unreadable_json_refuses_and_falls_back(tmp_path):
    write_model_artifact(tmp_path, ConstantModel(0.9))
    (tmp_path / "manifest.json").write_text("{not json")

    bundle = model.load_model(tmp_path)

    assert bundle.is_baseline is True
    assert bundle.integrity_ok is False


def test_artifact_without_predict_proba_refuses_and_falls_back(tmp_path):
    write_model_artifact(tmp_path, NoContractModel())

    bundle = model.load_model(tmp_path)

    assert bundle.is_baseline is True
    assert bundle.integrity_ok is False


def test_predict_proba_is_clamped_to_unit_interval(tmp_path):
    write_model_artifact(tmp_path, ConstantModel(1.7))

    bundle = model.load_model(tmp_path)

    assert bundle.predict_proba({"route_id": "1"}) == 1.0


def test_route_hour_table_falls_back_to_all_key_for_unknown_route(tmp_path):
    table = {"1": {"12": 0.4}, "_all": 0.2}
    (tmp_path / "baseline_route_hour.json").write_text(json.dumps(table))

    bundle = model.load_model(tmp_path)

    assert bundle.predict_proba({"route_id": "unknown-route"}) == 0.2
