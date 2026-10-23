"""Sandboxed writable trees: opened for the container user before a run, reclaimed after it.

Nothing here needs a Docker daemon. A scripted ``docker`` stands in for the client, logs every
call and the mode of watched host files at the moment a container would run. It proves the order
of events and how failures are classified; it says nothing about real Docker behavior (the
Docker-gated integration tests cover that and are NOT RUN without a daemon).
"""
import dataclasses
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_workbench.experiments import ExperimentSpec, core
from harness_workbench.experiments.backends import DockerBackend, Mounts, ProcessLimits
from harness_workbench.experiments.contracts import ExecutionResult, Measurement
from harness_workbench.experiments.core import run_trial
from harness_workbench.experiments.status import EvaluationStatus, ExecutionStatus
from tests.support import exact_task
from tests.unit.test_trial_pipeline import make_spec

IMAGE = "python:3.12-slim"
IMAGE_PYTHON = "/usr/local/bin/python3"
LIMITS = ProcessLimits(timeout_seconds=5.0)


def make_recording_docker(directory: Path, config: Path) -> tuple[Path, Path]:
    """A fake ``docker`` driven by ``config`` (JSON, re-read on every call).

    Keys: ``watch`` (host paths whose mode is logged on the solver run), ``run_sleep``,
    ``clean_rc`` (exit code of the reclaim container), ``rm_rc`` (exit code of ``rm``).
    """
    log = directory / "calls.jsonl"
    script = directory / "docker"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        f"cfg = json.load(open({str(config)!r}))\n"
        "argv = sys.argv[1:]\n"
        "event = {'argv': argv}\n"
        "name = argv[argv.index('--name') + 1] if '--name' in argv else ''\n"
        "if name.startswith('hw-run-'):\n"
        "    event['modes'] = {p: os.lstat(p).st_mode & 0o7777 for p in cfg.get('watch', [])}\n"
        f"with open({str(log)!r}, 'a') as fh:\n"
        "    fh.write(json.dumps(event) + '\\n')\n"
        "cmd = argv[0] if argv else ''\n"
        "if cmd == 'info':\n"
        "    print('99.0.0-fake'); sys.exit(0)\n"
        "if cmd == 'rm':\n"
        "    sys.exit(cfg.get('rm_rc', 0))\n"
        "if cmd == 'run':\n"
        "    if 'chmod' in argv:\n"
        "        sys.exit(cfg.get('clean_rc', 0))\n"
        "    if name.startswith('hw-run-'):\n"
        "        time.sleep(cfg.get('run_sleep', 0))\n"
        "        sys.exit(0)\n"
        "    print('HARNESS_SANDBOX_OK'); sys.exit(0)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


def events(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def kind(event: dict) -> str:
    """``run`` for the solver container, ``clean`` for the reclaim container, else the docker verb."""
    argv = event["argv"]
    if argv[0] == "run" and "chmod" in argv:
        return "clean"
    if argv[0] == "run" and any(a.startswith("hw-run-") for a in argv):
        return "run"
    return argv[0]


class ReclaimCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.config = self.root / "config.json"
        self.configure()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake, self.log = make_recording_docker(bin_dir, self.config)
        self.backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker=str(fake))
        self.backend.probe()
        self.log.unlink()  # the probe's own calls are not under test

    def configure(self, **values) -> None:
        self.config.write_text(json.dumps(values), encoding="utf-8")


