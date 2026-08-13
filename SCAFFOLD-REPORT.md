# pulse-serve -- scaffold report

Autonomous build, 2026-08-12/13. Status: **complete and green**, pushed to
`origin/main`, CI passing for real (not just locally).

## Status

Everything in the task spec is built and verified: the FastAPI service
(sync + async predict, results, health, metrics), the Redis reliable queue
+ worker, the Postgres results store with idempotent dedup, model loading
with sha256 verification and a BASELINE fallback, the multi-stage Docker
image, docker-compose full stack (api/worker/redis/postgres/prometheus/
grafana), Prometheus alert rules, a provisioned 4-panel Grafana dashboard,
GitHub Actions CI, and `docs/design.md` + `README.md`.

The Docker daemon was not running on this machine for the entire build
(`docker info` couldn't reach the socket) and, per instructions, was never
started. `docker compose config` validated the compose file's syntax
without the daemon. The actual `docker build` was verified for real in
GitHub Actions instead -- see the CI evidence below, which includes a
successful `docker build -f docker/Dockerfile .`.

## Commits (pushed, `origin/main` = local `main`)

```
aa4d8f0 ci: SHA-pin actions instead of trusting a moving major-version tag
6ae70aa docker+ops+ci+docs: full stack, monitoring, alarms, CI gate, design doc
c1e5df9 scaffold: pulse_serve service package -- FastAPI, model loading, queue, store
```

No `Co-Authored-By` lines; git identity is the preconfigured
`Shrey Patel <patelshrey77@gmail.com>` over SSH throughout.

## Tests

**56 tests, 56 passing.** `uv run pytest -v`:

- Against real local Postgres unconditionally (create/drop a scratch
  `pulse_serve_test` database per test, same convention as pulse-mbta's
  `tests/test_db.py`) -- always runs, never skipped.
- Against a real local redis when reachable; a `redis_client` fixture
  skips (not mocks, not fails) the redis-dependent tests when none is
  running. Verified both states locally: 56/56 passed with a transient
  local redis running, and 46 passed + 10 cleanly skipped (0 failed) with
  redis unreachable.
- **In CI, both services are real containers, so all 56 always run** --
  confirmed by pulling the actual GitHub Actions log:
  `======================== 56 passed, 1 warning in 3.60s ========================`

CI run (this repo, `main`, commit `aa4d8f0`): `ruff check .` clean, pytest
56/56, and a real `docker build -f docker/Dockerfile -t pulse-serve:ci .`
all green, ~52s total. `gh run list` shows two consecutive green runs (the
one before SHA-pinning the actions, and the one after -- both passed,
confirming the pin didn't change behavior, only supply-chain posture).

## Curl evidence (real local run: `uv run uvicorn`, real local Postgres,
a real transient local redis on port 6399)

```
$ curl -s http://127.0.0.1:8123/healthz
{"status":"ok","model_version":"baseline-fallback","is_baseline":true,
 "model_integrity_ok":true,"uptime_seconds":7.96,"queue_depth":0,
 "redis_connected":true,"db_connected":true}

$ curl -s -w "\nHTTP %{http_code}\n" http://127.0.0.1:8123/v1/predict -X POST \
  -H "Content-Type: application/json" \
  -d '{"route_id":"1","direction_id":0,"stop_id":"110","trip_id":"trip-1"}'
{"detail":"missing X-API-Key header"}
HTTP 401

$ curl -s http://127.0.0.1:8123/v1/predict -X POST \
  -H "Content-Type: application/json" -H "X-API-Key: local-dev-placeholder-key" \
  -d '{"route_id":"1","direction_id":0,"stop_id":"110","trip_id":"trip-1"}'
{"probability_delay_gt_180s":0.0,"predicted_label":false,
 "model_version":"baseline-fallback","is_baseline":true,
 "baseline_strategy":"always_on_time","horizon_in_trained_regime":true,
 "generated_at":"2026-08-13T04:57:23.073204Z","route_id":"1",
 "direction_id":0,"stop_id":"110","trip_id":"trip-1","horizon_min":10}

$ curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8123/docs
HTTP 200
```

BASELINE fallback confirmed exactly as specced: `model_version:
"baseline-fallback"` on every response, `always_on_time` strategy since
`models/` is genuinely empty (no `current.joblib` yet -- pulse-mbta hasn't
reached M3).

Full async round trip also verified live (enqueue -> pending -> real
`pulse_serve.worker` process consumes it -> `GET /v1/results/{id}` flips to
`"done"`), and a real model-integrity failure was verified to return HTTP
503 from `/healthz` with `model_integrity_ok: false` (a deliberately
corrupted sha256 in a test manifest). Both are exercised by
`tests/test_app.py` and `tests/test_worker.py`, not just by hand.

## Bugs found and fixed by actually running things (not just unit tests)

1. **`GET /v1/results/{malformed-id}` 500'd instead of returning
   `"pending"`.** `request_id` is a Postgres `uuid` column; a non-UUID path
   param raised `psycopg.errors.InvalidTextRepresentation`. Found by the
   full pytest run surfacing a real stack trace. Fixed in
   `pulse_serve/store.py`'s `get_prediction` (catch, return `None`, same as
   "not found") + regression test in `tests/test_store.py`.
