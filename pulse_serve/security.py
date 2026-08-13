"""API-key auth (X-API-Key vs PULSE_API_KEYS) and an in-process token-bucket
rate limiter, one bucket per API key.

Regime note (repeated from config.py because it matters at the call site):
this rate limiter's state lives in one Python process's memory. It is not
shared across uvicorn worker processes, across api/worker, or across
replicas, and every bucket resets to full capacity on process restart.
"""

from __future__ import annotations

import threading
import time

from fastapi import Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from pulse_serve import config

# A real security scheme (not a plain Header dependency) so the generated
# OpenAPI docs show an "Authorize" lock on every gated endpoint -- part of
# the syllabus's "auto docs" objective, not just enforcement.
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


class TokenBucket:
    """Classic token bucket: `capacity` tokens, refilled at `refill_per_sec`,
    consumed one per request. `clock` is injectable so tests don't need to
    sleep for real."""

    def __init__(self, capacity: int, refill_per_sec: float, clock=time.monotonic):
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self._clock = clock
        self._tokens = float(capacity)
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_sec)
        self._last = now

    def consume(self, cost: int = 1) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= cost:
                self._tokens -= cost
                return True
            return False

    def retry_after_seconds(self) -> float:
        with self._lock:
            self._refill()
            deficit = 1.0 - self._tokens
            if deficit <= 0:
                return 0.0
            return deficit / self.refill_per_sec if self.refill_per_sec > 0 else float("inf")


class RateLimiter:
    """One TokenBucket per API key, created lazily on first use."""

    def __init__(self, capacity: int, refill_per_sec: float, clock=time.monotonic):
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _bucket_for(self, key: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self.capacity, self.refill_per_sec, clock=self._clock)
                self._buckets[key] = bucket
            return bucket

    def allow(self, key: str) -> tuple[bool, float]:
        bucket = self._bucket_for(key)
        allowed = bucket.consume()
        retry_after = 0.0 if allowed else bucket.retry_after_seconds()
        return allowed, retry_after


def build_rate_limiter() -> RateLimiter:
    per_min = config.rate_limit_per_min()
    burst = config.rate_limit_burst()
    return RateLimiter(capacity=burst, refill_per_sec=per_min / 60.0)


async def require_api_key(x_api_key: str | None = Security(api_key_scheme)) -> str:
    """FastAPI dependency: 401 with no key, 403 with a key that isn't in
    PULSE_API_KEYS (including the local-dev placeholder when that's what's
    configured -- placeholder-friendly, not auth-optional)."""
    if x_api_key is None:
        raise HTTPException(status_code=401, detail="missing X-API-Key header")
    if x_api_key not in config.api_keys():
        raise HTTPException(status_code=403, detail="invalid API key")
    return x_api_key


async def rate_limit_dependency(
    request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")
) -> None:
    """Reads the RateLimiter off app.state (built once at startup with a
    real clock -- see app.py's lifespan) so tests can swap in a limiter with
    an injected clock via app.state without patching any global."""
    limiter: RateLimiter = request.app.state.rate_limiter
    allowed, retry_after = limiter.allow(x_api_key or "anonymous")
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(max(1, int(retry_after) + 1))},
        )
