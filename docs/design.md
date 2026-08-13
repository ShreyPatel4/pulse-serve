# pulse-serve -- design

Project 2 of the five-project program: "Pulse in production, model as a
service." Self-hosted on the operator's M1 Pro Mac, $0 vendors, everything
in this doc runnable up to the key-gated deployment line (see "Deployment
placeholders" at the end). The DE star for this project, per
`../pulse-mbta/docs/program-flavor.md`: the data path *under* the service --
API -> queue -> worker -> results store, with delivery semantics stated and
a cost-per-1k-predictions number, not just an ML endpoint.

## Architecture

```
                          +-----------------------+
                          |  client (rider app)     |
                          +-----------+-------------+
                                      | X-API-Key + JSON
                                      v
              +---------------------------------------------------+
              |     FastAPI api  (pulse_serve.app)                  |
              |     require_api_key  ->  rate_limit_dependency        |
              +---------------------------------------------------+
                 |                   |                   |
      sync path  |        async path |          read path |
                 v                   v                   v
      POST /v1/predict    POST /v1/predict/async    GET /v1/results/{id}
                 |                   |                   ^
                 v                   v                   |
        +----------------+   +----------------+          |
        |  ModelBundle     |   |  Redis           |          |
        |  in-process,     |   |  PENDING list     |          |
        |  loaded once at  |   |  (LPUSH)          |          |
        |  startup         |   +--------+---------+          |
        +--------+---------+            | BRPOPLPUSH          |
                 ^                      v                    |
                 |             +----------------+             |
                 |             |  Redis           |             |
                 |             |  PROCESSING list  |             |
                 |             +--------+---------+             |
                 |                      | worker reserves        |
                 |                      v                        |
                 |             +----------------------+           |
                 |             |  worker                |           |
                 |             |  (pulse_serve.worker)   |           |
                 |             |  reclaim_stale() first,  |           |
                 |             |  then predict -> insert   |           |
                 |             |  -> ack                    |           |
                 |             +-----------+--------------+           |
                 |                         | INSERT ... ON CONFLICT   |
                 |                         | (request_id) DO NOTHING   |
                 |                         v                          |
                 |               +----------------------+             |
                 |               |  Postgres               |          |
                 |               |  predictions (JSONB)     |----------+
                 |               |  dedup on request_id      |
                 |               +----------------------+
                 |
                 | models/current.joblib + manifest.json
                 | (sha256-verified) or BASELINE fallback
                 v
        +----------------------+
        |  models/ (host dir,    |
        |  read-only bind mount) |
        +----------------------+

   +----------------+   scrape   +--------------+   query   +-----------+
   |  api /metrics    |---------->|  Prometheus    |---------->|  Grafana   |
   |  worker /metrics  |---------->|  + alerts.yml  |           |  (provi-   |
   +----------------+           +--------------+           |  sioned    |
                                                             |  dashboard)|
                                                             +-----------+
```

One process (`api`, `uvicorn pulse_serve.app:app`) serves both the sync and
async HTTP surface; a second, separate process (`worker`,
`python -m pulse_serve.worker`) is the only thing that ever reads from the
PROCESSING side of the queue or writes to `predictions`. They share nothing
but Redis and Postgres -- no direct process-to-process call.

## Delivery semantics (summary -- full writeup in README.md)

At-least-once delivery (BRPOPLPUSH + startup `reclaim_stale()`, see
`pulse_serve/queue.py`) + an idempotent Postgres write (`ON CONFLICT
(request_id) DO NOTHING`, see `pulse_serve/store.py`) = **effectively-once**
from the caller's point of view: a redelivered job can run `predict()`
twice, but only the first write ever lands, and `GET /v1/results/{id}`
always returns that first result. The one honest gap: `reclaim_stale()`
runs at worker *startup*, not on a timer, so it only covers "worker
restarted" crash recovery, not "worker hung mid-job but is still running" --
and it assumes exactly one worker replica (a second replica's startup
reclaim could steal a job the first replica is still actively working).
docker-compose.yml runs one `worker` replica for exactly this reason.

## SLOs

All numbers below were measured on this machine during this build (2026-08-12,
M1 Pro, on battery, BASELINE-fallback model -- no trained model exists yet,
see README.md's honest-state section) -- not assumed. They're a floor, not a
production guarantee: a real trained model adds feature-computation +
inference cost the baseline doesn't have, and these were single-connection,
loopback, sequential-load measurements, not concurrent production traffic.

**Sync `POST /v1/predict`** -- 500 sequential requests, single httpx
connection, loopback:

| metric | measured |
|---|---|
| p50 | 0.53 ms |
| p95 | 0.78 ms |
| p99 | 0.97 ms |
| max | 5.81 ms |
| throughput (sequential, 1 connection) | ~1,740 req/s |

**Stated p95 SLO target: < 250 ms.** That's ~320x the measured baseline
floor -- deliberate headroom for a real model's inference cost, concurrent
load, and a non-loopback network hop once this sits behind a tunnel.
`ops/prometheus/alerts.yml`'s `PulseServeSyncLatencyP95HighAlert` fires on
this same threshold; keep them in sync if either changes.

**Async `POST /v1/predict/async` -> `GET /v1/results/{id}` done** -- 15
sequential enqueue-then-poll cycles against a worker already parked in its
blocking `BRPOPLPUSH` (steady state, no backlog), 1ms poll interval:

| metric | measured |
|---|---|
| median | 27.6 ms |
| p95 (n=15, small sample) | 147.6 ms |
| min | 11.4 ms |

The tail is dominated by `GET /v1/results` opening a fresh Postgres
connection per request (`store.connect()` is not pooled -- a stated MVP
simplification, see `pulse_serve/store.py`), not by queue or worker latency.
**Stated async p95 SLO target: < 1 s** with an empty backlog. Under an
actual backlog, completion time is `queue_depth / worker_drain_rate`; a
separate 100-job burst-drain measurement (worker already running, jobs
queued ahead of it) processed the batch in ~180 ms once the worker started
pulling, i.e. roughly 550 jobs/s drain throughput for this baseline-fallback
workload -- again, a floor, not a real-model number.

**Availability, stated honestly**: this runs on one MacBook Pro with no
redundancy. `docker-compose.yml` sets `restart: unless-stopped` on every
long-running service, so a crashed container (worker included -- its first
act on restart is `reclaim_stale()`, which is the actual crash-recovery
mechanism in practice) comes back on its own. That does **not** cover: the
laptop sleeping (pulse-mbta's ingestion poller solved this with a
`caffeinate` launchd agent -- no equivalent exists for pulse-serve yet, and
is a gap, not an oversight), the laptop losing power or rebooting (Docker
Desktop is not configured to launch at login here), or Docker Desktop simply
not running (it wasn't, for this entire build -- see the verification note
below). There is no SLA. Treat this as best-effort during an active
development/demo session, not continuous unattended service.

**Verification note**: the Docker daemon was not running on this machine at
build time (`docker info` failed to reach the socket). Per this task's
instructions, Docker Desktop was not started. `docker compose config`
validates the compose file's syntax and interpolation without the daemon and
passed; the actual image build and `docker compose up` were **not** run and
are unverified beyond that. CI (`.github/workflows/ci.yml`) runs a real
`docker build` on every push -- that is this project's actual build
verification, not this machine.

## Cost

**Power, measured**: `ioreg -rn AppleSmartBattery` on battery power, sampled
repeatedly during and around the sync-latency benchmark above (macOS reports
`InstantAmperage` as an unsigned 64-bit two's-complement wraparound for a
negative/discharging value -- converted below):

```
voltage:  11.3 V
current:  ~1.7-2.0 A (discharging)
power:    ~19.2 W, whole-system
```

This is whole-laptop power (display, OS, everything), not an isolated
Python-process measurement -- isolating that would need `powermetrics`,
which requires `sudo` and wasn't run. Sampling again *during* the 500-request
benchmark load showed **no measurable change** from the idle-ish reading
above; at this instrumentation's resolution, pulse-serve's marginal compute
draw doesn't move the needle against a MacBook's standing power draw. That's
itself the finding: at this scale, cost is dominated by keeping the machine
on, not by inference compute.

**Electricity rate**: $0.31/kWh -- Massachusetts residential, August 2026,
midpoint of a sourced 28.82-33 cents/kWh range (56% above the US national
average of 18.44 cents/kWh). This is a stated, sourced assumption, not a
measurement.

**Cost-per-1k-predictions, availability basis** (the economically meaningful
number at this scale: the laptop's standing power draw, amortized over
however many predictions actually get served):

```
19.2 W * 24 h = 0.4608 kWh/day
0.4608 kWh * $0.31/kWh = $0.143/day
```

```
cost_per_1k = cost_per_day / (daily_volume / 1000)
```

| assumed daily volume | units of 1,000 predictions/day | cost per 1,000 predictions |
|---|---|---|
| 1,000/day | 1 | $0.143 / 1 = $0.143 |
| 10,000/day | 10 | $0.143 / 10 = $0.0143 |
| 100,000/day | 100 | $0.143 / 100 = $0.00143 |

No real traffic exists yet (pre-launch), so the daily-volume column is a
labeled assumption, not a measurement -- pick the row that matches an
actual expected volume when one exists.

**Cost-per-1k-predictions, marginal-compute basis** (upper bound, for
comparison): attributing the *entire* measured 19.2W to just the p50
0.525ms request duration, even though load testing showed no measurable
power delta --

```
0.0192 kW x (0.000525 s / 3600 s/h) = 2.8e-9 kWh/prediction
2.8e-9 kWh x $0.31/kWh x 1,000 predictions = ~$0.00000087 per 1,000 predictions
```

Six orders of magnitude below the availability-basis number. The gap is the
whole point: at self-hosted single-laptop scale, "cost per prediction" is
not a compute question, it's a "how long do you keep the box on" question.

## AWS re-target table

The honest cloud mapping, in case this ever needs to leave the laptop.
Nothing here is built -- it's what each self-hosted piece stands in for.

| self-hosted (this repo) | AWS equivalent | notes |
|---|---|---|
| docker-compose (`api`, `worker`) | ECS Fargate services (or k3s on EC2) | one task def per service; `restart: unless-stopped` -> ECS's own restart policy |
| Redis reliable queue (`BRPOPLPUSH` + `reclaim_stale`) | SQS standard queue | SQS's visibility timeout replaces `reclaim_stale()`'s startup-only reclaim -- a real per-message lease, which this MVP doesn't have |
| Postgres `predictions` (JSONB, dedup on `request_id`) | DynamoDB (`request_id` as partition key, conditional `PutItem` for the same dedup) | JSONB's ad-hoc queryability trades for DynamoDB's operational simplicity + scale ceiling |
| Prometheus + Grafana + `alerts.yml` | CloudWatch Metrics + CloudWatch Alarms + Managed Grafana (or QuickSight) | `alerts.yml`'s rules translate near-1:1 to CloudWatch Alarm definitions |
| `X-API-Key` against `PULSE_API_KEYS` | API Gateway usage plans + IAM, or Cognito | this MVP's token-bucket rate limiter is in-process and per-replica; API Gateway's is a real shared limiter |
| `models/` read-only bind mount | S3 + an init container/sidecar that pulls `current.joblib` + `manifest.json` at task start | sha256 verification against the manifest is unchanged either way |
| local Postgres backups (none configured) | RDS automated snapshots | a real gap either way until backups are configured somewhere |
| cloudflared tunnel (placeholder, see below) | ALB + ACM (TLS) + Route53 | |
| laptop sleep / power loss | multi-AZ ECS + RDS Multi-AZ | the single-point-of-failure problem this MVP explicitly does not solve |

## Deployment placeholders

Everything below is wiring, not built -- this is the key-gated line the
task asked to stop at. Each needs a real secret this session doesn't have
and shouldn't fabricate.

- **cloudflared tunnel ingress for `pulse-api.coconutlabs.org`.** Same
  pattern as the LP's waterline Access stack (masterclass.coconutlabs.org
  precedent): a `cloudflared` sidecar/service routing
  `pulse-api.coconutlabs.org` -> `api:8000` inside the compose network,
  gated on a `CLOUDFLARE_TUNNEL_TOKEN` this repo does not have and does not
  generate. Per the no-per-project-domains policy, this is a *route* under
  `coconutlabs.org`, not a new domain -- consistent with how every other
  project in this program is meant to surface publicly.
- **`MBTA_API_KEY` passthrough.** pulse-serve itself never calls the MBTA
  API directly -- that's pulse-mbta's ingestion job, entirely upstream of
  this repo. This is a placeholder for a *future* serving-time feature (a
  live-schedule cross-check at prediction time, say) that would need the
  same key pulse-mbta's poller already uses. Not implemented; noted so it
  isn't rediscovered as a surprise later.
- **Resend alerting.** `ops/prometheus/alerts.yml`'s rules are real and
  would fire today against a running Alertmanager. What's missing is
  Alertmanager itself (not in `docker-compose.yml`) and a Resend receiver
  config keyed on a `RESEND_API_KEY` this repo does not have. The rules are
  the actual deliverable here; delivery is the placeholder.
- **`PULSE_API_KEYS` real values.** Everything in this repo runs today
  against the well-known placeholder key (`local-dev-placeholder-key`,
  `pulse_serve/config.py`). A real deployment must set `PULSE_API_KEYS` to
  generated secrets before it's reachable from anywhere but this laptop --
  `security.py` logs a loud startup warning whenever the placeholder is
  still active, precisely so this can't be missed silently.
- **Unauthenticated `/metrics`.** Fine on a private compose network; a
  public deployment needs this behind the tunnel's own access policy (or a
  separate internal-only ingress) rather than trusting network topology
  alone, since there's no API-key gate on this endpoint by design (see
  `pulse_serve/app.py`'s `/metrics` handler).
