"""Repeatable, paired harness experiments with independent verification.

Runs share a task/fixture/seed, but NEVER reuse a model session or workspace.
This is not deterministic replay of LLM sampling. Results record controls and
uncertainty; only independently verified outcomes can qualify for promotion.
"""
from __future__ import annotations

import dataclasses
import math
import os
import random
import shutil
import statistics
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_workbench.core.executor import BehavioralReport
from harness_workbench.experiments.backends import ExecutionBackend, ProcessLimits, require_backend
from harness_workbench.experiments.contracts import (
    TIMEOUT_POLICY_CHECK_ID, EvaluationResult, ExecutionResult, ExperimentDecision, Measurement,
    PublicTrialRequest, TrialRecord,
)
# Case, ExperimentSpec, TaskSpec and Variant moved to manifest.py; core re-exports them.
from harness_workbench.experiments.manifest import Case, ExperimentSpec, Split, TaskSpec, Variant  # noqa: F401
from harness_workbench.experiments.grader import (
    SnapshotLimits, _remove_tree, digest_path as _digest, evaluate_bundle, run_controls,
)
from harness_workbench.experiments.protocol import (
    FrozenSelection, build_plan, freeze_selection, holdout_fingerprints, select_finalist, verify_frozen,
)
from harness_workbench.experiments.provenance import (
    adapter_supports, detect_drift, implementation_hash, provenance_record, returned_model_status,
    unsupported_components,
)
from harness_workbench.experiments.stats import decide as default_decide
from harness_workbench.experiments.status import (
    USABLE_EVALUATION, USABLE_EXECUTION, DecisionStatus, EvaluationStatus, ExecutionStatus, Provenance,
)
from harness_workbench.experiments.validation import ValidationError, json_dumps_strict
from harness_workbench.experiments.report import RESULTS_SCHEMA_VERSION, render_html


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RunContext:
    experiment_id: str
    run_id: str
    case_id: str
    variant_id: str
    repetition: int
    seed: int
    workspace: Path | None


# Deprecated alias: the persisted ``runs[]`` entry is now a TrialRecord.
RunRecord = TrialRecord

# Where git_state looks: the checkout the package was imported from.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

_LEGACY_USAGE_PROVENANCE = frozenset({"estimated", "provider_reported", "adapter_reported"})


def _failed_execution(trial_id: str, status: ExecutionStatus, error: str) -> ExecutionResult:
    """An execution that produced nothing usable; never retryable here (retries are the backend's call)."""
    return ExecutionResult(
        trial_id=trial_id, status=status, outputs={}, exit_code=None, stdout_tail="", stderr_tail="",
        tokens=Measurement.unknown(), latency_ms=Measurement.not_applicable(),
        adapter_latency_ms=Measurement.not_applicable(), returned_model=None, artifact_hash=None,
        error=error, retryable=False,
    )


def _legacy_execution(executor: Any, variant: Variant, task: TaskSpec, request: PublicTrialRequest,
                      workspace: Path, *, repetition: int = 0, experiment_id: str = "") -> ExecutionResult:
    """Run an executor that predates ``execute_trial`` and wrap its report as an ExecutionResult.

    No supervised process exists here, so latency is not applicable and token counts carry only
    the provenance the executor declares in ``usage_provenance``.
    """
    try:
        execute_case = getattr(executor, "execute_case", None)
        if callable(execute_case):
            context = RunContext(experiment_id, request.trial_id, task.id, variant.id, repetition,
                                 request.seed, workspace)
            report = execute_case(variant.harness, task.task, context)
        else:
            # Existing Executor contract. No workspace, seed or tool-loop guarantees.
            report = executor.execute(variant.harness, task.task)
        if not isinstance(report, BehavioralReport):
            raise TypeError("executor must return a BehavioralReport")
    except Exception as exc:  # An individual agent failure must not abort the experiment.
        return _failed_execution(request.trial_id, ExecutionStatus.EXECUTION_ERROR, f"{type(exc).__name__}: {exc}")
    if ExecutionStatus.RESOURCE_LIMIT.value in report.failures:
        # The command adapter's legacy wrapper reports a bounded RESOURCE_LIMIT trial as a report that
        # names the status in `failures`. It is not a task outcome, so it must not become a scored run.
        return _failed_execution(request.trial_id, ExecutionStatus.RESOURCE_LIMIT,
                                 f"resource limit reported by the legacy adapter: {report.notes or 'no detail'}")
    declared = getattr(executor, "usage_provenance", None)
    tokens = Measurement.unknown()
    if declared in _LEGACY_USAGE_PROVENANCE:
        try:
            tokens = Measurement.reported(report.cost_tokens, Provenance(declared))
        except ValidationError:
            tokens = Measurement.unknown()  # a negative or non-finite count is not evidence
    returned = report.returned_model if isinstance(report.returned_model, str) and report.returned_model else None
    return ExecutionResult(
        trial_id=request.trial_id, status=ExecutionStatus.COMPLETED, outputs=dict(report.outputs),
        exit_code=None, stdout_tail="", stderr_tail="", tokens=tokens,
        latency_ms=Measurement.not_applicable(), adapter_latency_ms=Measurement.unknown(),
        returned_model=returned, artifact_hash=None, error=None, retryable=False,
    )


