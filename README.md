# pulse-serve

Project 2 of the five-project program: "Pulse in production, model as a
service." This is the self-hosted serving layer for pulse-mbta's model --
`P(delay>180s)` per (route, direction, stop, trip) at a 10-minute horizon
(full ML spec: `../pulse-mbta/docs/2026-08-13-pulse-design.md`). Standing
cost: $0. Runs entirely on the operator's M1 Pro Mac, no cloud, everything
here runnable up to the key-gated deployment line -- see
`docs/design.md`'s "Deployment placeholders" section for exactly what stops
short and why.

This repo covers the syllabus's serving-layer objectives with self-hosted
equivalents, documented honestly rather than silently substituted: FastAPI +
Pydantic validation, API-key auth + rate limiting, auto docs, model
serialization safety (the joblib trap), Docker multi-stage builds, an
async inference pipeline (API -> queue -> worker), a NoSQL-shaped results
store (Postgres JSONB, not literally DynamoDB), monitoring + alarms, and CI
that blocks a bad push. The full AWS re-target table is in
`docs/design.md`.

## Honest state

**No trained model exists yet.** pulse-mbta is still at M1 (ingestion); M3
(baselines + models) hasn't run. Every prediction this service returns
today is a clearly-labeled **BASELINE fallback** -- `model_version:
"baseline-fallback"` on every single response, never a faked trained-model
number. When pulse-mbta's M3 produces `current.joblib` + `manifest.json`,
drop them in `models/` (see `models/README.md` for the exact contract) and
this service picks them up on next startup with zero code changes.

Docker: the daemon was not running on the dev machine this was built on, so
`docker build` / `docker compose up` were never actually executed here --
only `docker compose config` (syntax + interpolation validation, no daemon
needed) was run, and it passed. CI (`.github/workflows/ci.yml`) runs a real
`docker build` on every push; that's this project's actual build gate, not
this laptop. See `docs/design.md`'s verification note for the full story.

## Quickstart

```bash
# Dev: just the api, against local Postgres/Redis (brew services, or
# anything already listening on the defaults below).
uv sync
uv run python scripts/migrate.py        # creates `pulse_serve` db + predictions table
uv run uvicorn pulse_serve.app:app --reload

# In another terminal, the worker (only needed for the async endpoints):
uv run python -m pulse_serve.worker

# Full stack: api + worker + redis + postgres + prometheus + grafana.
docker compose up --build
# api:        http://localhost:8000
# prometheus: http://localhost:9090
# grafana:    http://localhost:3000  (admin / local-dev-placeholder, see docs/design.md)
```

No `PULSE_API_KEYS` set? The service still runs -- it accepts exactly one
well-known placeholder key (`local-dev-placeholder-key`) rather than
disabling auth, and prints a loud startup warning that it's doing so. Real
deployments must set real keys; see "Environment variables" below.

```bash
curl -s localhost:8000/healthz | python3 -m json.tool

curl -s localhost:8000/v1/predict -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-Key: local-dev-placeholder-key" \
  -d '{"route_id":"1","direction_id":0,"stop_id":"110","trip_id":"trip-1"}'
```

Interactive API docs (FastAPI auto docs, syllabus objective): `/docs`
(Swagger UI) and `/redoc`.

## Delivery semantics

The async path (`POST /v1/predict/async` -> worker -> `GET
/v1/results/{request_id}`) is **at-least-once delivery + an idempotent
write = effectively-once** from the caller's point of view:

- **At-least-once**: `pulse_serve/queue.py` implements the reliable
  `BRPOPLPUSH` pattern -- a job moves atomically from a `PENDING` Redis list
  to a `PROCESSING` list when a worker reserves it, and is only removed from
  `PROCESSING` after a successful Postgres write (`ack()`). If the worker
  crashes between reserving and acking, the job sits in `PROCESSING` until
  the next worker startup, whose first act is `reclaim_stale()`: move
  everything still in `PROCESSING` back to `PENDING`. Without that reclaim
  step, a crash-before-ack job would be delivered *zero* times, not at
  least once -- it's not a nice-to-have, it's what makes the "at-least-once"
  claim true. Known limitation: this reclaim is startup-only (not a
  per-job lease/timeout) and is only correct with exactly one worker
  replica, which is what `docker-compose.yml` runs.
