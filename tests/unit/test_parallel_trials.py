"""Opt-in parallel trials: ``environment.parallelism`` runs a grid's trials in a thread pool.

Execution and grading happen in worker threads; every database write stays on the main thread; the
results are put back in the fixed (task, repetition, variant) order, so a decision never depends on
completion order. Nothing here starts Docker, a model or the network.
"""
from __future__ import annotations

import dataclasses
import json
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from harness_workbench.core.db import WorkbenchDB
from harness_workbench.core.harness import Harness
from harness_workbench.experiments import ExperimentSpec, core
from harness_workbench.experiments.backends import LocalBackend, ProcessLimits
from harness_workbench.experiments.contracts import ExecutionResult, Measurement
from harness_workbench.experiments.core import _Runner, run_experiment
from harness_workbench.experiments.manifest import EnvironmentConfig, ManifestError
from harness_workbench.experiments.protocol import build_plan
from harness_workbench.experiments.status import ExecutionStatus
from harness_workbench.experiments.validation import ValidationError
from tests.support import exact_task, write_manifest
from tests.unit import test_manifest as tm

LIMITS = ProcessLimits(timeout_seconds=30)
TASKS = ("t1", "t2", "t3")
REPETITIONS = 3
CANDIDATES = ({"id": "cand", "system_prompt": "cand"},)


class FakeExecutor:
    """Deterministic answers by (task, variant); sleeps a random 0-20 ms so completion order is shuffled.

    ``flaky`` is a task id whose first call per (variant, repetition) is a retryable infrastructure
    failure, so the retry loop runs inside a worker. ``broken`` is a task id whose call raises.
    """

    solver_model = "fake"
    usage_provenance = "adapter_reported"

    def __init__(self, *, flaky: str | None = None, seed: int = 0) -> None:
        self.flaky = flaky
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str, int], int] = {}
        self.threads: set[int] = set()

    def execute_trial(self, request, *, workspace, backend, limits):
        with self._lock:
            delay = self._rng.uniform(0, 0.020)
            self.threads.add(threading.get_ident())
        time.sleep(delay)
        prompt = request.harness_public["system_prompt"]
        task = request.task_description.rsplit(" ", 1)[-1]
        if self.flaky == task:
            with self._lock:
                key = (task, prompt, request.seed)
                self._seen[key] = self._seen.get(key, 0) + 1
                first = self._seen[key] == 1
            if first:
                return ExecutionResult(
                    trial_id=request.trial_id, status=ExecutionStatus.EXECUTION_ERROR, outputs={}, exit_code=None,
                    stdout_tail="", stderr_tail="", tokens=Measurement.unknown(),
                    latency_ms=Measurement.measured(1), adapter_latency_ms=Measurement.unknown(),
                    returned_model=None, artifact_hash=None, error="transient", retryable=True)
        # the candidate solves every task but t2, the baseline solves none
        correct = prompt == "cand" and task != "t2"
        return ExecutionResult(
            trial_id=request.trial_id, status=ExecutionStatus.COMPLETED,
            outputs={"answer": "CORRECT" if correct else "INCORRECT"}, exit_code=None, stdout_tail="",
            stderr_tail="", tokens=Measurement.reported(100, core.Provenance.ADAPTER_REPORTED),
            latency_ms=Measurement.measured(5), adapter_latency_ms=Measurement.unknown(),
            returned_model=None, artifact_hash=None, error=None, retryable=False)


