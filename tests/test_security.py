"""Token bucket unit tests (with an injected clock -- no real sleeping) and
the require_api_key dependency."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from pulse_serve import config
from pulse_serve.security import RateLimiter, TokenBucket, require_api_key


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_token_bucket_allows_up_to_capacity_then_blocks():
    clock = _FakeClock()
    bucket = TokenBucket(capacity=3, refill_per_sec=0, clock=clock)

    assert bucket.consume() is True
    assert bucket.consume() is True
    assert bucket.consume() is True
    assert bucket.consume() is False


def test_token_bucket_refills_over_time():
    clock = _FakeClock()
    bucket = TokenBucket(capacity=1, refill_per_sec=1.0, clock=clock)

    assert bucket.consume() is True
    assert bucket.consume() is False

    clock.advance(1.0)
    assert bucket.consume() is True


def test_token_bucket_retry_after_reports_wait_time():
    clock = _FakeClock()
    bucket = TokenBucket(capacity=1, refill_per_sec=0.5, clock=clock)
    bucket.consume()

    retry_after = bucket.retry_after_seconds()
    assert retry_after == pytest.approx(2.0)


def test_rate_limiter_tracks_buckets_per_key_independently():
    clock = _FakeClock()
    limiter = RateLimiter(capacity=1, refill_per_sec=0, clock=clock)

    allowed_a, _ = limiter.allow("key-a")
    allowed_a_again, _ = limiter.allow("key-a")
    allowed_b, _ = limiter.allow("key-b")

    assert allowed_a is True
    assert allowed_a_again is False
    assert allowed_b is True, "key-b must have its own bucket, unaffected by key-a's exhaustion"


async def test_require_api_key_rejects_missing_key():
    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(x_api_key=None)
    assert exc_info.value.status_code == 401


async def test_require_api_key_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("PULSE_API_KEYS", "real-key-1,real-key-2")
    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(x_api_key="wrong-key")
    assert exc_info.value.status_code == 403


async def test_require_api_key_accepts_configured_key(monkeypatch):
    monkeypatch.setenv("PULSE_API_KEYS", "real-key-1,real-key-2")
    accepted = await require_api_key(x_api_key="real-key-2")
    assert accepted == "real-key-2"


async def test_require_api_key_accepts_placeholder_when_unset(monkeypatch):
    monkeypatch.delenv("PULSE_API_KEYS", raising=False)
    accepted = await require_api_key(x_api_key=config.PLACEHOLDER_API_KEY)
    assert accepted == config.PLACEHOLDER_API_KEY