def _is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _effective_limits(limits: ProcessLimits, executor: Any) -> ProcessLimits:
    """The stricter of the environment's limits and whatever the executor declares for itself.

    An executor may declare a ``timeout_seconds`` cap and/or its own ``ProcessLimits``; both only
    tighten the environment configuration, never loosen it. A malformed declaration is ignored.
    """
    effective = limits
    own = getattr(executor, "limits", None)
    if isinstance(own, ProcessLimits):
        effective = ProcessLimits(
            timeout_seconds=min(effective.timeout_seconds, own.timeout_seconds),
            max_output_bytes=min(effective.max_output_bytes, own.max_output_bytes),
            max_files=min(effective.max_files, own.max_files),
            max_total_bytes=min(effective.max_total_bytes, own.max_total_bytes),
        )
    cap = getattr(executor, "timeout_seconds", None)
    if _is_positive_number(cap):
        effective = dataclasses.replace(effective, timeout_seconds=min(effective.timeout_seconds, cap))
    return effective


def _not_evaluated(trial_id: str, task: TaskSpec, status: ExecutionStatus) -> EvaluationResult:
    return EvaluationResult(
        trial_id, EvaluationStatus.NOT_EVALUATED, task.bundle.hash, None, [], None,
        f"not evaluated: execution status {status.value}",
    )


def _counted_timeout(evaluation: EvaluationResult) -> EvaluationResult:
    """A timeout counted under the declared policy is a failure whatever its partial outputs graded as.

    It keeps its place in the denominator (a scored FAILED) but can never read as a pass. An evaluator
    error stays an error: it is not a task failure.
    """
    if evaluation.status not in USABLE_EVALUATION:
        return evaluation
    marker = {"id": TIMEOUT_POLICY_CHECK_ID, "type": "policy", "weight": 1.0, "status": "failed",
              "passed": False, "detail": "timed out under declared policy"}
    return dataclasses.replace(evaluation, status=EvaluationStatus.FAILED, score=0.0,
                               checks=[*evaluation.checks, marker])


