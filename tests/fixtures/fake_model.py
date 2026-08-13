"""Tiny joblib-picklable objects for pulse_serve.model tests. Must live in a
real, importable module -- not defined inline in a test function -- because
joblib's pickle needs to resolve the class by module path at load time."""

from __future__ import annotations


class ConstantModel:
    """Satisfies pulse_serve.model's predict_proba(features: dict) -> float
    contract, always returning the same constant."""

    def __init__(self, value: float = 0.75):
        self.value = value

    def predict_proba(self, features: dict) -> float:
        return self.value


class NoContractModel:
    """Deliberately does NOT implement predict_proba -- exercises model.py's
    contract check."""

    def __init__(self):
        self.marker = "no-contract"
