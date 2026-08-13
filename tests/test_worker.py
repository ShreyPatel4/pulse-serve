"""worker._process_one against real local redis + a scratch Postgres --
the code path that actually turns a queued job into a stored, idempotent
prediction."""

from __future__ import annotations

import json

from pulse_serve import queue, store, worker
from pulse_serve.model import load_model


def test_process_one_inserts_and_acks(redis_client, pg_conn, tmp_path):
    bundle = load_model(tmp_path)  # clean baseline, no model file in tmp_path
    queue.enqueue(
        redis_client,
        {
            "request_id": "11111111-1111-1111-1111-111111111111",
            "route_id": "1",
            "direction_id": 0,
            "stop_id": "110",
            "trip_id": "trip-1",
            "horizon_min": 10,
            "queued_at": "2026-08-12T10:00:00+00:00",
        },
    )
    raw = queue.reserve(redis_client, timeout=2)
    assert raw is not None

    worker._process_one(raw, redis_client, pg_conn, bundle)

    assert queue.processing_depth(redis_client) == 0, "ack must remove the job from PROCESSING"
    row = store.get_prediction(pg_conn, "11111111-1111-1111-1111-111111111111")
    assert row is not None
    assert row["result"]["model_version"] == "baseline-fallback"


def test_process_one_redelivery_is_idempotent(redis_client, pg_conn, tmp_path):
    """Models a crash-before-ack: the same raw job is processed twice (the
    second time standing in for reclaim_stale() redelivering it after a
    worker restart). The Postgres row must not change on the second write."""
    bundle = load_model(tmp_path)
    payload = {
        "request_id": "22222222-2222-2222-2222-222222222222",
        "route_id": "1",
        "direction_id": 0,
        "stop_id": "110",
        "trip_id": "trip-1",
        "horizon_min": 10,
        "queued_at": "2026-08-12T10:00:00+00:00",
    }
    raw = json.dumps(payload)

    worker._process_one(raw, redis_client, pg_conn, bundle)
    first = store.get_prediction(pg_conn, payload["request_id"])

    # Second attempt: same raw string, as reclaim_stale() would redeliver it.
    worker._process_one(raw, redis_client, pg_conn, bundle)
    second = store.get_prediction(pg_conn, payload["request_id"])

    assert first["completed_at"] == second["completed_at"], "the row must not be rewritten on redelivery"


def test_safe_job_id_extracts_request_id():
    raw = json.dumps({"request_id": "abc-123", "route_id": "1"})
    assert worker._safe_job_id(raw) == "abc-123"


def test_safe_job_id_handles_garbage_without_raising():
    assert worker._safe_job_id("{not json") == "?"
