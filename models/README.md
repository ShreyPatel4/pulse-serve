# models/

Empty by default. This directory is where pulse-mbta's M3 (see
`../../pulse-mbta/docs/2026-08-13-pulse-design.md`) drops a trained model.
Until that happens, `pulse_serve` serves a clearly-labeled BASELINE
fallback -- it never fakes a trained model. See `pulse_serve/model.py`'s
module docstring for the full contract and the "joblib trap" writeup.

## What goes here

| file                        | required? | what it is |
|------------------------------|-----------|------------|
| `current.joblib`              | no (until M3) | the trained model, pickled via `joblib.dump` |
| `manifest.json`                | required alongside `current.joblib` | semver + sha256 + provenance, see below |
| `baseline_route_hour.json`     | optional | route-hour late-rate table used by the baseline fallback instead of always-on-time, when no trained model is present |

`current.joblib` and `manifest.json` are gitignored (never commit an actual
model binary or its real checksum into this repo) -- only the two
`*.example.*` files below are tracked, to document the shape without
shipping fake weights.

## `manifest.json` shape

```json
{
  "semver": "0.1.0",
  "sha256": "<sha256 of current.joblib, lowercase hex>",
  "trained_at": "2026-08-20T00:00:00Z",
  "data_window": "2026-08-13..2026-08-20"
}
```

All four keys are required. `pulse_serve.model.load_model` refuses to load
(and serves BASELINE instead) if any is missing, if `sha256` doesn't match
the actual file on disk, or if either file is present without its
counterpart. See `manifest.example.json` in this directory for a filled-in
example.

## The serving contract `current.joblib` must satisfy

The unpickled object must expose:

```python
def predict_proba(self, features: dict) -> float:
    ...
```

where `features` is exactly `{route_id, direction_id, stop_id, trip_id,
horizon_min}` -- the raw request fields, no feature engineering applied.
The trained model owns turning that into its real feature vector internally
(route-hour lookups, encoders, whatever pulse-mbta's M2/M3 pipeline
produces). This keeps `pulse_serve` free of a scikit-learn/pandas
dependency and lets the training side change freely without ever touching
this package. A thin wrapper class around a real sklearn/lightgbm/pytorch
model is expected to be the thing that actually gets `joblib.dump`ed.

## `baseline_route_hour.json` shape (optional)

```json
{
  "1": {"7": 0.18, "8": 0.31, "9": 0.22},
  "_all": 0.15
}
```

Top-level keys are route_ids; inner keys are the hour-of-day (0-23, Eastern,
string) at prediction time; values are the historical late rate for
`(route, hour)`. `_all` is the fallback for a route/hour combination not in
the table. See `baseline_route_hour.example.json`. If this file isn't
present, the baseline fallback is simply always-on-time (probability 0.0)
-- pulse-mbta's ML spec baseline #1.
