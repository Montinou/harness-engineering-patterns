"""Independent-unit statistics for confirmatory decisions (stdlib only).

The independent unit is the task family: repetitions of a task and the checks inside a task are not
independent samples, so they are averaged out before any bound is computed. ``paired_task_deltas`` pairs
candidate and baseline per ``(task, repetition)`` using the last attempt, ``family_units`` averages the
task deltas of each family, and the bounds below (a seeded cluster percentile bootstrap and a Student t
bound) are computed over those family units only.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence

from .contracts import TrialRecord

__all__ = [
    "METHODS", "IncompleteEvidence", "Unit", "cluster_percentile_ci_v1", "cluster_t_v1", "family_units",
    "lower_bound", "noninferiority", "paired_task_deltas", "reference_lower_bound", "t_quantile",
    "upper_bound", "zero_variance",
]

METHODS = ("cluster_percentile_ci_v1", "cluster_t_v1")


class IncompleteEvidence(ValueError):
    """The evidence cannot support a statistic: a pair is missing or unusable, or too few units exist."""


@dataclass(frozen=True)
class Unit:
    """One independent unit (a task family): the mean of its task deltas and how many tasks it holds."""

    id: str
    value: float
    tasks: int


# --- paired deltas and units ---------------------------------------------------------------------------


def paired_task_deltas(
    records: list[TrialRecord],
    *,
    variant_id: str,
    baseline_id: str = "baseline",
    task_ids: Iterable[str],
    repetitions: int,
) -> dict[str, float]:
    """Task id -> mean over repetitions of ``candidate - baseline`` (the last attempt of each trial).

    A missing or unusable member of any expected pair raises ``IncompleteEvidence`` naming it.
    """
    wanted = {variant_id, baseline_id}
    latest: dict[tuple[str, str, int], TrialRecord] = {}
    for record in records:
        if record.variant_id not in wanted:
            continue
        key = (record.variant_id, record.task_id, record.repetition)
        current = latest.get(key)
        if current is None or record.attempt > current.attempt:
            latest[key] = record

    def score(variant: str, task_id: str, repetition: int) -> float:
        record = latest.get((variant, task_id, repetition))
        where = f"variant {variant!r} task {task_id!r} repetition {repetition}"
        if record is None:
            raise IncompleteEvidence(f"missing trial for {where}")
        value = record.objective_score
        if value is None or not math.isfinite(value):
            raise IncompleteEvidence(f"unusable trial for {where}")
        return float(value)

    deltas: dict[str, float] = {}
    for task_id in task_ids:
        diffs = [score(variant_id, task_id, rep) - score(baseline_id, task_id, rep)
                 for rep in range(repetitions)]
        if not diffs:
            raise IncompleteEvidence(f"no repetitions for task {task_id!r}")
        deltas[task_id] = math.fsum(diffs) / len(diffs)
    return deltas


def family_units(task_deltas: dict[str, float], families: dict[str, str]) -> list[Unit]:
    """One unit per family: the mean of that family's task deltas."""
    grouped: dict[str, list[float]] = {}
    for task_id, delta in task_deltas.items():
        if task_id not in families:
            raise IncompleteEvidence(f"task {task_id!r} has no family")
        grouped.setdefault(families[task_id], []).append(delta)
    return [Unit(id=family, value=math.fsum(values) / len(values), tasks=len(values))
            for family, values in sorted(grouped.items())]


# --- Student t quantile --------------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _regularized_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _t_cdf(t: float, df: float) -> float:
    tail = 0.5 * _regularized_beta(df / 2.0, 0.5, df / (df + t * t))  # P(T > |t|)
    return 1.0 - tail if t > 0 else tail


