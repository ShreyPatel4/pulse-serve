"""Prometheus metrics. Two processes expose /metrics: the api process (via
app.py's GET /metrics) and the worker process (via prometheus_client's own
start_http_server in worker.py) -- they're separate Python processes with
separate default REGISTRYs, so their metric names don't collide, and
prometheus.yml scrapes both as distinct jobs.
"""

from __future__ import annotations

from collections.abc import Callable

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import CollectorRegistry

REQUEST_COUNTER = Counter(
    "pulse_serve_requests_total", "Requests handled by the api process", ["endpoint", "status"]
)
LATENCY_HISTOGRAM = Histogram(
    "pulse_serve_request_latency_seconds", "Request latency observed by the api process", ["endpoint"]
)
MODEL_INTEGRITY_GAUGE = Gauge(
    "pulse_serve_model_integrity_ok",
    "1 if the loaded model is either a verified real model or the clean no-model-file baseline state; "
    "0 if a model file was present but failed verification (see pulse_serve.model's integrity_ok)",
)
WORKER_PREDICTIONS_COUNTER = Counter(
    "pulse_serve_worker_predictions_total",
    "Predictions the worker process has written to Postgres",
    ["outcome"],
)


class QueueDepthCollector:
    """A custom Collector, not a plain Gauge.set() -- so pulse_serve_queue_depth
    is read live via Redis LLEN at *scrape* time, not cached from whatever it
    was when /healthz last ran. A Gauge updated only inside /healthz would go
    stale between health checks and mislead the dashboard panel that reads
    from it."""

    def __init__(self, depth_fn: Callable[[], int]):
        self._depth_fn = depth_fn

    def collect(self):
        gauge = GaugeMetricFamily(
            "pulse_serve_queue_depth", "Live Redis pending-queue length (LLEN), read at scrape time"
        )
        try:
            gauge.add_metric([], float(self._depth_fn()))
        except Exception:  # noqa: BLE001 - a scrape must never 500 because redis is down
            gauge.add_metric([], float("nan"))
        yield gauge


def register_queue_depth_collector(
    registry: CollectorRegistry, depth_fn: Callable[[], int]
) -> QueueDepthCollector:
    collector = QueueDepthCollector(depth_fn)
    registry.register(collector)
    return collector


def render_latest(registry: CollectorRegistry) -> tuple[bytes, str]:
    return generate_latest(registry), CONTENT_TYPE_LATEST
