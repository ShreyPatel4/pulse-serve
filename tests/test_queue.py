"""Redis reliable-queue tests against a real local redis (skipped, not
mocked, if none is reachable -- see conftest.redis_client)."""

from __future__ import annotations

import json

from pulse_serve import config, queue


def test_enqueue_reserve_ack_round_trip(redis_client):
    queue.enqueue(redis_client, {"request_id": "r1", "route_id": "1"})

    assert queue.pending_depth(redis_client) == 1
    assert queue.processing_depth(redis_client) == 0

    raw = queue.reserve(redis_client, timeout=1)
    assert raw is not None
    assert json.loads(raw)["request_id"] == "r1"
    assert queue.pending_depth(redis_client) == 0
    assert queue.processing_depth(redis_client) == 1

    queue.ack(redis_client, raw)
    assert queue.processing_depth(redis_client) == 0


def test_reserve_times_out_with_no_job(redis_client):
    raw = queue.reserve(redis_client, timeout=1)
    assert raw is None


def test_ack_removes_exact_raw_string_not_a_reserialized_copy(redis_client):
    # Keys in a different order than insertion would still be equal as
    # dicts, but must NOT be equal as raw strings -- this is exactly the
    # trap ack()'s docstring warns about. Key order chosen so sort_keys=True
    # actually reorders them (alphabetically: horizon_min, request_id,
    # route_id -- not the insertion order below).
    queue.enqueue(redis_client, {"route_id": "1", "horizon_min": 10, "request_id": "r1"})
    raw = queue.reserve(redis_client, timeout=1)

    reserialized = json.dumps(json.loads(raw), sort_keys=True)
    assert reserialized != raw, "test fixture must actually exercise a different serialization"

    queue.ack(redis_client, reserialized)
    assert queue.processing_depth(redis_client) == 1, (
        "acking a re-serialized copy must NOT remove the real entry"
    )

    queue.ack(redis_client, raw)
    assert queue.processing_depth(redis_client) == 0


def test_reclaim_stale_moves_processing_back_to_pending(redis_client):
    queue.enqueue(redis_client, {"request_id": "r1"})
    raw = queue.reserve(redis_client, timeout=1)
    assert raw is not None
    assert queue.processing_depth(redis_client) == 1
    assert queue.pending_depth(redis_client) == 0

    reclaimed = queue.reclaim_stale(redis_client)

    assert reclaimed == 1
    assert queue.processing_depth(redis_client) == 0
    assert queue.pending_depth(redis_client) == 1


def test_reclaim_stale_is_a_noop_when_processing_is_empty(redis_client):
    assert queue.reclaim_stale(redis_client) == 0


def test_fifo_order_preserved(redis_client):
    queue.enqueue(redis_client, {"request_id": "first"})
    queue.enqueue(redis_client, {"request_id": "second"})

    first = json.loads(queue.reserve(redis_client, timeout=1))
    second = json.loads(queue.reserve(redis_client, timeout=1))

    assert first["request_id"] == "first"
    assert second["request_id"] == "second"


def test_uses_configured_key_names(redis_client):
    queue.enqueue(redis_client, {"request_id": "r1"})
    assert redis_client.llen(config.PENDING_KEY) == 1
