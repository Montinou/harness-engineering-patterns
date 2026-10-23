"""Repeatable, paired harness experiments with independent verification.

Runs share a task/fixture/seed, but NEVER reuse a model session or workspace.
This is not deterministic replay of LLM sampling. Results record controls and
uncertainty; only independently verified outcomes can qualify for promotion.
"""
from __future__ import annotations

import json
import math
import platform
import random
import shutil
import statistics
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_workbench.core.executor import BehavioralReport
from harness_workbench.experiments.backends import ExecutionBackend, LocalBackend, ProcessLimits
# Case, ExperimentSpec, TaskSpec and Variant moved to manifest.py; core re-exports them.
from harness_workbench.experiments.manifest import Case, ExperimentSpec, TaskSpec, Variant  # noqa: F401
from harness_workbench.experiments.grader import SnapshotLimits, digest_path as _digest, evaluate_bundle
from harness_workbench.experiments.status import USABLE_EVALUATION
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


@dataclass
class RunRecord:
    run_id: str
    variant_id: str
    case_id: str
    repetition: int
    seed: int
    objective_score: float
    verified: bool
    passed: bool
    checks: list[dict[str, Any]]
    behavioral_score: float | None
    tokens: int
    latency_ms: int
    failures: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    fixture_before: str | None = None
    fixture_after: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return vars(self).copy()


def _observed(executor: Any, variant: Variant, case: Case, context: RunContext) -> BehavioralReport:
    execute_case = getattr(executor, "execute_case", None)
    if callable(execute_case):
        return execute_case(variant.harness, case.task, context)
    # Existing Executor contract. No workspace, seed or tool-loop guarantees.
    return executor.execute(variant.harness, case.task)


def _one(executor: Any, variant: Variant, case: Case, run_context: RunContext,
         backend: ExecutionBackend, limits: ProcessLimits) -> RunRecord:
    workspace: Path | None = run_context.workspace
    before: str | None = None
    after: str | None = None
    try:
        if workspace is not None and case.fixture is None:
            workspace.mkdir(parents=True, exist_ok=True)
            before = _digest(workspace)
        if case.fixture:
            if _digest(case.fixture) != case.fixture_hash:
                raise RuntimeError("source fixture changed during experiment")
            workspace = run_context.workspace
            if workspace is None:
                raise RuntimeError("workspace not initialized")
            shutil.copytree(case.fixture, workspace)
            before = _digest(workspace)
        real_context = RunContext(
            run_context.experiment_id, run_context.run_id, run_context.case_id,
            run_context.variant_id, run_context.repetition, run_context.seed, workspace,
        )
        report = _observed(executor, variant, case, real_context)
        if not isinstance(report, BehavioralReport):
            raise TypeError("executor must return a BehavioralReport")
        outputs = dict(report.outputs)
        evaluation = evaluate_bundle(
            case.bundle, outputs, workspace, backend=backend, limits=limits,
            snapshot_limits=SnapshotLimits(limits.max_files, limits.max_total_bytes),
            trial_id=run_context.run_id,
        )
        checks = evaluation.checks
        after = _digest(workspace) if workspace else None
        score = evaluation.score if evaluation.score is not None else 0.0
        usable = evaluation.status in USABLE_EVALUATION
        return RunRecord(
            run_context.run_id, variant.id, case.id, run_context.repetition,
            run_context.seed, round(score, 6), usable, evaluation.status.value == "passed",
            checks, report.score, report.cost_tokens, report.latency_ms,
            list(report.failures), outputs, before, after,
        )
    except Exception as exc:  # An individual agent failure must not abort the experiment.
        return RunRecord(
            run_context.run_id, variant.id, case.id, run_context.repetition,
            run_context.seed, 0.0, False, False, [], None, 0, 0,
            [type(exc).__name__], {}, before, after, f"{type(exc).__name__}: {exc}",
        )


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


def _summary(rows: list[RunRecord]) -> dict[str, Any]:
    objective = [row.objective_score for row in rows]
    return {
        "runs": len(rows),
        "mean_score": round(_mean(objective), 6),
        "stdev_score": round(statistics.stdev(objective), 6) if len(objective) > 1 else 0.0,
        "pass_rate": round(_mean([float(r.passed) for r in rows]), 6),
        "failure_rate": round(_mean([float(r.error is not None) for r in rows]), 6),
        "mean_tokens": round(_mean([float(r.tokens) for r in rows]), 3),
        "mean_latency_ms": round(_mean([float(r.latency_ms) for r in rows]), 3),
        "verified_runs": sum(row.verified for row in rows),
    }


