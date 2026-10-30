"""Regression tests for the phase 4 review (round 1): holdout exposure, overlapping confirmations,
the exit-125 retry loophole and the infrastructure failures of the ``improve`` validation gate."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from harness_workbench import cli
from harness_workbench.core.db import PromotionConflict, StoreBusyError, WorkbenchDB
from harness_workbench.core.executor import MockExecutor, Task
from harness_workbench.core.improver import Improver
from harness_workbench.core.proposer import RuleBasedProposer
from harness_workbench.experiments import ExperimentSpec, run_experiment
from harness_workbench.experiments.backends import BackendUnavailable, DockerBackend, LocalBackend
from harness_workbench.experiments.command import CommandHarnessExecutor
from harness_workbench.experiments.contracts import ExperimentDecision
from harness_workbench.experiments.status import DecisionStatus
from tests.support import exact_task, write_manifest
from tests.unit.test_cli_experiments import CliCase, SpyExecutor, run_cli
from tests.unit.test_improver_gate import base_harness
from tests.unit.test_run_experiment_phases import (
    HOLDOUT, REPETITIONS, PhaseCase, always, confirmatory_manifest, default_tasks, never, scripted,
)

TRUSTED = {"mode": "trusted_local", "trusted_solver_acknowledged": True}


def eligible_decision(plan, records, *, spec, selection, integrity_problems, exploratory_reason):
    """A decider that always claims eligibility; the runner and the store must still guard it."""
    return ExperimentDecision(
        DecisionStatus.ELIGIBLE, [], 0.5, 0.1, 4, plan.plan_hash, selection.finalist_id,
        selection.finalist_hash, selection.baseline_hash, spec.variants[0].harness.id,
        spec.variants[0].harness.version, [record.trial_id for record in records], [], {})


class ExploratoryHoldoutExposureTests(PhaseCase):
    """Finding 1: an exploratory run that executes holdout tasks consumes them."""

    def exploratory(self, name: str, candidates: tuple[str, ...], tasks: list[dict]) -> ExperimentSpec:
        return ExperimentSpec.load(write_manifest(
            self.root / name, tasks=tasks, variants=[{"id": n, "system_prompt": n} for n in candidates],
            schema_version=2, mode="exploratory", family="phases", dataset={"id": "phases", "version": "1"},
            repetitions=REPETITIONS, environment=TRUSTED))

    def test_exploratory_run_over_holdout_tasks_blocks_a_later_confirmation_of_the_same_content(self) -> None:
        names = ("a", "b", "c", "d", "e")
        spec = self.exploratory("expl", names, default_tasks())
        payload = self.run_exp(spec, scripted(a=never, b=never, c=always, d=never, e=never))
        self.assertEqual(payload["decision"]["status"], DecisionStatus.EXPLORATORY_ONLY.value)
        fingerprints = sorted({task.content_fingerprint for task in spec.tasks if task.id in HOLDOUT})
        rows = self.query("SELECT task_fingerprint, decision_status, blocked_reason FROM holdout_exposure"
                          " WHERE experiment_id=? ORDER BY task_fingerprint", payload["experiment_id"])
        self.assertEqual([row[0] for row in rows], fingerprints)
        self.assertEqual({row[1:] for row in rows}, {("exploratory_only", None)})
        # the winner of the exploratory run, confirmed on the same content under other ids
        confirm = self.spec("conf", ("c",), tasks=default_tasks(prefix="renamed-"))
        second = self.run_exp(confirm, scripted(c=always))
        self.assertIn("already consumed", second["exposure"]["block_reason"])
        self.assertEqual(second["decision"]["status"], DecisionStatus.EXPLORATORY_ONLY.value)

    def test_exploratory_run_without_holdout_splits_records_no_exposure(self) -> None:
        development_only = [exact_task(f"d{i}", split="development", runs_candidate_code=False)
                            for i in range(1, 7)]
        spec = self.exploratory("dev", ("a", "b"), development_only)
        self.run_exp(spec, scripted(a=always, b=never))
        self.assertEqual(self.query("SELECT COUNT(*) FROM holdout_exposure"), [(0,)])

    def test_exposure_is_written_before_the_first_holdout_trial(self) -> None:
        spec = self.exploratory("order", ("a",), default_tasks())
        seen: list[int] = []

        class Probe:
            usage_provenance = "adapter_reported"

            def __init__(self, inner: Any, outer: PhaseCase) -> None:
                self.inner, self.outer = inner, outer

            def execute_case(self, harness: Any, task: Any, context: Any) -> Any:
                if task.id in HOLDOUT and not seen:
                    seen.append(self.outer.query("SELECT COUNT(*) FROM holdout_exposure")[0][0])
                return self.inner.execute_case(harness, task, context)

        self.run_exp(spec, Probe(scripted(a=always), self))
        self.assertEqual(seen, [len(HOLDOUT)])


class OverlappingConfirmationTests(PhaseCase):
    """Finding 2: two confirmations of one candidate must not both end eligible."""

    def test_second_decision_on_the_same_holdout_is_downgraded_in_the_store(self) -> None:
        first = self.spec("m1", ("a",))
        second = self.spec("m2", ("a",))
        results: dict[str, Any] = {}
        release = threading.Event()

        class Holding:
            usage_provenance = "adapter_reported"

            def __init__(self, inner: Any) -> None:
                self.inner, self.held = inner, False

            def execute_case(self, harness: Any, task: Any, context: Any) -> Any:
                if not self.held:
                    self.held = True
                    release.wait(60)
                return self.inner.execute_case(harness, task, context)

        thread = threading.Thread(target=lambda: results.__setitem__("first", self.run_exp(
            first, Holding(scripted(a=always)), decide=eligible_decision)))
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        for _ in range(400):
            try:
                if self.query("SELECT COUNT(*) FROM holdout_exposure")[0][0]:
                    break
            except Exception:  # the store does not exist yet
                pass
            threading.Event().wait(0.05)
        results["second"] = self.run_exp(second, scripted(a=always), decide=eligible_decision)
        release.set()
        thread.join()
        self.assertEqual(results["second"]["decision"]["status"], "eligible")
        late = results["first"]["decision"]
        self.assertEqual(late["status"], DecisionStatus.EXPLORATORY_ONLY.value)
        self.assertTrue(any("already" in reason for reason in late["reasons"]), late["reasons"])
        self.assertEqual(results["first"]["summary"]["recommended"], [])
        stored = self.db().decision(results["first"]["decision_id"])
        self.assertIs(stored.status, DecisionStatus.EXPLORATORY_ONLY)
        candidate = first.variants[1].harness
        self.db().register_base(first.variants[0].harness)
        with self.assertRaises(PromotionConflict):
            self.db().promote(results["first"]["decision_id"], candidate.copy(), source="test")
        version, created = self.db().promote(results["second"]["decision_id"], candidate.copy(), source="test")
        self.assertTrue(created)
        self.assertEqual(version, "2")

    def test_a_lone_eligible_decision_is_untouched(self) -> None:
        payload = self.run_exp(self.spec("only", ("a",)), scripted(a=always), decide=eligible_decision)
        self.assertEqual(payload["decision"]["status"], "eligible")
        self.assertEqual(payload["summary"]["recommended"], ["a"])


SOLVER = r'''
import json, os, sys
from pathlib import Path
marks = Path(sys.argv[1]); marks.mkdir(exist_ok=True)
n = len(list(marks.glob("m-*"))) + 1
(marks / f"m-{n}").write_text("x")
if n % 2 == 1:
    sys.exit(125)  # the solver gives up on every odd call and hopes to be retried
json.dump({"outputs": {"answer": "CORRECT"}}, open(os.environ["HARNESS_RESULT_FILE"], "w"))
'''


class DockerLikeLocal(LocalBackend):
    """Runs locally but classifies outcomes exactly as ``DockerBackend.run`` does."""

    def run(self, argv, **kwargs):
        return DockerBackend._classify_docker_failure(super().run(argv, **kwargs))


class ExitCodeRetryLoopholeTests(unittest.TestCase):
    """Finding 3: a solver exit code must never turn a failed attempt into a retry."""

    def test_solver_exit_125_is_not_retried_and_stays_in_the_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = write_manifest(
                root / "m", tasks=[exact_task("t1")], variants=[{"id": "cand", "system_prompt": "cand"}],
                repetitions=1, schema_version=2, mode="exploratory", family="f",
                dataset={"id": "d", "version": "1"}, environment=TRUSTED, statistics={"max_attempts": 3})
            executor = CommandHarnessExecutor([sys.executable, "-I", "-c", SOLVER, str(root / "marks")],
                                              timeout_seconds=30)
            payload = run_experiment(ExperimentSpec.load(manifest), executor, root / "lib",
                                     backend=DockerLikeLocal())
        gave_up = [run for run in payload["runs"] if run["execution"]["exit_code"] == 125]
        self.assertTrue(gave_up)
        self.assertTrue(all(run["execution"]["status"] == "execution_error" for run in gave_up))
        self.assertTrue(all(run["execution"]["retryable"] is False for run in gave_up))
        self.assertEqual({run["attempt"] for run in payload["runs"]}, {1})  # nothing was retried


class ImproveGateInfrastructureTests(CliCase):
    """Finding 4: a gate infrastructure failure is an error (exit 2), never a rejection."""

    def sandboxed_confirmatory(self) -> Path:
        return write_manifest(
            self.tmp / "sandboxed", tasks=default_tasks(), variants=[{"id": "a", "system_prompt": "a"}],
            schema_version=2, mode="confirmatory", family="f", dataset={"id": "x", "version": "1"},
            repetitions=REPETITIONS, environment={"mode": "sandboxed"})

    def test_unavailable_sandbox_exits_two_before_the_improve_loop_runs(self) -> None:
        empty = self.tmp / "empty-bin"
        empty.mkdir()
        spy = SpyExecutor()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}), self.patch_backend(spy):
            code, out, err = run_cli(*self.improve_args(self.sandboxed_confirmatory()))
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: "), err)
        self.assertIn("docker", err.lower())
        self.assertEqual(spy.calls, 0)
        self.assertEqual(out, "")
        self.assertFalse(self.library.exists())

    def test_infrastructure_failure_inside_the_gate_exits_two_without_a_reject_lesson(self) -> None:
        spec = confirmatory_manifest(self.tmp / "conf", ("a",))
        for error in (StoreBusyError("store busy"), OSError("disk gone"), BackendUnavailable("no docker")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch("harness_workbench.experiments.run_experiment", side_effect=error), \
                    self.patch_backend(MockExecutor()):
                code, out, err = run_cli(*self.improve_args(spec))
            self.assertEqual(code, 2)
            self.assertIn(str(error), err)
            self.assertNotIn("did not qualify", out + err)
            self.assertFalse((self.library / "library" / "harnesses").exists())

    def test_promotion_conflict_exits_two_instead_of_a_traceback(self) -> None:
        with mock.patch.object(cli, "_cmd_improve", side_effect=PromotionConflict("decision is not eligible")):
            code, _out, err = run_cli("improve", "--harness", "unused", "--task", "t")
        self.assertEqual(code, 2)
        self.assertIn("decision is not eligible", err)


class ImproverGateErrorLessonTests(unittest.TestCase):
    def improve(self, gate: Any):
        return Improver(RuleBasedProposer(), MockExecutor(), promotion_gate=gate).improve(
            base_harness(), Task.from_text("research question", domain="research"), k=3, mode="sequential")

    def test_a_gate_failure_is_an_error_lesson_and_never_a_reject_lesson(self) -> None:
        def broken(old: Any, new: Any) -> bool:
            raise RuntimeError("gate unavailable")

        result = self.improve(broken)
        self.assertTrue(any(item.startswith("VALIDATION ERROR: RuntimeError") for item in result.lessons))
        self.assertFalse(any(item.startswith("VALIDATION REJECT") for item in result.lessons))

    def test_infrastructure_exceptions_propagate_out_of_the_improver(self) -> None:
        for error in (StoreBusyError("busy"), OSError("io"), BackendUnavailable("no docker")):
            with self.subTest(error=type(error).__name__):
                def gate(old: Any, new: Any, error: Exception = error) -> bool:
                    raise error

                with self.assertRaises(type(error)):
                    self.improve(gate)


if __name__ == "__main__":
    unittest.main()
