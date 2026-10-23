"""Repeatable, paired harness experiments with independent verification.

Runs share a task/fixture/seed, but NEVER reuse a model session or workspace.
This is not deterministic replay of LLM sampling. Results record controls and
uncertainty; only independently verified outcomes can qualify for promotion.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import platform
import random
import shutil
import statistics
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_workbench.core.executor import BehavioralReport
from harness_workbench.experiments.backends import ExecutionBackend, ProcessLimits, build_backend
from harness_workbench.experiments.contracts import (
    TIMEOUT_POLICY_CHECK_ID, EvaluationResult, ExecutionResult, Measurement, PublicTrialRequest, TrialRecord,
)
# Case, ExperimentSpec, TaskSpec and Variant moved to manifest.py; core re-exports them.
from harness_workbench.experiments.manifest import Case, ExperimentSpec, TaskSpec, Variant  # noqa: F401
from harness_workbench.experiments.grader import (
    SnapshotLimits, _remove_tree, digest_path as _digest, evaluate_bundle,
)
from harness_workbench.experiments.status import (
    USABLE_EVALUATION, USABLE_EXECUTION, EvaluationStatus, ExecutionStatus, Provenance,
)
from harness_workbench.experiments.validation import ValidationError
from harness_workbench.experiments.report import render_html


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

_LEGACY_USAGE_PROVENANCE = frozenset({"estimated", "provider_reported", "adapter_reported"})


def _implementation_hash() -> str:
    """sha256 over the relative paths and bytes of ``harness_workbench/**/*.py``."""
    package = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for item in sorted(package.rglob("*.py")):
        digest.update(item.relative_to(package).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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
    return ExecutionResult(
        trial_id=request.trial_id, status=ExecutionStatus.COMPLETED, outputs=dict(report.outputs),
        exit_code=None, stdout_tail="", stderr_tail="", tokens=tokens,
        latency_ms=Measurement.not_applicable(), adapter_latency_ms=Measurement.unknown(),
        returned_model=None, artifact_hash=None, error=None, retryable=False,
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
                key=lambda name: (-decisions[name]["mean_delta"], stats[name]["mean_tokens"], name),
            )[:1]}


def run_experiment(spec: ExperimentSpec, executor: Any, root: str | Path) -> dict[str, Any]:
    """Execute a fresh paired sample for each case/repetition/variant.

    An adapter implements ``execute_trial(request, workspace=, backend=, limits=)`` and sees only
    the public request. Older adapters that implement ``execute_case(harness, task, context)`` or
    ``execute(harness, task)`` are wrapped (see ``_legacy_execution``) and cannot claim supervised
    latency. The backend comes from ``spec.environment.mode``; a sandbox that cannot be provided
    raises ``BackendUnavailable`` and nothing is run.
    """
    # The environment mode picks the backend, and an unavailable sandbox refuses before anything starts.
    backend = build_backend(spec.environment)
    destination = Path(root).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    experiment_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    out_dir = destination / "experiments" / experiment_id
    out_dir.mkdir(parents=True, exist_ok=False)
    limits = ProcessLimits.from_config(spec.environment.limits)
    implementation_hash = _implementation_hash()
    records: list[TrialRecord] = []
    for case in spec.cases:
        for repetition in range(spec.repetitions):
            # Rotate execution order to reduce provider/time-of-day ordering bias.
            variants = list(spec.variants)
            if repetition % 2:
                variants.reverse()
            for variant in variants:
                records.append(run_trial(
                    spec, variant, case, repetition=repetition, attempt=1, executor=executor,
                    backend=backend, limits=limits, experiment_id=experiment_id,
                    implementation_hash=implementation_hash,
                ))
    summary = compare(spec, records)
    payload = {
        "experiment_id": experiment_id,
        "created_at": _utc(),
        "name": spec.name,
        "settings": {
            "repetitions": spec.repetitions, "seed": spec.seed,
            "min_samples": spec.min_samples, "min_improvement": spec.min_improvement,
            "max_regression": spec.max_regression,
            # None means unlimited; spec.max_cost_ratio maps it to inf, which JSON cannot carry.
            "max_cost_ratio": spec.budget.max_token_ratio,
            "max_latency_ratio": spec.budget.max_latency_ratio,
            "confidence": spec.confidence, "bootstrap_iterations": spec.bootstrap_iterations,
        },
        "provenance": {
            "manifest_sha256": spec.source_hash,
            "python": sys.version.split()[0], "platform": platform.platform(),
            "executor": type(executor).__name__,
            "workspace_and_seed_supported": any(
                callable(getattr(executor, name, None)) for name in ("execute_trial", "execute_case")),
            "source": str(spec.source) if spec.source else None,
            "model": str(getattr(executor, "solver_model", "unreported")),
            "executor_configured": spec.executor_config.get("type", "model"),
        },
        "variants": [{"id": v.id, "harness": str(v.source), "sha256": v.fingerprint,
                      "version": v.harness.version} for v in spec.variants],
        "tasks": [{"id": case.id, "fixture_sha256": case.fixture_hash, "checks": list(case.checks)}
                  for case in spec.cases],
        "summary": summary,
        "runs": [item.to_dict() for item in records],
    }
    (out_dir / "results.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str, allow_nan=False) + "\n", encoding="utf-8")
    (out_dir / "report.html").write_text(render_html(payload), encoding="utf-8")
    payload["report_path"] = str(out_dir / "report.html")
    payload["results_path"] = str(out_dir / "results.json")
    return payload
