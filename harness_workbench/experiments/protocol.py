"""Selection protocol: choose a finalist on development only, freeze it, name the holdout.

The order of a confirmatory experiment is fixed here. The finalist is chosen from the development split
alone (never from validation or regression records), then frozen together with the hashes that define the
comparison: the finalist and baseline execution configuration, the private plan and the statistics policy.
Later code verifies those hashes before it spends holdout exposure on the finalist, so any parameter edited
after the freeze is reported as a problem instead of silently becoming part of the evidence.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any, Iterable

from harness_workbench.core.harness import Harness
from harness_workbench.experiments.contracts import PrivateExperimentPlan, TrialRecord
from harness_workbench.experiments.manifest import ExperimentSpec, Split, StatisticsPolicy
from harness_workbench.experiments.validation import (
    ValidationError,
    assert_finite_tree,
    json_dumps_strict,
    json_loads_strict,
)

__all__ = [
    "FrozenSelection", "build_plan", "dev_effects", "freeze_selection", "holdout_fingerprints",
    "select_finalist", "verify_frozen",
]

_TIE_DIGITS = 12  # effects closer than this are one number: float noise must not pick a winner
_INCOMPLETE = "development evidence incomplete"
_HOLDOUT_SPLITS = (Split.VALIDATION, Split.REGRESSION)


# --- frozen selection -------------------------------------------------------------------------------


def _string(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class FrozenSelection:
    finalist_id: str
    finalist_hash: str
    baseline_hash: str
    plan_hash: str
    policy_hash: str
    frozen_at: str
    evidence: dict

    def __post_init__(self) -> None:
        for name in ("finalist_id", "finalist_hash", "baseline_hash", "plan_hash", "policy_hash", "frozen_at"):
            _string(name, getattr(self, name))
        if not isinstance(self.evidence, dict):
            raise ValidationError(f"evidence must be an object, got {type(self.evidence).__name__}")
        assert_finite_tree(self.evidence)
        # A detached copy: the frozen object must not change when the caller edits the dict it passed.
        object.__setattr__(self, "evidence", json_loads_strict(json_dumps_strict(self.evidence)))

    def to_dict(self) -> dict[str, Any]:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["evidence"] = json_loads_strict(json_dumps_strict(self.evidence))
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "FrozenSelection":
        if not isinstance(data, dict):
            raise ValidationError(f"frozen selection must be an object, got {type(data).__name__}")
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data:
                raise ValidationError(f"frozen selection is missing required field {f.name!r}")
            values[f.name] = data[f.name]
        return cls(**values)

    @property
    def selection_hash(self) -> str:
        """sha256 of every field: any change to what was frozen changes the hash."""
        return hashlib.sha256(json_dumps_strict(self.to_dict(), sort_keys=True).encode("utf-8")).hexdigest()


# --- plan -------------------------------------------------------------------------------------------


def build_plan(spec: ExperimentSpec) -> PrivateExperimentPlan:
    """The private plan of ``spec``: splits, families, hashes and the fingerprint of every task."""
    splits = {split.value: tuple(task.id for task in spec.tasks_in(split)) for split in Split}
    regression_families = sorted({task.family for task in spec.tasks_in(Split.REGRESSION)})
    return PrivateExperimentPlan(
        experiment_name=spec.name,
        mode=spec.mode,
        family=spec.family,
        dataset_id=spec.dataset_id,
        dataset_version=spec.dataset_version,
        splits=splits,
        families={task.id: task.family for task in spec.tasks},
        regression_groups={"regression": tuple(regression_families)} if regression_families else {},
        variant_hashes={variant.id: variant.config_hash for variant in spec.variants},
        rubric_hashes={task.id: task.bundle_hash for task in spec.tasks},
        task_fingerprints={task.id: task.content_fingerprint for task in spec.tasks},
        policy={f.name: getattr(spec.statistics, f.name) for f in fields(spec.statistics)},
        budget={
            f.name: list(value) if isinstance(value, tuple) else value
            for f in fields(spec.budget) for value in (getattr(spec.budget, f.name),)
        },
        repetitions=spec.repetitions,
        seed=spec.seed,
        environment_mode=spec.environment.mode,
    )


def holdout_fingerprints(spec: ExperimentSpec) -> dict[str, str]:
    """Task id -> content fingerprint for the validation and regression tasks.

    The fingerprint covers what a task asks and how it is graded (description, domain, constraints, fixture
    and grader bundle), not its id, family or split, so the same content is the same exposure under any name.
    """
    return {task.id: task.content_fingerprint for task in spec.tasks if task.split in _HOLDOUT_SPLITS}


# --- development evidence ---------------------------------------------------------------------------


def _latest_development(records: Iterable[TrialRecord], spec: ExperimentSpec,
                        ) -> dict[tuple[str, str, int], TrialRecord]:
    """Development trials by (variant, task, repetition); a higher attempt replaces a lower one."""
    development = {task.id for task in spec.tasks_in(Split.DEVELOPMENT)}
    latest: dict[tuple[str, str, int], TrialRecord] = {}
    for record in records:
        if record.task_id not in development:
            continue
        key = (record.variant_id, record.task_id, record.repetition)
        current = latest.get(key)
        if current is None or record.attempt > current.attempt:
            latest[key] = record
    return latest


def _effect(latest: dict[tuple[str, str, int], TrialRecord], spec: ExperimentSpec, variant_id: str,
            ) -> tuple[float | None, str | None]:
    """(effect, None) for a complete candidate, (None, reason) when any development pair is unusable."""
    baseline_id = spec.variants[0].id
    tasks = spec.tasks_in(Split.DEVELOPMENT)
    if not tasks:
        return None, "there are no development tasks"
    deltas_by_family: dict[str, list[float]] = {}
    bad = 0
    total = 0
    for task in tasks:
        for repetition in range(spec.repetitions):
            total += 1
            candidate = latest.get((variant_id, task.id, repetition))
            baseline = latest.get((baseline_id, task.id, repetition))
            if candidate is None or baseline is None:
                bad += 1
                continue
            left, right = candidate.objective_score, baseline.objective_score
            if left is None or right is None:
                bad += 1
                continue
            deltas_by_family.setdefault(task.family, []).append(left - right)
    if bad:
        return None, f"development evidence incomplete: {bad} of {total} paired trials are unusable or missing"
    family_means = [sum(deltas) / len(deltas) for _, deltas in sorted(deltas_by_family.items())]
    return sum(family_means) / len(family_means), None


def dev_effects(records: list[TrialRecord], spec: ExperimentSpec) -> dict[str, float | None]:
    """Candidate id -> mean over development families of the mean paired delta against the baseline.

    A pair is the same task and repetition for candidate and baseline (the latest attempt of each). The
    value is ``None`` when any pair is missing or unusable, so a candidate is never ranked on partial
    evidence. Records of other splits are ignored.
    """
    latest = _latest_development(records, spec)
    return {variant.id: _effect(latest, spec, variant.id)[0] for variant in spec.variants[1:]}


def _mean_tokens(latest: dict[tuple[str, str, int], TrialRecord], spec: ExperimentSpec,
                 variant_id: str) -> float | None:
    values: list[float] = []
    for task in spec.tasks_in(Split.DEVELOPMENT):
        for repetition in range(spec.repetitions):
            record = latest.get((variant_id, task.id, repetition))
            value = None if record is None else record.execution.tokens.value
            if value is None:
                return None
            values.append(value)
    return sum(values) / len(values) if values else None


def select_finalist(spec: ExperimentSpec, dev_records: list[TrialRecord]) -> tuple[str | None, dict]:
    """The finalist id and the evidence for the choice, from development records only.

    Highest development effect wins; a tie goes to the lower mean token count when every tied candidate has
    one, then to the lexically smaller id. Candidates with incomplete evidence are excluded with a reason.
    """
    candidates = [variant.id for variant in spec.variants[1:]]
    if not candidates:
        return None, {"problem": "no candidate variants"}
    if len(candidates) == 1:
        return candidates[0], {"selection": "single_candidate"}
    latest = _latest_development(dev_records, spec)
    effects: dict[str, float] = {}
    excluded: dict[str, str] = {}
    for candidate in candidates:
        effect, reason = _effect(latest, spec, candidate)
        if effect is None:
            excluded[candidate] = reason or _INCOMPLETE
        else:
            effects[candidate] = effect
    if not effects:
        return None, {"problem": _INCOMPLETE}
    best = max(round(effect, _TIE_DIGITS) for effect in effects.values())
    tied = sorted(candidate for candidate, effect in effects.items() if round(effect, _TIE_DIGITS) == best)
    tokens = {candidate: _mean_tokens(latest, spec, candidate) for candidate in tied}
    tie_break = "none"
    if len(tied) > 1:
        tie_break = "id"
        if all(value is not None for value in tokens.values()):
            tied.sort(key=lambda candidate: (tokens[candidate], candidate))
            if tokens[tied[0]] != tokens[tied[1]]:
                tie_break = "tokens"
    evidence = {
        "selection": "development",
        "effects": dict(sorted(effects.items())),
        "excluded": dict(sorted(excluded.items())),
        "tie_break": tie_break,
    }
    if tie_break == "tokens":
        evidence["mean_tokens"] = {candidate: tokens[candidate] for candidate in sorted(tokens)}
    return tied[0], evidence


# --- freezing ---------------------------------------------------------------------------------------


def freeze_selection(spec: ExperimentSpec, plan: PrivateExperimentPlan, finalist_id: str,
                     *, evidence: dict | None = None) -> FrozenSelection:
    """Bind the finalist, the baseline, the plan and the policy into one frozen selection."""
    baseline = spec.variants[0]
    finalist = next((variant for variant in spec.variants[1:] if variant.id == finalist_id), None)
    if finalist is None:
        raise ValueError(f"{finalist_id!r} is not a candidate variant of this experiment")
    if plan.variant_hashes.get(finalist.id) != finalist.config_hash:
        raise ValueError(f"the plan does not carry the execution hash of variant {finalist.id!r}")
    if plan.variant_hashes.get(baseline.id) != baseline.config_hash:
        raise ValueError(f"the plan does not carry the execution hash of the baseline {baseline.id!r}")
    return FrozenSelection(
        finalist_id=finalist.id,
        finalist_hash=finalist.config_hash,
        baseline_hash=baseline.config_hash,
        plan_hash=plan.plan_hash,
        policy_hash=spec.statistics.policy_hash,
        frozen_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        evidence=dict(evidence or {}),
    )


def _short(value: str) -> str:
    return value[:12]


def verify_frozen(selection: FrozenSelection, candidate: Harness, baseline: Harness,
                  plan: PrivateExperimentPlan) -> list[str]:
    """Every way the objects in hand differ from what was frozen, one sentence each; empty when none."""
    problems: list[str] = []
    actual = candidate.execution_config_hash()
    if actual != selection.finalist_hash:
        problems.append(
            f"finalist hash mismatch: frozen {_short(selection.finalist_hash)}, candidate now {_short(actual)}")
    actual = baseline.execution_config_hash()
    if actual != selection.baseline_hash:
        problems.append(
            f"baseline hash mismatch: frozen {_short(selection.baseline_hash)}, baseline now {_short(actual)}")
    actual = plan.plan_hash
    if actual != selection.plan_hash:
        problems.append(f"plan hash mismatch: frozen {_short(selection.plan_hash)}, plan now {_short(actual)}")
    recorded = plan.variant_hashes.get(selection.finalist_id)
    if recorded != selection.finalist_hash:
        problems.append(
            f"plan variant hash for {selection.finalist_id} is "
            f"{'missing' if recorded is None else _short(recorded)}, frozen finalist hash is "
            f"{_short(selection.finalist_hash)}")
    try:
        actual = StatisticsPolicy(**plan.policy).policy_hash
    except (TypeError, ValidationError) as exc:
        problems.append(f"plan policy is not a valid statistics policy: {exc}")
    else:
        if actual != selection.policy_hash:
            problems.append(
                f"policy hash mismatch: frozen {_short(selection.policy_hash)}, plan policy now {_short(actual)}")
    return problems