def run_trial(
    spec: ExperimentSpec,
    variant: Variant,
    task: TaskSpec,
    *,
    repetition: int,
    attempt: int,
    executor: Any,
    backend: ExecutionBackend,
    limits: ProcessLimits,
    experiment_id: str,
    implementation_hash: str,
) -> TrialRecord:
    """One trial: public request, fresh workspace, execution, snapshot and out-of-workspace evaluation.

    The executor sees only the public request. Anything that is not a usable execution is
    ``NOT_EVALUATED``, so no score exists for it.
    """
    trial_id = uuid.uuid4().hex
    solver_limits = _effective_limits(limits, executor)
    request = PublicTrialRequest(
        trial_id, task.task.description, task.task.domain, tuple(task.task.constraints),
        variant.harness.public_components(), task.fixture_hash, spec.seed + repetition,
        {"timeout_seconds": solver_limits.timeout_seconds, "max_output_bytes": solver_limits.max_output_bytes},
    )
    temp = Path(tempfile.mkdtemp(prefix="harness-experiment-"))
    try:
        workspace = temp / "workspace"
        execution = _prepare_workspace(task, workspace, trial_id)
        if execution is None:
            execution = _execute(executor, variant, task, request, workspace, backend, solver_limits,
                                 repetition, experiment_id)
        status = execution.status
        timeout_counts = status is ExecutionStatus.TIMEOUT and spec.statistics.timeout_is_task_failure
        if status in USABLE_EXECUTION or timeout_counts:
            try:
                evaluation = evaluate_bundle(
                    task.bundle, execution.outputs, workspace, backend=backend, limits=limits,
                    snapshot_limits=SnapshotLimits(limits.max_files, limits.max_total_bytes), trial_id=trial_id,
                )
            except Exception as exc:  # evaluator failure, never a task failure
                evaluation = EvaluationResult(
                    trial_id, EvaluationStatus.EVALUATION_ERROR, task.bundle.hash, None, [], None,
                    f"evaluation raised {type(exc).__name__}: {exc}",
                )
            if timeout_counts:
                status = ExecutionStatus.TASK_FAILURE  # the declared policy; `error` stays on the record
                evaluation = _counted_timeout(evaluation)
        else:
            evaluation = _not_evaluated(trial_id, task, status)
        execution = dataclasses.replace(execution, status=status, artifact_hash=evaluation.artifact_hash)
    finally:
        _remove_tree(temp)
    return TrialRecord(
        trial_id, variant.id, task.id, task.split.value if task.split else "unsplit", task.family,
        repetition, spec.seed + repetition, attempt, execution, evaluation, implementation_hash,
    )


def _prepare_workspace(task: TaskSpec, workspace: Path, trial_id: str) -> ExecutionResult | None:
    """Create the trial workspace; an ExecutionResult only when that failed."""
    try:
        if task.fixture is None:
            workspace.mkdir(parents=True)
            return None
        # symlinks=True keeps a link as a link: the copy never reads through it to a file outside the
        # fixture. The evaluator's snapshot refuses a symlinked workspace, so it can never score.
        shutil.copytree(task.fixture, workspace, symlinks=True)
        if _digest(workspace) != task.fixture_hash:
            shutil.rmtree(workspace, ignore_errors=True)
            return _failed_execution(trial_id, ExecutionStatus.EXECUTION_ERROR,
                                     "source fixture changed during experiment")
    except OSError as exc:
        return _failed_execution(trial_id, ExecutionStatus.EXECUTION_ERROR,
                                 f"fixture could not be copied: {type(exc).__name__}: {exc}")
    return None


def _execute(executor: Any, variant: Variant, task: TaskSpec, request: PublicTrialRequest, workspace: Path,
             backend: ExecutionBackend, limits: ProcessLimits, repetition: int, experiment_id: str) -> ExecutionResult:
    execute_trial = getattr(executor, "execute_trial", None)
    if not callable(execute_trial):
        return _legacy_execution(executor, variant, task, request, workspace,
                                 repetition=repetition, experiment_id=experiment_id)
    try:
        result = execute_trial(request, workspace=workspace, backend=backend, limits=limits)
        if not isinstance(result, ExecutionResult):
            raise TypeError("executor.execute_trial must return an ExecutionResult")
    except Exception as exc:  # An individual agent failure must not abort the experiment.
        return _failed_execution(request.trial_id, ExecutionStatus.EXECUTION_ERROR, f"{type(exc).__name__}: {exc}")
    return result


def _mean(items: list[float]) -> float:
    return statistics.fmean(items) if items else 0.0


def _bootstrap_interval(pairs: list[tuple[str, float]], seed: int, confidence: float, iterations: int) -> tuple[float, float]:
    """Hierarchical bootstrap: resample tasks, then within-task repetitions."""
    buckets: dict[str, list[float]] = {}
    for task_id, delta in pairs:
        buckets.setdefault(task_id, []).append(delta)
    keys = sorted(buckets)
    if not keys:
        return (0.0, 0.0)
    rng = random.Random(seed)
    results: list[float] = []
    for _ in range(iterations):
        sample = []
        for _ in keys:
            group = buckets[keys[rng.randrange(len(keys))]]
            sample.extend(group[rng.randrange(len(group))] for _ in group)
        results.append(_mean(sample))
    results.sort()
    tail = (1 - confidence) / 2
    low = min(len(results) - 1, math.floor(tail * len(results)))
    high = min(len(results) - 1, math.ceil((1 - tail) * len(results)) - 1)
    return (round(results[low], 6), round(results[high], 6))


