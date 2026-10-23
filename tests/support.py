"""Shared helpers for adversarial, audit-reproduction tests (stdlib only)."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from harness_workbench.core.executor import BehavioralReport, Task
from harness_workbench.core.harness import Harness
from harness_workbench.experiments.contracts import EvaluationResult, ExecutionResult, Measurement, TrialRecord
from harness_workbench.experiments.status import EvaluationStatus, ExecutionStatus, Provenance

# Placed in the parent environment by the A10 reproductions. The value is a
# literal marker, never a real secret.
ENV_CANARY = "canary-value"

# Every reproduction is decorated with ``@repro``. By default it is an expected
# failure so the suite stays green while the defect exists. With
# HARNESS_REPRO_STRICT=1 the decorator is the identity and the real assertion
# failures are shown.
repro = unittest.expectedFailure if not os.environ.get("HARNESS_REPRO_STRICT") else (lambda f: f)


class SimulatedCrash(Exception):
    """Raised by :class:`ScriptedExecutor` to emulate a mid-suite process crash."""


class ScriptedExecutor:
    """Deterministic adapter whose correctness is scripted per harness prompt.

    ``answers`` maps ``harness.system_prompt`` to a predicate on the task id.
    ``tokens`` and ``latency_ms`` are either one value for every harness or a
    mapping keyed by ``system_prompt``. ``crash_on_call`` makes the Nth
    ``execute_case`` call (1-based) raise :class:`SimulatedCrash`.
    """

    solver_model = "scripted"
    # Consumed from T11 on, when usage provenance becomes a promotion input.
    usage_provenance = "adapter_reported"

    def __init__(
        self,
        answers: Mapping[str, Callable[[str], bool]],
        *,
        tokens: int | Mapping[str, int] = 100,
        latency_ms: int | Mapping[str, int] = 10,
        crash_on_call: int | None = None,
    ) -> None:
        self.answers = dict(answers)
        self.tokens = tokens
        self.latency_ms = latency_ms
        self.crash_on_call = crash_on_call
        self.calls = 0

    @staticmethod
    def _pick(setting: int | Mapping[str, int], prompt: str) -> int:
        return int(setting[prompt]) if isinstance(setting, Mapping) else int(setting)

    def execute_case(self, harness: Harness, task: Task, context: Any) -> BehavioralReport:
        self.calls += 1
        if self.crash_on_call is not None and self.calls == self.crash_on_call:
            raise SimulatedCrash(f"simulated crash on call {self.calls}")
        predicate = self.answers.get(harness.system_prompt)
        correct = bool(predicate(task.id)) if predicate is not None else False
        return BehavioralReport(
            harness_id=harness.id, harness_version=harness.version, task_id=task.id,
            score=1.0, outputs={"answer": "CORRECT" if correct else "INCORRECT"},
            cost_tokens=self._pick(self.tokens, harness.system_prompt),
            latency_ms=self._pick(self.latency_ms, harness.system_prompt),
        )


def exact_task(task_id: str, description: str | None = None, **extra: Any) -> dict[str, Any]:
    """A manifest task whose only check is ``answer == "CORRECT"``."""
    task: dict[str, Any] = {
        "id": task_id,
        "description": description or f"return the correct answer for {task_id}",
        "checks": [{"id": "answer", "type": "exact", "target": "answer", "value": "CORRECT"}],
    }
    task.update(extra)
    return task


def write_manifest(root: Path, *, tasks: list[dict[str, Any]], variants: list[dict[str, Any]], **overrides: Any) -> Path:
    """Write ``base.json``, one harness file per variant and ``experiment.json``.

    Each variant dict needs ``id`` and ``system_prompt``. The baseline harness
    has system prompt ``"base"``. ``overrides`` become top-level manifest keys
    (``repetitions``, ``max_cost_ratio``, ...). Returns the manifest path.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "base.json").write_text(
        Harness(id="sample", version="1", system_prompt="base").to_json(), encoding="utf-8")
    entries: list[dict[str, str]] = []
    for index, variant in enumerate(variants):
        filename = f"{variant['id']}.json"
        (root / filename).write_text(
            Harness(id="sample", version=str(index + 2), system_prompt=str(variant["system_prompt"])).to_json(),
            encoding="utf-8")
        entries.append({"id": str(variant["id"]), "harness": filename})
    manifest: dict[str, Any] = {
        "name": "audit-reproduction", "baseline": "base.json", "variants": entries,
        "tasks": tasks, "seed": 7, "bootstrap_iterations": 300,
    }
    manifest.update(overrides)
    path = root / "experiment.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _resolve(setting: Any, variant_id: str, task_id: str, repetition: int) -> Any:
    return setting(variant_id, task_id, repetition) if callable(setting) else setting


