"""Experiment manifest, schema version 2, and the mapping from the legacy schema.

A manifest is a contract other code parses. Every number goes through
``validation.require_finite`` / ``require_int``; the bytes are read with
``json_loads_strict`` so ``NaN`` / ``Infinity`` never enter. A manifest without
``schema_version`` (or with ``1``) is *legacy*: it loads as exploratory and can
never become confirmatory evidence unless it is migrated explicitly
(``migrate_manifest``) and given splits and controls.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any

from harness_workbench.core.executor import Task
from harness_workbench.core.harness import ALL_COMPONENTS, Harness, load_harness
from harness_workbench.experiments.grader import GraderBundle, digest_path
from harness_workbench.experiments.profiles import PROFILES
from harness_workbench.experiments.status import Provenance
from harness_workbench.experiments.validation import (
    ValidationError,
    assert_finite_tree,
    json_dumps_strict,
    json_loads_strict,
    require_finite,
    require_int,
)

__all__ = [
    "ALL_COMPONENTS", "SCHEMA_VERSION", "BudgetPolicy", "Case", "CHECK_TYPES", "ControlCase", "Controls",
    "EnvironmentConfig", "ExecutorConfig", "ExperimentSpec", "ManifestError", "ProcessLimitsConfig",
    "Split", "SrtConfig", "StatisticsPolicy", "TaskSpec", "Variant", "digest_path", "migrate_manifest",
]

SCHEMA_VERSION = 2

CHECK_TYPES = frozenset({
    "exact", "contains", "not_contains", "regex", "json_equal",
    "file_exists", "file_equals", "command", "oracle",
})
ENVIRONMENT_MODES = ("trusted_local", "sandboxed", "os_sandboxed")
# Modes whose backend isolates candidate code (a container or an OS sandbox).
ISOLATED_MODES = ("sandboxed", "os_sandboxed")
EXPERIMENT_MODES = ("exploratory", "confirmatory")
_BARE_PYTHON = frozenset({"python", "python3"})
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")

class ManifestError(ValidationError):
    """Raised when an experiment manifest is invalid."""


class Split(str, Enum):
    DEVELOPMENT = "development"
    VALIDATION = "validation"
    REGRESSION = "regression"


# --- small helpers -------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(name: str, value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ValidationError(f"{name} must not be empty")
    return value


def _flag(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be true or false, got {type(value).__name__}")
    return value


def _string_list(name: str, value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        raise ValidationError(f"{name} must be a list of strings, got {type(value).__name__}")
    return tuple(_text(f"{name}[{index}]", item) for index, item in enumerate(value))


def _ratio(name: str, value: Any) -> float | None:
    """A ratio limit: a finite number >= 1, or ``None`` for no limit."""
    if value is None:
        return None
    return require_finite(name, value, minimum=1.0)


def _set(obj: Any, **values: Any) -> None:
    for key, value in values.items():
        object.__setattr__(obj, key, value)


def _is_bare_python(argv0: str) -> bool:
    return argv0 in _BARE_PYTHON


def _argv(name: str, value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or not value:
        raise ValidationError(f"{name} must be a non-empty list of strings")
    argv = tuple(_text(f"{name}[{index}]", item) for index, item in enumerate(value))
    if _is_bare_python(argv[0]):
        raise ValidationError(f"{name}: use {{python}} instead of the bare {argv[0]!r} interpreter name")
    return argv


# --- policies -------------------------------------------------------------------


@dataclass(frozen=True)
class StatisticsPolicy:
    method: str = "cluster_percentile_ci_v1"
    profile: str = "binary_v1"
    alpha: float = 0.05
    practical_margin: float = 0.05
    regression_margin: float = 0.10
    min_independent_units: int = 50
    min_regression_units: int = 80
    bootstrap_iterations: int = 10000
    timeout_is_task_failure: bool = False
    max_attempts: int = 1
    allow_zero_variance: bool = False
    zero_variance_justification: str = ""

    def __post_init__(self) -> None:
        _set(
            self,
            method=_text("method", self.method),
            profile=_text("profile", self.profile),
            alpha=require_finite("alpha", self.alpha, minimum=0.0, maximum=0.5, exclusive_minimum=True),
            practical_margin=require_finite("practical_margin", self.practical_margin, minimum=0.0),
            regression_margin=require_finite("regression_margin", self.regression_margin, minimum=0.0),
            min_independent_units=require_int("min_independent_units", self.min_independent_units, minimum=1),
            min_regression_units=require_int("min_regression_units", self.min_regression_units, minimum=1),
            bootstrap_iterations=require_int(
                "bootstrap_iterations", self.bootstrap_iterations, minimum=200, maximum=100000),
            timeout_is_task_failure=_flag("timeout_is_task_failure", self.timeout_is_task_failure),
            max_attempts=require_int("max_attempts", self.max_attempts, minimum=1, maximum=3),
            allow_zero_variance=_flag("allow_zero_variance", self.allow_zero_variance),
            zero_variance_justification=_text(
                "zero_variance_justification", self.zero_variance_justification, allow_empty=True),
        )
        if self.alpha >= 0.5:
            raise ValidationError(f"alpha must be < 0.5, got {self.alpha!r}")
        if self.allow_zero_variance and not self.zero_variance_justification.strip():
            raise ValidationError("allow_zero_variance requires a non-empty zero_variance_justification")

    @property
    def policy_hash(self) -> str:
        return _sha(json_dumps_strict({f.name: getattr(self, f.name) for f in fields(self)},
                                      sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True)
class BudgetPolicy:
    max_token_ratio: float | None = 1.5
    max_latency_ratio: float | None = 1.5
    absolute_token_limit: int | None = None
    accepted_provenance: tuple[str, ...] = ("measured", "provider_reported", "adapter_reported")

    def __post_init__(self) -> None:
        limit = self.absolute_token_limit
        provenance = _string_list("accepted_provenance", self.accepted_provenance)
        known = {item.value for item in Provenance}
        unknown = sorted(set(provenance) - known)
        if unknown:
            raise ValidationError(f"accepted_provenance has unknown value(s): {unknown}")
        _set(
            self,
            max_token_ratio=_ratio("max_token_ratio", self.max_token_ratio),
            max_latency_ratio=_ratio("max_latency_ratio", self.max_latency_ratio),
            absolute_token_limit=None if limit is None else require_int("absolute_token_limit", limit, minimum=1),
            accepted_provenance=provenance,
        )


@dataclass(frozen=True)
class ProcessLimitsConfig:
    timeout_seconds: float = 300.0
    max_output_bytes: int = 1_000_000
    max_files: int = 2000
    max_total_bytes: int = 50_000_000

    def __post_init__(self) -> None:
        _set(
            self,
            timeout_seconds=require_finite(
                "timeout_seconds", self.timeout_seconds, minimum=0.0, exclusive_minimum=True),
            max_output_bytes=require_int("max_output_bytes", self.max_output_bytes, minimum=1),
            max_files=require_int("max_files", self.max_files, minimum=1),
            max_total_bytes=require_int("max_total_bytes", self.max_total_bytes, minimum=1),
        )


@dataclass(frozen=True)
class SrtConfig:
    """Where Anthropic Sandbox Runtime lives: ``node <cli> --settings <file> -- <argv>``. Absolute paths."""
    node: str
    cli: str
    version: str

    def __post_init__(self) -> None:
        _set(self, node=_text("srt.node", self.node), cli=_text("srt.cli", self.cli),
             version=_text("srt.version", self.version))


@dataclass(frozen=True)
class EnvironmentConfig:
    mode: str
    image: str = "python:3.12-slim"
    image_python: str = "/usr/local/bin/python3"
    solver_env_passthrough: tuple[str, ...] = ()
    limits: ProcessLimitsConfig = field(default_factory=ProcessLimitsConfig)
    trusted_solver_acknowledged: bool = False
    # os_sandboxed only: the runtime, the hosts the solver (never a grader) may reach, and extra read-only
    # paths the solver needs (the Claude Code install).
    srt: SrtConfig | None = None
    egress_allow: tuple[str, ...] = ()
    solver_read_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in ENVIRONMENT_MODES:
            raise ValidationError(f"environment.mode must be one of {list(ENVIRONMENT_MODES)}, got {self.mode!r}")
        passthrough = _string_list("solver_env_passthrough", self.solver_env_passthrough)
        for name in passthrough:
            if not _ENV_NAME.match(name):
                raise ValidationError(f"solver_env_passthrough entry {name!r} is not an environment variable name")
        if not isinstance(self.limits, ProcessLimitsConfig):
            raise ValidationError("limits must be a ProcessLimitsConfig")
        egress = _string_list("egress_allow", self.egress_allow)
        read_paths = _string_list("solver_read_paths", self.solver_read_paths)
        if self.mode == "os_sandboxed":
            if not isinstance(self.srt, SrtConfig):
                raise ValidationError("environment.srt {node, cli, version} is required when mode is os_sandboxed")
        elif self.srt is not None or egress or read_paths:
            raise ValidationError("environment.srt, egress_allow and solver_read_paths apply only to mode os_sandboxed")
        _set(
            self,
            image=_text("image", self.image),
            image_python=_text("image_python", self.image_python),
            solver_env_passthrough=passthrough,
            trusted_solver_acknowledged=_flag("trusted_solver_acknowledged", self.trusted_solver_acknowledged),
            egress_allow=egress,
            solver_read_paths=read_paths,
        )


@dataclass(frozen=True)
class ExecutorConfig:
    type: str = "model"
    argv: tuple[str, ...] = ()
    timeout_seconds: float = 300.0
    model: str = "external-command"
    supports: frozenset[str] = frozenset(ALL_COMPONENTS)

    def __post_init__(self) -> None:
        if self.type not in ("model", "command"):
            raise ValidationError(f"executor.type must be 'model' or 'command', got {self.type!r}")
        argv = _argv("argv", self.argv) if (self.type == "command" or self.argv) else ()
        supports = frozenset(_string_list("supports", self.supports))
        unknown = sorted(supports - set(ALL_COMPONENTS))
        if unknown:
            raise ValidationError(f"supports has unknown component(s): {unknown}")
        _set(
            self,
            argv=argv,
            timeout_seconds=require_finite(
                "timeout_seconds", self.timeout_seconds, minimum=0.0, maximum=3600.0, exclusive_minimum=True),
            model=_text("model", self.model),
            supports=supports,
        )

    def to_config(self) -> dict[str, Any]:
        """The dict view older code reads (``executor_config``)."""
        return {
            "type": self.type, "argv": list(self.argv), "timeout_seconds": self.timeout_seconds,
            "model": self.model, "supports": sorted(self.supports),
        }


# --- tasks ----------------------------------------------------------------------


@dataclass(frozen=True)
class ControlCase:
    id: str
    outputs: dict[str, Any]
    artifact: Path | None = None

    def __post_init__(self) -> None:
        _text("control id", self.id)
        if not isinstance(self.outputs, dict):
            raise ValidationError(f"control {self.id} outputs must be an object")
        assert_finite_tree(self.outputs)


@dataclass(frozen=True)
class Controls:
    positive: ControlCase
    negatives: tuple[ControlCase, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.positive, ControlCase):
            raise ValidationError("controls.positive must be a ControlCase")
        negatives = tuple(self.negatives)
        if not negatives or not all(isinstance(item, ControlCase) for item in negatives):
            raise ValidationError("controls need at least one negative control")
        _set(self, negatives=negatives)


@dataclass(frozen=True)
class TaskSpec:
    id: str
    task: Task
    split: Split | None
    family: str
    checks: tuple[dict[str, Any], ...]
    grader_files: Path | None
    bundle_hash: str
    bundle: GraderBundle
    fixture: Path | None
    fixture_hash: str | None
    content_fingerprint: str
    runs_candidate_code: bool | None
    controls: Controls | None
    critical_check_ids: frozenset[str]


Case = TaskSpec


@dataclass(frozen=True)
class Variant:
    id: str
    harness: Harness
    source: Path
    fingerprint: str
    config_hash: str = ""

    def __post_init__(self) -> None:
        if not self.config_hash:
            _set(self, config_hash=self.harness.execution_config_hash())


def _content_fingerprint(task: Task, fixture_hash: str | None, bundle_hash: str) -> str:
    return _sha(json_dumps_strict(
        [task.description, task.domain, list(task.constraints), fixture_hash, bundle_hash],
        sort_keys=True, separators=(",", ":")))


def _looks_like_grader_file(argv: tuple[str, ...], index: int) -> bool:
    """Heuristic: does this argv entry name a relative script/data file?"""
    entry = argv[index]
    if index < 1 or entry.startswith("-") or _PLACEHOLDER.search(entry):
        return False
    if index >= 2 and argv[index - 1] in ("-c", "-m"):
        return False
    if any(ch.isspace() for ch in entry) or "://" in entry or os.path.isabs(entry):
        return False
    try:
        float(entry)
        return False
    except ValueError:
        pass
    return "." in entry or "/" in entry


def _validate_check(task_id: str, index: int, check: Any, grader_files: Path | None) -> None:
    where = f"task {task_id} check {index + 1}"
    if not isinstance(check, dict):
        raise ManifestError(f"{where} must be an object")
    kind = check.get("type")
    if kind not in CHECK_TYPES:
        raise ManifestError(f"{where}: unsupported acceptance check type: {kind!r}")
    require_finite(f"{where} weight", check.get("weight", 1), minimum=0.0, exclusive_minimum=True)
    if "critical" in check:
        _flag(f"{where} critical", check["critical"])
    if kind not in ("command", "oracle"):
        return
    argv = _argv(f"{where} argv", check.get("argv"))
    if "timeout_seconds" in check:
        require_finite(f"{where} timeout_seconds", check["timeout_seconds"],
                       minimum=0.0, maximum=300.0, exclusive_minimum=True)
    if kind == "command" and "exit_code" in check:
        require_int(f"{where} exit_code", check["exit_code"])
    if kind == "oracle":
        observed = check.get("observed")
        _text(f"{where} observed", observed)
        parts = Path(observed).parts
        if os.path.isabs(observed) or ".." in parts or "\\" in observed:
            raise ManifestError(f"{where} observed must be a relative path without '..': {observed!r}")
        if "expected" not in check:
            raise ManifestError(f"{where} requires an 'expected' value")
        assert_finite_tree(check["expected"])
        compare = check.get("compare", "exact")
        if compare not in ("exact", "json_equal"):
            raise ManifestError(f"{where} compare must be 'exact' or 'json_equal', got {compare!r}")
    for position in range(1, len(argv)):
        if not _looks_like_grader_file(argv, position):
            continue
        entry = argv[position]
        candidate = None if grader_files is None else (grader_files / entry).resolve()
        if candidate is None or not candidate.is_relative_to(grader_files.resolve()) or not candidate.is_file():
            raise ManifestError(f"task {task_id}: grader file missing: {entry}")


def _reject_duplicate_check_ids(task_id: str, checks: tuple[dict[str, Any], ...]) -> None:
    """Check ids name the critical invariants, so two checks of one task may not share an id (explicit or implicit)."""
    seen: set[str] = set()
    for index, check in enumerate(checks):
        identity = str(check.get("id") or f"check-{index + 1}")
        if identity in seen:
            raise ManifestError(f"task {task_id}: duplicate check id {identity!r}: check ids must be unique within a task")
        seen.add(identity)


def _critical_ids(checks: tuple[dict[str, Any], ...]) -> frozenset[str]:
    return frozenset(
        str(check.get("id") or f"check-{index + 1}")
        for index, check in enumerate(checks) if check.get("critical") is True
    )


def _directory(base: Path, value: Any, name: str) -> Path:
    path = (base / _text(name, value)).resolve()
    if not path.is_dir():
        raise ManifestError(f"{name} is not a directory: {path}")
    return path


def _control_case(raw: Any, base: Path, default_id: str, where: str) -> ControlCase:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where} must be an object")
    artifact = raw.get("artifact")
    return ControlCase(
        raw.get("id", default_id),
        raw.get("outputs", {}),
        None if artifact is None else _directory(base, artifact, f"{where} artifact"),
    )


def _controls(raw: Any, base: Path, task_id: str) -> Controls | None:
    if raw is None:
        return None
    where = f"task {task_id} controls"
    if not isinstance(raw, dict):
        raise ManifestError(f"{where} must be an object")
    negatives = raw.get("negatives")
    if not isinstance(negatives, list) or not negatives:
        raise ManifestError(f"{where} need at least one negative control")
    return Controls(
        _control_case(raw.get("positive"), base, "positive", f"{where} positive"),
        tuple(_control_case(item, base, f"negative-{n + 1}", f"{where} negative {n + 1}")
              for n, item in enumerate(negatives)),
    )


def _task_spec(item: Any, base: Path, *, legacy: bool) -> TaskSpec:
    if not isinstance(item, dict):
        raise ManifestError("each task must be an object")
    task_id = _text("task id", item.get("id"))
    where = f"task {task_id}"
    checks_raw = item.get("checks") or []
    if not isinstance(checks_raw, list):
        raise ManifestError(f"{where} checks must be a list")
    checks = tuple(checks_raw)
    if not checks:
        raise ManifestError(f"task {task_id} has no independent acceptance checks")
    grader_files = _directory(base, item["grader_files"], f"{where} grader_files") if item.get("grader_files") else None
    for index, check in enumerate(checks):
        _validate_check(task_id, index, check, grader_files)
    _reject_duplicate_check_ids(task_id, checks)
    fixture = _directory(base, item["fixture"], f"{where} fixture") if item.get("fixture") else None
    constraints = item.get("constraints") or []
    task = Task.from_text(
        _text(f"{where} description", item.get("description")),
        domain=_text(f"{where} domain", item.get("domain", "general")),
        task_id=task_id, constraints=list(_string_list(f"{where} constraints", constraints)),
    )
    fixture_hash = digest_path(fixture) if fixture else None
    bundle = GraderBundle.build(task_id, checks, grader_files)
    bundle_hash = bundle.hash
    split: Split | None = None
    family = task_id
    runs: bool | None = False
    controls: Controls | None = None
    if not legacy:
        raw_split = item.get("split")
        if raw_split is not None:
            try:
                split = Split(raw_split)
            except ValueError:
                raise ManifestError(
                    f"{where} split must be one of {[s.value for s in Split]}, got {raw_split!r}") from None
        family = _text(f"{where} family", item["family"]) if item.get("family") is not None else task_id
        runs = item.get("runs_candidate_code")
        if runs is not None:
            runs = _flag(f"{where} runs_candidate_code", runs)
        controls = _controls(item.get("controls"), base, task_id)
    return TaskSpec(
        task_id, task, split, family, checks, grader_files, bundle_hash, bundle, fixture, fixture_hash,
        _content_fingerprint(task, fixture_hash, bundle_hash), runs, controls, _critical_ids(checks),
    )


# --- legacy mapping ---------------------------------------------------------------

_LEGACY_DEFAULTS = {
    "min_improvement": 0.02, "max_regression": 0.05, "max_cost_ratio": 1.5, "max_latency_ratio": 1.5,
    "confidence": 0.95, "bootstrap_iterations": 2000,
}


def _legacy_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Validate the v1 numeric keys and return them mapped onto the v2 vocabulary."""
    practical = require_finite("min_improvement", data.get("min_improvement", 0.02), minimum=0.0)
    regression = require_finite("max_regression", data.get("max_regression", 0.05), minimum=0.0)
    confidence = require_finite(
        "confidence", data.get("confidence", 0.95), minimum=0.5, maximum=1.0, exclusive_minimum=True)
    if confidence >= 1.0:
        raise ValidationError(f"confidence must be < 1, got {confidence!r}")
    iterations = require_int("bootstrap_iterations", data.get("bootstrap_iterations", 2000),
                             minimum=200, maximum=100000)
    return {
        "statistics": {
            "practical_margin": practical, "regression_margin": regression,
            "alpha": round(1.0 - confidence, 12), "bootstrap_iterations": iterations,
        },
        "budget": {
            "max_token_ratio": _ratio("max_cost_ratio", data.get("max_cost_ratio", 1.5)),
            "max_latency_ratio": _ratio("max_latency_ratio", data.get("max_latency_ratio", 1.5)),
        },
    }