def _mean_or_none(items: list[float], digits: int) -> float | None:
    return round(statistics.fmean(items), digits) if items else None


def _summary(rows: list[TrialRecord]) -> dict[str, Any]:
    """Per-variant aggregates. Scores exist only for usable trials: an unusable one is absent, not 0.0."""
    usable = [row for row in rows if row.usable]
    scores = [float(row.objective_score) for row in usable]
    tokens = [float(row.execution.tokens.value) for row in rows if row.execution.tokens.value is not None]
    latency = [float(row.execution.latency_ms.value) for row in rows if row.execution.latency_ms.value is not None]
    return {
        "runs": len(rows),
        "mean_score": _mean_or_none(scores, 6),
        "stdev_score": None if not scores else (round(statistics.stdev(scores), 6) if len(scores) > 1 else 0.0),
        "pass_rate": _mean_or_none(
            [float(row.evaluation.status is EvaluationStatus.PASSED) for row in usable], 6),
        "failure_rate": round(_mean([float(row.error is not None) for row in rows]), 6),
        "mean_tokens": _mean_or_none(tokens, 3),
        "mean_latency_ms": _mean_or_none(latency, 3),
        "verified_runs": len(usable),
    }


def _pareto(stats: dict[str, dict[str, Any]]) -> list[str]:
    # An unknown score ranks below every known one and an unknown cost above every known one,
    # so a variant is never preferred for something nobody measured.
    def score(values: dict[str, Any]) -> float:
        return -math.inf if values["mean_score"] is None else values["mean_score"]

    def cost(values: dict[str, Any], key: str) -> float:
        return math.inf if values[key] is None else values[key]

    result: list[str] = []
    for name, values in stats.items():
        dominated = False
        for other, candidate in stats.items():
            if other == name:
                continue
            good = (
                score(candidate) >= score(values)
                and cost(candidate, "mean_tokens") <= cost(values, "mean_tokens")
                and cost(candidate, "mean_latency_ms") <= cost(values, "mean_latency_ms")
            )
            better = (
                score(candidate) > score(values)
                or cost(candidate, "mean_tokens") < cost(values, "mean_tokens")
                or cost(candidate, "mean_latency_ms") < cost(values, "mean_latency_ms")
            )
            if good and better:
                dominated = True
                break
        if not dominated:
            result.append(name)
    return result


def _ratio(value: float | None, reference: float | None) -> float | None:
    """value / reference, or None when either is unknown or the reference is zero."""
    return value / reference if value is not None and reference else None


