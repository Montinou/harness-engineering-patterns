"""Trial pipeline: public request -> execution -> snapshot -> evaluation.

``run_trial`` is the only place that turns an executor's answer into a ``TrialRecord``. These tests
use scripted in-process executors and ``LocalBackend``; nothing here starts Docker or a model.
"""
from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_workbench.core.anthropic_backend import AnthropicHarnessExecutor
from harness_workbench.core.executor import BehavioralReport, MockExecutor, Task
from harness_workbench.core.harness import ALL_COMPONENTS, Harness
from harness_workbench.experiments import ExperimentSpec, core
from harness_workbench.experiments.backends import (
    BackendUnavailable, LocalBackend, ProcessLimits,
)
from harness_workbench.experiments.command import CommandHarnessExecutor
from harness_workbench.experiments.contracts import (
    EvaluationResult, ExecutionResult, Measurement, PublicTrialRequest, TrialRecord,
)
from harness_workbench.experiments.core import compare, run_experiment, run_trial
from harness_workbench.experiments.manifest import EnvironmentConfig
from harness_workbench.experiments.status import EvaluationStatus, ExecutionStatus, Provenance
from tests.support import ScriptedExecutor, exact_task, write_manifest

LIMITS = ProcessLimits(timeout_seconds=30)


def make_spec(root: Path, *, tasks: list[dict] | None = None, fixture: bool = False, **overrides):
    tasks = tasks if tasks is not None else [exact_task("t1")]
    if fixture:
        (root / "m" / "fixture").mkdir(parents=True, exist_ok=True)
        (root / "m" / "fixture" / "seed.txt").write_text("original", encoding="utf-8")
        tasks = [dict(task, fixture="fixture") for task in tasks]
    manifest = write_manifest(root / "m", tasks=tasks,
                              variants=[{"id": "candidate", "system_prompt": "cand"}], **overrides)
    return ExperimentSpec.load(manifest)


def execution_result(request: PublicTrialRequest, status: ExecutionStatus = ExecutionStatus.COMPLETED, *,
                     outputs: dict | None = None, error: str | None = None,
                     tokens: Measurement | None = None) -> ExecutionResult:
    return ExecutionResult(
        trial_id=request.trial_id, status=status, outputs=outputs if outputs is not None else {},
        exit_code=None, stdout_tail="", stderr_tail="",
        tokens=tokens or Measurement.unknown(), latency_ms=Measurement.measured(5),
        adapter_latency_ms=Measurement.unknown(), returned_model=None, artifact_hash=None,
        error=error, retryable=False)


class TrialExecutor:
    """An ``execute_trial`` executor that answers CORRECT, or whatever ``respond`` builds."""

    def __init__(self, respond=None, **attributes):
        self.respond = respond
        self.requests: list[PublicTrialRequest] = []
        self.calls: list[dict] = []
        for name, value in attributes.items():
            setattr(self, name, value)

    def execute_trial(self, request, *, workspace, backend, limits):
        self.requests.append(request)
        self.calls.append({"workspace": Path(workspace), "backend": backend, "limits": limits})
        if self.respond is not None:
            return self.respond(request, Path(workspace))
        return execution_result(request, outputs={"answer": "CORRECT"})


class PipelineCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def run_trial(self, spec, executor, *, task=None, backend=None, limits=LIMITS, repetition=0, attempt=1):
        return run_trial(
            spec, spec.variants[1], task or spec.tasks[0], repetition=repetition, attempt=attempt,
            executor=executor, backend=backend or LocalBackend(), limits=limits,
            experiment_id="exp", implementation_hash="impl")


