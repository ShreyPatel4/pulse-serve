"""Redis queue: the reliable BRPOPLPUSH pattern, at-least-once delivery.

Two lists: PENDING (work not yet claimed) and PROCESSING (work a worker has
claimed but not yet acknowledged). `reserve()` uses BRPOPLPUSH to atomically
move one job from PENDING to PROCESSING -- the job is never in neither list,
so a worker crash between BRPOPLPUSH and predicting doesn't lose it.

**At-least-once has no teeth without `reclaim_stale()`.** If a worker pops a
job into PROCESSING and dies before `ack()`, that job sits in PROCESSING
forever and would be delivered *zero* times -- the opposite of at-least-once
-- unless something moves it back to PENDING. worker.py calls
`reclaim_stale()` once at startup for exactly this reason: a fresh worker
process assumes anything already in PROCESSING belongs to a predecessor that
never finished, and requeues it.

**Known limitation, stated rather than hidden**: this reclaim-on-startup
approach is only safe with a single worker process. With more than one
worker replica running concurrently, one worker's startup reclaim can steal
a job a second, still-alive worker is actively processing (no ownership
timestamp or lease is tracked). docker-compose.yml runs exactly one `worker`
replica for this reason. A production multi-worker queue would need a
per-job lease keyed by worker id with a visibility timeout (Redis ZSET with
a score, or a proper queue like Sidekiq/RQ's reliable-fetch libraries) --
out of scope for this self-hosted MVP.

**Queue durability**: docker-compose's redis runs with `--save ""` (no RDB
snapshots) since this queue is meant to hold seconds-to-minutes of transient
work, not a durable log -- a `docker compose restart redis` (or an OOM
eviction) loses whatever was in PENDING/PROCESSING at that moment. Combined
with idempotent writes in store.py, the end-to-end promise is: an
async-enqueued request is delivered "effectively once" *unless* redis itself
loses data, which is a documented gap, not a hidden one.
"""

from __future__ import annotations

import json
from typing import Any

import redis

from pulse_serve import config


def connect(url: str | None = None) -> redis.Redis:
    return redis.Redis.from_url(url or config.redis_url(), decode_responses=True)


def enqueue(client: redis.Redis, payload: dict[str, Any]) -> None:
    client.lpush(config.PENDING_KEY, json.dumps(payload, default=str))


def reserve(client: redis.Redis, timeout: int = 5) -> str | None:
    """Atomically move one job PENDING -> PROCESSING, blocking up to
    `timeout` seconds. Returns the raw JSON string (not parsed) -- keep it
    raw and pass it straight to ack(): re-serializing a parsed dict before
    calling LREM risks a byte-for-byte mismatch (key ordering, whitespace)
    that would silently leak the entry in PROCESSING forever."""
    result = client.brpoplpush(config.PENDING_KEY, config.PROCESSING_KEY, timeout=timeout)
    return result


def ack(client: redis.Redis, raw: str) -> None:
    """Remove exactly the raw string reserve() returned from PROCESSING.
    LREM count=1 matches byte-for-byte -- see reserve()'s docstring for why
    `raw` must be the untouched string, not a re-serialized dict."""
    client.lrem(config.PROCESSING_KEY, 1, raw)


def reclaim_stale(client: redis.Redis) -> int:
    """Move every entry currently in PROCESSING back onto PENDING. Call once
    at worker startup -- see module docstring for why this is what makes
    at-least-once actually true, and its single-worker limitation."""
    count = 0
    while client.rpoplpush(config.PROCESSING_KEY, config.PENDING_KEY) is not None:
        count += 1
    return count


def pending_depth(client: redis.Redis) -> int:
    return client.llen(config.PENDING_KEY)


def processing_depth(client: redis.Redis) -> int:
    return client.llen(config.PROCESSING_KEY)