def compare(spec: ExperimentSpec, records: list[TrialRecord]) -> dict[str, Any]:
    variants = {variant.id: [r for r in records if r.variant_id == variant.id] for variant in spec.variants}
    stats = {key: _summary(value) for key, value in variants.items()}
    baseline = stats["baseline"]
    baseline_by_pair = {(r.task_id, r.repetition): r for r in variants["baseline"]}
    decisions: dict[str, Any] = {}
    for variant_id, rows in variants.items():
        if variant_id == "baseline":
            continue
        differences: list[tuple[str, float]] = []
        by_case: dict[str, list[float]] = {}
        for row in rows:
            reference = baseline_by_pair.get((row.task_id, row.repetition))
            if reference is None:
                continue
            if not (row.usable and reference.usable):
                continue  # an unusable side has no score to difference, and 0.0 would invent one
            delta = row.objective_score - reference.objective_score
            differences.append((row.task_id, delta))
            by_case.setdefault(row.task_id, []).append(delta)
        ci = _bootstrap_interval(differences, spec.seed + sum(map(ord, variant_id)), spec.confidence, spec.bootstrap_iterations)
        mean_delta = _mean([delta for _, delta in differences])
        regressions = {
            name: round(_mean(values), 6)
            for name, values in by_case.items()
            if _mean(values) < -spec.max_regression
        }
        cost_ratio = _ratio(stats[variant_id]["mean_tokens"], baseline["mean_tokens"])
        latency_ratio = _ratio(stats[variant_id]["mean_latency_ms"], baseline["mean_latency_ms"])
        reasons: list[str] = []
        if len(differences) < spec.min_samples or any(len(by_case.get(case.id, [])) < spec.min_samples for case in spec.cases):
            reasons.append("insufficient paired samples")
        if any(not r.usable for r in rows + variants["baseline"]):
            reasons.append("missing objective verification or execution failure")
        if mean_delta < spec.min_improvement or ci[0] <= 0:
            reasons.append("improvement is not statistically supported")
        if regressions:
            reasons.append("task-level regressions")
        if cost_ratio is not None and cost_ratio > spec.max_cost_ratio:
            reasons.append("token budget exceeded")
        if latency_ratio is not None and latency_ratio > spec.max_latency_ratio:
            reasons.append("latency budget exceeded")
        decisions[variant_id] = {
            "mean_delta": round(mean_delta, 6),
            "confidence_interval": list(ci),
            "confidence": spec.confidence,
            "per_task_delta": {k: round(_mean(v), 6) for k, v in by_case.items()},
            "regressions": regressions,
            "token_ratio": round(cost_ratio, 3) if cost_ratio is not None else None,
            "latency_ratio": round(latency_ratio, 3) if latency_ratio is not None else None,
            "eligible": not reasons,
            "reasons": reasons,
        }
    return {"variants": stats, "contrasts": decisions, "pareto_frontier": _pareto(stats),
            "recommended": sorted(
                [name for name, decision in decisions.items() if decision["eligible"]],
                # An unknown token mean ranks after every known one: None never meets a float in a comparison.
                key=lambda name: (-decisions[name]["mean_delta"], stats[name]["mean_tokens"] is None,
                                  stats[name]["mean_tokens"] or 0.0, name),
            )[:1]}




# --- orchestration --------------------------------------------------------------------------------

_HOLDOUT_SPLITS = (Split.VALIDATION, Split.REGRESSION)
_ELIGIBLE_WITHOUT_RESERVATION = (
    "an eligible verdict needs the frozen finalist of a confirmatory run holding an unblocked holdout "
    "reservation; recorded as exploratory only")


def _alternate(variants: list[Variant], repetition: int) -> list[Variant]:
    """Execution order for one repetition: reversed on odd repetitions to spread ordering bias."""
    return list(reversed(variants)) if repetition % 2 else list(variants)


def _retry(record: TrialRecord) -> bool:
    """Only an infrastructure failure the backend marked retryable is re-run; nothing else ever is.

    Adapter protocol errors are EXECUTION_ERROR with ``retryable`` false; TASK_FAILURE, TIMEOUT,
    RESOURCE_LIMIT and every evaluated outcome are final whatever the flag says.
    """
    return record.execution.retryable and record.execution.status is ExecutionStatus.EXECUTION_ERROR


class _Runner:
    """Runs trials for one experiment and persists every attempt the moment it finishes."""

    def __init__(self, spec: ExperimentSpec, executor: Any, backend: ExecutionBackend, limits: ProcessLimits,
                 db: Any, experiment_id: str, implementation_hash: str) -> None:
        self.spec, self.executor, self.backend, self.limits = spec, executor, backend, limits
        self.db, self.experiment_id, self.implementation_hash = db, experiment_id, implementation_hash
        self.attempts: list[TrialRecord] = []  # every attempt, in execution order
        self.final: list[TrialRecord] = []  # the last attempt of each trial: what decisions use

    def trial(self, variant: Variant, task: TaskSpec, repetition: int) -> None:
        for attempt in range(1, self.spec.statistics.max_attempts + 1):
            record = run_trial(
                self.spec, variant, task, repetition=repetition, attempt=attempt, executor=self.executor,
                backend=self.backend, limits=self.limits, experiment_id=self.experiment_id,
                implementation_hash=self.implementation_hash,
            )
            self.db.record_trial(self.experiment_id, record)
            self.attempts.append(record)
            if not _retry(record):
                break
        self.final.append(record)

    def grid(self, variants: list[Variant], tasks: tuple[TaskSpec, ...]) -> None:
        for task in tasks:
            for repetition in range(self.spec.repetitions):
                for variant in _alternate(variants, repetition):
                    self.trial(variant, task, repetition)


