"""Shared fixtures.

Postgres: tests require a real local Postgres, same convention as
pulse-mbta's tests/test_db.py -- create/drop a scratch database per test.
The DSN is derived from PULSE_SERVE_DSN (falling back to
pulse_serve.config.DEFAULT_DSN) rather than hardcoded, so this works
unchanged against a local Unix-socket Postgres on a dev laptop and a
TCP+password Postgres service container in CI -- only the dbname is
overridden.

Redis: tests that need it use the `redis_client` fixture below, which skips
(not fails) when no redis is reachable -- per the task's own guidance to
keep dependencies real rather than mocking the queue.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import joblib
import psycopg
import pytest
import redis as redis_lib
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from pulse_serve import config

MIGRATION_SQL = (Path(__file__).resolve().parent.parent / "migrations" / "001_predictions.sql").read_text()


def _test_dsns() -> tuple[str, str]:
    base = os.environ.get("PULSE_SERVE_DSN", config.DEFAULT_DSN)
    params = conninfo_to_dict(base)
    admin_params = dict(params, dbname="postgres")
    test_params = dict(params, dbname="pulse_serve_test")
    return make_conninfo(**admin_params), make_conninfo(**test_params)


@pytest.fixture()
def pg_test_dsn():
    admin_dsn, test_dsn = _test_dsns()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS pulse_serve_test")
        admin.execute("CREATE DATABASE pulse_serve_test")

    with psycopg.connect(test_dsn, autocommit=True) as setup:
        setup.execute(MIGRATION_SQL)

    yield test_dsn

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS pulse_serve_test")


@pytest.fixture()
def pg_conn(pg_test_dsn):
    conn = psycopg.connect(pg_test_dsn, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def redis_client():
    url = os.environ.get("PULSE_SERVE_REDIS_URL", config.DEFAULT_REDIS_URL)
    client = redis_lib.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001 - any connectivity failure means "skip", not "fail"
        pytest.skip(f"redis not available at {url}: {exc}")

    client.delete(config.PENDING_KEY, config.PROCESSING_KEY)
    yield client
    client.delete(config.PENDING_KEY, config.PROCESSING_KEY)


def write_model_artifact(
    model_dir: Path,
    obj: object,
    *,
    semver: str = "0.1.0",
    trained_at: str = "2026-08-01T00:00:00Z",
    data_window: str = "2026-07-01..2026-08-01",
    sha256_override: str | None = None,
) -> tuple[Path, Path]:
    """Write a real joblib artifact + matching manifest.json into model_dir.
    Returns (current_path, manifest_path). Pass sha256_override to build a
    deliberately-mismatched pair for the integrity-check tests."""
    model_dir.mkdir(parents=True, exist_ok=True)
    current_path = model_dir / "current.joblib"
    joblib.dump(obj, current_path)
    actual_sha256 = hashlib.sha256(current_path.read_bytes()).hexdigest()
    manifest = {
        "semver": semver,
        "sha256": sha256_override or actual_sha256,
        "trained_at": trained_at,
        "data_window": data_window,
    }
    manifest_path = model_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return current_path, manifest_path
