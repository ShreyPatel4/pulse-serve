"""FastAPI integration tests via httpx TestClient -- auth, rate limiting,
sync predict's baseline fallback, /healthz, /metrics, and the full async
enqueue -> worker -> results round trip (guarded by the same skip-if-no-redis
fixture as tests/test_queue.py; Postgres is required unconditionally, same
convention as pulse-mbta)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from pulse_serve import config, worker
from pulse_serve.app import app

PAYLOAD = {"route_id": "1", "direction_id": 0, "stop_id": "110", "trip_id": "trip-1"}


def _headers(key: str = config.PLACEHOLDER_API_KEY) -> dict:
    return {"X-API-Key": key}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    # Empty tmp dir: deterministically the clean baseline state regardless
    # of what's in the repo's real models/ (which is empty pending
    # pulse-mbta's M3, but this keeps the test independent of that fact).
    monkeypatch.setenv("PULSE_SERVE_MODEL_DIR", str(tmp_path))
    monkeypatch.delenv("PULSE_API_KEYS", raising=False)
    with TestClient(app) as c:
        yield c


def test_predict_without_api_key_is_401(client):
    resp = client.post("/v1/predict", json=PAYLOAD)
    assert resp.status_code == 401


def test_predict_with_wrong_api_key_is_403(client):
    resp = client.post("/v1/predict", headers=_headers("wrong-key"), json=PAYLOAD)
    assert resp.status_code == 403


def test_predict_baseline_fallback_response_shape(client):
    resp = client.post("/v1/predict", headers=_headers(), json=PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_version"] == "baseline-fallback"
    assert body["is_baseline"] is True
    assert body["baseline_strategy"] == "always_on_time"
    assert body["probability_delay_gt_180s"] == 0.0
    assert body["predicted_label"] is False
    assert body["route_id"] == "1"
    assert body["horizon_in_trained_regime"] is True


def test_predict_validation_error_is_422(client):
    bad = dict(PAYLOAD, direction_id=5)
    resp = client.post("/v1/predict", headers=_headers(), json=bad)
    assert resp.status_code == 422


def test_predict_extra_field_rejected_422(client):
    bad = dict(PAYLOAD, unexpected="nope")
    resp = client.post("/v1/predict", headers=_headers(), json=bad)
    assert resp.status_code == 422


def test_healthz_reports_clean_baseline(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["model_version"] == "baseline-fallback"
    assert body["model_integrity_ok"] is True
    assert body["uptime_seconds"] >= 0


def test_healthz_503_when_model_integrity_fails(monkeypatch, tmp_path):
    from tests.conftest import write_model_artifact
    from tests.fixtures.fake_model import ConstantModel

    write_model_artifact(tmp_path, ConstantModel(0.5), sha256_override="0" * 64)
    monkeypatch.setenv("PULSE_SERVE_MODEL_DIR", str(tmp_path))
    monkeypatch.delenv("PULSE_API_KEYS", raising=False)

    with TestClient(app) as c:
        resp = c.get("/healthz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["model_integrity_ok"] is False


def test_metrics_exposes_expected_series(client):
    client.post("/v1/predict", headers=_headers(), json=PAYLOAD)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    for series in (
        "pulse_serve_requests_total",
        "pulse_serve_request_latency_seconds",
        "pulse_serve_queue_depth",
        "pulse_serve_model_integrity_ok",
    ):
        assert series in text, f"{series} missing from /metrics output"


def test_rate_limit_returns_429_with_retry_after(monkeypatch, tmp_path):
    monkeypatch.setenv("PULSE_SERVE_MODEL_DIR", str(tmp_path))
    monkeypatch.delenv("PULSE_API_KEYS", raising=False)
    monkeypatch.setenv("PULSE_SERVE_RATE_LIMIT_BURST", "2")
    monkeypatch.setenv("PULSE_SERVE_RATE_LIMIT_PER_MIN", "60")

    with TestClient(app) as c:
        r1 = c.post("/v1/predict", headers=_headers(), json=PAYLOAD)
        r2 = c.post("/v1/predict", headers=_headers(), json=PAYLOAD)
        r3 = c.post("/v1/predict", headers=_headers(), json=PAYLOAD)

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 429
        assert "Retry-After" in r3.headers


def test_results_unknown_request_id_is_pending(client, monkeypatch, pg_test_dsn):
    # store.connect() is called per-request (not cached on app.state), so
    # PULSE_SERVE_DSN can be pointed at the scratch test db after the app is
    # already running.
    monkeypatch.setenv("PULSE_SERVE_DSN", pg_test_dsn)
    resp = client.get("/v1/results/does-not-exist", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {
        "request_id": "does-not-exist",
        "status": "pending",
        "result": None,
        "completed_at": None,
    }


def test_async_predict_then_worker_then_results_end_to_end(
    monkeypatch, tmp_path, redis_client, pg_conn, pg_test_dsn
):
    monkeypatch.setenv("PULSE_SERVE_MODEL_DIR", str(tmp_path))
    monkeypatch.delenv("PULSE_API_KEYS", raising=False)
    monkeypatch.setenv("PULSE_SERVE_DSN", pg_test_dsn)

    with TestClient(app) as c:
        enqueue_resp = c.post("/v1/predict/async", headers=_headers(), json=PAYLOAD)
        assert enqueue_resp.status_code == 200
        request_id = enqueue_resp.json()["request_id"]
        assert enqueue_resp.json()["status"] == "queued"

        pending = c.get(f"/v1/results/{request_id}", headers=_headers())
        assert pending.json()["status"] == "pending"

        # Stand in for worker.py's loop: reserve the same job off the same
        # redis this app's lifespan connected to, process it with the same
        # code worker.py runs, write to the same scratch Postgres.
        raw = redis_client.brpoplpush(config.PENDING_KEY, config.PROCESSING_KEY, timeout=2)
        assert raw is not None
        job = json.loads(raw)
        assert job["request_id"] == request_id

        bundle = app.state.model_bundle
        worker._process_one(raw, redis_client, pg_conn, bundle)

        done = c.get(f"/v1/results/{request_id}", headers=_headers())
        assert done.status_code == 200
        body = done.json()
        assert body["status"] == "done"
        assert body["result"]["model_version"] == "baseline-fallback"
        assert body["completed_at"] is not None