def _guard_eligible(decision: ExperimentDecision, *, spec: ExperimentSpec, selection: FrozenSelection | None,
                    reserved: bool, integrity_problems: list[str],
                    exploratory_reason: str | None) -> ExperimentDecision:
    """An eligible verdict stands only for the frozen finalist of a clean confirmatory run with a reservation.

    ``record_decision`` does not check exposure, so this is where it is enforced; anything else is
    downgraded (fail closed) rather than recorded as eligible.
    """
    if decision.status is not DecisionStatus.ELIGIBLE:
        return decision
    if (spec.mode == "confirmatory" and selection is not None and reserved and not integrity_problems
            and decision.candidate_hash == selection.finalist_hash):
        return decision
    status = DecisionStatus.INVALID_EXPERIMENT if integrity_problems else DecisionStatus.EXPLORATORY_ONLY
    added = [*integrity_problems, *([exploratory_reason] if exploratory_reason else []), _ELIGIBLE_WITHOUT_RESERVATION]
    return dataclasses.replace(decision, status=status,
                               reasons=[*decision.reasons, *(item for item in added if item not in decision.reasons)])


def _earlier_exposure(db: Any, earlier: tuple[str, ...]) -> str | None:
    """Why a rerun of an interrupted confirmation cannot be eligible, or ``None`` when nothing was exposed.

    The holdout trials an earlier attempt of this candidate already ran were drawn and recorded; letting the
    rerun draw fresh samples for the same tasks would turn kill-and-rerun into best-of-N (spec section 7).
    An earlier attempt that died before its first holdout trial exposed nothing.
    """
    holdout = {split.value for split in _HOLDOUT_SPLITS}
    counts = {experiment_id: sum(1 for trial in db.trials(experiment_id) if trial.split in holdout)
              for experiment_id in earlier}
    counts = {experiment_id: count for experiment_id, count in counts.items() if count}
    if not counts:
        return None
    return (f"{sum(counts.values())} holdout trial(s) of this candidate were already run by earlier interrupted "
            f"confirmation(s) {', '.join(counts)}; a rerun cannot select fresh samples, recorded as exploratory only")


def _check_evidence(decision: ExperimentDecision, trial_ids: set[str]) -> ExperimentDecision:
    """A decision may cite only trials that exist in this experiment; anything else makes it invalid."""
    stray = [trial_id for trial_id in decision.evidence_ids if trial_id not in trial_ids]
    if not stray:
        return decision
    shown = ", ".join(stray[:3]) + (f" and {len(stray) - 3} more" if len(stray) > 3 else "")
    return dataclasses.replace(
        decision, status=DecisionStatus.INVALID_EXPERIMENT,
        reasons=[*decision.reasons, f"decision cites evidence that is not trials of this experiment: {shown}"])


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _settings(spec: ExperimentSpec) -> dict[str, Any]:
    return {
        "repetitions": spec.repetitions, "seed": spec.seed,
        "min_samples": spec.min_samples, "min_improvement": spec.min_improvement,
        "max_regression": spec.max_regression,
        # None means unlimited; spec.max_cost_ratio maps it to inf, which strict JSON cannot carry.
        "max_cost_ratio": spec.budget.max_token_ratio,
        "max_latency_ratio": spec.budget.max_latency_ratio,
        "confidence": spec.confidence, "bootstrap_iterations": spec.bootstrap_iterations,
        "max_attempts": spec.statistics.max_attempts,
    }


def _provenance(spec: ExperimentSpec, executor: Any, record: dict[str, Any],
                attempts: list[TrialRecord]) -> dict[str, Any]:
    """The persisted provenance: the full record plus the manifest and adapter facts older readers use."""
    returned = returned_model_status(attempts)
    return {
        **record,
        "returned_model_status": returned["status"],
        "returned_models": returned["models"],
        "manifest_sha256": spec.source_hash,
        "workspace_and_seed_supported": any(
            callable(getattr(executor, name, None)) for name in ("execute_trial", "execute_case")),
        "source": str(spec.source) if spec.source else None,
        "model": record["model_requested"],
        "executor_configured": spec.executor_config.get("type", "model"),
    }


