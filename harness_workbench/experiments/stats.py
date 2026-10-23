"""Independent-unit statistics for confirmatory decisions (stdlib only).

The independent unit is the task family: repetitions of a task and the checks inside a task are not
independent samples, so they are averaged out before any bound is computed. ``paired_task_deltas`` pairs
candidate and baseline per ``(task, repetition)`` using the last attempt, ``family_units`` averages the
task deltas of each family, and the bounds below (a seeded cluster percentile bootstrap and a Student t
bound) are computed over those family units only.

``decide`` turns the records of one experiment into the promotion decision. It fails closed: anything that
cannot be shown (usage that was never measured, a missing pair, a degenerate interval, a regression group
the data cannot clear) is never ``ELIGIBLE``.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from .contracts import ExperimentDecision, PrivateExperimentPlan, TrialRecord
from .status import DecisionStatus, Provenance

if TYPE_CHECKING:  # manifest and protocol import contracts only; nothing here needs them at run time
    from .manifest import ExperimentSpec
    from .protocol import FrozenSelection

__all__ = [
    "METHODS", "ConflictingEvidence", "IncompleteEvidence", "Unit", "cluster_percentile_ci_v1", "cluster_t_v1",
    "decide", "family_units", "latest_attempts", "lower_bound", "noninferiority", "paired_task_deltas",
    "reference_lower_bound", "t_quantile", "upper_bound", "zero_variance",
]

METHODS = ("cluster_percentile_ci_v1", "cluster_t_v1")


class IncompleteEvidence(ValueError):
    """The evidence cannot support a statistic: a pair is missing or unusable, or too few units exist."""


class ConflictingEvidence(IncompleteEvidence):
    """Two different records claim the same attempt of one trial: the evidence is ambiguous."""


@dataclass(frozen=True)
class Unit:
    """One independent unit (a task family): the mean of its task deltas and how many tasks it holds."""

    id: str
    value: float
    tasks: int


# --- paired deltas and units ---------------------------------------------------------------------------


def latest_attempts(records: Iterable[TrialRecord], variants: Iterable[str] | None = None,
                    ) -> dict[tuple[str, str, int], TrialRecord]:
    """``(variant, task, repetition)`` -> the record of the highest attempt, for the named variants.

    Two different trials claiming the same attempt of one key cannot be ordered, so that raises
    ``ConflictingEvidence`` instead of letting list order pick a winner; the same trial listed twice is fine.
    """
    wanted = None if variants is None else set(variants)
    latest: dict[tuple[str, str, int], TrialRecord] = {}
    for record in records:
        if wanted is not None and record.variant_id not in wanted:
            continue
        key = (record.variant_id, record.task_id, record.repetition)
        current = latest.get(key)
        if current is not None and record.attempt == current.attempt and record.trial_id != current.trial_id:
            raise ConflictingEvidence(
                f"conflicting records for variant {key[0]!r} task {key[1]!r} repetition {key[2]} "
                f"attempt {record.attempt}: trials {current.trial_id!r} and {record.trial_id!r}")
        if current is None or record.attempt > current.attempt:
            latest[key] = record
    return latest


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
    latest = latest_attempts(records, {variant_id, baseline_id})

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
    bad = [value for value in values if not math.isfinite(value)]
    if bad:
        raise IncompleteEvidence(f"a bound needs finite unit values, got {len(bad)} non-finite of {len(values)}")
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


# --- the decision --------------------------------------------------------------------------------------

_HOLDOUT_SPLITS = ("validation", "regression")
_MAX_LISTED = 10  # trials named one by one in a reason; the rest are counted

_INSUFFICIENT = DecisionStatus.INSUFFICIENT_EVIDENCE
_REJECTED = DecisionStatus.REJECTED
Finding = tuple[DecisionStatus, str]


def _candidate(spec: "ExperimentSpec", selection: "FrozenSelection | None") -> tuple[str | None, str | None]:
    """The variant id and execution hash a decision is about: the frozen finalist, else the only candidate."""
    if selection is not None:
        return selection.finalist_id, selection.finalist_hash
    if len(spec.variants) == 2:
        only = spec.variants[1]
        return only.id, only.harness.execution_config_hash()
    return None, None


@dataclass
class _Primary:
    """The paired comparison of one candidate against the baseline over a set of tasks."""

    task_deltas: dict[str, float] = field(default_factory=dict)
    units: list[Unit] = field(default_factory=list)
    error: str | None = None  # why the pairs or the bound could not be computed
    estimate: float | None = None
    lower: float | None = None


def _compare(spec: "ExperimentSpec", plan: PrivateExperimentPlan, records: list[TrialRecord], candidate_id: str,
             task_ids: Sequence[str], *, bound: bool = True) -> _Primary:
    result = _Primary()
    try:
        result.task_deltas = paired_task_deltas(
            records, variant_id=candidate_id, baseline_id=spec.variants[0].id, task_ids=task_ids,
            repetitions=spec.repetitions)
        result.units = family_units(result.task_deltas, plan.families)
    except ConflictingEvidence:
        raise
    except IncompleteEvidence as exc:
        result.error = str(exc)
        return result
    if not bound:
        return result
    policy = spec.statistics
    try:
        result.estimate, result.lower = lower_bound(
            [unit.value for unit in result.units], method=policy.method, alpha=policy.alpha,
            iterations=policy.bootstrap_iterations, seed=spec.seed)
    except IncompleteEvidence as exc:
        result.error = str(exc)
    return result


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def _usage_findings(spec: "ExperimentSpec", scoped: list[TrialRecord], candidate_id: str,
                    diagnostics: dict[str, Any]) -> list[Finding]:
    """Token and latency budgets. Usage nobody measured blocks; it is never read as zero or as within budget."""
    budget = spec.budget
    baseline_id = spec.variants[0].id
    usable = [record for record in scoped if record.usable]
    findings: list[Finding] = []

    accepted = set(budget.accepted_provenance)
    unknown = sum(1 for r in usable
                  if r.execution.tokens.provenance.value not in accepted or r.execution.tokens.value is None)
    diagnostics["unknown_usage_trials"] = unknown
    if budget.max_token_ratio is not None or budget.absolute_token_limit is not None:
        base = [r.execution.tokens.value for r in usable if r.variant_id == baseline_id]
        cand = [r.execution.tokens.value for r in usable if r.variant_id == candidate_id]
        if unknown:
            findings.append((_INSUFFICIENT, f"token usage unknown for {unknown} trials"))
        elif not base or not cand:
            findings.append((_INSUFFICIENT, "token usage unavailable: no usable trial for the baseline or the candidate"))
        else:
            base_mean, cand_mean = _mean(base), _mean(cand)
            if base_mean > 0:
                diagnostics["token_ratio"] = cand_mean / base_mean
            elif budget.absolute_token_limit is None:
                findings.append((_INSUFFICIENT, "token ratio undefined: baseline usage is zero"))
            ratio = diagnostics["token_ratio"]
            if budget.max_token_ratio is not None and ratio is not None and ratio > budget.max_token_ratio:
                findings.append((_REJECTED, f"token ratio {ratio:.3f} exceeds max_token_ratio {budget.max_token_ratio}"))
            if budget.absolute_token_limit is not None and cand_mean > budget.absolute_token_limit:
                findings.append((_REJECTED, f"candidate mean tokens {cand_mean:.1f} exceed absolute_token_limit "
                                            f"{budget.absolute_token_limit}"))
    elif not unknown:
        base = [r.execution.tokens.value for r in usable if r.variant_id == baseline_id]
        cand = [r.execution.tokens.value for r in usable if r.variant_id == candidate_id]
        if base and cand and _mean(base) > 0:
            diagnostics["token_ratio"] = _mean(cand) / _mean(base)

    if budget.max_latency_ratio is None:
        diagnostics["latency_gate"] = "disabled: no max_latency_ratio"
        return findings
    provenance = [r.execution.latency_ms.provenance for r in usable]
    unknown_latency = sum(1 for item in provenance if item is Provenance.UNKNOWN)
    not_applicable = sum(1 for item in provenance if item is Provenance.NOT_APPLICABLE)
    if unknown_latency:
        diagnostics["latency_gate"] = "blocked: latency unknown"
        findings.append((_INSUFFICIENT, f"latency unknown for {unknown_latency} trials"))
    elif provenance and not_applicable == len(provenance):
        diagnostics["latency_gate"] = "not_applicable: in-process executor"
    elif not_applicable:
        diagnostics["latency_gate"] = "blocked: latency not applicable on some trials"
        findings.append((_INSUFFICIENT, f"latency not applicable for {not_applicable} of {len(provenance)} trials"))
    else:
        base = [r.execution.latency_ms.value for r in usable if r.variant_id == baseline_id]
        cand = [r.execution.latency_ms.value for r in usable if r.variant_id == candidate_id]
        if not base or not cand:
            diagnostics["latency_gate"] = "blocked: no usable trial"
            findings.append((_INSUFFICIENT, "latency unavailable: no usable trial for the baseline or the candidate"))
        elif _mean(base) <= 0:
            diagnostics["latency_gate"] = "blocked: baseline latency is zero"
            findings.append((_INSUFFICIENT, "latency ratio undefined: baseline latency is zero"))
        else:
            diagnostics["latency_gate"] = "applied"
            ratio = diagnostics["latency_ratio"] = _mean(cand) / _mean(base)
            if ratio > budget.max_latency_ratio:
                findings.append((_REJECTED, f"latency ratio {ratio:.3f} exceeds max_latency_ratio "
                                            f"{budget.max_latency_ratio}"))
    return findings


def _regression_findings(spec: "ExperimentSpec", plan: PrivateExperimentPlan, regression: _Primary,
                         regression_ids: Sequence[str], diagnostics: dict[str, Any]) -> list[Finding]:
    """Non-inferiority per regression group, with alpha split across the groups (Bonferroni)."""
    policy = spec.statistics
    by_family = {unit.id: unit.value for unit in regression.units}
    groups = {name: tuple(families) for name, families in plan.regression_groups.items()}
    if not groups and regression_ids:
        groups = {"regression": tuple(sorted({plan.families[task] for task in regression_ids if task in plan.families}))}
    group_alpha = policy.alpha / len(groups) if groups else policy.alpha
    diagnostics["group_alpha"] = group_alpha
    if regression.error is not None and not regression.units:
        return []  # the pairs are incomplete: that is already a finding, and there is nothing to bound
    findings: list[Finding] = []
    covered = {family for families in groups.values() for family in families}
    uncovered = sorted(set(by_family) - covered)
    if uncovered:
        findings.append((_INSUFFICIENT, f"regression families not covered by any regression group: {', '.join(uncovered)}"))
    rejected: list[Finding] = []
    undecided: list[Finding] = []
    for name, families in groups.items():
        values = [by_family[family] for family in families if family in by_family]
        entry: dict[str, Any] = {"units": len(values), "families": list(families), "outcome": "undecided"}
        diagnostics["noninferiority"][name] = entry
        kwargs = dict(alpha=group_alpha, iterations=policy.bootstrap_iterations, seed=spec.seed, method=policy.method)
        try:
            estimate, outcome = noninferiority(values, margin=policy.regression_margin, **kwargs)
            entry["estimate"] = estimate
            entry["lower_bound"] = lower_bound(values, **kwargs)[1]
            entry["upper_bound"] = upper_bound(values, **kwargs)
        except IncompleteEvidence as exc:
            entry["error"] = str(exc)
            undecided.append((_INSUFFICIENT, f"regression group {name}: {exc}"))
            continue
        margin = policy.regression_margin
        if outcome is True:
            entry["outcome"] = "non_inferior"
        elif outcome is False:
            entry["outcome"] = "regressed"
            rejected.append((_REJECTED, f"regression group {name}: demonstrated regression (estimate {estimate:.4f}, "
                                        f"upper bound {entry['upper_bound']:.4f} < -margin {-margin})"))
        else:
            undecided.append((_INSUFFICIENT, f"regression group {name}: non-inferiority undecided (estimate "
                                             f"{estimate:.4f}, lower bound {entry['lower_bound']:.4f} <= -margin {-margin})"))
    # A demonstrated regression is definitive, so it outranks a group the data cannot decide.
    return [*rejected, *findings, *undecided]


def decide(plan: PrivateExperimentPlan, records: list[TrialRecord], *, spec: "ExperimentSpec",
           selection: "FrozenSelection | None", integrity_problems: list[str],
           exploratory_reason: str | None) -> ExperimentDecision:
    """The promotion decision of one experiment; the first matching rule wins and its reasons accumulate.

    1. ``integrity_problems`` -> ``INVALID_EXPERIMENT``.
    2. A non-confirmatory or legacy spec -> ``EXPLORATORY_ONLY``. 3. ``exploratory_reason`` -> same.
    4. A baseline or finalist holdout trial that is not usable -> ``INVALID_EXPERIMENT``.
    5. A finalist trial failing a critical check -> ``REJECTED``.
    6. Missing pairs, too few independent units or regression units -> ``INSUFFICIENT_EVIDENCE``.
    7. Token and latency budgets. 8. The primary bound against the practical margin.
    9. Non-inferiority per regression group. 10. Otherwise ``ELIGIBLE``.

    Rules 6 to 9 are all evaluated; the status is the first finding's and the reasons are the findings of
    that status. ``diagnostics`` is filled whenever the data allows, whatever the status.
    """
    baseline = spec.variants[0]
    baseline_id = baseline.id
    candidate_id, candidate_hash = _candidate(spec, selection)
    policy = spec.statistics
    confirmatory = spec.mode == "confirmatory" and not spec.legacy
    if confirmatory:
        validation_ids = tuple(plan.splits.get("validation", ()))
        regression_ids = tuple(plan.splits.get("regression", ()))
    else:
        validation_ids, regression_ids = tuple(task.id for task in spec.tasks), ()
    scope = set(validation_ids) | set(regression_ids)

    diagnostics: dict[str, Any] = {
        "n_units": 0, "n_regression_units": 0, "repetitions": spec.repetitions, "method": policy.method,
        "estimate": None, "lower_bound": None, "zero_variance": None, "token_ratio": None, "latency_ratio": None,
        "latency_gate": None, "task_deltas": {"primary": {}, "regression": {}}, "noninferiority": {},
        "group_alpha": policy.alpha, "unknown_usage_trials": 0,
    }
    findings: list[Finding] = []
    unusable: list[str] = []
    critical: list[str] = []
    conflict: str | None = None
    primary = _Primary()
    scoped: list[TrialRecord] = []

    try:
        latest = latest_attempts(records, {baseline_id, candidate_id} if candidate_id else {baseline_id})
        scoped = [record for record in latest.values() if record.task_id in scope]
    except ConflictingEvidence as exc:
        conflict = str(exc)

    compared: dict[str, _Primary] = {}
    if conflict is None:
        # An exploratory comparison shows its numbers for every candidate; a confirmatory one for its own.
        for variant in (spec.variants[1:] if not confirmatory else ()):
            try:
                compared[variant.id] = _compare(spec, plan, records, variant.id, validation_ids)
            except ConflictingEvidence as exc:
                conflict = str(exc)
                break
        if not confirmatory and conflict is None:
            diagnostics["per_variant"] = {
                variant_id: {"n_units": len(other.units), "estimate": other.estimate, "lower_bound": other.lower,
                             "task_deltas": other.task_deltas, "error": other.error}
                for variant_id, other in compared.items()}
    if conflict is None and candidate_id is not None:
        try:
            primary = compared.get(candidate_id) or _compare(spec, plan, records, candidate_id, validation_ids)
        except ConflictingEvidence as exc:
            conflict = str(exc)
    if conflict is None and candidate_id is not None:
        unusable = [record.trial_id for record in scoped if not record.usable]
        tasks = {task.id: task for task in spec.tasks}
        for record in scoped:
            if record.variant_id != candidate_id or not record.usable:
                continue
            for check_id in sorted(tasks[record.task_id].critical_check_ids):
                check = next((c for c in record.evaluation.checks if isinstance(c, dict) and c.get("id") == check_id), None)
                reason = f"critical invariant {check_id} failed in task {record.task_id}"
                if (check is None or check.get("passed") is not True) and reason not in critical:
                    critical.append(reason)

        # rule 6
        if primary.error is not None and not primary.units:
            findings.append((_INSUFFICIENT, f"incomplete validation evidence: {primary.error}"))
        diagnostics["n_units"] = len(primary.units)
        diagnostics["task_deltas"]["primary"] = dict(primary.task_deltas)
        if len(primary.units) < policy.min_independent_units:
            findings.append((_INSUFFICIENT, f"independent units {len(primary.units)} is below "
                                            f"min_independent_units {policy.min_independent_units}"))
        regression = _Primary()
        if confirmatory:
            regression = _compare(spec, plan, records, candidate_id, regression_ids, bound=False)
            if regression.error is not None and not regression.units:
                findings.append((_INSUFFICIENT, f"incomplete regression evidence: {regression.error}"))
            diagnostics["n_regression_units"] = len(regression.units)
            diagnostics["task_deltas"]["regression"] = dict(regression.task_deltas)
            if len(regression.units) < policy.min_regression_units:
                findings.append((_INSUFFICIENT, f"regression units {len(regression.units)} is below "
                                                f"min_regression_units {policy.min_regression_units}"))
        # rule 7
        findings.extend(_usage_findings(spec, scoped, candidate_id, diagnostics))
        # rule 8
        if primary.units:
            values = [unit.value for unit in primary.units]
            diagnostics["zero_variance"] = zero_variance(values)
            if primary.error is not None:
                findings.append((_INSUFFICIENT, f"primary bound unavailable: {primary.error}"))
            else:
                diagnostics["estimate"], diagnostics["lower_bound"] = primary.estimate, primary.lower
                if diagnostics["zero_variance"] and not policy.allow_zero_variance:
                    findings.append((_INSUFFICIENT, "degenerate interval: zero variance across units"))
                elif diagnostics["zero_variance"]:
                    diagnostics["zero_variance_justification"] = policy.zero_variance_justification
                if primary.lower <= policy.practical_margin:
                    findings.append((_REJECTED, f"improvement not supported: lower bound {primary.lower:.4f} <= "
                                                f"margin {policy.practical_margin}"))
        # rule 9
        if confirmatory:
            findings.extend(_regression_findings(spec, plan, regression, regression_ids, diagnostics))

    if confirmatory:
        evidence = [record.trial_id for record in scoped]
    else:
        evidence = [record.trial_id for record in records]
    baseline_hash = selection.baseline_hash if selection is not None else baseline.harness.execution_config_hash()

    def decision(status: DecisionStatus, reasons: list[str]) -> ExperimentDecision:
        pending = list(reasons) if status is DecisionStatus.INSUFFICIENT_EVIDENCE else []
        return ExperimentDecision(
            status=status, reasons=reasons, estimate=diagnostics["estimate"], lower_bound=diagnostics["lower_bound"],
            n_units=diagnostics["n_units"], plan_hash=plan.plan_hash, candidate_id=candidate_id,
            candidate_hash=candidate_hash, baseline_hash=baseline_hash, baseline_id=baseline.harness.id,
            baseline_version=baseline.harness.version, evidence_ids=evidence, pending_requirements=pending,
            diagnostics=diagnostics)

    # rule 1
    if integrity_problems:
        return decision(DecisionStatus.INVALID_EXPERIMENT, list(integrity_problems))
    # rules 2 and 3
    if not confirmatory or exploratory_reason:
        reasons = [f"mode is {spec.mode}"] if spec.mode != "confirmatory" else []
        if spec.legacy:
            reasons.append("schema v1 manifest")
        if exploratory_reason:
            reasons.append(exploratory_reason)
        return decision(DecisionStatus.EXPLORATORY_ONLY, reasons)
    if candidate_id is None:
        return decision(DecisionStatus.INVALID_EXPERIMENT, [
            "no frozen selection: the experiment has several candidates and none was frozen"])
    if conflict is not None:
        return decision(DecisionStatus.INVALID_EXPERIMENT, [conflict])
    # rule 4
    if unusable:
        listed = [f"unresolved execution/evaluation error in trial {trial_id}" for trial_id in unusable[:_MAX_LISTED]]
        if len(unusable) > _MAX_LISTED:
            listed.append(f"and {len(unusable) - _MAX_LISTED} more unresolved trials")
        return decision(DecisionStatus.INVALID_EXPERIMENT, listed)
    # rule 5
    if critical:
        return decision(_REJECTED, critical)
    # rules 6 to 9
    if findings:
        first = findings[0][0]
        reasons = list(dict.fromkeys(reason for status, reason in findings if status is first))
        return decision(first, reasons)
    # rule 10
    return decision(DecisionStatus.ELIGIBLE, [
        f"improvement supported: lower bound {primary.lower:.4f} > margin {policy.practical_margin}; "
        "no integrity, budget or regression block"])
