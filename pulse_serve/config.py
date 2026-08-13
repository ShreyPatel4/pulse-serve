"""Environment configuration. Read lazily at call time (not frozen at import)
so tests can monkeypatch os.environ per-test without reloading modules --
same convention pulse-mbta's pulse/db.py uses for PULSE_DSN.
"""

from __future__ import annotations

import os
from pathlib import Path

# -- Postgres --------------------------------------------------------------
# Unix-socket DSN by default (no host/port/password needed for a local brew
# postgres with trust auth). CI and docker-compose both override this with a
# TCP DSN via the PULSE_SERVE_DSN env var -- see tests/conftest.py and
# docker-compose.yml.
DEFAULT_DSN = "postgresql:///pulse_serve"

# -- Redis (queue) -----------------------------------------------------------
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
PENDING_KEY = "pulse:predict:pending"
PROCESSING_KEY = "pulse:predict:processing"

# -- Model -------------------------------------------------------------------
DEFAULT_MODEL_DIR = "models"

# -- Auth ----------------------------------------------------------------
# "placeholder-friendly": running locally with PULSE_API_KEYS unset still
# requires a key on every request (auth is never silently disabled) -- it
# just accepts this one well-known placeholder so a fresh clone works without
# provisioning a real secret first. security.py logs a loud warning at
# startup whenever this placeholder is the active key set. Production
# deployments MUST set PULSE_API_KEYS to real generated values.
PLACEHOLDER_API_KEY = "local-dev-placeholder-key"

# -- Rate limiting -------------------------------------------------------
# Regime note: the token bucket lives in the API process's memory (see
# security.py). It is NOT shared across uvicorn worker processes or replicas
# -- run api with --workers N and the effective ceiling is N times this
# value, and every bucket resets to full on process restart. A shared limit
# across replicas would need a store (e.g. Redis INCR+EXPIRE); out of scope
# for this self-hosted MVP and documented here rather than silently assumed.
DEFAULT_RATE_LIMIT_PER_MIN = 60
DEFAULT_RATE_LIMIT_BURST = 10

# -- Worker metrics --------------------------------------------------------
DEFAULT_WORKER_METRICS_PORT = 9200

# -- Model's trained regime -------------------------------------------------
# pulse-mbta's ML spec (docs/2026-08-13-pulse-design.md) trains against a
# fixed 10-minute horizon. PredictRequest.horizon_min accepts a wider range
# (see schemas.py) so the API doesn't hard-reject a reasonable request, but
# any value other than this is extrapolation outside the trained regime --
# surfaced back to the caller via PredictResponse.horizon_in_trained_regime.
TRAINED_HORIZON_MIN = 10


def dsn() -> str:
    return os.environ.get("PULSE_SERVE_DSN", DEFAULT_DSN)


def redis_url() -> str:
    return os.environ.get("PULSE_SERVE_REDIS_URL", DEFAULT_REDIS_URL)


def model_dir() -> Path:
    return Path(os.environ.get("PULSE_SERVE_MODEL_DIR", DEFAULT_MODEL_DIR))


def api_keys() -> set[str]:
    """Parse PULSE_API_KEYS (comma-separated). Falls back to a single
    well-known placeholder key when unset -- see PLACEHOLDER_API_KEY."""
    raw = os.environ.get("PULSE_API_KEYS", "")
    keys = {key.strip() for key in raw.split(",") if key.strip()}
    return keys or {PLACEHOLDER_API_KEY}


def using_placeholder_api_key() -> bool:
    return api_keys() == {PLACEHOLDER_API_KEY}


def rate_limit_per_min() -> int:
    return int(os.environ.get("PULSE_SERVE_RATE_LIMIT_PER_MIN", DEFAULT_RATE_LIMIT_PER_MIN))


def rate_limit_burst() -> int:
    return int(os.environ.get("PULSE_SERVE_RATE_LIMIT_BURST", DEFAULT_RATE_LIMIT_BURST))


def worker_metrics_port() -> int:
    return int(os.environ.get("PULSE_SERVE_WORKER_METRICS_PORT", DEFAULT_WORKER_METRICS_PORT))