def make_records(
    spec: Any,
    *,
    score: Callable[[str, str, int], float | None] | float | None = None,
    tokens: Callable[[str, str, int], Measurement] | Measurement | None = None,
    latency: Callable[[str, str, int], Measurement] | Measurement | None = None,
    checks: Callable[[str, str, int], list[dict]] | None = None,
    variants: Sequence[str] | None = None,
    splits: Sequence[str] | None = ("validation", "regression"),
    repetitions: int | None = None,
) -> list[TrialRecord]:
    """Synthetic last-attempt trial records for ``spec``: one per (variant, task, repetition).

    ``score(variant_id, task_id, repetition)`` gives the objective score; ``None`` makes the trial unusable
    (an execution error, never evaluated). ``tokens`` and ``latency`` are a Measurement or a callable
    returning one (defaults: 100 adapter-reported tokens, 10 ms measured). ``checks`` returns the check
    results of a usable trial (default: one ``answer`` check that passed iff the score is at least 0.5).
    ``variants`` defaults to the baseline and the first candidate; ``splits`` picks the tasks (``None``: every
    task, split or not). The records
    are labelled synthetic by construction: nothing ran.
    """
    baseline_id = spec.variants[0].id
    chosen = list(variants) if variants is not None else [baseline_id, spec.variants[1].id]
    default_score = (lambda variant, task, rep: 0.0 if variant == baseline_id else 1.0)
    score = default_score if score is None else score
    tokens = Measurement.reported(100, Provenance.ADAPTER_REPORTED) if tokens is None else tokens
    latency = Measurement.measured(10.0) if latency is None else latency
    count = spec.repetitions if repetitions is None else repetitions
    records: list[TrialRecord] = []
    for task in spec.tasks:
        label = task.split.value if task.split is not None else "unsplit"
        if splits is not None and label not in splits:
            continue
        for repetition in range(count):
            for variant_id in chosen:
                value = _resolve(score, variant_id, task.id, repetition)
                usable = value is not None
                trial_id = f"{variant_id}.{task.id}.{repetition}"
                execution = ExecutionResult(
                    trial_id=trial_id,
                    status=ExecutionStatus.COMPLETED if usable else ExecutionStatus.EXECUTION_ERROR,
                    outputs={}, exit_code=None, stdout_tail="", stderr_tail="",
                    tokens=_resolve(tokens, variant_id, task.id, repetition),
                    latency_ms=_resolve(latency, variant_id, task.id, repetition),
                    adapter_latency_ms=Measurement.unknown(), returned_model=None, artifact_hash=None,
                    error=None if usable else "synthetic execution error",
                )
                if usable:
                    results = (checks(variant_id, task.id, repetition) if checks is not None else
                               [{"id": "answer", "type": "exact", "weight": 1.0,
                                 "status": "passed" if value >= 0.5 else "failed",
                                 "passed": value >= 0.5, "detail": "synthetic"}])
                    evaluation = EvaluationResult(
                        trial_id, EvaluationStatus.PASSED if value >= 0.5 else EvaluationStatus.FAILED,
                        task.bundle_hash, None, results, float(value), None)
                else:
                    evaluation = EvaluationResult(
                        trial_id, EvaluationStatus.NOT_EVALUATED, task.bundle_hash, None, [], None,
                        "not evaluated: synthetic")
                records.append(TrialRecord(
                    trial_id, variant_id, task.id, label, task.family, repetition,
                    spec.seed + repetition, 1, execution, evaluation, "synthetic-impl"))
    return records
