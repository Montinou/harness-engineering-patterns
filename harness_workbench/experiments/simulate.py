"""Monte Carlo simulator for the confirmatory protocol (synthetic outcomes, the real decision functions).

Every simulation draws synthetic trial records through the real contracts (``TrialRecord``,
``ExecutionResult``, ``EvaluationResult``), then runs the real ``select_finalist`` on the development
records, freezes the choice with the real ``freeze_selection`` and hands the holdout records to the real
``decide``. Nothing runs a model, a subprocess or the network, and every result is labeled ``synthetic``:
Bernoulli and Beta outcomes are not the distribution of any language model.

Assumptions (all printed in the results through the scenario they belong to):

* The independent unit is the task family (``family_size`` tasks, one unit when it is 1).
* Binary outcome: each task has a success probability ``p`` drawn uniform in
  ``[p_base - difficulty_spread, p_base + difficulty_spread]``, shifted by a per-family latent offset
  ``N(0, 0.5 * sqrt(family_correlation))`` (the intra-family correlation of the outcomes at ``p = 0.5``
  before clipping), clipped to ``[0, 1]``. Every repetition is an independent Bernoulli trial. The candidate
  adds ``effect`` to ``p`` on the improved units (``effect_concentration`` is the fraction of units that
  improve; each improved unit gains ``effect / effect_concentration`` so the mean effect is ``effect``
  before clipping), clipped to ``[0, 1]``.
* Continuous outcome: ``Beta(p * K, (1 - p) * K)`` with ``K = 10``, so the score is bounded in ``[0, 1]``.
* ``dev_candidates`` candidates compete on ``dev_units`` development units. One of them (chosen at random
  per simulation, so a tie-break by id cannot favor it) carries ``effect`` and ``regression_effect``; the
  others are null-effect clones. The finalist picked on development alone is the only candidate confirmed.
* ``error_rate`` marks that share of trials ``EXECUTION_ERROR``; the real ``decide`` then fails closed.
* The statistics seed of the experiment (``spec.seed``) is fixed, as in a real manifest; the variation across
  simulations comes from the data, drawn from ``random.Random(f"{seed}:{scenario.id}:{index}")``.

The structure of an experiment (spec, plan, tasks) depends only on the scenario geometry and the policy, so
it is built once per process and reused by every simulation; per simulation only records are created.
"""
from __future__ import annotations

import argparse
import dataclasses
import functools
import hashlib
import json
import math
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from harness_workbench.core.harness import Harness
from harness_workbench.experiments.contracts import (
    EvaluationResult,
    ExecutionResult,
    ExperimentDecision,
    Measurement,
    PrivateExperimentPlan,
    TrialRecord,
)
from harness_workbench.experiments.manifest import (
    BudgetPolicy,
    EnvironmentConfig,
    ExecutorConfig,
    ExperimentSpec,
    StatisticsPolicy,
    Variant,
    _task_spec,
)
from harness_workbench.experiments.profiles import CALIBRATION_GRID, PROFILES, StatisticalProfile, grid_points
from harness_workbench.experiments.protocol import build_plan, freeze_selection, select_finalist
from harness_workbench.experiments.stats import METHODS, decide
from harness_workbench.experiments.status import DecisionStatus, EvaluationStatus, ExecutionStatus, Provenance

__all__ = [
    "CALIBRATION_GRID", "MATRIX_FULL", "MATRIX_SMOKE", "RECREATIONS", "Scenario", "build_matrix", "grid_points",
    "main", "run_protocol", "run_scenario", "simulate_records", "target_scenario", "wilson_interval",
]

Z_ONE_SIDED_95 = 1.6448536269514722  # the spec's "unilateral 95 %" limit (spec 5.4)
Z_TWO_SIDED_95 = 1.959963984540054
BETA_CONCENTRATION = 10.0
SMOKE_ITERATIONS = 1000  # the smoke matrix reduces the bootstrap unless --iterations says otherwise
SYNTHETIC_IMPLEMENTATION = "synthetic-impl"
_BASELINE = "baseline"
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:e[-+]?\d+)?")
_TOKENS = Measurement.reported(100, Provenance.ADAPTER_REPORTED)
_UNKNOWN_TOKENS = Measurement.unknown()
_LATENCY = Measurement.measured(10.0)
_PASSED_CHECKS = [{"id": "answer", "type": "exact", "weight": 1.0, "status": "passed", "passed": True,
                   "detail": "synthetic"}]
