"""Phased experiment runner: controls, development selection, frozen finalist, holdout and persistence.

Confirmatory manifests here have 6 development, 3 validation and 1 regression task, controls on every
holdout task, ``environment.mode`` trusted_local acknowledged and ``runs_candidate_code`` false. The
executors are scripted in-process adapters on ``LocalBackend``; nothing starts Docker or a model.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from harness_workbench.core.db import WorkbenchDB
from harness_workbench.experiments import ExperimentSpec, core
from harness_workbench.experiments.backends import LocalBackend
from harness_workbench.experiments.contracts import ExecutionResult, ExperimentDecision, Measurement
from harness_workbench.experiments.core import run_experiment
from harness_workbench.experiments.status import DecisionStatus, ExecutionStatus
from tests.support import ScriptedExecutor, exact_task, write_manifest

DEVELOPMENT = tuple(f"d{i}" for i in range(1, 7))
VALIDATION = ("v1", "v2", "v3")
REGRESSION = ("r1",)
HOLDOUT = VALIDATION + REGRESSION
CONTROLS = {"positive": {"outputs": {"answer": "CORRECT"}}, "negatives": [{"outputs": {"answer": "INCORRECT"}}]}
REPETITIONS = 5


class Interrupted(BaseException):
    """A process-level interruption (like KeyboardInterrupt) that no per-trial isolation absorbs."""


class Interrupting:
    """Delegates to ``inner`` and raises :class:`Interrupted` on the Nth ``execute_case`` call (1-based)."""

    def __init__(self, inner: ScriptedExecutor, on_call: int) -> None:
        self.inner = inner
        self.on_call = on_call
        self.calls = 0
        self.usage_provenance = inner.usage_provenance

    def execute_case(self, harness, task, context):
        self.calls += 1
        if self.calls == self.on_call:
            raise Interrupted(f"interrupted on call {self.calls}")
        return self.inner.execute_case(harness, task, context)


def holdout_task(task_id: str, split: str, *, content: str | None = None, controls: dict | None = None) -> dict:
    return exact_task(task_id, f"holdout case number {content or task_id}", split=split,
                      runs_candidate_code=False, controls=controls or CONTROLS)


def default_tasks(*, prefix: str = "", controls: dict | None = None) -> list[dict]:
    tasks = [exact_task(task_id, split="development", runs_candidate_code=False) for task_id in DEVELOPMENT]
    tasks += [holdout_task(prefix + task_id, "validation", content=task_id, controls=controls) for task_id in VALIDATION]
    tasks += [holdout_task(prefix + task_id, "regression", content=task_id, controls=controls) for task_id in REGRESSION]
    return tasks


def confirmatory_manifest(root: Path, candidates: tuple[str, ...], *, tasks: list[dict] | None = None,
                          **overrides: Any) -> Path:
    return write_manifest(
        root, tasks=tasks if tasks is not None else default_tasks(),
        variants=[{"id": name, "system_prompt": name} for name in candidates],
        schema_version=2, mode="confirmatory", family="phases", dataset={"id": "phases", "version": "1"},
        repetitions=REPETITIONS, environment={"mode": "trusted_local", "trusted_solver_acknowledged": True},
        **overrides)


def always(_task_id: str) -> bool:
    return True


def never(_task_id: str) -> bool:
    return False


def scripted(**answers: Any) -> ScriptedExecutor:
    return ScriptedExecutor({"base": never, **answers})


class PhaseCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.library = self.root / "library-root"

    def spec(self, name: str, candidates: tuple[str, ...], **kwargs: Any) -> ExperimentSpec:
        return ExperimentSpec.load(confirmatory_manifest(self.root / name, candidates, **kwargs))

    def run_exp(self, spec: ExperimentSpec, executor: Any, **kwargs: Any) -> dict:
        return run_experiment(spec, executor, self.library, backend=LocalBackend(), **kwargs)

    def db(self) -> WorkbenchDB:
        db = WorkbenchDB.open(self.library)
        self.addCleanup(db.close)
        return db

    def query(self, sql: str, *args: Any) -> list[tuple]:
        conn = sqlite3.connect(str(self.library / "library" / "workbench.sqlite3"))
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


class FinalistTests(PhaseCase):
    def test_only_finalist_and_baseline_run_on_holdout(self) -> None:
        spec = self.spec("m", ("a", "b", "c"))
        executor = scripted(a=always, b=lambda t: t == "d1", c=never)
        payload = self.run_exp(spec, executor)
        self.assertEqual(payload["selection"]["finalist_id"], "a")
        holdout = [run for run in payload["runs"] if run["task_id"] in HOLDOUT]
        self.assertEqual({run["variant_id"] for run in holdout}, {"baseline", "a"})
        self.assertEqual(len(holdout), 2 * len(HOLDOUT) * REPETITIONS)
        development = [run for run in payload["runs"] if run["task_id"] in DEVELOPMENT]
        self.assertEqual(len(development), 4 * len(DEVELOPMENT) * REPETITIONS)
        stored = self.db().trials(payload["experiment_id"])
        self.assertEqual(len(stored), len(payload["runs"]))
        self.assertEqual(payload["decision"]["status"], DecisionStatus.INSUFFICIENT_EVIDENCE.value)
        # Decided by the statistics (3 validation families, far below the 30-unit floor), not a placeholder.
        self.assertIn("independent units 3 is below min_independent_units 30", payload["decision"]["reasons"])
        self.assertEqual(payload["decision"]["n_units"], 3)
        self.assertEqual(payload["decision"]["diagnostics"]["n_regression_units"], 1)
        self.assertEqual(payload["decision"]["candidate_id"], "a")
        self.assertIsNone(payload["exposure"]["block_reason"])
        self.assertEqual(sorted(payload["exposure"]["fingerprints"]),
                         sorted(task.content_fingerprint for task in spec.tasks if task.id in HOLDOUT))
        for key in ("experiment_id", "created_at", "name", "mode", "schema_version", "settings", "provenance",
                    "variants", "tasks", "selection", "exposure", "summary", "decision", "decision_id", "runs",
                    "report_path", "results_path"):
            self.assertIn(key, payload)
        self.assertTrue(Path(payload["results_path"]).is_file())
        self.assertTrue(Path(payload["report_path"]).is_file())
        self.assertEqual(self.query("SELECT status FROM experiments WHERE experiment_id=?",
                                    payload["experiment_id"]), [("complete",)])

    def test_selection_is_frozen_and_persisted_before_holdout_trials(self) -> None:
        spec = self.spec("m", ("a", "b"))
        payload = self.run_exp(spec, scripted(a=always, b=never))
        experiment_id = payload["experiment_id"]
        selection = self.db().selection(experiment_id)
        self.assertEqual(selection["finalist_id"], "a")
        self.assertEqual(selection["finalist_hash"], spec.variants[1].harness.execution_config_hash())
        self.assertEqual(selection["baseline_hash"], spec.variants[0].harness.execution_config_hash())
        (first_holdout,), = self.query(
            "SELECT MIN(created_at) FROM trials WHERE experiment_id=? AND split IN ('validation','regression')",
            experiment_id)
        (last_development,), = self.query(
            "SELECT MAX(created_at) FROM trials WHERE experiment_id=? AND split='development'", experiment_id)
        self.assertLessEqual(selection["frozen_at"], first_holdout)
        self.assertLessEqual(last_development, selection["frozen_at"])

    def test_candidate_changed_after_freeze_invalidates(self) -> None:
        spec = self.spec("m", ("a", "b"))
        real = core.freeze_selection

        def freeze_then_edit(spec_arg, plan, finalist_id, **kwargs):
            frozen = real(spec_arg, plan, finalist_id, **kwargs)
            finalist = next(variant for variant in spec_arg.variants if variant.id == finalist_id)
            finalist.harness.guardrails.append("edited after the freeze")
            return frozen

        with mock.patch.object(core, "freeze_selection", side_effect=freeze_then_edit):
            payload = self.run_exp(spec, scripted(a=always, b=never))
        self.assertEqual(payload["decision"]["status"], DecisionStatus.INVALID_EXPERIMENT.value)
        self.assertTrue(any("finalist hash mismatch" in reason for reason in payload["decision"]["reasons"]))
        self.assertEqual([run for run in payload["runs"] if run["task_id"] in HOLDOUT], [])
        self.assertEqual(self.query("SELECT status FROM experiments WHERE experiment_id=?",
                                    payload["experiment_id"]), [("invalid",)])


class ExposureTests(PhaseCase):
    def test_second_experiment_with_other_candidate_on_same_holdout_is_exploratory_only_even_with_renamed_tasks(
            self) -> None:
        first = self.run_exp(self.spec("first", ("a",)), scripted(a=always))
        self.assertEqual(first["decision"]["status"], DecisionStatus.INSUFFICIENT_EVIDENCE.value)
        renamed = self.spec("second", ("b",), tasks=default_tasks(prefix="renamed-"))
        self.assertNotIn("v1", {task.id for task in renamed.tasks})
        second = self.run_exp(renamed, scripted(b=always))
        self.assertEqual(second["decision"]["status"], DecisionStatus.EXPLORATORY_ONLY.value)
        reason = second["exposure"]["block_reason"]
        self.assertIsNotNone(reason)
        self.assertIn(reason, second["decision"]["reasons"])
        self.assertEqual(sorted(second["exposure"]["fingerprints"]), sorted(first["exposure"]["fingerprints"]))

    def test_rerun_after_completed_decision_is_exploratory_only_even_for_the_same_candidate(self) -> None:
        self.run_exp(self.spec("m", ("a",)), scripted(a=always))
        again = self.run_exp(self.spec("m", ("a",)), scripted(a=always))
        self.assertEqual(again["decision"]["status"], DecisionStatus.EXPLORATORY_ONLY.value)
        self.assertIn("already consumed by a completed decision", again["exposure"]["block_reason"])
        self.assertIn(again["exposure"]["block_reason"], again["decision"]["reasons"])

    def test_same_candidate_rerun_after_interruption_is_allowed_and_old_run_stays_incomplete(self) -> None:
        spec = self.spec("m", ("a",))
        with self.assertRaises(Interrupted):
            self.run_exp(spec, Interrupting(scripted(a=always), on_call=3))
        (interrupted,) = self.db().incomplete_experiments()
        self.assertEqual(len(self.db().trials(interrupted)), 2)
        rerun = self.run_exp(self.spec("m", ("a",)), scripted(a=always))
        self.assertIsNone(rerun["exposure"]["block_reason"])
        # The interrupted run already ran holdout trials of this candidate: a rerun cannot be eligible, or
        # kill-and-rerun would draw fresh samples until one passes (spec section 7).
        self.assertEqual(rerun["decision"]["status"], DecisionStatus.EXPLORATORY_ONLY.value)
        self.assertTrue(any(interrupted in reason and "2 holdout trial" in reason
                            for reason in rerun["decision"]["reasons"]), rerun["decision"]["reasons"])
        self.assertEqual(rerun["decision"]["diagnostics"]["earlier_experiment_ids"], [interrupted])
        self.assertEqual(self.db().incomplete_experiments(), [interrupted])
        self.assertEqual(self.query("SELECT COUNT(*) FROM decisions WHERE experiment_id=?", interrupted), [(0,)])

    def test_rerun_after_an_interruption_before_any_holdout_trial_is_not_exposed(self) -> None:
        spec = self.spec("m", ("a",))
        with self.assertRaises(Interrupted):
            self.run_exp(spec, Interrupting(scripted(a=always), on_call=1))
        (interrupted,) = self.db().incomplete_experiments()
        self.assertEqual(self.db().trials(interrupted), [])
        rerun = self.run_exp(self.spec("m", ("a",)), scripted(a=always))
        self.assertIsNone(rerun["exposure"]["block_reason"])
        self.assertEqual(rerun["decision"]["status"], DecisionStatus.INSUFFICIENT_EVIDENCE.value)
        self.assertEqual(rerun["decision"]["diagnostics"]["earlier_experiment_ids"], [interrupted])

    def test_an_eligible_verdict_needs_an_unblocked_reservation(self) -> None:
        def eligible(plan, records, *, spec, selection, integrity_problems, exploratory_reason):
            return ExperimentDecision(
                DecisionStatus.ELIGIBLE, [], 0.5, 0.1, 4, plan.plan_hash, selection.finalist_id,
                selection.finalist_hash, selection.baseline_hash, spec.variants[0].harness.id,
                spec.variants[0].harness.version, [record.trial_id for record in records], [], {})

        first = self.run_exp(self.spec("first", ("a",)), scripted(a=always), decide=eligible)
        self.assertEqual(first["decision"]["status"], DecisionStatus.ELIGIBLE.value)
        second = self.run_exp(self.spec("second", ("b",)), scripted(b=always), decide=eligible)
        self.assertEqual(second["decision"]["status"], DecisionStatus.EXPLORATORY_ONLY.value)
        self.assertIn(second["exposure"]["block_reason"], second["decision"]["reasons"])
        self.assertEqual(self.query("SELECT status FROM decisions WHERE experiment_id=?", second["experiment_id"]),
                         [(DecisionStatus.EXPLORATORY_ONLY.value,)])


class EvidenceTests(PhaseCase):
    @staticmethod
    def decision_citing(evidence: list[str]):
        def decide(plan, records, *, spec, selection, integrity_problems, exploratory_reason):
            return ExperimentDecision(
                DecisionStatus.ELIGIBLE, [], 0.5, 0.1, 4, plan.plan_hash, selection.finalist_id,
                selection.finalist_hash, selection.baseline_hash, spec.variants[0].harness.id,
                spec.variants[0].harness.version, evidence, [], {})
        return decide

    def test_a_decision_citing_trials_of_no_such_experiment_is_invalid(self) -> None:
        payload = self.run_exp(self.spec("m", ("a",)), scripted(a=always),
                               decide=self.decision_citing(["not-a-trial"]))
        self.assertEqual(payload["decision"]["status"], DecisionStatus.INVALID_EXPERIMENT.value)
        self.assertTrue(any("not-a-trial" in reason and "not trials of this experiment" in reason
                            for reason in payload["decision"]["reasons"]), payload["decision"]["reasons"])
        self.assertEqual(self.query("SELECT status FROM experiments WHERE experiment_id=?",
                                    payload["experiment_id"]), [("invalid",)])

    def test_a_decision_citing_its_own_trials_stands(self) -> None:
        spec = self.spec("m", ("a",))
        payload = self.run_exp(spec, scripted(a=always), decide=lambda plan, records, **kw: self.decision_citing(
            [record.trial_id for record in records])(plan, records, **kw))
        self.assertEqual(payload["decision"]["status"], DecisionStatus.ELIGIBLE.value)

    def test_the_real_decision_cites_the_holdout_trials_of_the_baseline_and_the_finalist(self) -> None:
        payload = self.run_exp(self.spec("m", ("a", "b")), scripted(a=always, b=never))
        cited = set(payload["decision"]["evidence_ids"])
        trials = {record.trial_id: record for record in self.db().trials(payload["experiment_id"])}
        self.assertTrue(cited and cited <= set(trials))
        self.assertEqual({trials[trial_id].variant_id for trial_id in cited}, {"baseline", "a"})
        self.assertEqual({trials[trial_id].task_id for trial_id in cited}, set(HOLDOUT))


class EligibleEndToEndTests(PhaseCase):
    def test_a_run_with_enough_independent_units_is_eligible_and_recorded(self) -> None:
        tasks = [holdout_task(f"v{i}", "validation") for i in range(30)]
        tasks += [holdout_task(f"r{i}", "regression") for i in range(30)]
        spec = self.spec("m", ("a",), tasks=tasks)
        executor = scripted(a=lambda t: t.startswith("r") or int(t[1:]) % 3 != 0)
        executor.answers["base"] = lambda t: t.startswith("r")
        payload = self.run_exp(spec, executor)
        decision = payload["decision"]
        self.assertEqual(decision["status"], DecisionStatus.ELIGIBLE.value, decision["reasons"])
        self.assertEqual(decision["n_units"], 30)
        self.assertEqual(decision["candidate_id"], "a")
        self.assertEqual(len(decision["evidence_ids"]), 2 * 60 * REPETITIONS)
        self.assertEqual(payload["summary"]["recommended"], ["a"])
        stored = self.db().decision(payload["decision_id"])
        self.assertEqual(stored.status, DecisionStatus.ELIGIBLE)
        self.assertEqual(self.query("SELECT status FROM experiments WHERE experiment_id=?",
                                    payload["experiment_id"]), [("complete",)])


class IntegrityTests(PhaseCase):
    def test_control_failure_invalidates_before_any_trial(self) -> None:
        broken = {"positive": {"outputs": {"answer": "INCORRECT"}}, "negatives": [{"outputs": {"answer": "x"}}]}
        spec = self.spec("m", ("a", "b"), tasks=default_tasks(controls=broken))
        executor = scripted(a=always, b=never)
        payload = self.run_exp(spec, executor)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(payload["runs"], [])
        self.assertEqual(self.db().trials(payload["experiment_id"]), [])
        self.assertIsNone(payload["selection"])
        self.assertEqual(payload["decision"]["status"], DecisionStatus.INVALID_EXPERIMENT.value)
        self.assertTrue(any("control positive of task v1" in reason for reason in payload["decision"]["reasons"]))
        self.assertEqual(self.query("SELECT status FROM experiments WHERE experiment_id=?",
                                    payload["experiment_id"]), [("invalid",)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM decisions WHERE experiment_id=?",
                                    payload["experiment_id"]), [(1,)])
        self.assertTrue(Path(payload["results_path"]).is_file())


class RetryExecutor:
    """``execute_trial`` adapter scripted per (system prompt, task description, repetition)."""

    def __init__(self, spec: ExperimentSpec, script: dict[tuple[str, str, int], Any]) -> None:
        self.seed = spec.seed
        self.descriptions = {task.task.description: task.id for task in spec.tasks}
        self.script = script
        self.calls: dict[tuple[str, str, int], int] = {}

    def execute_trial(self, request, *, workspace, backend, limits):
        key = (str(request.harness_public.get("system_prompt")), self.descriptions[request.task_description],
               request.seed - self.seed)
        self.calls[key] = self.calls.get(key, 0) + 1
        status, retryable, error = ExecutionStatus.COMPLETED, False, None
        step = self.script.get(key)
        if step is not None:
            status, retryable, error = step(self.calls[key])
        return ExecutionResult(
            trial_id=request.trial_id, status=status,
            outputs={"answer": "CORRECT"} if status is ExecutionStatus.COMPLETED else {},
            exit_code=None, stdout_tail="", stderr_tail="", tokens=Measurement.unknown(),
            latency_ms=Measurement.measured(1), adapter_latency_ms=Measurement.unknown(), returned_model=None,
            artifact_hash=None, error=error, retryable=retryable)


def infra_failure_until(success_call: int):
    def step(call: int):
        if call < success_call:
            return ExecutionStatus.EXECUTION_ERROR, True, "backend spawn failed"
        return ExecutionStatus.COMPLETED, False, None
    return step


class RetryTests(PhaseCase):
    def test_execution_error_retry_records_every_attempt_and_protocol_errors_are_not_retried(self) -> None:
        spec = self.spec("m", ("a",), statistics={"max_attempts": 3})
        script = {
            ("base", "v1", 0): infra_failure_until(3),  # two retryable failures, then success
            ("a", "v3", 2): infra_failure_until(99),  # always retryable: bounded by max_attempts
            ("a", "v2", 0): lambda call: (ExecutionStatus.EXECUTION_ERROR, False, "adapter protocol error: bad"),
            ("base", "r1", 1): lambda call: (ExecutionStatus.TIMEOUT, True, "timed out"),
            ("a", "r1", 3): lambda call: (ExecutionStatus.RESOURCE_LIMIT, True, "output limit"),
        }
        executor = RetryExecutor(spec, script)
        payload = self.run_exp(spec, executor)
        self.assertEqual(executor.calls[("base", "v1", 0)], 3)
        self.assertEqual(executor.calls[("a", "v3", 2)], 3)
        self.assertEqual(executor.calls[("a", "v2", 0)], 1)
        self.assertEqual(executor.calls[("base", "r1", 1)], 1)
        self.assertEqual(executor.calls[("a", "r1", 3)], 1)
        stored = self.db().trials(payload["experiment_id"])
        self.assertEqual(len(stored), 2 * len(HOLDOUT) * REPETITIONS + 2 + 2)
        self.assertEqual(len(payload["runs"]), len(stored))

        def attempts(variant: str, task: str, repetition: int):
            return [r for r in stored if (r.variant_id, r.task_id, r.repetition) == (variant, task, repetition)]

        retried = attempts("baseline", "v1", 0)
        self.assertEqual([r.attempt for r in retried], [1, 2, 3])
        self.assertEqual([r.execution.status for r in retried],
                         [ExecutionStatus.EXECUTION_ERROR, ExecutionStatus.EXECUTION_ERROR, ExecutionStatus.COMPLETED])
        self.assertEqual([r.attempt for r in attempts("a", "v3", 2)], [1, 2, 3])
        self.assertEqual([r.attempt for r in attempts("a", "v2", 0)], [1])
        evidence = set(payload["decision"]["evidence_ids"])
        self.assertIn(retried[-1].trial_id, evidence)
        self.assertFalse({r.trial_id for r in retried[:-1]} & evidence)


class LegacyTests(PhaseCase):
    def test_legacy_manifest_runs_all_variants_and_is_exploratory_only(self) -> None:
        manifest = write_manifest(self.root / "legacy", tasks=[exact_task("t1"), exact_task("t2")],
                                  variants=[{"id": "c1", "system_prompt": "c1"}, {"id": "c2", "system_prompt": "c2"}],
                                  repetitions=2)
        spec = ExperimentSpec.load(manifest)
        payload = self.run_exp(spec, scripted(c1=always, c2=never))
        self.assertEqual(len(payload["runs"]), 3 * 2 * 2)
        self.assertEqual({run["variant_id"] for run in payload["runs"]}, {"baseline", "c1", "c2"})
        self.assertEqual(payload["decision"]["status"], DecisionStatus.EXPLORATORY_ONLY.value)
        self.assertEqual(payload["mode"], "exploratory")
        self.assertEqual(payload["schema_version"], 1)
        self.assertIsNone(payload["selection"])
        self.assertEqual(payload["exposure"], {"fingerprints": [], "block_reason": None})
        self.assertEqual(payload["summary"]["recommended"], [])
        self.assertEqual(len(self.db().trials(payload["experiment_id"])), 12)
        self.assertEqual(self.query("SELECT COUNT(*) FROM holdout_exposure"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM selections"), [(0,)])

    def test_results_json_is_strict_json(self) -> None:
        manifest = write_manifest(self.root / "legacy", tasks=[exact_task("t1")],
                                  variants=[{"id": "c1", "system_prompt": "c1"}], repetitions=1,
                                  max_cost_ratio=None, max_latency_ratio=None)
        payload = self.run_exp(ExperimentSpec.load(manifest), scripted(c1=always))
        from harness_workbench.experiments.validation import json_loads_strict
        data = json_loads_strict(Path(payload["results_path"]).read_text(encoding="utf-8"))
        self.assertEqual(data["decision_id"], payload["decision_id"])
        self.assertIsNone(data["settings"]["max_cost_ratio"])

    def test_backend_comes_from_require_backend_when_not_given(self) -> None:
        manifest = write_manifest(self.root / "legacy", tasks=[exact_task("t1")],
                                  variants=[{"id": "c1", "system_prompt": "c1"}], repetitions=1)
        spec = ExperimentSpec.load(manifest)
        with mock.patch.object(core, "require_backend", return_value=LocalBackend()) as required:
            run_experiment(spec, scripted(c1=always), self.library)
        required.assert_called_once_with(spec.environment)


if __name__ == "__main__":
    unittest.main()