def t_quantile(p: float, df: int) -> float:
    """The ``p`` quantile of Student's t with ``df`` degrees of freedom, by bisection on the CDF."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p!r}")
    if df < 1:
        raise ValueError(f"df must be >= 1, got {df!r}")
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -t_quantile(1.0 - p, df)
    low, high = 0.0, 1.0
    while _t_cdf(high, df) < p and high < 1e15:
        low, high = high, high * 2.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if _t_cdf(mid, df) < p:
            low = mid
        else:
            high = mid
        if high - low < 1e-12 * max(1.0, high):
            break
    return (low + high) / 2.0


# --- bounds --------------------------------------------------------------------------------------------


def _check(values: Sequence[float], alpha: float) -> None:
    if len(values) < 2:
        raise IncompleteEvidence(f"a bound needs at least 2 independent units, got {len(values)}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")


def _bootstrap_means(values: Sequence[float], iterations: int, seed: int) -> list[float]:
    if iterations < 1:
        raise ValueError(f"iterations must be >= 1, got {iterations!r}")
    rng = random.Random(seed)
    n = len(values)
    return sorted(math.fsum(rng.choices(values, k=n)) / n for _ in range(iterations))


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    position = q * (len(sorted_values) - 1)
    low = math.floor(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def cluster_percentile_ci_v1(values: Sequence[float], *, alpha: float, iterations: int, seed: int,
                             ) -> tuple[float, float]:
    """``(mean, alpha-quantile of the bootstrap means)``, resampling whole units with a seeded generator."""
    _check(values, alpha)
    return statistics.fmean(values), _quantile(_bootstrap_means(values, iterations, seed), alpha)


def _cluster_percentile_upper(values: Sequence[float], *, alpha: float, iterations: int, seed: int) -> float:
    _check(values, alpha)
    return _quantile(_bootstrap_means(values, iterations, seed), 1.0 - alpha)


def _t_margin(values: Sequence[float], alpha: float) -> float:
    n = len(values)
    return t_quantile(1.0 - alpha, n - 1) * statistics.stdev(values) / math.sqrt(n)


def cluster_t_v1(values: Sequence[float], *, alpha: float) -> tuple[float, float]:
    """``(mean, mean - t_{1-alpha, n-1} * sd / sqrt(n))`` over the units."""
    _check(values, alpha)
    mean = statistics.fmean(values)
    return mean, mean - _t_margin(values, alpha)


def _check_method(method: str) -> None:
    if method not in METHODS:
        raise ValueError(f"unknown bound method {method!r}; expected one of {METHODS}")


def lower_bound(values: Sequence[float], *, method: str, alpha: float, iterations: int, seed: int,
                ) -> tuple[float, float]:
    """``(estimate, one-sided lower confidence bound)`` by the named method."""
    _check_method(method)
    if method == "cluster_percentile_ci_v1":
        return cluster_percentile_ci_v1(values, alpha=alpha, iterations=iterations, seed=seed)
    return cluster_t_v1(values, alpha=alpha)


def upper_bound(values: Sequence[float], *, method: str, alpha: float, iterations: int, seed: int) -> float:
    """The one-sided upper confidence bound by the named method (mirror of ``lower_bound``)."""
    _check_method(method)
    if method == "cluster_percentile_ci_v1":
        return _cluster_percentile_upper(values, alpha=alpha, iterations=iterations, seed=seed)
    _check(values, alpha)
    return statistics.fmean(values) + _t_margin(values, alpha)


def reference_lower_bound(values: Sequence[float], *, alpha: float, iterations: int, seed: int) -> float:
    """An independent implementation of the percentile lower bound, used to cross-check the main one."""
    _check(values, alpha)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iterations):
        indexes = [rng.randrange(n) for _ in range(n)]
        means.append(sum(values[i] for i in indexes) / n)
    cuts = statistics.quantiles(means, n=1000, method="inclusive")
    return cuts[min(max(round(alpha * 1000), 1), 999) - 1]


def noninferiority(values: Sequence[float], *, margin: float, alpha: float, iterations: int, seed: int,
                   method: str) -> tuple[float, bool | None]:
    """``(estimate, outcome)``: True when the lower bound is above ``-margin`` (non-inferior), False when
    the upper bound is below ``-margin`` (a demonstrated regression), None when the data cannot tell."""
    estimate, lower = lower_bound(values, method=method, alpha=alpha, iterations=iterations, seed=seed)
    if lower > -margin:
        return estimate, True
    if upper_bound(values, method=method, alpha=alpha, iterations=iterations, seed=seed) < -margin:
        return estimate, False
    return estimate, None


def zero_variance(values: Sequence[float]) -> bool:
    """True when every unit has the same value (or there is at most one): the spread is unobserved."""
    return len(set(values)) <= 1