class RecordingDB:
    """A db double: remembers who wrote and fails the test if a write is not on the main thread."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []  # (task_id, variant_id) in write order
        self.off_main: list[str] = []

    def record_trial(self, experiment_id, record) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.off_main.append(threading.current_thread().name)
        self.writes.append((record.task_id, record.variant_id, record.repetition, record.attempt))


class ParallelCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def spec(self, parallelism: int = 1, *, max_attempts: int = 1, name: str = "m") -> ExperimentSpec:
        tasks = [exact_task(task, f"return the correct answer for {task}") for task in TASKS]
        manifest = write_manifest(self.root / name, tasks=tasks, variants=list(CANDIDATES),
                                  repetitions=REPETITIONS)
        spec = ExperimentSpec.load(manifest)
        # schema v1 cannot declare an environment or max_attempts; set them on the loaded spec
        return dataclasses.replace(
            spec, environment=dataclasses.replace(spec.environment, parallelism=parallelism),
            statistics=dataclasses.replace(spec.statistics, max_attempts=max_attempts))

    def runner(self, spec: ExperimentSpec, executor, db=None) -> _Runner:
        return _Runner(spec, executor, LocalBackend(), LIMITS, db if db is not None else RecordingDB(),
                       "exp", "impl")

    def grid_order(self, spec: ExperimentSpec) -> list[tuple[str, int, str]]:
        return [(task.id, repetition, variant.id)
                for task in spec.tasks for repetition in range(spec.repetitions)
                for variant in core._alternate(list(spec.variants), repetition)]


class ParallelEquivalenceTests(ParallelCase):
    def test_parallel_matches_sequential(self) -> None:
        results = {}
        for parallelism in (1, 4):
            spec = self.spec(parallelism, max_attempts=2, name=f"p{parallelism}")
            executor = FakeExecutor(flaky="t2", seed=parallelism)
            payload = run_experiment(spec, executor, self.root / f"out{parallelism}")
            db = WorkbenchDB.open(self.root / f"out{parallelism}")
            try:
                trials = db.trials(payload["experiment_id"])
            finally:
                db.close()
            names = {trial.trial_id: f"{trial.task_id}.{trial.repetition}.{trial.variant_id}.{trial.attempt}"
                     for trial in trials}
            rows = sorted(json.dumps(self.anonymise(trial.to_dict(), names), sort_keys=True) for trial in trials)
            decision = json.dumps(self.anonymise(payload["decision"], names), sort_keys=True)
            order = [names[row["trial_id"]] for row in payload["runs"]]
            results[parallelism] = (rows, decision, payload["summary"], order, executor.threads)
        sequential, parallel = results[1], results[4]
        self.assertEqual(sequential[0], parallel[0])  # identical trial rows, trial ids aside
        self.assertEqual(sequential[1], parallel[1])  # identical decision
        self.assertEqual(sequential[2], parallel[2])  # identical summary
        self.assertEqual(sorted(sequential[3]), sorted(parallel[3]))
        self.assertEqual(len(sequential[4]), 1)
        self.assertGreater(len(parallel[4]), 1)  # the pool really used several threads
        self.assertTrue(any(name.endswith(".2") for name in sequential[3]))  # a retry happened

    @staticmethod
    def anonymise(data, names: dict[str, str]):
        """Drop timestamps and replace each random trial id by its (task, rep, variant, attempt) name."""
        text = json.dumps(data, sort_keys=True)
        for trial_id, name in names.items():
            text = text.replace(trial_id, name)
        out = json.loads(text)

        def strip(node):
            if isinstance(node, dict):
                return {k: strip(v) for k, v in node.items()
                        if k not in {"created_at", "finished_at", "decision_id", "latency_ms"}}
            if isinstance(node, list):
                return [strip(item) for item in node]
            return node
        return strip(out)

    def test_final_and_attempts_are_in_grid_order(self) -> None:
        for parallelism in (1, 4):
            spec = self.spec(parallelism, max_attempts=2, name=f"o{parallelism}")
            runner = self.runner(spec, FakeExecutor(flaky="t2", seed=7))
            runner.grid(list(spec.variants), spec.tasks)
            self.assertEqual(
                [(r.task_id, r.repetition, r.variant_id) for r in runner.final], self.grid_order(spec),
                f"parallelism {parallelism}")
            # attempts: grid order, and a trial's attempts stay together and in attempt order
            keys = [(r.task_id, r.repetition, r.variant_id, r.attempt) for r in runner.attempts]
            self.assertEqual(keys, sorted(keys, key=lambda k: (
                self.grid_order(spec).index(k[:3]), k[3])))
            self.assertEqual({r.attempt for r in runner.attempts if r.task_id == "t2"}, {1, 2})

    def test_one_worker_runs_inline_on_the_main_thread(self) -> None:
        spec = self.spec(1)
        executor = FakeExecutor()
        runner = self.runner(spec, executor)
        runner.grid(list(spec.variants), spec.tasks)
        self.assertEqual(executor.threads, {threading.get_ident()})
        self.assertEqual(len(runner.final), len(TASKS) * REPETITIONS * len(spec.variants))

    def test_second_grid_appends_after_the_first(self) -> None:
        spec = self.spec(4, name="two")
        runner = self.runner(spec, FakeExecutor())
        runner.grid(list(spec.variants), spec.tasks[:1])
        runner.grid(list(spec.variants), spec.tasks[1:])
        self.assertEqual([(r.task_id, r.repetition, r.variant_id) for r in runner.final], self.grid_order(spec))


class MainThreadWritesTests(ParallelCase):
    def test_db_writes_on_main_thread_only(self) -> None:
        spec = self.spec(4, max_attempts=2)
        db = RecordingDB()
        executor = FakeExecutor(flaky="t1")
        runner = self.runner(spec, executor, db)
        runner.grid(list(spec.variants), spec.tasks)
        self.assertEqual(db.off_main, [])
        self.assertGreater(len(executor.threads - {threading.get_ident()}), 0)
        self.assertEqual(len(db.writes), len(runner.attempts))
        # a trial's attempts are written in attempt order
        for task, rep, variant in {w[:3] for w in db.writes}:
            attempts = [w[3] for w in db.writes if w[:3] == (task, rep, variant)]
            self.assertEqual(attempts, sorted(attempts))

    def test_real_sqlite_connection_is_only_touched_by_the_main_thread(self) -> None:
        # sqlite3 refuses cross-thread use by default, so a worker write would raise ProgrammingError.
        spec = self.spec(4, name="sqlite")
        payload = run_experiment(spec, FakeExecutor(), self.root / "sqlite-out")
        self.assertEqual(len(payload["runs"]), len(TASKS) * REPETITIONS * len(spec.variants))


class WorkerFailureTests(ParallelCase):
    def test_worker_exception_propagates_like_inline(self) -> None:
        # Results must not depend on parallelism (spec 7a): an exception escaping run_trial aborts the run at
        # every parallelism, after the trials that already finished were recorded.
        real = core.run_trial

        for parallelism in (2, 4):
            db = RecordingDB()

            def run_trial(spec, variant, task, **kwargs):
                if task.id == "t2" and kwargs["repetition"] == 1 and variant.id == "cand":
                    deadline = time.monotonic() + 10
                    while not db.writes and time.monotonic() < deadline:
                        time.sleep(0.005)  # let a finished trial reach the main thread first
                    raise RuntimeError("worker blew up")
                return real(spec, variant, task, **kwargs)

            with self.subTest(parallelism=parallelism), mock.patch.object(core, "run_trial", run_trial):
                spec = self.spec(parallelism, name=f"x{parallelism}")
                runner = self.runner(spec, FakeExecutor(), db)
                with self.assertRaisesRegex(RuntimeError, "worker blew up"):
                    runner.grid(list(spec.variants), spec.tasks)
                self.assertEqual(db.off_main, [])
                self.assertGreater(len(db.writes), 0)  # finished trials were recorded
                self.assertNotIn(("t2", "cand", 1, 1), db.writes)  # the failed one is not invented as a trial
                self.assertEqual(len(runner.final), len(db.writes))
                self.assertFalse(any(r.execution.error and "trial raised" in r.execution.error
                                     for r in runner.final))

    def test_inline_exception_propagates_at_parallelism_1(self) -> None:
        # With 1 the behavior is today's: an exception escaping run_trial aborts the run.
        real = core.run_trial
        calls = []

        def run_trial(spec, variant, task, **kwargs):
            calls.append((task.id, kwargs["repetition"], variant.id))
            if task.id == "t2" and kwargs["repetition"] == 1 and variant.id == "cand":
                raise RuntimeError("inline blew up")
            return real(spec, variant, task, **kwargs)

        with mock.patch.object(core, "run_trial", run_trial):
            spec = self.spec(1, name="inline")
            db = RecordingDB()
            runner = self.runner(spec, FakeExecutor(), db)
            with self.assertRaisesRegex(RuntimeError, "inline blew up"):
                runner.grid(list(spec.variants), spec.tasks)
        self.assertEqual(len(runner.final), len(runner.attempts))
        self.assertEqual(len(db.writes), len(runner.attempts))  # earlier trials were persisted as they finished
        self.assertNotIn(("t2", "cand", 1, 1), db.writes)
        self.assertEqual(calls[-1], ("t2", 1, "cand"))  # nothing ran after the failure

    def test_database_failure_stops_the_grid_and_propagates(self) -> None:
        class Failing(RecordingDB):
            def record_trial(self, experiment_id, record) -> None:
                super().record_trial(experiment_id, record)
                if len(self.writes) == 3:
                    raise RuntimeError("disk full")

        spec = self.spec(4, name="dbfail")
        runner = self.runner(spec, FakeExecutor(), Failing())
        with self.assertRaisesRegex(RuntimeError, "disk full"):
            runner.grid(list(spec.variants), spec.tasks)


class ParallelismSettingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "base.json").write_text(Harness(id="h", version="1", system_prompt="base").to_json())
        (self.root / "cand.json").write_text(Harness(id="h", version="2", system_prompt="candidate").to_json())
        (self.root / "fixture").mkdir()
        (self.root / "fixture" / "seed.txt").write_text("seed")
        (self.root / "graders").mkdir()
        (self.root / "graders" / "check.py").write_text("print('ok')\n")

    def load(self, **environment):
        data = tm.v2_dict(mode="exploratory")
        data["environment"] = {"mode": "trusted_local", "trusted_solver_acknowledged": True, **environment}
        return ExperimentSpec.from_dict(data, self.root)

    def test_default_is_one(self) -> None:
        self.assertEqual(self.load().environment.parallelism, 1)
        self.assertEqual(EnvironmentConfig("trusted_local").parallelism, 1)

    def test_parallelism_bounds(self) -> None:
        for good in (1, 6, 16):
            self.assertEqual(self.load(parallelism=good).environment.parallelism, good)
        for bad in (0, 17, -1, 2.0, "4", True, None):
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                self.load(parallelism=bad)
        for bad in (0, 17):
            with self.subTest(direct=bad), self.assertRaises(ValidationError):
                EnvironmentConfig("trusted_local", parallelism=bad)

    def test_parallelism_not_in_plan_or_policy_hash(self) -> None:
        one, many = self.load(parallelism=1), self.load(parallelism=16)
        self.assertEqual(build_plan(one).plan_hash, build_plan(many).plan_hash)
        self.assertEqual(build_plan(one).to_dict(), build_plan(many).to_dict())
        self.assertEqual(one.statistics.policy_hash, many.statistics.policy_hash)
        self.assertNotIn("parallelism", json.dumps(build_plan(many).to_dict()))


if __name__ == "__main__":
    unittest.main()