def _pareto(stats: dict[str, dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for name, values in stats.items():
        dominated = False
        for other, candidate in stats.items():
            if other == name:
                continue
            good = (
                candidate["mean_score"] >= values["mean_score"]
                and candidate["mean_tokens"] <= values["mean_tokens"]
                and candidate["mean_latency_ms"] <= values["mean_latency_ms"]
            )
            better = (
                candidate["mean_score"] > values["mean_score"]
                or candidate["mean_tokens"] < values["mean_tokens"]
                or candidate["mean_latency_ms"] < values["mean_latency_ms"]
            )
            if good and better:
                dominated = True
                break
        if not dominated:
            result.append(name)
    return result


def compare(spec: ExperimentSpec, records: list[RunRecord]) -> dict[str, Any]:
    variants = {variant.id: [r for r in records if r.variant_id == variant.id] for variant in spec.variants}
    stats = {key: _summary(value) for key, value in variants.items()}
    baseline = stats["baseline"]
    baseline_by_pair = {(r.case_id, r.repetition): r for r in variants["baseline"]}
    decisions: dict[str, Any] = {}
    for variant_id, rows in variants.items():
        if variant_id == "baseline":
            continue
        differences: list[tuple[str, float]] = []
        by_case: dict[str, list[float]] = {}
        for row in rows:
            reference = baseline_by_pair.get((row.case_id, row.repetition))
            if reference is None:
                continue
            delta = row.objective_score - reference.objective_score
            differences.append((row.case_id, delta))
            by_case.setdefault(row.case_id, []).append(delta)
        ci = _bootstrap_interval(differences, spec.seed + sum(map(ord, variant_id)), spec.confidence, spec.bootstrap_iterations)
        mean_delta = _mean([delta for _, delta in differences])
        regressions = {
            name: round(_mean(values), 6)
            for name, values in by_case.items()
            if _mean(values) < -spec.max_regression
        }
        cost_ratio = stats[variant_id]["mean_tokens"] / baseline["mean_tokens"] if baseline["mean_tokens"] else None
        latency_ratio = stats[variant_id]["mean_latency_ms"] / baseline["mean_latency_ms"] if baseline["mean_latency_ms"] else None
        reasons: list[str] = []
        if len(differences) < spec.min_samples or any(len(by_case.get(case.id, [])) < spec.min_samples for case in spec.cases):
            reasons.append("insufficient paired samples")
        if any(not r.verified for r in rows + variants["baseline"]):
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

    An adapter may implement execute_case(harness, task, context) to consume the
    isolated workspace and seed. Legacy execute(harness, task) remains supported,
    but cannot claim tool/workspace determinism.
    """
    destination = Path(root).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    experiment_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    out_dir = destination / "experiments" / experiment_id
    out_dir.mkdir(parents=True, exist_ok=False)
    # Interim: trusted local execution until the supervisor selects a backend per environment mode.
    backend = LocalBackend()
    limits = ProcessLimits.from_config(spec.environment.limits)
    records: list[RunRecord] = []
    for case in spec.cases:
        for repetition in range(spec.repetitions):
            seed = spec.seed + repetition
            # Rotate execution order to reduce provider/time-of-day ordering bias.
            variants = list(spec.variants)
            if repetition % 2:
                variants.reverse()
            for variant in variants:
                run_id = uuid.uuid4().hex
                with tempfile.TemporaryDirectory(prefix="harness-experiment-") as temp:
                    context = RunContext(experiment_id, run_id, case.id, variant.id, repetition, seed, Path(temp) / "workspace")
                    records.append(_one(executor, variant, case, context, backend, limits))
    summary = compare(spec, records)
    payload = {
        "experiment_id": experiment_id,
        "created_at": _utc(),
        "name": spec.name,
        "settings": {
            "repetitions": spec.repetitions, "seed": spec.seed,
            "min_samples": spec.min_samples, "min_improvement": spec.min_improvement,
            "max_regression": spec.max_regression,
            "max_cost_ratio": spec.max_cost_ratio, "max_latency_ratio": spec.max_latency_ratio,
            "confidence": spec.confidence, "bootstrap_iterations": spec.bootstrap_iterations,
        },
        "provenance": {
            "manifest_sha256": spec.source_hash,
            "python": sys.version.split()[0], "platform": platform.platform(),
            "executor": type(executor).__name__,
            "workspace_and_seed_supported": callable(getattr(executor, "execute_case", None)),
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
    (out_dir / "results.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    (out_dir / "report.html").write_text(render_html(payload), encoding="utf-8")
    payload["report_path"] = str(out_dir / "report.html")
    payload["results_path"] = str(out_dir / "results.json")
    return payload