# --- the experiment ---------------------------------------------------------------


def _policy(cls: type, raw: Any, key: str, **extra: Any) -> Any:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{key} must be an object")
    allowed = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ManifestError(f"{key} has unknown field(s): {unknown}")
    try:
        return cls(**{**raw, **extra})
    except ManifestError:
        raise
    except ValidationError as exc:
        raise ManifestError(f"{key}: {exc}") from exc


def _environment(raw: Any) -> EnvironmentConfig:
    if not isinstance(raw, dict) or "mode" not in raw:
        raise ManifestError(
            "environment.mode is required in schema v2 ('trusted_local' is opt-in, 'sandboxed' and 'os_sandboxed' are isolated)")
    values = dict(raw)
    values["limits"] = _policy(ProcessLimitsConfig, values.get("limits"), "environment.limits")
    if values.get("srt") is not None:
        values["srt"] = _policy(SrtConfig, values["srt"], "environment.srt")
    for name in ("egress_allow", "solver_read_paths"):
        if name in values:
            values[name] = _string_list(f"environment.{name}", values[name])
    if "solver_env_passthrough" in values:
        values["solver_env_passthrough"] = _string_list(
            "environment.solver_env_passthrough", values["solver_env_passthrough"])
    return _policy(EnvironmentConfig, values, "environment")