def run_experiment(spec: ExperimentSpec, executor: Any, root: str | Path, *,
                   backend: ExecutionBackend | None = None, db: Any = None, decide=None) -> dict[str, Any]:
    """Run ``spec`` in phases, persisting every trial as it finishes, and record one decision.

    Confirmatory: controls of every validation and regression task are checked first (a failure is an
    ``INVALID_EXPERIMENT`` with no trials); with more than one candidate a development phase chooses the
    finalist, which is frozen and persisted; the holdout is reserved for it, the frozen hashes are verified,
    and only the baseline and the finalist run the validation and regression tasks. A blocked reservation
    makes the run's results exploratory. Exploratory (legacy or ``mode: exploratory``): every variant runs
    every task. A trial is retried only for a retryable infrastructure failure, up to
    ``statistics.max_attempts``; every attempt is recorded and decisions use the last one.

    The backend is ``require_backend(spec.environment)`` unless one is given: a sandbox that cannot be
    provided raises ``BackendUnavailable`` before anything is created. An exception after the experiment
    row is written leaves it ``running`` (``db.incomplete_experiments()`` lists it) and is re-raised.
    """
    backend = backend or require_backend(spec.environment)
    limits = ProcessLimits.from_config(spec.environment.limits)  # run_trial applies the executor's own cap
    destination = Path(root).resolve()
    owned = db is None
    if owned:
        from harness_workbench.core.db import WorkbenchDB  # imported here: core.db imports this package
        from harness_workbench.core.store import preflight_library
        # An unmigrated library is refused before the database exists: creating it would hide the legacy
        # versions from report and let the holdout be used up on a library that cannot promote.
        preflight_library(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if owned:
        db = WorkbenchDB.open(destination)
    try:
        return _run(spec, executor, destination, backend, limits, db, decide or default_decide)
    finally:
        if owned:
            db.close()


def _run(spec: ExperimentSpec, executor: Any, destination: Path, backend: ExecutionBackend, limits: ProcessLimits,
         db: Any, decide: Any) -> dict[str, Any]:
    record = provenance_record(spec, executor, backend, root=_REPOSITORY_ROOT)
    started_hash = record["implementation_hash"]
    plan = build_plan(spec)
    experiment_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    db.start_experiment(experiment_id, name=spec.name, family=spec.family, mode=spec.mode,
                        schema_version=spec.schema_version, plan=plan, implementation_hash=started_hash)
    out_dir = destination / "experiments" / experiment_id
    out_dir.mkdir(parents=True, exist_ok=False)
    runner = _Runner(spec, executor, backend, limits, db, experiment_id, started_hash)
    baseline = spec.variants[0]
    confirmatory = spec.mode == "confirmatory"
    fingerprints = holdout_fingerprints(spec) if confirmatory else {}
    problems: list[str] = []
    selection: FrozenSelection | None = None
    exploratory_reason: str | None = None
    earlier: tuple[str, ...] = ()
    rerun_reason: str | None = None  # an interrupted attempt of this candidate already ran holdout trials
    reserved = False
    if confirmatory:
        # A change the adapter cannot apply would be compared against nothing: refuse before any trial.
        problems.extend(unsupported_components(spec, adapter_supports(executor)))
        snapshot = SnapshotLimits(limits.max_files, limits.max_total_bytes)
        for split in _HOLDOUT_SPLITS:
            for task in spec.tasks_in(split):
                problems.extend(run_controls(task, backend=backend, limits=limits, snapshot_limits=snapshot))
    if confirmatory and not problems:
        candidates = list(spec.variants[1:])
        if len(candidates) > 1:
            runner.grid(list(spec.variants), spec.tasks_in(Split.DEVELOPMENT))
        finalist_id, evidence = select_finalist(
            spec, [record for record in runner.final if record.split == Split.DEVELOPMENT.value])
        if finalist_id is None:
            problems.append(f"no finalist could be selected: {evidence.get('problem', 'unknown reason')}")
        else:
            selection = freeze_selection(spec, plan, finalist_id, evidence=evidence)
            db.record_selection(experiment_id, selection.to_dict())
            reservation = db.reserve_holdout_attempt(
                task_fingerprints=fingerprints.values(), candidate_hash=selection.finalist_hash,
                baseline_hash=selection.baseline_hash, policy_hash=selection.policy_hash,
                experiment_id=experiment_id)
            exploratory_reason, earlier = reservation.blocked_reason, reservation.earlier_experiment_ids
            reserved = exploratory_reason is None
            rerun_reason = _earlier_exposure(db, earlier) if reserved else None
            finalist = next(variant for variant in candidates if variant.id == finalist_id)
            problems.extend(verify_frozen(selection, finalist.harness, baseline.harness, plan))
            if not problems:
                holdout = tuple(task for split in _HOLDOUT_SPLITS for task in spec.tasks_in(split))
                runner.grid([baseline, finalist], holdout)
    elif not confirmatory:
        # Every variant runs every task, so results on holdout content are exposed to selection: consume
        # that content before it runs, or a later confirmation of the winner would reuse it (spec §4).
        exposed = holdout_fingerprints(spec)
        if exposed:
            db.record_holdout_exposure(task_fingerprints=exposed.values(), experiment_id=experiment_id)
        runner.grid(list(spec.variants), spec.tasks)
    if confirmatory:  # a changed implementation, rubric or model makes the trials incomparable
        problems.extend(detect_drift(runner.attempts, {task.id: task.bundle_hash for task in spec.tasks}))
        # Every trial is stamped with the hash taken at the start, so compare it to the code as it is now.
        if (finished_hash := implementation_hash()) != started_hash:
            problems.append("implementation drift: the package code changed while the experiment ran "
                            f"({started_hash[:12]} at start, {finished_hash[:12]} at the end)")

    # Diagnostics first: if they cannot be computed the experiment stays running, with no decision.
    summary = compare(spec, runner.final)
    why_exploratory = exploratory_reason or rerun_reason
    decision = decide(plan, list(runner.final), spec=spec, selection=selection, integrity_problems=list(problems),
                      exploratory_reason=why_exploratory)
    decision = _check_evidence(decision, {record.trial_id for record in runner.attempts})
    decision = _guard_eligible(decision, spec=spec, selection=selection, reserved=reserved and not rerun_reason,
                               integrity_problems=problems, exploratory_reason=why_exploratory)
    if earlier:
        decision.diagnostics = {**decision.diagnostics, "earlier_experiment_ids": list(earlier)}
    decision_id = db.record_decision(experiment_id, decision)
    # The store may downgrade an eligible decision (holdout already decided elsewhere): report what it kept.
    decision = db.decision(decision_id) or decision
    db.finish_experiment(experiment_id, "invalid" if decision.status is DecisionStatus.INVALID_EXPERIMENT
                         else "complete")

    # The contrasts are diagnostics; only an eligible decision recommends anything.
    summary["recommended"] = (
        [decision.candidate_id] if decision.status is DecisionStatus.ELIGIBLE and decision.candidate_id else [])
    payload: dict[str, Any] = {
        "experiment_id": experiment_id,
        "created_at": _utc(),
        "name": spec.name,
        "mode": spec.mode,
        "schema_version": RESULTS_SCHEMA_VERSION,  # the layout of this file, not the manifest's version
        "manifest_schema_version": spec.schema_version,
        "settings": _settings(spec),
        "provenance": _provenance(spec, executor, record, runner.attempts),
        "variants": [{"id": v.id, "harness": str(v.source), "sha256": v.fingerprint,
                      "version": v.harness.version} for v in spec.variants],
        "tasks": [{"id": task.id, "split": task.split.value if task.split else None, "family": task.family,
                   "fixture_sha256": task.fixture_hash, "checks": list(task.checks)} for task in spec.tasks],
        "selection": selection.to_dict() if selection is not None else None,
        "exposure": {"fingerprints": sorted(fingerprints.values()), "block_reason": exploratory_reason},
        "summary": summary,
        "decision": decision.to_dict(),
        "decision_id": decision_id,
        "runs": [record.to_dict() for record in runner.attempts],
        "report_path": str(out_dir / "report.html"),
        "results_path": str(out_dir / "results.json"),
    }
    _write_atomic(out_dir / "results.json", json_dumps_strict(payload, indent=2, ensure_ascii=False) + "\n")
    _write_atomic(out_dir / "report.html", render_html(payload))
    return payload
