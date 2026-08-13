"""FastAPI app: POST /v1/predict, POST /v1/predict/async, GET
/v1/results/{request_id}, GET /healthz, GET /metrics.

Model loading, redis, and the rate limiter are all built once in the
lifespan context manager and cached on app.state -- see the syllabus
objective list in docs/design.md ("startup loading, caching").
"""

from __future__ import annotations

import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
import redis
from fastapi import Depends, FastAPI, HTTPException, Response
from prometheus_client import REGISTRY

from pulse_serve import config, queue, store
from pulse_serve.metrics import (
    LATENCY_HISTOGRAM,
    MODEL_INTEGRITY_GAUGE,
    REQUEST_COUNTER,
    QueueDepthCollector,
    register_queue_depth_collector,
    render_latest,
)
from pulse_serve.model import load_model
from pulse_serve.predict import run_prediction
from pulse_serve.schemas import (
    AsyncEnqueueResponse,
    HealthResponse,
    PredictRequest,
    PredictResponse,
    ResultResponse,
)
from pulse_serve.security import RateLimiter, build_rate_limiter, rate_limit_dependency, require_api_key


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.start_time = time.monotonic()
    app.state.model_bundle = load_model(config.model_dir())
    MODEL_INTEGRITY_GAUGE.set(1 if app.state.model_bundle.integrity_ok else 0)

    if config.using_placeholder_api_key():
        print(
            "pulse_serve.app: PULSE_API_KEYS is unset -- accepting only the well-known placeholder key "
            f"{config.PLACEHOLDER_API_KEY!r}. Set PULSE_API_KEYS before any non-local deployment.",
            file=sys.stderr,
        )

    app.state.rate_limiter: RateLimiter = build_rate_limiter()

    try:
        app.state.redis = redis.Redis.from_url(
            config.redis_url(), decode_responses=True, socket_connect_timeout=1
        )
    except Exception:  # noqa: BLE001 - redis is optional for sync predict; /healthz reports the outage
        app.state.redis = None

    def _queue_depth() -> int:
        return queue.pending_depth(app.state.redis)

    collector: QueueDepthCollector = register_queue_depth_collector(REGISTRY, _queue_depth)

    try:
        yield
    finally:
        REGISTRY.unregister(collector)
        if app.state.redis is not None:
            try:
                app.state.redis.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup on shutdown
                pass


app = FastAPI(
    title="pulse-serve",
    description="Self-hosted serving layer for Pulse's P(delay>180s) model. See docs/design.md.",
    version="0.1.0",
    lifespan=lifespan,
)


def _redis_ping(client: redis.Redis | None) -> bool:
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception:  # noqa: BLE001 - health check, never raise
        return False


def _db_ping() -> bool:
    try:
        conn = store.connect()
        try:
            conn.execute("SELECT 1")
            return True
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - health check, never raise
        return False


@app.get("/healthz", response_model=HealthResponse)
async def healthz(response: Response) -> HealthResponse:
    bundle = app.state.model_bundle
    redis_connected = _redis_ping(app.state.redis)
    queue_depth = queue.pending_depth(app.state.redis) if redis_connected else None
    db_connected = _db_ping()

    body = HealthResponse(
        status="ok" if bundle.integrity_ok else "degraded",
        model_version=bundle.version,
        is_baseline=bundle.is_baseline,
        model_integrity_ok=bundle.integrity_ok,
        uptime_seconds=time.monotonic() - app.state.start_time,
        queue_depth=queue_depth,
        redis_connected=redis_connected,
        db_connected=db_connected,
    )
    # Only a model-integrity failure fails the health check's HTTP status --
    # redis/db connectivity are surfaced as booleans in the body but don't
    # flip the status code, since a sync-only demo run doesn't need either.
    if not bundle.integrity_ok:
        response.status_code = 503
    return body


@app.get("/metrics")
async def metrics() -> Response:
    # Unauthenticated by convention (Prometheus scraping is usually
    # unauthenticated within a private network) -- see docs/design.md's
    # deployment-placeholders section for why a public deployment needs this
    # behind the tunnel's own access policy rather than an API key here.
    payload, content_type = render_latest(REGISTRY)
    return Response(content=payload, media_type=content_type)


@app.post(
    "/v1/predict",
    response_model=PredictResponse,
    dependencies=[Depends(require_api_key), Depends(rate_limit_dependency)],
)
async def predict(body: PredictRequest) -> PredictResponse:
    started = time.monotonic()
    result = run_prediction(app.state.model_bundle, body)
    LATENCY_HISTOGRAM.labels(endpoint="/v1/predict").observe(time.monotonic() - started)
    REQUEST_COUNTER.labels(endpoint="/v1/predict", status="200").inc()
    return PredictResponse(
        route_id=body.route_id,
        direction_id=body.direction_id,
        stop_id=body.stop_id,
        trip_id=body.trip_id,
        horizon_min=body.horizon_min,
        **result.model_dump(),
    )


@app.post(
    "/v1/predict/async",
    response_model=AsyncEnqueueResponse,
    dependencies=[Depends(require_api_key), Depends(rate_limit_dependency)],
)
async def predict_async(body: PredictRequest) -> AsyncEnqueueResponse:
    if app.state.redis is None:
        REQUEST_COUNTER.labels(endpoint="/v1/predict/async", status="503").inc()
        raise HTTPException(status_code=503, detail="queue (redis) unavailable")

    response = AsyncEnqueueResponse(request_id=str(uuid.uuid4()))
    payload = {
        "request_id": response.request_id,
        "route_id": body.route_id,
        "direction_id": body.direction_id,
        "stop_id": body.stop_id,
        "trip_id": body.trip_id,
        "horizon_min": body.horizon_min,
        "queued_at": response.queued_at.isoformat(),
    }
    try:
        queue.enqueue(app.state.redis, payload)
    except redis.RedisError as exc:
        # redis-py connects lazily -- from_url() in lifespan() never raised,
        # so this is where an actually-unreachable redis first surfaces.
        REQUEST_COUNTER.labels(endpoint="/v1/predict/async", status="503").inc()
        raise HTTPException(status_code=503, detail=f"queue (redis) unavailable: {exc}") from None
    REQUEST_COUNTER.labels(endpoint="/v1/predict/async", status="200").inc()
    return response


@app.get(
    "/v1/results/{request_id}",
    response_model=ResultResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_result(request_id: str) -> ResultResponse:
    try:
        conn: psycopg.Connection = store.connect()
    except Exception as exc:  # noqa: BLE001 - surfaced as 503, not a stack trace
        raise HTTPException(status_code=503, detail=f"results store unavailable: {exc}") from None

    try:
        row = store.get_prediction(conn, request_id)
    finally:
        conn.close()

    if row is None:
        # Deliberately identical to "still queued/processing" -- see
        # README.md's delivery-semantics section for why an unknown
        # request_id and an in-flight one are not distinguished.
        return ResultResponse(request_id=request_id, status="pending")

    return ResultResponse(
        request_id=request_id,
        status="done",
        result=row["result"],
        completed_at=row["completed_at"],
    )
