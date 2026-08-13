"""Postgres results store: dedup on request_id (the delivery-semantics
story), against a real local scratch Postgres database. Same shape as
pulse-mbta's tests/test_db.py."""

from __future__ import annotations

import datetime as dt
import uuid

from pulse_serve import store


def _row(**overrides) -> dict:
    row = {
        "request_id": str(uuid.uuid4()),
        "route_id": "1",
        "direction_id": 0,
        "stop_id": "110",
        "trip_id": "trip-1",
        "horizon_min": 10,
        "result": {"probability_delay_gt_180s": 0.3, "model_version": "baseline-fallback"},
        "model_version": "baseline-fallback",
        "is_baseline": True,
        "queued_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    row.update(overrides)
    return row


def test_insert_then_get_round_trips_jsonb_result(pg_conn):
    row = _row()

    inserted = store.insert_prediction(pg_conn, row)
    assert inserted is True

    fetched = store.get_prediction(pg_conn, row["request_id"])
    assert fetched is not None
    assert fetched["result"] == row["result"]
    assert fetched["route_id"] == "1"
    assert fetched["is_baseline"] is True


def test_insert_same_request_id_twice_second_call_is_noop(pg_conn):
    row = _row()

    assert store.insert_prediction(pg_conn, row) is True
    assert store.insert_prediction(pg_conn, row) is False

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM predictions WHERE request_id = %s", (row["request_id"],))
        assert cur.fetchone()[0] == 1


def test_get_unknown_request_id_returns_none(pg_conn):
    assert store.get_prediction(pg_conn, str(uuid.uuid4())) is None


def test_get_malformed_request_id_returns_none_not_a_db_error(pg_conn):
    """request_id is a uuid column; a caller can pass any string as the path
    param, and "not even valid UUID syntax" must look the same as "unknown"
    to the API rather than surfacing a raw psycopg error as a 500."""
    assert store.get_prediction(pg_conn, "does-not-exist") is None


def test_redelivered_job_with_different_result_still_keeps_first_write(pg_conn):
    """Models the at-least-once redelivery scenario: the same request_id is
    inserted twice with the write happening from two "different" worker
    attempts. ON CONFLICT DO NOTHING means the second write never overwrites
    the first -- the result a caller reads back is whichever attempt won the
    race to insert first, deterministically."""
    request_id = str(uuid.uuid4())
    first = _row(request_id=request_id, result={"probability_delay_gt_180s": 0.1})
    second = _row(request_id=request_id, result={"probability_delay_gt_180s": 0.9})

    store.insert_prediction(pg_conn, first)
    store.insert_prediction(pg_conn, second)

    fetched = store.get_prediction(pg_conn, request_id)
    assert fetched["result"]["probability_delay_gt_180s"] == 0.1