- **Idempotent write**: `pulse_serve/store.py`'s `INSERT ... ON CONFLICT
  (request_id) DO NOTHING` means a redelivered job's second write is a
  silent no-op. `request_id` (a UUID generated at enqueue time) is the
  single source of truth for "did this job already run" -- the same shape
  pulse-mbta's `stop_events` uses `(trip_id, stop_id, polled_at)` for.
- **`GET /v1/results/{request_id}`** returns `status: "pending"` for both
  "still queued or being worked" and "this request_id was never issued" --
  deliberately not distinguished, since there's no separate
  queued-request tracking table (only a completed-facts table, same
  immutable-facts philosophy as pulse-mbta's `stop_events`). A malformed
  (non-UUID) `request_id` gets the same `"pending"` answer rather than a
  raw database error -- see `pulse_serve/store.py`'s `get_prediction`.
- **Queue durability**: `docker-compose.yml`'s redis runs with `--save ""
  --appendonly no` -- the queue holds seconds-to-minutes of transient work,
  not a durable log. A redis restart (or eviction) loses whatever was in
  `PENDING`/`PROCESSING` at that moment. That's a real gap in the
  effectively-once story, stated rather than hidden: effectively-once holds
  *unless redis itself loses data*.

The sync path (`POST /v1/predict`) has no delivery-semantics story at all --
it's a normal HTTP request/response, no queue involved.

## Model loading + the joblib trap

`pulse_serve/model.py` loads `models/current.joblib` only if
`models/manifest.json` is also present and its declared `sha256` matches
the actual file on disk -- computed *before* `joblib.load()` is ever called.
`joblib.load` is `pickle` underneath, and unpickling executes arbitrary code
chosen by whoever wrote the bytes; sha256 pinning catches corruption, a
wrong-file swap, or a stale copy, but does **not** prove the artifact is
safe if an attacker controls both the artifact and the manifest describing
it (they'd just recompute a matching hash). The real defense is provenance:
only ever place a `current.joblib` here that pulse-mbta's own training
pipeline produced. Full writeup: `pulse_serve/model.py`'s module docstring.

Every failure mode -- hash mismatch, one file present without its
counterpart, a manifest missing required keys, an unpickle failure, an
artifact that doesn't implement the `predict_proba(features: dict) -> float`
contract -- serves the BASELINE fallback rather than crashing, but is
tracked separately from the clean "no model file yet" state via
`integrity_ok`: `GET /healthz` returns HTTP 503 (`status: "degraded"`) when
a model file was present but failed verification, vs. HTTP 200 (`status:
"ok"`) for both a verified real model and the honest empty-`models/` state.
`pulse_serve_model_integrity_ok` on `/metrics` (and
`PulseServeModelIntegrityFailed` in `ops/prometheus/alerts.yml`) carries the
same distinction into monitoring.

## Environment variables

| var | default | notes |
|---|---|---|
| `PULSE_API_KEYS` | unset -> placeholder key only | comma-separated. See `pulse_serve/config.py` |
| `PULSE_SERVE_DSN` | `postgresql:///pulse_serve` | Postgres, Unix-socket by default; docker-compose overrides to a TCP DSN |
| `PULSE_SERVE_REDIS_URL` | `redis://localhost:6379/0` | |
| `PULSE_SERVE_MODEL_DIR` | `models` | |
| `PULSE_SERVE_RATE_LIMIT_PER_MIN` / `PULSE_SERVE_RATE_LIMIT_BURST` | `60` / `10` | per-API-key token bucket, **in-process** -- see the regime note below |
| `PULSE_SERVE_WORKER_METRICS_PORT` | `9200` | worker's own `/metrics` |

**Rate-limit regime note**: the token bucket lives in one Python process's
memory (`pulse_serve/security.py`). It is not shared across uvicorn worker
processes or replicas -- run `--workers N` and the effective ceiling is `N x`
the configured value, and every bucket resets to full on process restart.

## Ops

- **Migrations**: `uv run python scripts/migrate.py` -- creates the
  database if absent, applies `migrations/*.sql` in order, tracks applied
  migrations in a `schema_migrations` table. Same shape as pulse-mbta's
  `scripts/migrate.py`. Runs automatically as a one-shot `migrate` service
  in `docker-compose.yml` before `api`/`worker` start.
- **Monitoring**: Prometheus scrapes `api:8000/metrics` and
  `worker:9200/metrics` as separate jobs (`ops/prometheus/prometheus.yml`);
  Grafana is pre-provisioned with a datasource + a 4-panel dashboard
  (request rate, p95 latency, live queue depth, worker throughput --
  `ops/grafana/`). Queue depth is read live at scrape time via a custom
  Prometheus collector (`pulse_serve/metrics.py`'s `QueueDepthCollector`),
  not cached from the last `/healthz` call.
- **Alarms**: `ops/prometheus/alerts.yml` -- api down, worker down, model
  integrity failed, queue depth sustained high, sync p95 latency over SLO.
  The rules are real; alert *delivery* (Alertmanager + Resend) is a
  deployment placeholder, see `docs/design.md`.
- **Tests**: `uv run pytest`. Requires a real local Postgres (same
  create/drop-a-scratch-database convention as pulse-mbta's `tests/test_db.py`)
  -- unconditionally, not guarded, since Postgres is assumed to already be a
  standing local service. Redis-dependent tests use a `redis_client` fixture
  that **skips** (not fails, not mocks) when no redis is reachable --
  `uv run pytest` alone will show those as skipped; start any local redis
  and re-run to exercise them for real. CI runs both services for real, so
  nothing is ever skipped there.
- **Lint**: `uv run ruff check .`

## Architecture, SLOs, cost, and the AWS re-target table

All in `docs/design.md`: an ASCII architecture diagram, measured (not
assumed) p50/p95 sync latency and async completion-time numbers, a
measured-wattage cost-per-1k-predictions calculation with the assumption
chain labeled, the full AWS service-by-service re-target table, and the
deployment-placeholders list (cloudflared tunnel, `MBTA_API_KEY`
passthrough, Resend alerting) this build deliberately stops short of.