_FAILED_CHECKS = [{"id": "answer", "type": "exact", "weight": 1.0, "status": "failed", "passed": False,
                   "detail": "synthetic"}]
_CONTROLS = {"positive": {"id": "pass", "outputs": {"answer": "CORRECT"}},
             "negatives": [{"id": "wrong", "outputs": {"answer": "INCORRECT"}}]}


# --- scenarios -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    id: str
    kind: str  # "null": a promotion is a false promotion; "power": a promotion is a true one
    n_units: int  # independent validation units (families)
    regression_units: int
    repetitions: int
    dev_candidates: int
    dev_units: int
    outcome: str  # "binary" or "continuous"
    p_base: float
    effect: float
    effect_concentration: float = 1.0
    family_size: int = 1
    family_correlation: float = 0.0
    regression_effect: float = 0.0
    error_rate: float = 0.0
    difficulty_spread: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("scenario id must be a non-empty string")
        if self.kind not in ("null", "power"):
            raise ValueError(f"kind must be 'null' or 'power', got {self.kind!r}")
        if self.outcome not in ("binary", "continuous"):
            raise ValueError(f"outcome must be 'binary' or 'continuous', got {self.outcome!r}")
        for name in ("n_units", "regression_units", "repetitions", "dev_candidates", "dev_units", "family_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be an integer >= 1, got {value!r}")
        for name in ("p_base", "effect", "effect_concentration", "family_correlation", "regression_effect",
                     "error_rate", "difficulty_spread"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite, got {getattr(self, name)!r}")
        if not 0.0 <= self.p_base <= 1.0:
            raise ValueError(f"p_base must be in [0, 1], got {self.p_base!r}")
        if not 0.0 < self.effect_concentration <= 1.0:
            raise ValueError(f"effect_concentration must be in (0, 1], got {self.effect_concentration!r}")
        if not 0.0 <= self.family_correlation < 1.0:
            raise ValueError(f"family_correlation must be in [0, 1), got {self.family_correlation!r}")
        if not 0.0 <= self.error_rate < 1.0:
            raise ValueError(f"error_rate must be in [0, 1), got {self.error_rate!r}")
        if self.difficulty_spread < 0.0:
            raise ValueError(f"difficulty_spread must be >= 0, got {self.difficulty_spread!r}")


def target_scenario(profile: StatisticalProfile) -> Scenario:
    """The pre-registered power scenario: the profile's own plan, success 0.50 -> 0.70, no regression."""
    base, goal = profile.target_effect
    return Scenario(
        id="target-power", kind="power", n_units=profile.min_independent_units,
        regression_units=profile.min_regression_units, repetitions=profile.repetitions, dev_candidates=5,
        dev_units=profile.min_independent_units, outcome=profile.outcome, p_base=base,
        effect=round(goal - base, 12), regression_effect=0.0)


def _factorial(profile: StatisticalProfile) -> list[Scenario]:
    scenarios = []
    for candidates in (1, 5, 20):
        for units, regression in ((10, 10), (profile.min_independent_units, profile.min_regression_units)):
            for repetitions in (1, 3, 5, 10):
                for outcome in ("binary", "continuous"):
                    scenarios.append(Scenario(
                        id=f"null-d{candidates}-n{units}-r{repetitions}-{outcome}", kind="null", n_units=units,
                        regression_units=regression, repetitions=repetitions, dev_candidates=candidates,
                        dev_units=units, outcome=outcome, p_base=0.5, effect=0.0))
    return scenarios


def _at_profile(profile: StatisticalProfile, scenario_id: str, kind: str, **fields: Any) -> Scenario:
    values: dict[str, Any] = dict(
        n_units=profile.min_independent_units, regression_units=profile.min_regression_units,
        repetitions=profile.repetitions, dev_candidates=5, dev_units=profile.min_independent_units,
        outcome="binary", p_base=0.5, effect=0.0)
    values.update(fields)
    return Scenario(id=scenario_id, kind=kind, **values)


def _full_matrix(profile: StatisticalProfile) -> list[Scenario]:
    margin = profile.practical_margin
    power = 0.2
    special = [
        _at_profile(profile, "hetero-null", "null", difficulty_spread=0.2),
        _at_profile(profile, "hetero-power", "power", difficulty_spread=0.2, effect=power),
        _at_profile(profile, "famcorr-null", "null", family_size=3, family_correlation=0.5),
        _at_profile(profile, "famcorr-power", "power", family_size=3, family_correlation=0.5, effect=power),
        _at_profile(profile, "concentrated-power", "power", effect_concentration=0.25, p_base=0.3, effect=power),
        _at_profile(profile, "boundary-d1", "null", dev_candidates=1, effect=margin),
        _at_profile(profile, "boundary-d5", "null", effect=margin),
        _at_profile(profile, "boundary-d20", "null", dev_candidates=20, effect=margin),
        _at_profile(profile, "regression-blocked", "null", effect=power, regression_effect=-0.10),
        _at_profile(profile, "missingness-5pct", "power", effect=power, error_rate=0.05),
    ]
    return [*_factorial(profile), *special, target_scenario(profile)]


def _smoke_matrix(profile: StatisticalProfile) -> list[Scenario]:
    return [
        _at_profile(profile, "null-d1", "null", dev_candidates=1),
        _at_profile(profile, "null-d5", "null"),
        _at_profile(profile, "boundary-d5", "null", effect=profile.practical_margin),
        target_scenario(profile),
        _at_profile(profile, "small-n1", "null", n_units=1, regression_units=1, dev_units=1),
        _at_profile(profile, "regression-blocked", "null", effect=0.2, regression_effect=-0.10),
    ]


def _recreations() -> list[Scenario]:
    """Diagnostic recreations of the audit's A2, A3 and A8 configurations (not their numbers)."""
    return [
        Scenario(id="A2", kind="null", n_units=3, regression_units=3, repetitions=5, dev_candidates=5, dev_units=3,
                 outcome="binary", p_base=0.5, effect=0.0),
        Scenario(id="A3", kind="null", n_units=1, regression_units=1, repetitions=5, dev_candidates=1, dev_units=1,
                 outcome="binary", p_base=0.5, effect=0.0),
        Scenario(id="A8", kind="power", n_units=5, regression_units=5, repetitions=5, dev_candidates=1, dev_units=5,
                 outcome="binary", p_base=0.5, effect=0.2),
    ]


def build_matrix(name: str, profile: StatisticalProfile) -> list[Scenario]:
    """The named scenario list for ``profile`` (the profile decides the plan the scenarios run at)."""
    builders = {"smoke": _smoke_matrix, "full": _full_matrix, "recreations": lambda _profile: _recreations()}
    if name not in builders:
        raise ValueError(f"unknown matrix {name!r}; expected one of {sorted(builders)}")
    return builders[name](profile)


MATRIX_SMOKE: list[Scenario] = build_matrix("smoke", PROFILES["binary_v1"])
MATRIX_FULL: list[Scenario] = build_matrix("full", PROFILES["binary_v1"])
RECREATIONS: list[Scenario] = build_matrix("recreations", PROFILES["binary_v1"])


# --- statistics ----------------------------------------------------------------------------------------


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for ``k`` successes in ``n`` trials."""
    if n < 1 or k < 0 or k > n:
        raise ValueError(f"need 0 <= k <= n and n >= 1, got k={k!r}, n={n!r}")
    p = k / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denominator
    # The exact end points: float rounding must not turn 'all successes' into an upper bound below 1.
    return (0.0 if k == 0 else max(0.0, center - half)), (1.0 if k == n else min(1.0, center + half))


# --- the synthetic structure of one experiment (built once, reused by every simulation) ----------------


@dataclass(frozen=True)
class _Split:
    """The tasks of one split, grouped by family: ``families[i]`` is the task ids of unit ``i``."""

    name: str
    families: tuple[tuple[str, ...], ...]
    family_ids: tuple[str, ...]
    rubric: dict


@dataclass(frozen=True)
class _Structure:
    spec: ExperimentSpec
    plan: PrivateExperimentPlan
    baseline_id: str
    candidate_ids: tuple[str, ...]
    development: _Split
    validation: _Split
    regression: _Split


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _task_items(prefix: str, split: str, units: int, family_size: int) -> tuple[list[dict], _Split]:
    items: list[dict] = []
    families: list[tuple[str, ...]] = []
    family_ids: list[str] = []
    for unit in range(units):
        family = f"{prefix}{unit}"
        ids = []
        for member in range(family_size):
            task_id = f"{prefix}{unit}-{member}" if family_size > 1 else family
            description = f"synthetic {split} unit {unit}" + (f" task {member}" if family_size > 1 else "")
            item: dict[str, Any] = {
                "id": task_id, "description": description, "split": split, "family": family,
                "runs_candidate_code": False,
                "checks": [{"id": "answer", "type": "exact", "target": "answer", "value": "CORRECT"}],
            }
            if split != "development":
                item["controls"] = _CONTROLS
            items.append(item)
            ids.append(task_id)
        families.append(tuple(ids))
        family_ids.append(family)
    return items, _Split(split, tuple(families), tuple(family_ids), {})


@functools.lru_cache(maxsize=256)
def _structure(geometry: tuple, policy: tuple) -> _Structure:
    n_units, regression_units, dev_units, dev_candidates, family_size, repetitions = geometry
    (method, profile_id, alpha, practical_margin, regression_margin, min_units, min_regression,
     iterations) = policy
    dev_items, development = _task_items("d", "development", dev_units if dev_candidates > 1 else 1, family_size)
    val_items, validation = _task_items("v", "validation", n_units, family_size)
    reg_items, regression = _task_items("r", "regression", regression_units, family_size)
    base = Path(".")
    tasks = tuple(_task_spec(item, base, legacy=False) for item in (*dev_items, *val_items, *reg_items))
    rubric = {task.id: task.bundle_hash for task in tasks}
    development, validation, regression = (
        dataclasses.replace(split, rubric=rubric) for split in (development, validation, regression))
    candidate_ids = tuple(f"cand-{index:02d}" for index in range(dev_candidates))
    variants = tuple(
        Variant(variant_id, Harness(id="synthetic", version=str(index + 1),
                                    system_prompt=f"synthetic variant {variant_id}"),
                Path(f"{variant_id}.json"), _sha(variant_id))
        for index, variant_id in enumerate((_BASELINE, *candidate_ids)))
    statistics = StatisticsPolicy(
        method=method, profile=profile_id, alpha=alpha, practical_margin=practical_margin,
        regression_margin=regression_margin, min_independent_units=min_units, min_regression_units=min_regression,
        bootstrap_iterations=iterations)
    spec = ExperimentSpec(
        schema_version=2, name="synthetic", mode="exploratory", family="synthetic", dataset_id="synthetic",
        dataset_version="1", variants=variants, tasks=tasks, repetitions=repetitions, seed=1,
        statistics=statistics, budget=BudgetPolicy(), environment=EnvironmentConfig(mode="sandboxed"),
        executor=ExecutorConfig(), source=None, source_hash=_sha(repr((geometry, policy))), legacy=False,
        warnings=())
    # The spec is built in exploratory mode and then marked confirmatory: ``validate_confirmatory`` would
    # reject a grid point below the published profile floors, and those points are exactly what the
    # calibration has to simulate. ``decide`` reads ``spec.mode``; the structure is otherwise a valid
    # confirmatory spec (a test calls ``validate_confirmatory`` on it at the profile plan).
    object.__setattr__(spec, "mode", "confirmatory")
    return _Structure(spec, build_plan(spec), _BASELINE, candidate_ids, development, validation, regression)


def _structure_for(scenario: Scenario, profile: StatisticalProfile, method: str) -> _Structure:
    dev_units = scenario.dev_units if scenario.dev_candidates > 1 else 1  # a single candidate needs no selection
    geometry = (scenario.n_units, scenario.regression_units, dev_units, scenario.dev_candidates,
                scenario.family_size, scenario.repetitions)
    policy = (method, profile.id, profile.alpha, profile.practical_margin, profile.regression_margin,
              profile.min_independent_units, profile.min_regression_units, profile.bootstrap_iterations)
    return _structure(geometry, policy)


# --- drawing records -----------------------------------------------------------------------------------


def _base_probabilities(rng: random.Random, scenario: Scenario, split: _Split) -> list[list[float]]:
    """The baseline success probability of every task, per family."""
    spread, correlation = scenario.difficulty_spread, scenario.family_correlation
    sigma = 0.5 * math.sqrt(correlation)
    rows = []
    for family in split.families:
        offset = rng.gauss(0.0, sigma) if correlation else 0.0
        row = []
        for _ in family:
            p = scenario.p_base + offset
            if spread:
                p += rng.uniform(-spread, spread)
            row.append(min(1.0, max(0.0, p)))
        rows.append(row)
    return rows


def _gains(rng: random.Random, scenario: Scenario, units: int, effect: float, concentration: float) -> list[float]:
    """The per-family probability shift of the candidate that carries the effect."""
    if effect == 0.0:
        return [0.0] * units
    if concentration >= 1.0:
        return [effect] * units
    improved = rng.sample(range(units), max(1, round(concentration * units)))
    gains = [0.0] * units
    for index in improved:
        gains[index] = effect / concentration
    return gains


def _draw(rng: random.Random, outcome: str, p: float) -> float:
    if outcome == "binary":
        return 1.0 if rng.random() < p else 0.0
    p = min(0.99, max(0.01, p))
    return min(1.0, max(0.0, rng.betavariate(p * BETA_CONCENTRATION, (1.0 - p) * BETA_CONCENTRATION)))


def _emit(rng: random.Random, scenario: Scenario, split: _Split, base: list[list[float]],
          variants: Sequence[tuple[str, list[float] | None]], tokens: Measurement, out: list[TrialRecord]) -> None:
    """Records for the baseline and the given candidates on one split (``None`` gains: the baseline)."""
    outcome, error_rate, reps = scenario.outcome, scenario.error_rate, scenario.repetitions
    for fam_index, family in enumerate(split.families):
        family_id = split.family_ids[fam_index]
        for member, task_id in enumerate(family):
            p_base = base[fam_index][member]
            rubric = split.rubric[task_id]
            for repetition in range(reps):
                for variant_id, gains in variants:
                    p = p_base if gains is None else min(1.0, max(0.0, p_base + gains[fam_index]))
                    trial_id = f"{variant_id}.{task_id}.{repetition}"
                    if error_rate and rng.random() < error_rate:
                        execution = ExecutionResult(
                            trial_id, ExecutionStatus.EXECUTION_ERROR, {}, None, "", "", tokens, _LATENCY,
                            Measurement.unknown(), None, None, "synthetic execution error")
                        evaluation = EvaluationResult(
                            trial_id, EvaluationStatus.NOT_EVALUATED, rubric, None, [], None,
                            "not evaluated: synthetic")
                    else:
                        score = _draw(rng, outcome, p)
                        passed = score >= 0.5
                        execution = ExecutionResult(
                            trial_id, ExecutionStatus.COMPLETED, {}, None, "", "", tokens, _LATENCY,
                            Measurement.unknown(), None, None, None)
                        evaluation = EvaluationResult(
                            trial_id, EvaluationStatus.PASSED if passed else EvaluationStatus.FAILED, rubric, None,
                            _PASSED_CHECKS if passed else _FAILED_CHECKS, score, None)
                    out.append(TrialRecord(
                        trial_id, variant_id, task_id, split.name, split.family_ids[fam_index], repetition,
                        repetition + 1, 1, execution, evaluation, SYNTHETIC_IMPLEMENTATION))


def _candidate_gains(rng: random.Random, scenario: Scenario, structure: _Structure, true_index: int,
                     which: Sequence[int], split: _Split, effect: float, concentration: float,
                     ) -> list[tuple[str, list[float]]]:
    """``(variant id, gains)`` for the named candidates; only the true candidate carries the effect."""
    units = len(split.families)
    true_gains = _gains(rng, scenario, units, effect, concentration)
    zeros = [0.0] * units
    return [(structure.candidate_ids[index], true_gains if index == true_index else zeros) for index in which]


def _tokens(unknown_usage: bool) -> Measurement:
    return _UNKNOWN_TOKENS if unknown_usage else _TOKENS


def _resolve(profile: StatisticalProfile | None, method: str | None) -> tuple[StatisticalProfile, str]:
    profile = PROFILES["binary_v1"] if profile is None else profile
    method = profile.method if method is None else method
    if method not in METHODS:
        raise ValueError(f"unknown bound method {method!r}; expected one of {METHODS}")
    return profile, method


def simulate_records(
    scenario: Scenario, rng: random.Random, *, profile_like: StatisticalProfile | None = None,
    method: str | None = None, unknown_usage: bool = False,
) -> tuple[list[TrialRecord], PrivateExperimentPlan, ExperimentSpec]:
    """Every record of the experiment (every variant on every split), its private plan and its spec.

    One randomly chosen candidate carries ``effect`` (development and validation) and ``regression_effect``;
    the other candidates are null-effect clones. ``run_scenario`` draws only what the protocol needs.
    """
    profile, method = _resolve(profile_like, method)
    structure = _structure_for(scenario, profile, method)
    true_index = rng.randrange(scenario.dev_candidates)
    everyone = range(scenario.dev_candidates)
    tokens = _tokens(unknown_usage)
    records: list[TrialRecord] = []
    for split, effect, concentration in (
            (structure.development, scenario.effect, scenario.effect_concentration),
            (structure.validation, scenario.effect, scenario.effect_concentration),
            (structure.regression, scenario.regression_effect, 1.0)):
        if split is structure.development and scenario.dev_candidates == 1:
            continue
        base = _base_probabilities(rng, scenario, split)
        variants = [(structure.baseline_id, None),
                    *_candidate_gains(rng, scenario, structure, true_index, everyone, split, effect, concentration)]
        _emit(rng, scenario, split, base, variants, tokens, records)
    return records, structure.plan, structure.spec


# --- the protocol --------------------------------------------------------------------------------------


def _decision(scenario: Scenario, profile: StatisticalProfile, rng: random.Random, method: str,
              unknown_usage: bool) -> ExperimentDecision:
    structure = _structure_for(scenario, profile, method)
    spec, plan = structure.spec, structure.plan
    true_index = rng.randrange(scenario.dev_candidates)
    tokens = _tokens(unknown_usage)
    development: list[TrialRecord] = []
    if scenario.dev_candidates > 1:
        split = structure.development
        base = _base_probabilities(rng, scenario, split)
        variants = [(structure.baseline_id, None),
                    *_candidate_gains(rng, scenario, structure, true_index, range(scenario.dev_candidates), split,
                                      scenario.effect, scenario.effect_concentration)]
        _emit(rng, scenario, split, base, variants, tokens, development)
    finalist_id, evidence = select_finalist(spec, development)
    problems: list[str] = []
    selection = None
    holdout: list[TrialRecord] = []
    if finalist_id is None:
        problems.append(f"no finalist could be selected: {evidence.get('problem', 'unknown reason')}")
    else:
        selection = freeze_selection(spec, plan, finalist_id, evidence=evidence)
        finalist_index = structure.candidate_ids.index(finalist_id)
        for split, effect, concentration in (
                (structure.validation, scenario.effect, scenario.effect_concentration),
                (structure.regression, scenario.regression_effect, 1.0)):
            base = _base_probabilities(rng, scenario, split)
            variants = [(structure.baseline_id, None),
                        *_candidate_gains(rng, scenario, structure, true_index, [finalist_index], split, effect,
                                          concentration)]
            _emit(rng, scenario, split, base, variants, tokens, holdout)
    return decide(plan, holdout, spec=spec, selection=selection, integrity_problems=problems,
                  exploratory_reason=None)


def run_protocol(scenario: Scenario, profile_like: StatisticalProfile, rng: random.Random, *, method: str,
                 unknown_usage: bool = False) -> DecisionStatus:
    """One simulated experiment end to end: select on development, freeze, decide on the holdout."""
    profile, method = _resolve(profile_like, method)
    return _decision(scenario, profile, rng, method, unknown_usage).status


def _normalise(reason: str) -> str:
    return _NUMBER.sub("#", reason)[:160]


def _simulation_rng(seed: int, scenario: Scenario, index: int) -> random.Random:
    return random.Random(f"{seed}:{scenario.id}:{index}")


def _run_chunk(args: tuple) -> tuple[dict[str, int], dict[str, int]]:
    scenario, profile, method, seed, start, stop, unknown_usage = args
    statuses: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for index in range(start, stop):
        decision = _decision(scenario, profile, _simulation_rng(seed, scenario, index), method, unknown_usage)
        statuses[decision.status.value] += 1
        for reason in dict.fromkeys(_normalise(item) for item in decision.reasons):
            reasons[reason] += 1
    return dict(statuses), dict(reasons)


def _chunks(simulations: int, workers: int) -> list[tuple[int, int]]:
    count = min(workers, simulations)
    bounds = [simulations * i // count for i in range(count + 1)]
    return [(bounds[i], bounds[i + 1]) for i in range(count)]


def run_scenario(
    scenario: Scenario, profile_like: StatisticalProfile, *, simulations: int, seed: int, method: str,
    workers: int = 1, unknown_usage: bool = False,
) -> dict[str, Any]:
    """Run ``simulations`` independent experiments of ``scenario``; the result does not depend on ``workers``."""
    if isinstance(simulations, bool) or not isinstance(simulations, int) or simulations < 1:
        raise ValueError(f"simulations must be an integer >= 1, got {simulations!r}")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError(f"workers must be an integer >= 1, got {workers!r}")
    profile, method = _resolve(profile_like, method)
    jobs = [(scenario, profile, method, seed, start, stop, unknown_usage)
            for start, stop in _chunks(simulations, workers)]
    if len(jobs) == 1:
        parts = [_run_chunk(jobs[0])]
    else:
        import multiprocessing

        with multiprocessing.get_context("spawn").Pool(len(jobs)) as pool:
            parts = pool.map(_run_chunk, jobs)
    statuses: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for chunk_statuses, chunk_reasons in parts:
        statuses.update(chunk_statuses)
        reasons.update(chunk_reasons)
    promoted = statuses.get(DecisionStatus.ELIGIBLE.value, 0)
    lower, upper = wilson_interval(promoted, simulations, z=Z_ONE_SIDED_95)
    return {
        "synthetic": True,
        "scenario": dataclasses.asdict(scenario),
        "simulations": simulations,
        "promoted": promoted,
        "rate": promoted / simulations,
        "lower_95": lower,
        "upper_95": upper,
        "status_counts": dict(sorted(statuses.items())),
        "reason_counts": dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0]))),
        "method": method,
        "seed": seed,
    }


# --- command line --------------------------------------------------------------------------------------


def _implementation_hash() -> str:
    package = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for item in sorted(package.rglob("*.py")):
        digest.update(item.relative_to(package).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness_workbench.experiments.simulate",
        description="Monte Carlo simulation of the confirmatory protocol over the real selection and decision "
                    "functions. All outcomes are synthetic.")
    parser.add_argument("--profile", default="binary_v1", help="published profile to simulate (default binary_v1)")
    parser.add_argument("--matrix", choices=("smoke", "full", "recreations"), required=True)
    parser.add_argument("--simulations", type=_positive, help="simulations per scenario (both kinds)")
    parser.add_argument("--null-simulations", type=_positive, help="simulations per null scenario")
    parser.add_argument("--power-simulations", type=_positive, help="simulations per power scenario")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--method", choices=METHODS, help="override the profile's method")
    parser.add_argument("--units", type=_positive, help="override min_independent_units (and the scenario size)")
    parser.add_argument("--regression-units", type=_positive, help="override min_regression_units")
    parser.add_argument("--regression-margin", type=float, help="override regression_margin")
    parser.add_argument("--iterations", type=_positive,
                        help=f"override bootstrap_iterations (the smoke matrix defaults to {SMOKE_ITERATIONS})")
    parser.add_argument("--workers", type=_positive, default=1)
    parser.add_argument("--unknown-usage", action="store_true",
                        help="draw every trial with unknown token usage to exercise the blocking path")
    parser.add_argument("--out", help="write the results JSON to this path")
    parser.add_argument("--json", action="store_true", help="print the results JSON instead of a table")
    return parser


def _table(payload: dict[str, Any]) -> str:
    lines = [f"SYNTHETIC Monte Carlo results (Bernoulli/Beta outcomes, not language-model data): profile "
             f"{payload['profile']}, method {payload['method']}, seed {payload['seed']}",
             f"{'scenario':<34}{'kind':<7}{'sims':>7}{'promoted':>9}{'rate':>8}{'lower95':>9}{'upper95':>9}  statuses"]
    for item in payload["scenarios"]:
        scenario = item["scenario"]
        statuses = ", ".join(f"{name}={count}" for name, count in item["status_counts"].items())
        lines.append(f"{scenario['id']:<34}{scenario['kind']:<7}{item['simulations']:>7}{item['promoted']:>9}"
                     f"{item['rate']:>8.3f}{item['lower_95']:>9.3f}{item['upper_95']:>9.3f}  {statuses}")
    for note in payload["notes"]:
        lines.append(f"note: {note}")
    lines.append(f"wall {payload['wall_seconds']:.1f}s, python {payload['python'].split()[0]}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.profile not in PROFILES:
        parser.error(f"unknown profile {args.profile!r}; known: {sorted(PROFILES)}")
    overrides: dict[str, Any] = {}
    notes: list[str] = []
    if args.method is not None:
        overrides["method"] = args.method
    if args.units is not None:
        overrides["min_independent_units"] = args.units
    if args.regression_units is not None:
        overrides["min_regression_units"] = args.regression_units
    if args.regression_margin is not None:
        overrides["regression_margin"] = args.regression_margin
    if args.iterations is not None:
        overrides["bootstrap_iterations"] = args.iterations
    elif args.matrix == "smoke":
        overrides["bootstrap_iterations"] = SMOKE_ITERATIONS
        notes.append(f"the smoke matrix uses {SMOKE_ITERATIONS} bootstrap iterations unless --iterations is given")
    try:
        profile = dataclasses.replace(PROFILES[args.profile], **{k: v for k, v in overrides.items() if k != "method"})
        if "method" in overrides:
            profile = dataclasses.replace(profile, method=overrides["method"])
    except ValueError as exc:
        parser.error(str(exc))
    matrix = build_matrix(args.matrix, profile)
    counts = {"null": args.null_simulations or args.simulations, "power": args.power_simulations or args.simulations}
    needed = {scenario.kind for scenario in matrix}
    missing = sorted(kind for kind in needed if counts[kind] is None)
    if missing:
        parser.error(f"give --simulations or --{missing[0]}-simulations (no count for {missing[0]} scenarios)")
    started = time.perf_counter()
    results = [
        run_scenario(scenario, profile, simulations=counts[scenario.kind], seed=args.seed, method=profile.method,
                     workers=args.workers, unknown_usage=args.unknown_usage)
        for scenario in matrix]
    payload = {
        "synthetic": True,
        "profile": args.profile,
        "overrides": overrides,
        "method": profile.method,
        "seed": args.seed,
        "simulations": {kind: counts[kind] for kind in sorted(needed)},
        "matrix": args.matrix,
        "unknown_usage": args.unknown_usage,
        "interval": {"kind": "wilson", "z": Z_ONE_SIDED_95,
                     "meaning": "one-sided 95 % bounds on the promotion rate (spec 5.4)"},
        "notes": notes,
        "scenarios": results,
        "implementation_hash": _implementation_hash(),
        "python": sys.version,
        "wall_seconds": time.perf_counter() - started,
    }
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2) if args.json else _table(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