def _executor(raw: Any) -> ExecutorConfig:
    return _policy(ExecutorConfig, raw, "executor")


def _words(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"\w+", text.lower()))


@dataclass(frozen=True)
class ExperimentSpec:
    schema_version: int
    name: str
    mode: str
    family: str
    dataset_id: str
    dataset_version: str
    variants: tuple[Variant, ...]
    tasks: tuple[TaskSpec, ...]
    repetitions: int
    seed: int
    statistics: StatisticsPolicy
    budget: BudgetPolicy
    environment: EnvironmentConfig
    executor: ExecutorConfig
    source: Path | None
    source_hash: str
    legacy: bool
    warnings: tuple[str, ...]
    duplicate_review: tuple[str, ...] = ()
    finalists: int | None = None

    def __post_init__(self) -> None:
        # Runs for every construction path (JSON, ``dataclasses.replace``, direct calls), so an
        # object built through the Python API is validated like a parsed manifest.
        try:
            _set(
                self,
                schema_version=require_int("schema_version", self.schema_version),
                repetitions=require_int("repetitions", self.repetitions, minimum=1, maximum=1000),
                seed=require_int("seed", self.seed, minimum=0),
            )
            if self.finalists is not None:
                _set(self, finalists=require_int("finalists", self.finalists))
        except ManifestError:
            raise
        except ValidationError as exc:
            raise ManifestError(str(exc)) from exc
        if self.schema_version not in (1, SCHEMA_VERSION):
            raise ManifestError(
                f"unsupported schema_version {self.schema_version}; this build reads 1 and {SCHEMA_VERSION}")
        if self.mode not in EXPERIMENT_MODES:
            raise ManifestError(f"mode must be one of {list(EXPERIMENT_MODES)}, got {self.mode!r}")
        for name, kind in (("statistics", StatisticsPolicy), ("budget", BudgetPolicy),
                           ("environment", EnvironmentConfig), ("executor", ExecutorConfig)):
            if not isinstance(getattr(self, name), kind):
                raise ManifestError(f"{name} must be a {kind.__name__}, got {type(getattr(self, name)).__name__}")
        if self.mode == "confirmatory":
            problems = self.validate_confirmatory()
            if problems:
                raise ManifestError("confirmatory manifest is not valid: " + "; ".join(problems))

    # -- loading --

    @classmethod
    def load(cls, path: str | Path) -> "ExperimentSpec":
        source = Path(path).resolve()
        raw = source.read_bytes()
        try:
            data = json_loads_strict(raw.decode("utf-8"))
        except ManifestError:
            raise
        except ValidationError as exc:
            raise ManifestError(f"{source.name}: {exc}") from exc
        except (UnicodeDecodeError, ValueError) as exc:
            raise ManifestError(f"{source.name} is not valid JSON: {exc}") from exc
        return cls.from_dict(data, source.parent, source=source, source_hash=hashlib.sha256(raw).hexdigest())

    @classmethod
    def from_dict(
        cls,
        data: Any,
        directory: str | Path,
        *,
        source: Path | None = None,
        source_hash: str | None = None,
    ) -> "ExperimentSpec":
        if not isinstance(data, dict):
            raise ManifestError("experiment manifest must be a JSON object")
        try:
            assert_finite_tree(data)
            spec = cls._build(data, Path(directory).resolve(), source, source_hash)
        except ManifestError:
            raise
        except ValidationError as exc:
            raise ManifestError(str(exc)) from exc
        return spec

    @classmethod
    def _build(cls, data: dict[str, Any], base: Path, source: Path | None, source_hash: str | None) -> "ExperimentSpec":
        version = data.get("schema_version", 1)
        version = require_int("schema_version", version)
        if version not in (1, SCHEMA_VERSION):
            raise ManifestError(f"unsupported schema_version {version}; this build reads 1 and {SCHEMA_VERSION}")
        legacy = version == 1
        warnings: list[str] = []
        name = str(data["name"]) if data.get("name") else (source.stem if source else "experiment")
        if legacy:
            mapped = _legacy_settings(data)
            statistics = _policy(StatisticsPolicy, None, "statistics", **mapped["statistics"])
            budget = _policy(BudgetPolicy, None, "budget", **mapped["budget"])
            environment = EnvironmentConfig(mode="trusted_local")
            warnings.append(
                "schema v1 manifest: environment.mode defaults to trusted_local (solver code and graders "
                "share the host); run 'harness-workbench migrate-manifest' to adopt schema v2")
            if "min_samples" in data:
                warnings.append(
                    "min_samples is dropped: every task needs all of its repetitions (min_samples = repetitions)")
            mode = "exploratory"
            experiment_family, dataset_id, dataset_version = name, "", ""
            duplicate_review: tuple[str, ...] = ()
            finalists = None
        else:
            statistics = _policy(StatisticsPolicy, data.get("statistics"), "statistics")
            budget = _policy(BudgetPolicy, data.get("budget"), "budget")
            environment = _environment(data.get("environment"))
            mode = data.get("mode", "exploratory")
            if mode not in EXPERIMENT_MODES:
                raise ManifestError(f"mode must be one of {list(EXPERIMENT_MODES)}, got {mode!r}")
            experiment_family = _text("family", data["family"]) if data.get("family") is not None else name
            dataset = data.get("dataset") or {}
            if not isinstance(dataset, dict):
                raise ManifestError("dataset must be an object with id and version")
            dataset_id = _text("dataset.id", dataset.get("id", ""), allow_empty=True)
            dataset_version = _text("dataset.version", str(dataset.get("version", "")), allow_empty=True)
            duplicate_review = _string_list("duplicate_review", data.get("duplicate_review") or [])
            for pair in duplicate_review:
                if pair.count(":") != 1 or not all(pair.split(":")):
                    raise ManifestError(f"duplicate_review entry {pair!r} must look like 'idA:idB'")
            finalists = require_int("finalists", data["finalists"]) if "finalists" in data else None
        executor = _executor(data.get("executor"))
        repetitions = require_int("repetitions", data.get("repetitions", 5), minimum=1, maximum=1000)
        seed = require_int("seed", data.get("seed", 42), minimum=0)

        if "baseline" not in data:
            raise ManifestError("experiment manifest requires a 'baseline' harness path")
        variants: list[Variant] = []
        names: set[str] = set()
        entries = [{"id": "baseline", "harness": data["baseline"]}, *(data.get("variants") or [])]
        for entry in entries:
            if not isinstance(entry, dict):
                raise ManifestError("each variant must be an object")
            identity = entry.get("id")
            if not isinstance(identity, str) or not identity or identity in names or "/" in identity or "\\" in identity:
                raise ManifestError(f"invalid or duplicate variant id: {identity}")
            names.add(identity)
            filename = (base / _text(f"variant {identity} harness", entry.get("harness"))).resolve()
            try:
                harness = load_harness(filename)
            except ManifestError:
                raise
            except ValueError as exc:
                raise ManifestError(f"cannot load harness {filename}: {exc}") from exc
            variants.append(Variant(identity, harness, filename, digest_path(filename),
                                    harness.execution_config_hash()))
        if len(variants) < 2:
            raise ManifestError("at least one candidate variant is required")

        tasks: list[TaskSpec] = []
        ids: set[str] = set()
        raw_tasks = data.get("tasks") or []
        if not isinstance(raw_tasks, list):
            raise ManifestError("tasks must be a list")
        for item in raw_tasks:
            task = _task_spec(item, base, legacy=legacy)
            if task.id in ids:
                raise ManifestError(f"invalid or duplicate task id: {task.id}")
            ids.add(task.id)
            tasks.append(task)
        if not tasks:
            raise ManifestError("at least one task is required")
        return cls(
            schema_version=version, name=name, mode=mode, family=experiment_family,
            dataset_id=dataset_id, dataset_version=dataset_version,
            variants=tuple(variants), tasks=tuple(tasks), repetitions=repetitions, seed=seed,
            statistics=statistics, budget=budget, environment=environment, executor=executor,
            source=source,
            source_hash=source_hash if source_hash is not None else _sha(json_dumps_strict(data, sort_keys=True)),
            legacy=legacy, warnings=tuple(warnings), duplicate_review=duplicate_review, finalists=finalists,
        )

    # -- views --

    def tasks_in(self, split: Split | str) -> tuple[TaskSpec, ...]:
        wanted = Split(split)
        return tuple(task for task in self.tasks if task.split == wanted)

    @property
    def cases(self) -> tuple[TaskSpec, ...]:
        return self.tasks

    @property
    def executor_config(self) -> dict[str, Any]:
        return self.executor.to_config()

    @property
    def min_improvement(self) -> float:
        return self.statistics.practical_margin

    @property
    def max_regression(self) -> float:
        return self.statistics.regression_margin

    @property
    def max_cost_ratio(self) -> float:
        ratio = self.budget.max_token_ratio
        return float("inf") if ratio is None else ratio

    @property
    def max_latency_ratio(self) -> float:
        ratio = self.budget.max_latency_ratio
        return float("inf") if ratio is None else ratio

    @property
    def confidence(self) -> float:
        return round(1.0 - self.statistics.alpha, 12)

    @property
    def bootstrap_iterations(self) -> int:
        return self.statistics.bootstrap_iterations

    @property
    def min_samples(self) -> int:
        return self.repetitions

    # -- confirmatory validation --

    def validate_confirmatory(self) -> list[str]:
        """Every reason this experiment cannot be confirmatory, one sentence each."""
        problems: list[str] = []
        if self.mode != "confirmatory":
            problems.append(f"mode must be confirmatory, got {self.mode}")
        baseline = self.variants[0] if self.variants else None
        candidates = self.variants[1:]
        if len(self.variants) < 2:
            problems.append("at least one candidate variant besides the baseline is required (len(variants) >= 2)")
        by_split = {split: self.tasks_in(split) for split in Split}
        if len(candidates) > 1 and not by_split[Split.DEVELOPMENT]:
            problems.append("the development split is empty but more than one candidate is compared")
        for split in (Split.VALIDATION, Split.REGRESSION):
            if not by_split[split]:
                problems.append(f"the {split.value} split has no tasks")
        for task in self.tasks:
            if task.split is None:
                problems.append(f"task {task.id} has no split")
            elif task.split in (Split.VALIDATION, Split.REGRESSION) and task.controls is None:
                problems.append(f"task {task.id} ({task.split.value}) has no controls")
            if task.runs_candidate_code is None:
                problems.append(f"task {task.id} must state runs_candidate_code (true or false)")
            elif task.runs_candidate_code:
                if self.environment.mode not in ISOLATED_MODES:
                    problems.append(
                        f"task {task.id} runs candidate code, which requires environment.mode sandboxed "
                        f"or os_sandboxed (got {self.environment.mode})")
                if not any(check.get("type") == "oracle" for check in task.checks):
                    problems.append(f"task {task.id} runs candidate code and needs at least one oracle check")
            for index, check in enumerate(task.checks):
                argv = check.get("argv")
                if (check.get("type") in ("command", "oracle") and isinstance(argv, list) and argv
                        and argv[0] == "{python}" and (len(argv) < 2 or argv[1] != "-I")):
                    problems.append(
                        f"task {task.id} check {check.get('id') or f'check-{index + 1}'} runs {{python}} "
                        "without -I as argv[1]; isolated mode is required")
        if self.environment.mode == "trusted_local" and not self.environment.trusted_solver_acknowledged:
            problems.append(
                "trusted_local lets the solver read host files including grader files; "
                "acknowledge or use sandboxed")
        by_fingerprint: dict[str, list[TaskSpec]] = {}
        for task in self.tasks:
            by_fingerprint.setdefault(task.content_fingerprint, []).append(task)
        for group in by_fingerprint.values():
            if len({task.split for task in group}) > 1:
                problems.append("tasks with the same content appear in different splits: "
                                + ", ".join(task.id for task in group))
        splits_by_family: dict[str, list[TaskSpec]] = {}
        for task in self.tasks:
            if task.split is not None:
                splits_by_family.setdefault(task.family, []).append(task)
        for family, members in splits_by_family.items():
            spanned = sorted({task.split.value for task in members})
            if len(spanned) > 1:
                problems.append(
                    f"family {family} appears in more than one split ({', '.join(spanned)}): "
                    + ", ".join(task.id for task in members))
        reviewed = set()
        for pair in self.duplicate_review:
            left, right = pair.split(":")
            reviewed.update({(left, right), (right, left)})
        words = {task.id: _words(task.task.description) for task in self.tasks}
        for index, first in enumerate(self.tasks):
            for second in self.tasks[index + 1:]:
                if ((first.family == second.family and first.split == second.split)
                        or (first.id, second.id) in reviewed):
                    continue
                a, b = words[first.id], words[second.id]
                if a and b and len(a & b) / len(a | b) >= 0.8:
                    problems.append(
                        f"near-duplicate descriptions between families: {first.id}:{second.id} "
                        "(list the pair in duplicate_review once reviewed)")
        profile = PROFILES.get(self.statistics.profile)
        if profile is None:
            problems.append(f"statistics.profile {self.statistics.profile!r} is not a known profile")
        else:
            # The sampling plan may exceed the profile but never fall below it; the decision rule itself
            # (method, error rate, margins) is part of what the profile published and must match exactly.
            floors = (
                ("repetitions", self.repetitions, profile.repetitions),
                ("min_independent_units", self.statistics.min_independent_units, profile.min_independent_units),
                ("min_regression_units", self.statistics.min_regression_units, profile.min_regression_units),
                ("bootstrap_iterations", self.statistics.bootstrap_iterations, profile.bootstrap_iterations),
            )
            for key, value, floor in floors:
                if value < floor:
                    problems.append(f"{key} {value} is below the {profile.id} profile minimum {floor}")
            exact = (
                ("method", self.statistics.method, profile.method),
                ("regression_margin", self.statistics.regression_margin, profile.regression_margin),
                ("alpha", self.statistics.alpha, profile.alpha),
                ("practical_margin", self.statistics.practical_margin, profile.practical_margin),
            )
            for key, value, published in exact:
                if value != published:
                    problems.append(f"{key} {value!r} differs from the {profile.id} profile value {published!r}")
        if baseline is not None:
            for variant in candidates:
                if variant.config_hash == baseline.config_hash:
                    problems.append(
                        f"candidate {variant.id} has the same execution configuration as the baseline; "
                        "there is nothing to compare")
        if self.finalists is not None and self.finalists != 1:
            problems.append(f"finalists must be 1 (multiple confirmatory finalists are not supported), got {self.finalists}")
        return problems


