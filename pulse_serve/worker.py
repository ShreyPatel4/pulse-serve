"""Async worker: consumes pulse_serve's Redis queue, predicts, writes to
Postgres. Run with: uv run python -m pulse_serve.worker

Runs as exactly one replica -- see pulse_serve.queue's module docstring for
why reclaim_stale()'s startup-only reclaim is only safe single-worker.
"""

from __future__ import annotations

import datetime as dt
import json
import signal
import sys
import time

import psycopg
import redis
from prometheus_client import start_http_server

from pulse_serve import config, queue, store
from pulse_serve.metrics import WORKER_PREDICTIONS_COUNTER
from pulse_serve.model import load_model
from pulse_serve.predict import run_prediction
from pulse_serve.schemas import PredictRequest

RESERVE_TIMEOUT_SECONDS = 5

_shutdown = False


def _handle_signal(signum, frame) -> None:  # noqa: ARG001
    global _shutdown
    _shutdown = True


def _process_one(raw: str, redis_client: redis.Redis, db_conn: psycopg.Connection, bundle) -> None:
    payload = json.loads(raw)
    request = PredictRequest(
        route_id=payload["route_id"],
        direction_id=payload["direction_id"],
        stop_id=payload["stop_id"],
        trip_id=payload["trip_id"],
        horizon_min=payload["horizon_min"],
    )
    result = run_prediction(bundle, request)

    row = {
        "request_id": payload["request_id"],
        "route_id": payload["route_id"],
        "direction_id": payload["direction_id"],
        "stop_id": payload["stop_id"],
        "trip_id": payload["trip_id"],
        "horizon_min": payload["horizon_min"],
        "result": result.model_dump(mode="json"),
        "model_version": result.model_version,
        "is_baseline": result.is_baseline,
        "queued_at": payload["queued_at"],
    }
    inserted = store.insert_prediction(db_conn, row)
    if inserted:
        print(f"pulse_serve.worker: request_id={row['request_id']} inserted")
    else:
        print(
            f"pulse_serve.worker: request_id={row['request_id']} already present "
            "(redelivery, idempotent no-op)"
        )

    # ack only after the write lands -- a crash between predict and ack
    # leaves this job in PROCESSING, to be redelivered by the next worker
    # startup's reclaim_stale(). The idempotent insert above is what makes
    # that redelivery safe rather than a duplicate.
    queue.ack(redis_client, raw)
    WORKER_PREDICTIONS_COUNTER.labels(outcome="success").inc()


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    start_http_server(config.worker_metrics_port())
    print(f"pulse_serve.worker: metrics on :{config.worker_metrics_port()}/metrics")

    bundle = load_model(config.model_dir())
    print(f"pulse_serve.worker: model_version={bundle.version} is_baseline={bundle.is_baseline}")

    redis_client = queue.connect()
    reclaimed = queue.reclaim_stale(redis_client)
    if reclaimed:
        print(f"pulse_serve.worker: reclaimed {reclaimed} job(s) left in PROCESSING by a previous run")

    db_conn = store.connect()

    print(f"pulse_serve.worker: started at {dt.datetime.now(dt.UTC).isoformat()}")
    while not _shutdown:
        try:
            raw = queue.reserve(redis_client, timeout=RESERVE_TIMEOUT_SECONDS)
        except redis.RedisError as exc:
            print(f"pulse_serve.worker: redis error on reserve: {exc}", file=sys.stderr)
            time.sleep(1)
            continue

        if raw is None:
            continue  # reserve() timed out with no job -- normal, loop and check _shutdown

        try:
            _process_one(raw, redis_client, db_conn, bundle)
        except Exception as exc:  # noqa: BLE001 - a bad job must not crash the worker
            job_id = _safe_job_id(raw)
            print(
                f"pulse_serve.worker: job {job_id} failed, left in PROCESSING for reclaim: {exc}",
                file=sys.stderr,
            )
            WORKER_PREDICTIONS_COUNTER.labels(outcome="error").inc()
            # Deliberately not acked: reclaim_stale() on the next worker
            # restart will requeue it. A payload that always raises (a
            # "poison pill") will loop forever across restarts -- there is
            # no dead-letter queue in this MVP; documented as a known gap.

    db_conn.close()
    print("pulse_serve.worker: shut down cleanly")
    return 0


def _safe_job_id(raw: str) -> str:
    try:
        return json.loads(raw).get("request_id", "?")
    except (json.JSONDecodeError, AttributeError):
        return "?"


if __name__ == "__main__":
    sys.exit(main())
