"""Published statistical profiles for confirmatory experiments.

A profile fixes the decision method, the error rate, the margins and the sampling plan (repetitions, number
of independent units, bootstrap iterations) that a confirmatory manifest must meet. The manifest loader
compares a manifest against ``PROFILES[policy.profile]``; the Monte Carlo simulator (``simulate.py``) runs
the real selection and decision functions to show what a profile does to the error rates.

``binary_v1`` carries the sampling plan, regression tolerance and method selected by the pre-registered
calibration (``docs/statistical-validation.md``). ``alpha`` and ``practical_margin`` are never part of that
grid: they are chosen before looking at any simulation.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any

from .stats import METHODS

__all__ = ["CALIBRATION_GRID", "PROFILES", "StatisticalProfile", "grid_points"]


@dataclass(frozen=True)
class StatisticalProfile:
    id: str
    method: str
    alpha: float
    practical_margin: float
    regression_margin: float
    min_independent_units: int
    min_regression_units: int
    repetitions: int
    bootstrap_iterations: int
    outcome: str
    target_effect: tuple[float, float]  # (baseline success rate, success rate the profile must detect)
    calibration_doc: str

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError(f"unknown bound method {self.method!r}; expected one of {METHODS}")
        if not (math.isfinite(self.alpha) and 0.0 < self.alpha < 0.5):
            raise ValueError(f"alpha must be in (0, 0.5), got {self.alpha!r}")
        for name in ("practical_margin", "regression_margin"):
            value = getattr(self, name)
            if not (math.isfinite(value) and value >= 0.0):
                raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")
        for name in ("min_independent_units", "min_regression_units", "repetitions", "bootstrap_iterations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be an integer >= 1, got {value!r}")
        if self.outcome not in ("binary", "continuous"):
            raise ValueError(f"outcome must be 'binary' or 'continuous', got {self.outcome!r}")
        target = tuple(self.target_effect)
        if len(target) != 2 or not all(math.isfinite(item) and 0.0 <= item <= 1.0 for item in target):
            raise ValueError(f"target_effect must be two rates in [0, 1], got {self.target_effect!r}")
        object.__setattr__(self, "target_effect", target)


PROFILES: dict[str, StatisticalProfile] = {
    "binary_v1": StatisticalProfile(
        id="binary_v1", method="cluster_percentile_ci_v1", alpha=0.05, practical_margin=0.05,
        regression_margin=0.10, min_independent_units=50, min_regression_units=80, repetitions=5,
        bootstrap_iterations=10000, outcome="binary", target_effect=(0.5, 0.7),
        calibration_doc="docs/statistical-validation.md"),
}

# Pre-registered before any simulation (see T24): the only dimensions the calibration may vary.
CALIBRATION_GRID: dict[str, tuple[Any, ...]] = {
    "method": ("cluster_percentile_ci_v1", "cluster_t_v1"),
    "min_independent_units": (30, 40, 50, 60),
    "min_regression_units": (30, 40, 60, 80),
    "regression_margin": (0.05, 0.10),
    "repetitions": (5,),
}


def grid_points() -> list[dict[str, Any]]:
    """Every point of ``CALIBRATION_GRID`` as a dict, in a fixed order (64 for the published grid)."""
    names = list(CALIBRATION_GRID)
    return [dict(zip(names, values)) for values in itertools.product(*(CALIBRATION_GRID[name] for name in names))]