class TrialRecordTests(PipelineCase):
    def test_trial_record_has_explicit_statuses_and_scores_only_when_usable(self) -> None:
        spec = make_spec(self.root)
        passed = self.run_trial(spec, TrialExecutor())
        self.assertIsInstance(passed, TrialRecord)
        self.assertEqual(passed.execution.status, ExecutionStatus.COMPLETED)
        self.assertEqual(passed.evaluation.status, EvaluationStatus.PASSED)
        self.assertEqual(passed.objective_score, 1.0)
        self.assertTrue(passed.usable)
        self.assertEqual((passed.variant_id, passed.task_id, passed.split, passed.family, passed.repetition,
                          passed.seed, passed.attempt, passed.implementation_hash),
                         ("candidate", "t1", "unsplit", spec.tasks[0].family, 0, spec.seed, 1, "impl"))

        failed = self.run_trial(spec, TrialExecutor(
            lambda request, workspace: execution_result(request, outputs={"answer": "WRONG"})), repetition=3)
        self.assertEqual(failed.evaluation.status, EvaluationStatus.FAILED)
        self.assertEqual(failed.objective_score, 0.0)
        self.assertTrue(failed.usable)
        self.assertEqual(failed.seed, spec.seed + 3)

        errored = self.run_trial(spec, TrialExecutor(
            lambda request, workspace: execution_result(request, ExecutionStatus.EXECUTION_ERROR, error="boom")))
        self.assertIsNone(errored.objective_score)
        self.assertIsNone(errored.evaluation.score)
        self.assertFalse(errored.usable)

        data = passed.to_dict()
        self.assertEqual(data["execution"]["status"], "completed")
        self.assertEqual(data["evaluation"]["status"], "passed")
        self.assertEqual(data["execution"]["artifact_hash"], data["evaluation"]["artifact_hash"])
        self.assertIsNotNone(data["evaluation"]["artifact_hash"])

    def test_request_is_public_and_carries_the_derived_seed_and_budget(self) -> None:
        spec = make_spec(self.root)
        executor = TrialExecutor()
        record = self.run_trial(spec, executor, repetition=2, limits=ProcessLimits(timeout_seconds=9, max_output_bytes=777))
        request = executor.requests[0]
        self.assertEqual(request.trial_id, record.trial_id)
        self.assertEqual(request.seed, spec.seed + 2)
        self.assertEqual(request.budget, {"timeout_seconds": 9, "max_output_bytes": 777})
        self.assertEqual(request.harness_public, spec.variants[1].harness.public_components())
        self.assertEqual(request.task_description, spec.tasks[0].task.description)
        self.assertEqual(request.fixture_hash, spec.tasks[0].fixture_hash)
        self.assertNotIn("candidate", repr(request.to_dict()))  # no variant identity
        self.assertEqual(len(record.trial_id), 32)

    def test_execution_error_leads_to_not_evaluated(self) -> None:
        spec = make_spec(self.root)
        raising = TrialExecutor(lambda request, workspace: (_ for _ in ()).throw(RuntimeError("adapter blew up")))
        record = self.run_trial(spec, raising)
        self.assertEqual(record.execution.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertFalse(record.execution.retryable)
        self.assertIn("RuntimeError: adapter blew up", record.execution.error)
        self.assertEqual(record.evaluation.status, EvaluationStatus.NOT_EVALUATED)
        self.assertIsNone(record.evaluation.score)

        reported = self.run_trial(spec, TrialExecutor(
            lambda request, workspace: execution_result(request, ExecutionStatus.EXECUTION_ERROR, error="spawn failed")))
        self.assertEqual(reported.evaluation.status, EvaluationStatus.NOT_EVALUATED)
        self.assertEqual(reported.to_dict()["error"], "spawn failed")

        wrong_type = self.run_trial(spec, TrialExecutor(lambda request, workspace: {"answer": "CORRECT"}))
        self.assertEqual(wrong_type.execution.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertEqual(wrong_type.evaluation.status, EvaluationStatus.NOT_EVALUATED)


class LegacyShimTests(PipelineCase):
    def test_legacy_execute_case_adapter_is_wrapped_with_declared_provenance_and_not_applicable_latency(self) -> None:
        spec = make_spec(self.root)
        scripted = ScriptedExecutor({"cand": lambda task_id: True}, tokens=123, latency_ms=45)
        record = self.run_trial(spec, scripted)
        self.assertEqual(record.execution.tokens, Measurement.reported(123, Provenance.ADAPTER_REPORTED))
        self.assertEqual(record.execution.latency_ms, Measurement.not_applicable())
        self.assertEqual(record.execution.status, ExecutionStatus.COMPLETED)
        self.assertEqual(record.evaluation.status, EvaluationStatus.PASSED)

        class Undeclared:
            def execute_case(self, harness, task, context):
                return BehavioralReport(harness.id, harness.version, task.id, 1.0,
                                        outputs={"answer": "CORRECT"}, cost_tokens=9, latency_ms=9)

        undeclared = self.run_trial(spec, Undeclared())
        self.assertEqual(undeclared.execution.tokens, Measurement.unknown())
        self.assertEqual(undeclared.execution.latency_ms, Measurement.not_applicable())

        class Misdeclared(Undeclared):
            usage_provenance = "measured"  # not a provenance a legacy report can claim

        self.assertEqual(self.run_trial(spec, Misdeclared()).execution.tokens, Measurement.unknown())

    def test_legacy_execute_without_workspace_support_is_still_run(self) -> None:
        spec = make_spec(self.root)

        class Flat:
            usage_provenance = "estimated"

            def execute(self, harness, task):
                return BehavioralReport(harness.id, harness.version, task.id, 0.1,
                                        outputs={"answer": "CORRECT"}, cost_tokens=5)

        record = self.run_trial(spec, Flat())
        self.assertEqual(record.evaluation.status, EvaluationStatus.PASSED)
        self.assertEqual(record.execution.tokens, Measurement.reported(5, Provenance.ESTIMATED))

    def test_legacy_context_carries_repetition_seed_and_workspace(self) -> None:
        spec = make_spec(self.root)
        seen = []

        class Spy:
            def execute_case(self, harness, task, context):
                seen.append(context)
                return BehavioralReport(harness.id, harness.version, task.id, 1.0, outputs={"answer": "CORRECT"})

        self.run_trial(spec, Spy(), repetition=4)
        context = seen[0]
        self.assertEqual((context.experiment_id, context.case_id, context.variant_id, context.repetition, context.seed),
                         ("exp", "t1", "candidate", 4, spec.seed + 4))
        self.assertTrue(context.workspace.name == "workspace")

    def test_legacy_exception_is_a_non_retryable_execution_error(self) -> None:
        spec = make_spec(self.root)
        record = self.run_trial(spec, ScriptedExecutor({}, crash_on_call=1))
        self.assertEqual(record.execution.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertFalse(record.execution.retryable)
        self.assertIn("SimulatedCrash", record.execution.error)
        self.assertEqual(record.evaluation.status, EvaluationStatus.NOT_EVALUATED)

    def test_mock_executor_tokens_are_estimated(self) -> None:
        self.assertEqual(MockExecutor.usage_provenance, "estimated")
        self.assertEqual(MockExecutor.supports, frozenset(ALL_COMPONENTS))
        self.assertEqual(AnthropicHarnessExecutor.usage_provenance, "provider_reported")
        spec = make_spec(self.root)
        record = self.run_trial(spec, MockExecutor())
        self.assertEqual(record.execution.tokens.provenance, Provenance.ESTIMATED)
        self.assertGreater(record.execution.tokens.value, 0)
        self.assertEqual(record.execution.latency_ms, Measurement.not_applicable())


class TimeoutPolicyTests(PipelineCase):
    def timeout_executor(self):
        return TrialExecutor(lambda request, workspace: execution_result(
            request, ExecutionStatus.TIMEOUT, outputs={"answer": "CORRECT"}, error="timed out after 1s"))

    def test_timeout_is_evaluated_only_under_the_declared_policy(self) -> None:
        spec = make_spec(self.root)
        default = self.run_trial(spec, self.timeout_executor())
        self.assertFalse(spec.statistics.timeout_is_task_failure)
        self.assertEqual(default.execution.status, ExecutionStatus.TIMEOUT)
        self.assertEqual(default.evaluation.status, EvaluationStatus.NOT_EVALUATED)
        self.assertIsNone(default.evaluation.score)

        policy = dataclasses.replace(
            spec, statistics=dataclasses.replace(spec.statistics, timeout_is_task_failure=True))
        counted = self.run_trial(policy, self.timeout_executor())
        self.assertEqual(counted.execution.status, ExecutionStatus.TASK_FAILURE)
        self.assertEqual(counted.execution.error, "timed out after 1s")
        # The outputs would pass the grader, yet a timeout is a counted failure, never a pass.
        self.assertEqual(counted.evaluation.status, EvaluationStatus.FAILED)
        self.assertEqual(counted.evaluation.score, 0.0)
        self.assertEqual(counted.objective_score, 0.0)
        self.assertTrue(counted.usable)  # kept in the denominator, not dropped
        self.assertIn("timed out under declared policy",
                      [c["detail"] for c in counted.evaluation.checks if c["id"] == "timeout_policy"])
        data = counted.to_dict()
        self.assertEqual(data["error"], "timed out after 1s")
        self.assertIs(data["passed"], False)
        self.assertIs(data["verified"], False)
        self.assertEqual(data["objective_score"], 0.0)
        self.assertIs(default.to_dict()["passed"], False)
        self.assertIs(default.to_dict()["verified"], False)
        self.assertIsNone(default.to_dict()["objective_score"])

    def test_counted_timeout_with_passing_outputs_stays_a_failure_in_the_summary_and_after_a_round_trip(self) -> None:
        spec = make_spec(self.root)
        policy = dataclasses.replace(
            spec, statistics=dataclasses.replace(spec.statistics, timeout_is_task_failure=True))
        counted = self.run_trial(policy, self.timeout_executor())
        again = TrialRecord.from_dict(counted.to_dict())
        self.assertIs(again.to_dict()["passed"], False)
        self.assertIs(again.to_dict()["verified"], False)
        self.assertEqual(again.objective_score, 0.0)
        summary = core._summary([counted])
        self.assertEqual(summary["pass_rate"], 0.0)
        self.assertEqual(summary["mean_score"], 0.0)


class FixtureTests(PipelineCase):
    def test_fixture_tamper_is_execution_error(self) -> None:
        spec = make_spec(self.root, fixture=True)
        executor = TrialExecutor()
        (self.root / "m" / "fixture" / "seed.txt").write_text("tampered", encoding="utf-8")
        record = self.run_trial(spec, executor)
        self.assertEqual(record.execution.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertEqual(record.execution.error, "source fixture changed during experiment")
        self.assertFalse(record.execution.retryable)
        self.assertEqual(record.evaluation.status, EvaluationStatus.NOT_EVALUATED)
        self.assertEqual(executor.calls, [])
        self.assertEqual(record.to_dict()["error"], "source fixture changed during experiment")

    def test_fixture_is_copied_into_a_fresh_workspace(self) -> None:
        spec = make_spec(self.root, fixture=True)
        seen = []

        def respond(request, workspace):
            seen.append((workspace, (workspace / "seed.txt").read_text()))
            (workspace / "seed.txt").write_text("changed", encoding="utf-8")
            return execution_result(request, outputs={"answer": "CORRECT"})

        self.run_trial(spec, TrialExecutor(respond))
        self.run_trial(spec, TrialExecutor(respond))
        self.assertEqual([text for _, text in seen], ["original", "original"])
        self.assertNotEqual(seen[0][0], seen[1][0])
        self.assertEqual((self.root / "m" / "fixture" / "seed.txt").read_text(), "original")
        self.assertFalse(seen[0][0].exists())  # the temp directory is gone after the trial

    def test_no_fixture_means_an_empty_workspace(self) -> None:
        spec = make_spec(self.root)
        listing = []

        def respond(request, workspace):
            listing.append(sorted(workspace.iterdir()))
            return execution_result(request, outputs={"answer": "CORRECT"})

        self.run_trial(spec, TrialExecutor(respond))
        self.assertEqual(listing, [[]])

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_fixture_is_copied_without_following_symlinks(self) -> None:
        outside = self.root / "outside-secret.txt"
        outside.write_text("canary-value", encoding="utf-8")
        spec = make_spec(self.root, fixture=True)
        link = self.root / "m" / "fixture" / "link.txt"
        link.symlink_to(outside)
        # The manifest was loaded before the link existed, so re-freeze the fixture the way a load would.
        from harness_workbench.experiments.grader import digest_path
        task = dataclasses.replace(spec.tasks[0], fixture_hash=digest_path(spec.tasks[0].fixture))
        seen = []

        def respond(request, workspace):
            copied = workspace / "link.txt"
            seen.append((copied.is_symlink(), os.readlink(copied) if copied.is_symlink() else None))
            return execution_result(request, outputs={"answer": "CORRECT"})

        record = self.run_trial(spec, TrialExecutor(respond), task=task)
        self.assertEqual(seen, [(True, str(outside))])  # the link is preserved, its target not copied in
        # The evaluator's snapshot refuses a symlink, so the trial is an evaluation error, never a pass.
        self.assertEqual(record.evaluation.status, EvaluationStatus.EVALUATION_ERROR)
        self.assertIn("symlink", record.evaluation.error.lower())


def make_record(variant="candidate", task="t1", repetition=0, *, execution=ExecutionStatus.COMPLETED,
                evaluation=EvaluationStatus.PASSED, score=1.0, error=None, evaluation_error=None,
                tokens=None, trial="a" * 32) -> TrialRecord:
    """A TrialRecord built by hand, so a summary or contrast sees exactly the statuses a test names."""
    return TrialRecord(
        trial, variant, task, "unsplit", "family", repetition, 7 + repetition, 1,
        ExecutionResult(trial, execution, {}, None, "", "", tokens or Measurement.unknown(),
                        Measurement.not_applicable(), Measurement.unknown(), None, None, error, False),
        EvaluationResult(trial, evaluation, "rubric", None, [], score, evaluation_error),
        "impl")


class UnusableRunsNeverLookUsableTests(PipelineCase):
    UNUSABLE = (ExecutionStatus.TIMEOUT, ExecutionStatus.RESOURCE_LIMIT, ExecutionStatus.EXECUTION_ERROR,
                ExecutionStatus.CANCELLED)

    def test_unusable_execution_that_would_pass_reads_unverified_and_unpassed(self) -> None:
        spec = make_spec(self.root)
        for status in self.UNUSABLE:
            with self.subTest(status=status.value):
                record = self.run_trial(spec, TrialExecutor(lambda request, workspace, status=status: execution_result(
                    request, status, outputs={"answer": "CORRECT"}, error=f"{status.value} happened")))
                data = record.to_dict()
                self.assertEqual(record.evaluation.status, EvaluationStatus.NOT_EVALUATED)
                self.assertIs(data["verified"], False)
                self.assertIs(data["passed"], False)
                self.assertIsNone(data["objective_score"])
                self.assertIsNone(record.objective_score)
                self.assertFalse(record.usable)
                self.assertEqual(data["error"], f"{status.value} happened")

    def test_timeout_that_would_pass_is_not_verified_or_passed(self) -> None:
        spec = make_spec(self.root)
        record = self.run_trial(spec, TrialExecutor(lambda request, workspace: execution_result(
            request, ExecutionStatus.TIMEOUT, outputs={"answer": "CORRECT"}, error="timed out after 1s")))
        data = record.to_dict()
        self.assertIs(data["verified"], False)
        self.assertIs(data["passed"], False)
        self.assertIsNone(data["objective_score"])

    def test_a_hand_built_unusable_execution_with_a_passing_evaluation_cannot_read_true(self) -> None:
        for status in self.UNUSABLE:
            with self.subTest(status=status.value):
                record = make_record(execution=status, error="x", evaluation=EvaluationStatus.PASSED, score=1.0)
                data = record.to_dict()
                self.assertFalse(record.usable)
                self.assertIsNone(record.objective_score)
                self.assertIs(data["verified"], False)
                self.assertIs(data["passed"], False)
                self.assertIsNone(data["objective_score"])

    def test_legacy_resource_limit_report_is_an_unusable_not_evaluated_trial(self) -> None:
        class LegacyOnly:
            """Only the pre-execute_trial entry point, as an old adapter exposes it."""

            def __init__(self, inner):
                self.inner = inner

            def execute_case(self, harness, task, context):
                return self.inner.execute_case(harness, task, context)

        inner = CommandHarnessExecutor(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000)"],
            limits=ProcessLimits(timeout_seconds=30, max_output_bytes=100))
        spec = make_spec(self.root)
        for executor in (LegacyOnly(inner), inner):
            with self.subTest(path="shim" if isinstance(executor, LegacyOnly) else "execute_trial"):
                record = self.run_trial(spec, executor)
                self.assertEqual(record.execution.status, ExecutionStatus.RESOURCE_LIMIT)
                self.assertEqual(record.evaluation.status, EvaluationStatus.NOT_EVALUATED)
                self.assertIsNone(record.objective_score)
                self.assertFalse(record.usable)
                self.assertIs(record.to_dict()["verified"], False)
                self.assertIs(record.to_dict()["passed"], False)

    def test_task_failure_and_evaluation_error_keep_their_meaning(self) -> None:
        failure = make_record(execution=ExecutionStatus.TASK_FAILURE, evaluation=EvaluationStatus.FAILED, score=0.0)
        self.assertTrue(failure.usable)
        self.assertIs(failure.to_dict()["verified"], True)
        self.assertIs(failure.to_dict()["passed"], False)
        errored = make_record(evaluation=EvaluationStatus.EVALUATION_ERROR, score=None, evaluation_error="grader down")
        self.assertFalse(errored.usable)
        self.assertIs(errored.to_dict()["verified"], False)
        self.assertEqual(errored.to_dict()["error"], "grader down")


class SummaryTests(unittest.TestCase):
    def test_unusable_runs_do_not_leak_a_zero_score_into_the_summary(self) -> None:
        rows = [
            make_record(repetition=0, score=1.0),
            make_record(repetition=1, evaluation=EvaluationStatus.FAILED, score=0.5),
            make_record(repetition=2, evaluation=EvaluationStatus.EVALUATION_ERROR, score=None,
                        evaluation_error="grader down"),
            make_record(repetition=3, execution=ExecutionStatus.TIMEOUT, evaluation=EvaluationStatus.NOT_EVALUATED,
                        score=None, error="timed out"),
        ]
        summary = core._summary(rows)
        self.assertEqual(summary["runs"], 4)
        self.assertEqual(summary["verified_runs"], 2)
        self.assertEqual(summary["mean_score"], 0.75)  # (1.0 + 0.5) / 2, not / 4
        self.assertEqual(summary["pass_rate"], 0.5)  # one of the two usable runs passed
        self.assertEqual(summary["failure_rate"], 0.5)  # the evaluation error and the timeout
        self.assertAlmostEqual(summary["stdev_score"], 0.353553, places=6)

    def test_no_usable_run_means_no_score_at_all(self) -> None:
        rows = [make_record(execution=ExecutionStatus.EXECUTION_ERROR, evaluation=EvaluationStatus.NOT_EVALUATED,
                            score=None, error="boom"),
                make_record(repetition=1, evaluation=EvaluationStatus.EVALUATION_ERROR, score=None,
                            evaluation_error="grader down")]
        summary = core._summary(rows)
        self.assertEqual(summary["verified_runs"], 0)
        self.assertIsNone(summary["mean_score"])
        self.assertIsNone(summary["stdev_score"])
        self.assertIsNone(summary["pass_rate"])
        self.assertEqual(summary["failure_rate"], 1.0)
        self.assertEqual(core._summary([])["mean_score"], None)

    def test_usable_inconsistent_record_does_not_count(self) -> None:
        rows = [make_record(execution=ExecutionStatus.TIMEOUT, error="late", evaluation=EvaluationStatus.PASSED, score=1.0)]
        summary = core._summary(rows)
        self.assertEqual(summary["verified_runs"], 0)
        self.assertIsNone(summary["mean_score"])

    def test_tokens_and_latency_average_only_known_measurements(self) -> None:
        rows = [make_record(repetition=0, tokens=Measurement.reported(100, Provenance.ADAPTER_REPORTED)),
                make_record(repetition=1, tokens=Measurement.unknown()),
                make_record(repetition=2, tokens=Measurement.reported(300, Provenance.ADAPTER_REPORTED))]
        summary = core._summary(rows)
        self.assertEqual(summary["mean_tokens"], 200.0)
        self.assertIsNone(summary["mean_latency_ms"])  # not applicable for every row
        self.assertIsNone(core._summary([make_record()])["mean_tokens"])


class ContrastTests(PipelineCase):
    def spec(self):
        return make_spec(self.root, repetitions=2)

    def records(self, candidate):
        baseline = [make_record("baseline", repetition=i, score=0.5, evaluation=EvaluationStatus.FAILED) for i in range(2)]
        return baseline + candidate

    def test_paired_delta_skips_pairs_with_an_unusable_side(self) -> None:
        spec = self.spec()
        candidate = [make_record("candidate", repetition=0, score=1.0),
                     make_record("candidate", repetition=1, evaluation=EvaluationStatus.EVALUATION_ERROR,
                                 score=None, evaluation_error="grader down")]
        decision = compare(spec, self.records(candidate))["contrasts"]["candidate"]
        self.assertEqual(decision["mean_delta"], 0.5)  # the errored pair is absent, not scored 0.0
        self.assertEqual(decision["per_task_delta"], {"t1": 0.5})
        self.assertFalse(decision["eligible"])
        self.assertIn("insufficient paired samples", decision["reasons"])
        self.assertIn("missing objective verification or execution failure", decision["reasons"])

    def test_unusable_baseline_side_is_skipped_the_same_way(self) -> None:
        spec = self.spec()
        baseline = [make_record("baseline", repetition=0, score=0.5, evaluation=EvaluationStatus.FAILED),
                    make_record("baseline", repetition=1, execution=ExecutionStatus.TIMEOUT,
                                evaluation=EvaluationStatus.NOT_EVALUATED, score=None, error="late")]
        candidate = [make_record("candidate", repetition=i, score=1.0) for i in range(2)]
        decision = compare(spec, baseline + candidate)["contrasts"]["candidate"]
        self.assertEqual(decision["mean_delta"], 0.5)
        self.assertEqual(decision["per_task_delta"], {"t1": 0.5})
        self.assertFalse(decision["eligible"])

    def test_all_unusable_variant_has_no_score_and_does_not_dominate(self) -> None:
        spec = self.spec()
        candidate = [make_record("candidate", repetition=i, execution=ExecutionStatus.EXECUTION_ERROR,
                                 evaluation=EvaluationStatus.NOT_EVALUATED, score=None, error="boom") for i in range(2)]
        result = compare(spec, self.records(candidate))
        self.assertIsNone(result["variants"]["candidate"]["mean_score"])
        self.assertEqual(result["contrasts"]["candidate"]["mean_delta"], 0)
        self.assertFalse(result["contrasts"]["candidate"]["eligible"])
        self.assertEqual(result["recommended"], [])
        self.assertIn("baseline", result["pareto_frontier"])

    def test_unknown_tokens_do_not_break_the_cost_ratio(self) -> None:
        spec = self.spec()
        known = Measurement.reported(100, Provenance.ADAPTER_REPORTED)
        baseline = [make_record("baseline", repetition=i, score=0.5, evaluation=EvaluationStatus.FAILED, tokens=known)
                    for i in range(2)]
        candidate = [make_record("candidate", repetition=i, score=1.0) for i in range(2)]
        decision = compare(spec, baseline + candidate)["contrasts"]["candidate"]
        self.assertIsNone(decision["token_ratio"])
        self.assertIsNone(decision["latency_ratio"])


class EffectiveLimitsTests(PipelineCase):
    def limits_seen(self, executor, limits):
        spec = make_spec(self.root)
        recorder = TrialExecutor(**executor)
        self.run_trial(spec, recorder, limits=limits)
        return recorder.calls[0]["limits"], recorder.requests[0].budget

    def test_the_executors_declared_timeout_caps_a_looser_environment(self) -> None:
        seen, budget = self.limits_seen({"timeout_seconds": 2.0}, ProcessLimits(timeout_seconds=60))
        self.assertEqual(seen.timeout_seconds, 2.0)
        self.assertEqual(budget["timeout_seconds"], 2.0)

    def test_a_stricter_environment_wins_over_a_looser_executor(self) -> None:
        seen, budget = self.limits_seen({"timeout_seconds": 600.0}, ProcessLimits(timeout_seconds=1.5))
        self.assertEqual(seen.timeout_seconds, 1.5)
        self.assertEqual(budget["timeout_seconds"], 1.5)

    def test_the_executors_own_limits_apply_field_by_field_with_the_stricter_value(self) -> None:
        own = ProcessLimits(timeout_seconds=3, max_output_bytes=10, max_files=5000, max_total_bytes=100)
        seen, budget = self.limits_seen({"limits": own}, ProcessLimits(
            timeout_seconds=60, max_output_bytes=1_000, max_files=7, max_total_bytes=1_000_000))
        self.assertEqual(seen, ProcessLimits(timeout_seconds=3, max_output_bytes=10, max_files=7, max_total_bytes=100))
        self.assertEqual(budget, {"timeout_seconds": 3, "max_output_bytes": 10})

    def test_an_executor_without_declared_limits_gets_the_environment_unchanged(self) -> None:
        environment = ProcessLimits(timeout_seconds=42, max_output_bytes=999)
        seen, _ = self.limits_seen({}, environment)
        self.assertEqual(seen, environment)

    def test_invalid_declared_timeout_is_ignored_rather_than_trusted(self) -> None:
        environment = ProcessLimits(timeout_seconds=42)
        for bad in (True, "5", float("nan"), -1, 0, None):
            with self.subTest(bad=bad):
                seen, _ = self.limits_seen({"timeout_seconds": bad}, environment)
                self.assertEqual(seen, environment)

    def test_the_grader_keeps_the_environment_limits_not_the_solver_cap(self) -> None:
        spec = make_spec(self.root)
        environment = ProcessLimits(timeout_seconds=60)
        with mock.patch.object(core, "evaluate_bundle", wraps=core.evaluate_bundle) as evaluate:
            self.run_trial(spec, TrialExecutor(timeout_seconds=2.0), limits=environment)
        self.assertEqual(evaluate.call_args.kwargs["limits"], environment)

    def test_a_command_executors_own_timeout_actually_stops_a_slow_solver(self) -> None:
        spec = make_spec(self.root)
        executor = CommandHarnessExecutor([sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=0.5)
        record = self.run_trial(spec, executor, limits=ProcessLimits(timeout_seconds=60))
        self.assertEqual(record.execution.status, ExecutionStatus.TIMEOUT)
        self.assertLess(record.execution.latency_ms.value, 20_000)
        self.assertEqual(record.evaluation.status, EvaluationStatus.NOT_EVALUATED)
        self.assertIs(record.to_dict()["verified"], False)


class BackendSelectionTests(PipelineCase):
    def test_trusted_local_environment_selects_the_local_backend(self) -> None:
        spec = make_spec(self.root)
        executor = TrialExecutor()
        run_experiment(dataclasses.replace(spec, repetitions=1), executor, self.root / "out")
        self.assertTrue(executor.calls)
        self.assertTrue(all(isinstance(call["backend"], LocalBackend) for call in executor.calls))

    def test_the_backend_comes_from_the_environment_mode(self) -> None:
        spec = make_spec(self.root)
        sentinel = LocalBackend()
        executor = TrialExecutor()
        with mock.patch.object(core, "build_backend", return_value=sentinel) as build:
            run_experiment(dataclasses.replace(spec, repetitions=1), executor, self.root / "out")
        build.assert_called_once_with(spec.environment)
        self.assertTrue(all(call["backend"] is sentinel for call in executor.calls))

    def test_sandboxed_mode_without_a_usable_docker_refuses_and_never_runs_locally(self) -> None:
        spec = dataclasses.replace(make_spec(self.root), environment=EnvironmentConfig(mode="sandboxed"))
        executor = TrialExecutor()
        empty = self.root / "empty-bin"
        empty.mkdir()
        out = self.root / "out"
        with mock.patch.dict(os.environ, {"PATH": str(empty)}), \
                mock.patch.object(core, "LocalBackend", side_effect=AssertionError("fell back to LocalBackend"),
                                  create=True):
            with self.assertRaises(BackendUnavailable):
                run_experiment(spec, executor, out)
        self.assertEqual(executor.calls, [])
        self.assertFalse((out / "experiments").exists())  # nothing was started

    def test_unknown_environment_mode_cannot_reach_a_backend(self) -> None:
        spec = make_spec(self.root)
        environment = EnvironmentConfig(mode="trusted_local")
        object.__setattr__(environment, "mode", "mystery")
        with self.assertRaises(BackendUnavailable):
            run_experiment(dataclasses.replace(spec, environment=environment), TrialExecutor(), self.root / "out")


class ExperimentEndToEndTests(PipelineCase):
    def test_unusable_runs_are_persisted_with_errors_and_no_score(self) -> None:
        spec = make_spec(self.root, repetitions=2)
        executor = TrialExecutor(lambda request, workspace: execution_result(
            request, ExecutionStatus.EXECUTION_ERROR, error="adapter exploded"))
        payload = run_experiment(spec, executor, self.root / "out")
        runs = payload["runs"]
        self.assertEqual(len(runs), 4)
        for run in runs:
            self.assertEqual(run["execution"]["status"], "execution_error")
            self.assertEqual(run["evaluation"]["status"], "not_evaluated")
            self.assertIsNone(run["objective_score"])
            self.assertIs(run["verified"], False)
            self.assertIs(run["passed"], False)
            self.assertEqual(run["error"], "adapter exploded")
        summary = payload["summary"]["variants"]["candidate"]
        self.assertIsNone(summary["mean_score"])
        self.assertIsNone(summary["pass_rate"])
        self.assertEqual(summary["failure_rate"], 1.0)
        self.assertEqual(summary["verified_runs"], 0)
        self.assertFalse(payload["summary"]["contrasts"]["candidate"]["eligible"])
        html = Path(payload["report_path"]).read_text(encoding="utf-8")
        self.assertIn("adapter exploded", html)
        results = (Path(payload["results_path"])).read_text(encoding="utf-8")
        self.assertNotIn("NaN", results)

    def test_every_repetition_gets_a_fresh_trial_id_and_the_paired_seed(self) -> None:
        spec = make_spec(self.root, repetitions=3)
        payload = run_experiment(spec, TrialExecutor(), self.root / "out")
        runs = payload["runs"]
        self.assertEqual(len({run["trial_id"] for run in runs}), len(runs))
        for run in runs:
            self.assertEqual(run["seed"], spec.seed + run["repetition"])
            self.assertEqual(run["attempt"], 1)
            self.assertEqual(run["case_id"], run["task_id"])
        self.assertEqual(len({run["implementation_hash"] for run in runs}), 1)


if __name__ == "__main__":
    unittest.main()