class OpenAndReclaimTests(ReclaimCase):
    def setUp(self) -> None:
        super().setUp()
        self.work = self.root / "work"
        (self.work / "src" / "deep").mkdir(parents=True)
        self.app = self.work / "src" / "app.py"
        self.app.write_text("print(1)", encoding="utf-8")
        self.private = self.work / "src" / "deep" / "private.txt"
        self.private.write_text("secret", encoding="utf-8")
        self.script = self.work / "run.sh"
        self.script.write_text("#!/bin/sh\n", encoding="utf-8")
        self.script.chmod(0o755)
        self.app.chmod(0o644)
        self.private.chmod(0o600)
        (self.work / "src").chmod(0o755)
        (self.work / "src" / "deep").chmod(0o700)
        self.work.chmod(0o755)

    def run_it(self, limits=LIMITS):
        return self.backend.run(
            [IMAGE_PYTHON, "-c", "pass"], cwd=self.work, env={}, limits=limits,
            mounts=Mounts(writable=(self.work,)))

    def test_whole_writable_tree_is_opened_before_the_container_starts(self) -> None:
        watched = [self.work, self.work / "src", self.work / "src" / "deep", self.app, self.private,
                   self.script]
        self.configure(watch=[str(p) for p in watched])
        self.run_it()
        modes = next(e for e in events(self.log) if kind(e) == "run")["modes"]
        for path in (self.work, self.work / "src", self.work / "src" / "deep"):
            self.assertEqual(modes[str(path)], 0o777, f"{path} not world rwx when the container ran")
        self.assertEqual(modes[str(self.app)], 0o666)
        self.assertEqual(modes[str(self.private)], 0o666)
        self.assertEqual(modes[str(self.script)], 0o777)  # X keeps an executable executable

    def test_symlinks_are_neither_followed_nor_changed(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("host file", encoding="utf-8")
        outside.chmod(0o600)
        outside_dir = self.root / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "inner.txt").write_text("x", encoding="utf-8")
        (outside_dir / "inner.txt").chmod(0o600)
        outside_dir.chmod(0o700)
        os.symlink(outside, self.work / "link_file")
        os.symlink(outside_dir, self.work / "link_dir")
        self.run_it()
        self.assertEqual(outside.stat().st_mode & 0o777, 0o600)
        self.assertEqual(outside_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((outside_dir / "inner.txt").stat().st_mode & 0o777, 0o600)

    def test_reclaim_container_runs_after_the_solver_and_not_before(self) -> None:
        self.run_it()
        self.assertEqual([kind(e) for e in events(self.log)], ["run", "clean"])
        clean = events(self.log)[1]["argv"]
        self.assertIn(f"{self.work}:/x", clean)
        self.assertEqual(clean[clean.index("--user") + 1], "0")
        self.assertEqual(clean[-4:], ["chmod", "-R", "a+rwX", "/x"])

    def test_every_writable_mount_is_reclaimed(self) -> None:
        other = self.root / "other"
        other.mkdir()
        self.backend.run([IMAGE_PYTHON, "-c", "pass"], cwd=self.work, env={}, limits=LIMITS,
                         mounts=Mounts(writable=(self.work, other)))
        cleaned = [a for e in events(self.log) if kind(e) == "clean" for a in e["argv"] if a.endswith(":/x")]
        self.assertEqual(cleaned, [f"{self.work}:/x", f"{other}:/x"])

    def test_a_completed_run_is_not_touched_by_a_successful_reclaim(self) -> None:
        outcome = self.run_it()
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.exit_code, 0)

    def test_failed_reclaim_is_a_retryable_infrastructure_error(self) -> None:
        self.configure(clean_rc=1)
        outcome = self.run_it()
        self.assertEqual(outcome.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertTrue(outcome.retryable)
        self.assertIn("could not be reclaimed", outcome.error)

    def test_failed_reclaim_overrides_a_timeout_it_cannot_snapshot(self) -> None:
        self.configure(run_sleep=20, clean_rc=1)
        outcome = self.run_it(ProcessLimits(timeout_seconds=0.5))
        self.assertEqual(outcome.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertIn("could not be reclaimed", outcome.error)

    def test_timeout_is_kept_when_removal_and_reclaim_succeed(self) -> None:
        self.configure(run_sleep=20)
        outcome = self.run_it(ProcessLimits(timeout_seconds=0.5))
        self.assertEqual(outcome.status, ExecutionStatus.TIMEOUT)
        sequence = [kind(e) for e in events(self.log)]
        self.assertEqual(sequence, ["run", "rm", "clean"])  # the container is gone before the tree is reclaimed

    def test_failed_container_removal_after_timeout_is_an_infrastructure_error(self) -> None:
        self.configure(run_sleep=20, rm_rc=1)
        outcome = self.run_it(ProcessLimits(timeout_seconds=0.5))
        self.assertEqual(outcome.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertTrue(outcome.retryable)
        self.assertIn("could not be removed", outcome.error)
        self.assertIn("timeout", outcome.error)


def trial_executor(backend: DockerBackend, spec_calls: list):
    """An ``execute_trial`` executor whose solver run goes through the sandbox backend."""

    class Executor:
        def execute_trial(self, request, *, workspace, backend, limits):
            spec_calls.append("execute")
            outcome = backend.run([IMAGE_PYTHON, "-c", "pass"], cwd=workspace, env={}, limits=limits,
                                  mounts=Mounts(writable=(workspace,)))
            return ExecutionResult(
                trial_id=request.trial_id, status=outcome.status, outputs={"answer": "CORRECT"},
                exit_code=outcome.exit_code, stdout_tail="", stderr_tail="",
                tokens=Measurement.unknown(), latency_ms=Measurement.measured(1),
                adapter_latency_ms=Measurement.unknown(), returned_model=None, artifact_hash=None,
                error=outcome.error, retryable=outcome.retryable)

    return Executor()


class PipelineOrderTests(ReclaimCase):
    """Through ``run_trial``: open before the run, reclaim after it and before the evaluation reads the tree."""

    def private_fixture_spec(self):
        """A spec whose fixture holds a file only its owner may touch (the digest covers modes)."""
        make_spec(self.root, tasks=[exact_task("t1")], fixture=True)
        (self.root / "m" / "fixture" / "seed.txt").chmod(0o600)
        return ExperimentSpec.load(self.root / "m" / "experiment.json")

    def run_pipeline(self):
        spec = self.private_fixture_spec()
        task = spec.tasks[0]
        seen: dict = {}
        real = core.evaluate_bundle

        def watching(*args, **kwargs):
            seen["before_evaluation"] = [kind(e) for e in events(self.log)]
            return real(*args, **kwargs)

        order: list = []
        with mock.patch.object(core, "evaluate_bundle", side_effect=watching) as patched:
            record = run_trial(
                spec, spec.variants[1], task, repetition=0, attempt=1,
                executor=trial_executor(self.backend, order), backend=self.backend, limits=LIMITS,
                experiment_id="exp", implementation_hash="impl")
        return record, seen, patched

    def test_fixture_copy_is_opened_then_reclaimed_before_the_snapshot_is_taken(self) -> None:
        record, seen, _ = self.run_pipeline()
        self.assertEqual(record.evaluation.status, EvaluationStatus.PASSED)
        self.assertEqual(seen["before_evaluation"], ["run", "clean"])

    def test_workspace_copy_of_a_private_fixture_file_is_writable_when_the_container_runs(self) -> None:
        spec = self.private_fixture_spec()
        task = spec.tasks[0]
        modes: list = []

        class Executor:
            def execute_trial(inner, request, *, workspace, backend, limits):
                self.configure(watch=[str(Path(workspace) / "seed.txt")])
                outcome = backend.run([IMAGE_PYTHON, "-c", "pass"], cwd=workspace, env={}, limits=limits,
                                      mounts=Mounts(writable=(workspace,)))
                modes.append(next(e for e in events(self.log) if kind(e) == "run")["modes"])
                return ExecutionResult(
                    trial_id=request.trial_id, status=outcome.status, outputs={"answer": "CORRECT"},
                    exit_code=0, stdout_tail="", stderr_tail="", tokens=Measurement.unknown(),
                    latency_ms=Measurement.measured(1), adapter_latency_ms=Measurement.unknown(),
                    returned_model=None, artifact_hash=None, error=outcome.error, retryable=False)

        record = run_trial(spec, spec.variants[1], task, repetition=0, attempt=1, executor=Executor(),
                           backend=self.backend, limits=LIMITS, experiment_id="exp", implementation_hash="impl")
        self.assertEqual(record.evaluation.status, EvaluationStatus.PASSED)
        (mode,) = list(modes[0].values())
        self.assertEqual(mode, 0o666)

    def test_failed_reclaim_makes_the_trial_not_evaluated_infrastructure_error(self) -> None:
        self.configure(clean_rc=1)
        record, seen, patched = self.run_pipeline()
        self.assertEqual(record.execution.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertEqual(record.evaluation.status, EvaluationStatus.NOT_EVALUATED)
        self.assertIsNone(record.objective_score)
        self.assertFalse(record.usable)
        self.assertIn("could not be reclaimed", record.execution.error)
        patched.assert_not_called()

    def test_failed_container_removal_after_a_counted_timeout_is_not_scored_as_a_task_failure(self) -> None:
        self.configure(run_sleep=20, rm_rc=1)
        spec = self.private_fixture_spec()
        slow = ProcessLimits(timeout_seconds=0.5)
        record = run_trial(
            spec, spec.variants[1], spec.tasks[0], repetition=0, attempt=1,
            executor=trial_executor(self.backend, []), backend=self.backend, limits=slow,
            experiment_id="exp", implementation_hash="impl")
        self.assertEqual(record.execution.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertEqual(record.evaluation.status, EvaluationStatus.NOT_EVALUATED)
        self.assertIn("could not be removed", record.execution.error)


if __name__ == "__main__":
    unittest.main()
