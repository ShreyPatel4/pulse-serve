"""Postgres results store: the `predictions` table, JSONB result column,
dedup on request_id. See migrations/001_predictions.sql for the schema."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from pulse_serve import config

_INSERT_SQL = """
INSERT INTO predictions (
    request_id, route_id, direction_id, stop_id, trip_id, horizon_min,
    result, model_version, is_baseline, queued_at
) VALUES (
    %(request_id)s, %(route_id)s, %(direction_id)s, %(stop_id)s, %(trip_id)s, %(horizon_min)s,
    %(result)s, %(model_version)s, %(is_baseline)s, %(queued_at)s
)
ON CONFLICT (request_id) DO NOTHING
"""

_SELECT_SQL = """
SELECT request_id, route_id, direction_id, stop_id, trip_id, horizon_min,
       result, model_version, is_baseline, queued_at, completed_at
FROM predictions
WHERE request_id = %(request_id)s
"""


def connect(dsn: str | None = None) -> psycopg.Connection:
    dsn = dsn or os.environ.get("PULSE_SERVE_DSN", config.DEFAULT_DSN)
    conn = psycopg.connect(dsn)
    conn.autocommit = True
    return conn


def insert_prediction(conn: psycopg.Connection, row: Mapping[str, Any]) -> bool:
    """Idempotent write: request_id is the dedup key (worker.py's at-least-
    once redelivery can call this twice for the same job; the second call is
    a no-op). Returns True if this call actually inserted the row, False if
    it was already there.

    row["result"] must already be JSON-safe (e.g. built via a Pydantic
    model's .model_dump(mode="json")) -- Jsonb wraps it with the standard
    json.dumps, which can't serialize datetime/UUID objects on its own.
    """
    params = dict(row)
    params["result"] = Jsonb(params["result"])
    with conn.cursor() as cur:
        cur.execute(_INSERT_SQL, params)
        return cur.rowcount == 1


def get_prediction(conn: psycopg.Connection, request_id: str) -> dict | None:
    """None covers both "no row with this request_id" and "request_id isn't
    even valid UUID syntax" -- app.py's GET /v1/results/{request_id} takes
    an arbitrary path string, and a malformed id can never match a row
    either way, so it gets the same "pending" answer rather than a raw
    500 from Postgres's uuid input parser. See connect()'s autocommit=True:
    a caught error here doesn't need an explicit rollback."""
    with conn.cursor() as cur:
        try:
            cur.execute(_SELECT_SQL, {"request_id": request_id})
        except pg_errors.InvalidTextRepresentation:
            return None
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc.name for desc in cur.description]
        return dict(zip(columns, row, strict=True))
