"""Typed contracts for trials, evaluations, decisions and the private plan.

These objects are the results JSON schema. ``to_dict`` writes enum ``.value``
strings and JSON-ready lists; ``from_dict`` accepts those strings, rebuilds
tuples where the type says tuple, and raises ``ValidationError`` on unknown
enum values, missing fields or wrong types. No non-finite float is accepted
or written anywhere.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

from .status import (
    USABLE_EVALUATION,
    USABLE_EXECUTION,
    DecisionStatus,
    EvaluationStatus,
    ExecutionStatus,
    Provenance,
)
from .validation import (
    ValidationError,
    assert_finite_tree,
    json_dumps_strict,
    json_loads_strict,
    require_finite,
    require_int,
)

E = TypeVar("E", bound=Enum)

_NO_VALUE = {Provenance.UNKNOWN, Provenance.NOT_APPLICABLE}


# --- helpers -----------------------------------------------------------------


def _mapping(name: str, data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValidationError(f"{name} must be an object, got {type(data).__name__}")
    return data


def _field(data: dict, key: str, owner: str) -> Any:
    if key not in data:
        raise ValidationError(f"{owner} is missing required field {key!r}")
    return data[key]


def _enum(cls: type[E], name: str, value: Any) -> E:
    if isinstance(value, cls):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string, got {type(value).__name__}")
    try:
        return cls(value)
    except ValueError:
        allowed = ", ".join(member.value for member in cls)
        raise ValidationError(f"{name} has unknown value {value!r}; expected one of: {allowed}") from None


def _str(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string, got {type(value).__name__}")
    return value


def _opt_str(name: str, value: Any) -> str | None:
    return None if value is None else _str(name, value)


def _str_tuple(name: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{name} must be a list, got {type(value).__name__}")
    return tuple(_str(f"{name}[{i}]", item) for i, item in enumerate(value))


def _str_list(name: str, value: Any) -> list[str]:
    return list(_str_tuple(name, value))


def _dict(name: str, value: Any) -> dict:
    return _mapping(name, value)


def _deep_copy_json(name: str, value: dict) -> dict:
    """Detached JSON copy (tuples become lists); rejects non-finite or non-JSON values."""
    assert_finite_tree(value)
    try:
        return json_loads_strict(json_dumps_strict(value))
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(f"{name} must be JSON-serializable: {exc}") from exc


def _list(name: str, value: Any) -> list:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{name} must be a list, got {type(value).__name__}")
    return list(value)


def _str_map(name: str, value: Any) -> dict[str, str]:
    mapping = _mapping(name, value)
    return {_str(f"{name} key", k): _str(f"{name}[{k!r}]", v) for k, v in mapping.items()}


def _tuple_map(name: str, value: Any) -> dict[str, tuple[str, ...]]:
    mapping = _mapping(name, value)
    return {_str(f"{name} key", k): _str_tuple(f"{name}[{k!r}]", v) for k, v in mapping.items()}


def _bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean, got {type(value).__name__}")
    return value


def _opt_finite(name: str, value: Any) -> float | None:
    return None if value is None else require_finite(name, value)


def _opt_int(name: str, value: Any) -> int | None:
    return None if value is None else require_int(name, value)


# --- Measurement -------------------------------------------------------------


@dataclass(frozen=True)
class Measurement:
    value: float | None
    provenance: Provenance

    def __post_init__(self) -> None:
        provenance = _enum(Provenance, "provenance", self.provenance)
        object.__setattr__(self, "provenance", provenance)
        if provenance in _NO_VALUE:
            if self.value is not None:
                raise ValidationError(f"a {provenance.value} measurement must not carry a value")
        else:
            if self.value is None:
                raise ValidationError(f"a {provenance.value} measurement requires a value")
            object.__setattr__(self, "value", require_finite("measurement value", self.value, minimum=0))

    @classmethod
    def unknown(cls) -> "Measurement":
        return cls(None, Provenance.UNKNOWN)

    @classmethod
    def not_applicable(cls) -> "Measurement":
        return cls(None, Provenance.NOT_APPLICABLE)

    @classmethod
    def measured(cls, value: float) -> "Measurement":
        return cls(value, Provenance.MEASURED)

    @classmethod
    def reported(cls, value: float, provenance: Provenance) -> "Measurement":
        return cls(value, provenance)

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "provenance": self.provenance.value}

    @classmethod
    def from_dict(cls, data: Any) -> "Measurement":
        data = _mapping("measurement", data)
        return cls(_field(data, "value", "measurement"), _enum(Provenance, "provenance", _field(data, "provenance", "measurement")))


# --- Trial request -----------------------------------------------------------


@dataclass(frozen=True)
class PublicTrialRequest:
    trial_id: str
    task_description: str
    domain: str
    constraints: tuple[str, ...]
    harness_public: dict
    fixture_hash: str | None
    seed: int
    budget: dict

    def __post_init__(self) -> None:
        object.__setattr__(self, "constraints", _str_tuple("constraints", self.constraints))
        require_int("seed", self.seed)
        assert_finite_tree(self.harness_public)
        assert_finite_tree(self.budget)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "trial_id": self.trial_id,
            "task_description": self.task_description,
            "domain": self.domain,
            "constraints": list(self.constraints),
            "harness_public": self.harness_public,
            "fixture_hash": self.fixture_hash,
            "seed": self.seed,
            "budget": self.budget,
        }
        assert_finite_tree(data)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "PublicTrialRequest":
        data = _mapping("trial request", data)
        owner = "trial request"
        return cls(
            trial_id=_str("trial_id", _field(data, "trial_id", owner)),
            task_description=_str("task_description", _field(data, "task_description", owner)),
            domain=_str("domain", _field(data, "domain", owner)),
            constraints=_str_tuple("constraints", _field(data, "constraints", owner)),
            harness_public=_dict("harness_public", _field(data, "harness_public", owner)),
            fixture_hash=_opt_str("fixture_hash", _field(data, "fixture_hash", owner)),
            seed=require_int("seed", _field(data, "seed", owner)),
            budget=_dict("budget", _field(data, "budget", owner)),
        )


# --- Execution result --------------------------------------------------------


@dataclass
class ExecutionResult:
    trial_id: str
    status: ExecutionStatus
    outputs: dict
    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    tokens: Measurement
    latency_ms: Measurement
    adapter_latency_ms: Measurement
    returned_model: str | None
    artifact_hash: str | None
    error: str | None
    # Set only by the backend for infrastructure failures (spawn error, backend
    # unavailable), never for adapter protocol errors.
    retryable: bool = False

    def __post_init__(self) -> None:
        self.status = _enum(ExecutionStatus, "execution status", self.status)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "trial_id": self.trial_id,
            "status": self.status.value,
            "outputs": self.outputs,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "tokens": self.tokens.to_dict(),
            "latency_ms": self.latency_ms.to_dict(),
            "adapter_latency_ms": self.adapter_latency_ms.to_dict(),
            "returned_model": self.returned_model,
            "artifact_hash": self.artifact_hash,
            "error": self.error,
            "retryable": self.retryable,
        }
        assert_finite_tree(data)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "ExecutionResult":
        data = _mapping("execution result", data)
        owner = "execution result"
        return cls(
            trial_id=_str("trial_id", _field(data, "trial_id", owner)),
            status=_enum(ExecutionStatus, "status", _field(data, "status", owner)),
            outputs=_dict("outputs", _field(data, "outputs", owner)),
            exit_code=_opt_int("exit_code", _field(data, "exit_code", owner)),
            stdout_tail=_str("stdout_tail", _field(data, "stdout_tail", owner)),
            stderr_tail=_str("stderr_tail", _field(data, "stderr_tail", owner)),
            tokens=Measurement.from_dict(_field(data, "tokens", owner)),
            latency_ms=Measurement.from_dict(_field(data, "latency_ms", owner)),
            adapter_latency_ms=Measurement.from_dict(_field(data, "adapter_latency_ms", owner)),
            returned_model=_opt_str("returned_model", _field(data, "returned_model", owner)),
            artifact_hash=_opt_str("artifact_hash", _field(data, "artifact_hash", owner)),
            error=_opt_str("error", _field(data, "error", owner)),
            retryable=_bool("retryable", data.get("retryable", False)),
        )


# --- Evaluation result -------------------------------------------------------


@dataclass
class EvaluationResult:
    trial_id: str
    status: EvaluationStatus
    rubric_hash: str
    artifact_hash: str | None
    checks: list[dict]
    score: float | None
    error: str | None

    def __post_init__(self) -> None:
        self.status = _enum(EvaluationStatus, "evaluation status", self.status)
        usable = self.status in USABLE_EVALUATION
        if usable != (self.score is not None):
            raise ValidationError(
                f"score must be set iff evaluation status is passed or failed "
                f"(status={self.status.value}, score={self.score!r})"
            )
        if self.score is not None:
            self.score = require_finite("score", self.score)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "trial_id": self.trial_id,
            "status": self.status.value,
            "rubric_hash": self.rubric_hash,
            "artifact_hash": self.artifact_hash,
            "checks": self.checks,
            "score": self.score,
            "error": self.error,
        }
        assert_finite_tree(data)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "EvaluationResult":
        data = _mapping("evaluation result", data)
        owner = "evaluation result"
        return cls(
            trial_id=_str("trial_id", _field(data, "trial_id", owner)),
            status=_enum(EvaluationStatus, "status", _field(data, "status", owner)),
            rubric_hash=_str("rubric_hash", _field(data, "rubric_hash", owner)),
            artifact_hash=_opt_str("artifact_hash", _field(data, "artifact_hash", owner)),
            checks=_list("checks", _field(data, "checks", owner)),
            score=_opt_finite("score", _field(data, "score", owner)),
            error=_opt_str("error", _field(data, "error", owner)),
        )


# --- Trial record ------------------------------------------------------------


@dataclass
class TrialRecord:
    trial_id: str
    variant_id: str
    task_id: str
    split: str
    family: str
    repetition: int
    seed: int
    attempt: int
    execution: ExecutionResult
    evaluation: EvaluationResult
    implementation_hash: str

    @property
    def usable(self) -> bool:
        return self.execution.status in USABLE_EXECUTION and self.evaluation.status in USABLE_EVALUATION

    @property
    def objective_score(self) -> float | None:
        return self.evaluation.score

    def to_dict(self) -> dict[str, Any]:
        data = {
            "trial_id": self.trial_id,
            "variant_id": self.variant_id,
            "task_id": self.task_id,
            "split": self.split,
            "family": self.family,
            "repetition": self.repetition,
            "seed": self.seed,
            "attempt": self.attempt,
            "execution": self.execution.to_dict(),
            "evaluation": self.evaluation.to_dict(),
            "implementation_hash": self.implementation_hash,
            # Compatibility keys read by pre-existing report code.
            "case_id": self.task_id,
            "objective_score": self.objective_score,
            "verified": self.evaluation.status in USABLE_EVALUATION,
            "passed": self.evaluation.status == EvaluationStatus.PASSED,
            "error": self.execution.error or self.evaluation.error,
            "tokens": self.execution.tokens.value,
            "latency_ms": self.execution.latency_ms.value,
        }
        assert_finite_tree(data)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "TrialRecord":
        data = _mapping("trial record", data)
        owner = "trial record"
        return cls(
            trial_id=_str("trial_id", _field(data, "trial_id", owner)),
            variant_id=_str("variant_id", _field(data, "variant_id", owner)),
            task_id=_str("task_id", _field(data, "task_id", owner)),
            split=_str("split", _field(data, "split", owner)),
            family=_str("family", _field(data, "family", owner)),
            repetition=require_int("repetition", _field(data, "repetition", owner)),
            seed=require_int("seed", _field(data, "seed", owner)),
            attempt=require_int("attempt", _field(data, "attempt", owner)),
            execution=ExecutionResult.from_dict(_field(data, "execution", owner)),
            evaluation=EvaluationResult.from_dict(_field(data, "evaluation", owner)),
            implementation_hash=_str("implementation_hash", _field(data, "implementation_hash", owner)),
        )


# --- Decision ----------------------------------------------------------------


@dataclass
class ExperimentDecision:
    status: DecisionStatus
    reasons: list[str]
    estimate: float | None
    lower_bound: float | None
    n_units: int
    plan_hash: str
    candidate_id: str | None
    candidate_hash: str | None
    baseline_hash: str
    baseline_id: str
    baseline_version: str
    evidence_ids: list[str]
    pending_requirements: list[str]
    diagnostics: dict

    def __post_init__(self) -> None:
        self.status = _enum(DecisionStatus, "decision status", self.status)
        if self.estimate is not None:
            self.estimate = require_finite("estimate", self.estimate)
        if self.lower_bound is not None:
            self.lower_bound = require_finite("lower_bound", self.lower_bound)
        require_int("n_units", self.n_units, minimum=0)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "status": self.status.value,
            "reasons": list(self.reasons),
            "estimate": self.estimate,
            "lower_bound": self.lower_bound,
            "n_units": self.n_units,
            "plan_hash": self.plan_hash,
            "candidate_id": self.candidate_id,
            "candidate_hash": self.candidate_hash,
            "baseline_hash": self.baseline_hash,
            "baseline_id": self.baseline_id,
            "baseline_version": self.baseline_version,
            "evidence_ids": list(self.evidence_ids),
            "pending_requirements": list(self.pending_requirements),
            "diagnostics": self.diagnostics,
        }
        assert_finite_tree(data)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "ExperimentDecision":
        data = _mapping("experiment decision", data)
        owner = "experiment decision"
        return cls(
            status=_enum(DecisionStatus, "status", _field(data, "status", owner)),
            reasons=_str_list("reasons", _field(data, "reasons", owner)),
            estimate=_opt_finite("estimate", _field(data, "estimate", owner)),
            lower_bound=_opt_finite("lower_bound", _field(data, "lower_bound", owner)),
            n_units=require_int("n_units", _field(data, "n_units", owner), minimum=0),
            plan_hash=_str("plan_hash", _field(data, "plan_hash", owner)),
            candidate_id=_opt_str("candidate_id", _field(data, "candidate_id", owner)),
            candidate_hash=_opt_str("candidate_hash", _field(data, "candidate_hash", owner)),
            baseline_hash=_str("baseline_hash", _field(data, "baseline_hash", owner)),
            baseline_id=_str("baseline_id", _field(data, "baseline_id", owner)),
            baseline_version=_str("baseline_version", _field(data, "baseline_version", owner)),
            evidence_ids=_str_list("evidence_ids", _field(data, "evidence_ids", owner)),
            pending_requirements=_str_list("pending_requirements", _field(data, "pending_requirements", owner)),
            diagnostics=_dict("diagnostics", _field(data, "diagnostics", owner)),
        )


# --- Private plan ------------------------------------------------------------


@dataclass(frozen=True)
class PrivateExperimentPlan:
    experiment_name: str
    mode: str
    family: str
    dataset_id: str
    dataset_version: str
    splits: dict[str, tuple[str, ...]]  # split -> task ids
    families: dict[str, str]  # task id -> family
    regression_groups: dict[str, tuple[str, ...]]  # group -> families
    variant_hashes: dict[str, str]
    rubric_hashes: dict[str, str]
    task_fingerprints: dict[str, str]
    policy: dict
    budget: dict
    repetitions: int
    seed: int
    environment_mode: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "splits", _tuple_map("splits", self.splits))
        object.__setattr__(self, "regression_groups", _tuple_map("regression_groups", self.regression_groups))
        object.__setattr__(self, "families", _str_map("families", self.families))
        object.__setattr__(self, "variant_hashes", _str_map("variant_hashes", self.variant_hashes))
        object.__setattr__(self, "rubric_hashes", _str_map("rubric_hashes", self.rubric_hashes))
        object.__setattr__(self, "task_fingerprints", _str_map("task_fingerprints", self.task_fingerprints))
        require_int("repetitions", self.repetitions)
        require_int("seed", self.seed)
        object.__setattr__(self, "policy", _deep_copy_json("policy", _dict("policy", self.policy)))
        object.__setattr__(self, "budget", _deep_copy_json("budget", _dict("budget", self.budget)))

    def to_dict(self) -> dict[str, Any]:
        data = {
            "experiment_name": self.experiment_name,
            "mode": self.mode,
            "family": self.family,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "splits": {k: list(v) for k, v in self.splits.items()},
            "families": dict(self.families),
            "regression_groups": {k: list(v) for k, v in self.regression_groups.items()},
            "variant_hashes": dict(self.variant_hashes),
            "rubric_hashes": dict(self.rubric_hashes),
            "task_fingerprints": dict(self.task_fingerprints),
            "policy": _deep_copy_json("policy", self.policy),
            "budget": _deep_copy_json("budget", self.budget),
            "repetitions": self.repetitions,
            "seed": self.seed,
            "environment_mode": self.environment_mode,
        }
        assert_finite_tree(data)
        return data

    @property
    def plan_hash(self) -> str:
        text = json_dumps_strict(self.to_dict(), sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Any) -> "PrivateExperimentPlan":
        data = _mapping("experiment plan", data)
        owner = "experiment plan"
        return cls(
            experiment_name=_str("experiment_name", _field(data, "experiment_name", owner)),
            mode=_str("mode", _field(data, "mode", owner)),
            family=_str("family", _field(data, "family", owner)),
            dataset_id=_str("dataset_id", _field(data, "dataset_id", owner)),
            dataset_version=_str("dataset_version", _field(data, "dataset_version", owner)),
            splits=_tuple_map("splits", _field(data, "splits", owner)),
            families=_str_map("families", _field(data, "families", owner)),
            regression_groups=_tuple_map("regression_groups", _field(data, "regression_groups", owner)),
            variant_hashes=_str_map("variant_hashes", _field(data, "variant_hashes", owner)),
            rubric_hashes=_str_map("rubric_hashes", _field(data, "rubric_hashes", owner)),
            task_fingerprints=_str_map("task_fingerprints", _field(data, "task_fingerprints", owner)),
            policy=_dict("policy", _field(data, "policy", owner)),
            budget=_dict("budget", _field(data, "budget", owner)),
            repetitions=require_int("repetitions", _field(data, "repetitions", owner)),
            seed=require_int("seed", _field(data, "seed", owner)),
            environment_mode=_str("environment_mode", _field(data, "environment_mode", owner)),
        )
