"""Model loading: the joblib trap, sha256 verification, semver, startup
caching, and the BASELINE fallback that never fakes a trained model.

**Serving contract** (M3's training step owns satisfying this -- pulse_serve
is deliberately decoupled from feature engineering): ``models/current.joblib``
must unpickle to an object exposing::

    predict_proba(features: dict) -> float

where ``features`` is exactly the raw request dict
``{route_id, direction_id, stop_id, trip_id, horizon_min}``. The trained
model owns turning that into its real feature vector (encoders, route-hour
lookups, whatever) internally. This keeps pulse_serve free of a
scikit-learn/pandas dependency and lets M3 swap sklearn/lightgbm/pytorch
freely without ever touching this package.

**The joblib trap, documented honestly**: ``joblib.load`` is ``pickle`` under
the hood, and unpickling executes arbitrary code chosen by whoever wrote the
bytes on disk -- it is not a data format, it's code. The mitigation here is
sha256 pinning: the sha256 of ``current.joblib`` must match ``manifest.json``
before ``joblib.load`` is ever called. **This is not a complete defense.** It
proves the artifact matches what the manifest *claims*, and catches
corruption, an accidental wrong-file swap, or a stale copy -- it does
**not** prove the artifact is safe if an attacker controls both the artifact
and the manifest that describes it (they'd just recompute a matching hash).
The real security boundary is provenance: only ever place a
``current.joblib`` here that was produced by pulse-mbta's own training
pipeline. sha256 pinning is a tripwire against a *mismatched* pair reaching
this service, not a sandbox around what runs after ``joblib.load`` succeeds.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import joblib

BASELINE_VERSION = "baseline-fallback"
_EASTERN = ZoneInfo("America/New_York")
_REQUIRED_MANIFEST_KEYS = ("semver", "sha256", "trained_at", "data_window")


class _PredictProtocol(Protocol):
    def predict_proba(self, features: dict) -> float: ...


@dataclasses.dataclass
class ModelBundle:
    """Everything app.py and worker.py need to serve a prediction, loaded
    once at startup and cached (never reloaded per-request)."""

    version: str
    is_baseline: bool
    baseline_strategy: str | None  # "route_hour_table" | "always_on_time" | None (real model)
    integrity_ok: bool
    predict_fn: Any  # Callable[[dict], float]
    manifest: dict | None = None

    def predict_proba(self, features: dict) -> float:
        value = float(self.predict_fn(features))
        return min(1.0, max(0.0, value))


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_baseline_route_hour(model_dir: Path) -> dict | None:
    path = model_dir / "baseline_route_hour.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"pulse_serve.model: failed to read baseline_route_hour.json, ignoring: {exc}", file=sys.stderr)
        return None


def _route_hour_predict_fn(table: dict):
    def _predict(features: dict) -> float:
        route_id = str(features.get("route_id"))
        hour = str(dt.datetime.now(_EASTERN).hour)
        by_hour = table.get(route_id)
        if by_hour is not None and hour in by_hour:
            return float(by_hour[hour])
        fallback = table.get("_all")
        if fallback is not None:
            return float(fallback if not isinstance(fallback, dict) else fallback.get(hour, 0.0))
        return 0.0

    return _predict


def _always_on_time_predict_fn(features: dict) -> float:
    return 0.0


def _baseline_bundle(model_dir: Path, *, integrity_ok: bool) -> ModelBundle:
    table = _load_baseline_route_hour(model_dir)
    if table is not None:
        return ModelBundle(
            version=BASELINE_VERSION,
            is_baseline=True,
            baseline_strategy="route_hour_table",
            integrity_ok=integrity_ok,
            predict_fn=_route_hour_predict_fn(table),
        )
    return ModelBundle(
        version=BASELINE_VERSION,
        is_baseline=True,
        baseline_strategy="always_on_time",
        integrity_ok=integrity_ok,
        predict_fn=_always_on_time_predict_fn,
    )


def load_model(model_dir: Path | str) -> ModelBundle:
    """Load the trained model if -- and only if -- current.joblib and
    manifest.json are both present and consistent. Every other state
    (neither present, only one present, hashes disagree, unpickle fails, the
    unpickled object doesn't satisfy the predict_proba contract) serves the
    BASELINE fallback instead of raising, because a bad model file must never
    take the whole service down -- but it is NOT treated as equivalent to the
    "no model file yet" state: integrity_ok is False in every case except a
    verified real model or a genuinely empty models/ dir, and callers
    (app.py's /healthz, metrics.py's gauge) surface that distinction rather
    than silently returning a normal-looking baseline response.
    """
    model_dir = Path(model_dir)
    current_path = model_dir / "current.joblib"
    manifest_path = model_dir / "manifest.json"

    has_current = current_path.exists()
    has_manifest = manifest_path.exists()

    if not has_current and not has_manifest:
        # The documented "no model file exists" state -- clean baseline,
        # nothing to be suspicious of.
        return _baseline_bundle(model_dir, integrity_ok=True)

    if has_current != has_manifest:
        missing = "manifest.json" if has_current else "current.joblib"
        print(
            f"pulse_serve.model: {missing} missing while its counterpart is present in {model_dir} -- "
            "refusing to load an unverifiable model, serving BASELINE fallback",
            file=sys.stderr,
        )
        return _baseline_bundle(model_dir, integrity_ok=False)

    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"pulse_serve.model: manifest.json unreadable ({exc}) -- serving BASELINE fallback",
            file=sys.stderr,
        )
        return _baseline_bundle(model_dir, integrity_ok=False)

    missing_keys = [key for key in _REQUIRED_MANIFEST_KEYS if key not in manifest]
    if missing_keys:
        print(
            f"pulse_serve.model: manifest.json missing required keys {missing_keys} -- "
            "serving BASELINE fallback",
            file=sys.stderr,
        )
        return _baseline_bundle(model_dir, integrity_ok=False)

    actual_sha256 = _sha256_of(current_path)
    if actual_sha256 != manifest["sha256"]:
        print(
            f"pulse_serve.model: sha256 MISMATCH for current.joblib "
            f"(manifest={manifest['sha256']!r} actual={actual_sha256!r}) -- refusing to unpickle, "
            "serving BASELINE fallback. This is the joblib trap's tripwire: see module docstring.",
            file=sys.stderr,
        )
        return _baseline_bundle(model_dir, integrity_ok=False)

    # sha256 verified against the manifest's claim -- see the module
    # docstring for exactly what this does and does not defend against.
    try:
        obj = joblib.load(current_path)
    except Exception as exc:  # noqa: BLE001 - a bad pickle must not crash the service
        print(f"pulse_serve.model: joblib.load failed ({exc}) -- serving BASELINE fallback", file=sys.stderr)
        return _baseline_bundle(model_dir, integrity_ok=False)

    if not hasattr(obj, "predict_proba") or not callable(obj.predict_proba):
        print(
            "pulse_serve.model: current.joblib does not implement predict_proba(features: dict) -> float "
            "-- serving BASELINE fallback",
            file=sys.stderr,
        )
        return _baseline_bundle(model_dir, integrity_ok=False)

    return ModelBundle(
        version=str(manifest["semver"]),
        is_baseline=False,
        baseline_strategy=None,
        integrity_ok=True,
        predict_fn=obj.predict_proba,
        manifest=manifest,
    )
