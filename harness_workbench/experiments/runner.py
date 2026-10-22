"""Development selection, a sealed validation round, and a regression gate."""
from __future__ import annotations

import copy
import hashlib
import math
import platform
import random
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from harness_workbench.core.executor import BehavioralReport, Task, TraceStep
from harness_workbench.core.harness import utc_now
from harness_workbench.experiments.evaluation import evaluate
from harness_workbench.experiments.execution import Context, TrialExecutor, build_executor, populate
from harness_workbench.experiments.models import ExperimentSpec, Variant, canonical, digest, snapshot
from harness_workbench.experiments.statistics import paired_statistics, pareto_frontier, summarize
from harness_workbench.experiments.store import ExperimentStore


def implementation_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    return digest({p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(root.rglob("*.py"))})


class ExperimentRunner:
    def __init__(self, store: ExperimentStore, *, executor: TrialExecutor | None = None,
                 allow_commands: bool = False) -> None:
        self.store = store
        self.executor = executor
        self.allow_commands = allow_commands

    def run(self, spec: ExperimentSpec) -> dict:
        # Freeze deep copies BEFORE the first model call or external process.
        spec = copy.deepcopy(spec)
        spec.validate()
        if not self.allow_commands and any(check["kind"] == "command" for c in spec.cases for check in c.checks):
            raise ValueError("command graders require --allow-commands")
        execute = self.executor or build_executor(spec.backend, allow_commands=self.allow_commands)
        code_hash = implementation_hash()
        manifest = {"spec": spec.to_dict(), "created_at": utc_now(),
                    "runtime": {"python": sys.version, "platform": platform.platform(),
                                "implementation_sha256": code_hash,
                                "seed_scope": "order and adapter input; not a guarantee of model determinism"}}
        experiment_id = self.store.create(manifest)
        baseline = Variant("baseline", spec.baseline)
        all_variants = [baseline, *spec.variants]
        records: list[dict] = []
        budget_exhausted = False
        used_tokens = 0

        def phase(split: str, variants: list[Variant]) -> None:
            nonlocal used_tokens, budget_exhausted
            jobs = []
            for case in spec.cases:
                if case.split != split:
                    continue
                for repetition in range(spec.policy.repetitions):
                    pair_seed = int(digest({"seed": spec.policy.seed, "task": case.task.id,
                                           "repetition": repetition})[:8], 16)
                    ordered = list(variants)
                    random.Random(pair_seed).shuffle(ordered)
                    jobs.extend((variant, case, repetition, pair_seed) for variant in ordered)
            self.store.event(experiment_id, "phase_started", {"split": split, "planned_runs": len(jobs)})
            with ThreadPoolExecutor(max_workers=spec.policy.max_workers) as pool:
                for offset in range(0, len(jobs), spec.policy.max_workers):
                    limit = spec.policy.max_total_tokens
                    if limit is not None and used_tokens >= limit:
                        budget_exhausted = True
                        break
                    batch = jobs[offset:offset + spec.policy.max_workers]
                    futures = [pool.submit(self._trial, execute, experiment_id, *job) for job in batch]
                    for future in futures:
                        record = future.result()
                        records.append(record)
                        used_tokens += record["report"]["cost_tokens"]
                        if limit is not None and (used_tokens > limit or not record["report"]["metadata"].get("usage_known", False)):
                            budget_exhausted = True
                    if budget_exhausted:
                        break
            self.store.event(experiment_id, "phase_finished", {"split": split, "used_tokens": used_tokens,
                                                               "budget_exhausted": budget_exhausted})

        try:
            phase("development", all_variants)
            dev = self._summaries(records, "development", all_variants)
            # Select ONE finalist only on development data. Never try a runner-up
            # after looking at holdout feedback. Its selection is persisted first.
            eligible = [v for v in spec.variants if not dev[v.label]["errors"]
                        and dev[v.label]["score"] - dev["baseline"]["score"] > spec.policy.margin]
            nominee = min(eligible, key=lambda v: (-dev[v.label]["score"], dev[v.label]["mean_tokens"], v.label)) if eligible else None
            self.store.event(experiment_id, "selection_frozen", {"nominee": nominee.label if nominee else None,
                                                               "development": dev, "pareto": pareto_frontier(dev)})
            reasons = []
            if budget_exhausted:
                reasons.append("budget_exhausted_or_usage_unknown")
            if any(row["errors"] for row in dev.values()):
                reasons.append("development_execution_or_evaluation_error")
            validation, regression, stats = {}, {}, None
            if nominee is None:
                reasons.append("no_development_improvement")
            elif not reasons:
                phase("validation", [baseline, nominee])
                if not budget_exhausted:
                    phase("regression", [baseline, nominee])
                validation = self._summaries(records, "validation", [baseline, nominee])
                regression = self._summaries(records, "regression", [baseline, nominee])
                stats, gate_reasons = self._gate(spec, records, nominee, validation, regression)
                reasons.extend(gate_reasons)
                if budget_exhausted:
                    reasons.append("budget_exhausted_or_usage_unknown")
            returned_models = {r["report"]["metadata"].get("solver_returned") for r in records}
            returned_models.discard(None)
            if len(returned_models) > 1:
                reasons.append("solver_model_changed_between_trials")
            if implementation_hash() != code_hash:
                reasons.append("workbench_code_changed_during_experiment")
            result = {"experiment_id": experiment_id, "promote": nominee is not None and not reasons,
                      "nominee": nominee.label if nominee else None, "reasons": sorted(set(reasons)),
                      "development": dev, "validation": validation, "regression": regression,
                      "statistics": stats, "pareto_development": pareto_frontier(dev),
                      "total_runs": len(records), "total_tokens": used_tokens,
                      "tokens_known": all(r["report"]["metadata"].get("usage_known", False) for r in records),
                      "status": "completed", "synthetic": any(r["report"]["metadata"].get("synthetic", False) for r in records)}
            # A synthetic backend can test acceptance logic but not produce a
            # production promotion, even when it returns artificially high scores.
            if result["synthetic"]:
                result["promote"] = False
                result["reasons"].append("synthetic_backend_not_production_evidence")
            adopted = None
            if result["promote"] and nominee is not None:
                adopted = nominee.harness.fork(source="experiment", version=experiment_id)
                adopted.lineage.parent_id = spec.baseline.id
                adopted.lineage.parent_version = spec.baseline.version
                adopted.lineage.metrics = {"experiment_id": experiment_id, "statistics": stats,
                                           "baseline_hash": digest(spec.baseline.to_dict()), "variant_id": nominee.id}
                adopted.lineage.lessons = ["Promoted only after development selection, held-out validation and regression gates."]
            self.store.finish(experiment_id, result, harness=adopted.to_dict() if adopted is not None else None)
            return result
        except BaseException as exc:
            self.store.event(experiment_id, "interrupted", {"error_type": type(exc).__name__, "completed_runs": len(records)})
            raise

    def _trial(self, execute: TrialExecutor, experiment_id: str, variant: Variant,
               case, repetition: int, seed: int) -> dict:
        run_id = "run-" + uuid4().hex
        started, status, artifacts = time.monotonic(), "completed", {}
        expected_hash = digest(variant.harness.to_dict())
        report = BehavioralReport(variant.harness.id, variant.harness.version, case.task.id, 0.0,
                                  metadata={"usage_known": False})
        with tempfile.TemporaryDirectory(prefix="harness-trial-") as tmp:
            workspace = Path(tmp)
            populate(workspace, case.fixtures)
            harness = variant.harness.copy()
            task = Task(**copy.deepcopy(case.task.to_dict()))
            try:
                report = execute(harness, task, Context(workspace, run_id, seed, repetition))
                if digest(harness.to_dict()) != expected_hash or task.to_dict() != case.task.to_dict():
                    raise ValueError("executor mutated experimental inputs")
                if not isinstance(report, BehavioralReport) or not isinstance(report.output, str):
                    raise ValueError("invalid executor report")
                if (report.harness_id, report.harness_version, report.task_id) != (variant.harness.id, variant.harness.version, case.task.id):
                    raise ValueError("executor report identity does not match trial")
                if type(report.cost_tokens) is not int or report.cost_tokens < 0 or isinstance(report.score, bool) or not isinstance(report.score, (int, float)) or not math.isfinite(report.score) or not 0 <= report.score <= 1:
                    raise ValueError("invalid metrics")
                if not isinstance(report.metadata, dict) or type(report.metadata.get("usage_known", False)) is not bool:
                    raise ValueError("invalid report metadata")
                if not isinstance(report.traces, list) or any(not isinstance(trace, TraceStep) for trace in report.traces):
                    raise ValueError("invalid observable traces")
                if not isinstance(report.failures, list) or any(not isinstance(item, str) for item in report.failures):
                    raise ValueError("invalid failure codes")
                canonical(report.to_dict())
                artifacts = snapshot(workspace)
            except Exception as exc:
                status = "execution_error"
                safe_tokens = report.cost_tokens if isinstance(report, BehavioralReport) and type(report.cost_tokens) is int and report.cost_tokens >= 0 else 0
                report = BehavioralReport(variant.harness.id, variant.harness.version, case.task.id, 0.0,
                                          cost_tokens=safe_tokens, failures=[type(exc).__name__],
                                          metadata={"usage_known": False})
        elapsed_ms = int((time.monotonic() - started) * 1000)
        evaluation = evaluate(report.output, artifacts, case.checks, allow_commands=self.allow_commands)
        if status != "completed":
            evaluation.score, evaluation.success, evaluation.error = 0.0, False, True
        if evaluation.error and status == "completed":
            status = "evaluation_error"
        row = {"run_id": run_id, "variant_id": variant.id, "label": variant.label,
               "harness_hash": expected_hash, "task_id": case.task.id, "split": case.split,
               "task_hash": digest(case.to_dict()), "fixture_hash": digest(case.fixtures),
               "repetition": repetition, "seed": seed, "status": status, "elapsed_ms": elapsed_ms,
               "report": report.to_dict(), "evaluation": evaluation.to_dict(), "artifacts": artifacts}
        self.store.record(experiment_id, row)
        return row

    @staticmethod
    def _summaries(records: list[dict], split: str, variants: list[Variant]) -> dict:
        return {v.label: summarize([r for r in records if r["split"] == split and r["label"] == v.label]) for v in variants}

    @staticmethod
    def _gate(spec: ExperimentSpec, records: list[dict], nominee: Variant, validation: dict, regression: dict) -> tuple[dict, list[str]]:
        policy, reasons = spec.policy, []
        for split in ("validation", "regression"):
            expected_tasks = {c.task.id for c in spec.cases if c.split == split}
            for label in ("baseline", nominee.label):
                rows = [r for r in records if r["split"] == split and r["label"] == label]
                counts = {task: sum(r["task_id"] == task for r in rows) for task in expected_tasks}
                if any(count != policy.repetitions for count in counts.values()):
                    reasons.append(f"incomplete_{split}_trials")
        base, candidate = validation["baseline"], validation[nominee.label]
        shared = sorted(set(base["tasks"]) & set(candidate["tasks"]))
        deltas = [candidate["tasks"][key]["score"] - base["tasks"][key]["score"] for key in shared]
        stats = paired_statistics(deltas, margin=policy.margin, alpha=policy.alpha,
                                  samples=policy.bootstrap_samples, seed=policy.seed)
        if len(shared) < policy.min_validation_tasks:
            reasons.append("insufficient_independent_validation_tasks")
        if not deltas or stats["mean_delta"] <= policy.margin or stats["ci_low"] <= policy.margin or stats["sign_p"] > policy.alpha:
            reasons.append("validation_improvement_not_established")
        for split, summaries in (("validation", validation), ("regression", regression)):
            b, c = summaries["baseline"], summaries[nominee.label]
            if b["errors"] or c["errors"]:
                reasons.append(f"{split}_execution_or_evaluation_error")
            for key in set(b["tasks"]) & set(c["tasks"]):
                if any(c["tasks"][key][metric] + policy.regression_tolerance + 1e-12 < b["tasks"][key][metric]
                       for metric in ("score", "success_rate")):
                    reasons.append(f"{split}_regression:{key}")
            if policy.max_tokens_ratio is not None:
                if not b["usage_known"] or not c["usage_known"]:
                    reasons.append(f"{split}_token_usage_unknown")
                elif c["mean_tokens"] > b["mean_tokens"] * policy.max_tokens_ratio:
                    reasons.append(f"{split}_token_budget_regression")
            if policy.max_latency_ratio is not None and c["mean_latency_ms"] > b["mean_latency_ms"] * policy.max_latency_ratio:
                reasons.append(f"{split}_latency_regression")
        return stats, reasons