2. **The worker intermittently errored on every idle poll cycle.** Found by
   actually running `pulse_serve.worker` as a live process (not just its
   unit-tested `_process_one`): redis-py 8.x defaults `socket_timeout=5`,
   which races `BRPOPLPUSH`'s own 5s server-side blocking wait and
   raises `Timeout reading from socket` client-side. The main loop already
   tolerated this (logs, sleeps, retries -- never crashed), but it was log
   noise on every idle cycle, not a real fault. Fixed in
   `pulse_serve/queue.py`'s `connect()`: `socket_timeout=None` so the
   client waits for whatever `BRPOPLPUSH`'s own timeout produces instead of
   racing it. Verified fixed with a 12-second idle run producing zero
   errors.
3. **CI referenced stale action versions.** Wrote `astral-sh/setup-uv@v3`
   and `actions/checkout@v4` from memory; checked against
   `gh api repos/<owner>/<repo>/releases` before trusting it and found the
   real state: `setup-uv` was at v10.0.0 (and, as of v8, moving major-tag
   aliases like `@v8` don't exist anymore -- only full/SHA pins work),
   `checkout` at v7.0.1. Re-pinned both to verified commit SHAs. Notably,
   the *first* CI run (still on the stale `@v3`/`@v4` tags) also passed --
   both tags evidently still resolved -- but the pin is the correct
   long-term posture regardless, and is what's on `main` now.

## Measured, not assumed

- Sync `/v1/predict` latency (500 requests, baseline fallback): p50 0.53ms,
  p95 0.78ms, p99 0.97ms.
- Async enqueue-to-done (steady-state worker, 15 samples): median 27.6ms,
  observed max 147.6ms.
- Power draw via `ioreg -rn AppleSmartBattery` (two's-complement
  `InstantAmperage` decoded): ~19.2W whole-system, no measurable delta
  under load.
- Electricity rate: $0.31/kWh, sourced (Massachusetts residential, August
  2026, WebSearch-verified range 28.82-33 cents/kWh).
- Cost-per-1k-predictions computed both ways (availability-basis:
  $0.0143-$0.143 depending on assumed daily volume; marginal-compute-basis:
  ~$0.00000087) -- full derivation in `docs/design.md`.

All of this lives in `docs/design.md`'s SLO and cost sections, with the
assumption chain labeled at each step per the task's "stated as estimate"
instruction.

## What remains key-gated (deliberately not built)

Per `docs/design.md`'s "Deployment placeholders" section:

- **cloudflared tunnel** for `pulse-api.coconutlabs.org` -- needs a
  `CLOUDFLARE_TUNNEL_TOKEN` this session doesn't have.
- **`MBTA_API_KEY` passthrough** -- wiring point noted for a hypothetical
  future serving-time feature; pulse-serve itself never calls the MBTA API.
- **Resend alerting delivery** -- `ops/prometheus/alerts.yml`'s rules are
  real and would fire against a running Alertmanager today; Alertmanager
  itself and a `RESEND_API_KEY`-keyed receiver are not wired up.
- **Real `PULSE_API_KEYS`** -- the whole service runs today on the
  well-known placeholder key by design ("placeholder-friendly"); a startup
  warning fires whenever that's still the active key set.
- **A real trained model** -- `models/` is genuinely empty; pulse-mbta is
  at M1 (ingestion only). BASELINE fallback is not a stand-in bug, it's the
  honest current state, and the service says so on every single response.

## Key files

- `pulse_serve/app.py`, `pulse_serve/model.py`, `pulse_serve/queue.py`,
  `pulse_serve/store.py`, `pulse_serve/worker.py`, `pulse_serve/security.py`
  -- the service itself.
- `docs/design.md` -- architecture diagram, SLOs, cost, AWS re-target
  table, deployment placeholders.
- `README.md` -- quickstart, honest state, delivery semantics, ops.
- `models/README.md` -- the exact contract pulse-mbta's M3 needs to satisfy
  to hand off a real model.
- `.github/workflows/ci.yml` -- the blocking gate, SHA-pinned, verified
  green twice.