# --- migration --------------------------------------------------------------------


def migrate_manifest(data: dict[str, Any], *, splits: dict[str, str] | None = None) -> dict[str, Any]:
    """Return a schema v2 dict for a v1 manifest dict. The input is not modified.

    The result is always ``exploratory``: splits can be supplied, but controls,
    ``runs_candidate_code`` and the environment decision need an author.
    """
    if not isinstance(data, dict):
        raise ManifestError("experiment manifest must be a JSON object")
    version = require_int("schema_version", data.get("schema_version", 1))
    if version != 1:
        raise ManifestError(f"only schema v1 manifests can be migrated, got schema_version {version}")
    try:
        assert_finite_tree(data)
        mapped = _legacy_settings(data)
    except ManifestError:
        raise
    except ValidationError as exc:
        raise ManifestError(str(exc)) from exc
    tasks = [dict(task) for task in (data.get("tasks") or [])]
    ids = [str(task.get("id")) for task in tasks]
    splits = dict(splits or {})
    unknown = sorted(set(splits) - set(ids))
    if unknown:
        raise ManifestError(f"splits name unknown task id(s): {unknown}")
    valid = {split.value for split in Split}
    for task_id, value in splits.items():
        if value not in valid:
            raise ManifestError(f"split for task {task_id} must be one of {sorted(valid)}, got {value!r}")
    notes: list[str] = []

    def placeholder_python(owner: dict[str, Any], where: str) -> None:
        argv = owner.get("argv")
        if isinstance(argv, list) and argv and isinstance(argv[0], str) and _is_bare_python(argv[0]):
            owner["argv"] = ["{python}", *argv[1:]]
            notes.append(f"{where}: bare {argv[0]!r} was replaced by the {{python}} placeholder")

    if "min_samples" in data:
        notes.append("min_samples was dropped: every task needs all of its repetitions")
    for task in tasks:
        task["checks"] = [dict(check) if isinstance(check, dict) else check for check in task.get("checks") or []]
        for index, check in enumerate(task["checks"]):
            if isinstance(check, dict) and check.get("type") in ("command", "oracle"):
                placeholder_python(check, f"task {task.get('id')} check {index + 1}")
        task["split"] = splits.get(str(task.get("id")))
        task.setdefault("family", task.get("id"))
        task.setdefault("runs_candidate_code", None)
    missing_splits = [str(task.get("id")) for task in tasks if task["split"] is None]
    if missing_splits:
        notes.append("tasks without a split: " + ", ".join(missing_splits))
    present = {task["split"] for task in tasks if task["split"] is not None}
    absent = [split.value for split in Split if split.value not in present]
    if absent:
        notes.append("no task assigned to split(s): " + ", ".join(absent))
    notes.append("controls are missing for every task (validation and regression tasks need a positive "
                 "and at least one negative control)")
    notes.append("runs_candidate_code is unset for every task and must be stated for confirmatory use")
    notes.append("environment.mode was set to trusted_local; confirmatory use needs trusted_solver_acknowledged "
                 "or a sandboxed environment")
    notes.append("mode is exploratory until splits, controls and the statistics profile are in place")
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    if data.get("name"):
        out["name"] = data["name"]
        out["family"] = data["name"]
    out.update({
        "mode": "exploratory",
        "dataset": {"id": "", "version": ""},
        "baseline": data.get("baseline"),
        "variants": [dict(item) if isinstance(item, dict) else item for item in (data.get("variants") or [])],
        "tasks": tasks,
        "repetitions": data.get("repetitions", 5),
        "seed": data.get("seed", 42),
        "statistics": mapped["statistics"],
        "budget": mapped["budget"],
        "environment": {"mode": "trusted_local"},
    })
    if data.get("executor") is not None:
        out["executor"] = dict(data["executor"]) if isinstance(data["executor"], dict) else data["executor"]
        if isinstance(out["executor"], dict):
            placeholder_python(out["executor"], "executor")
    out["_migration_notes"] = notes
    return out
