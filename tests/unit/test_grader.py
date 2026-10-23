"""Grader bundle, validated workspace snapshot, out-of-workspace evaluation and oracle checks."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_workbench.experiments.backends import LocalBackend, ProcessLimits
from harness_workbench.experiments.checks import evaluate_checks
from harness_workbench.experiments.grader import (
    EvaluationEnvironment,
    GraderBundle,
    SnapshotError,
    SnapshotLimits,
    digest_path,
    evaluate_bundle,
    run_controls,
    snapshot_workspace,
)
from harness_workbench.experiments.manifest import ControlCase, Controls, TaskSpec
from harness_workbench.experiments.status import EvaluationStatus

LIMITS = ProcessLimits(timeout_seconds=30)
SNAPSHOT = SnapshotLimits(max_files=100, max_total_bytes=1_000_000)
CANARY = "canary-value"


def py(code: str, *extra: str) -> list[str]:
    return ["{python}", "-I", "-c", code, *extra]


class GraderCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.addCleanup(self._make_writable)

    def _make_writable(self) -> None:
        for path in [self.root, *self.root.rglob("*")]:
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)

    def workspace(self, files: dict[str, str] | None = None) -> Path:
        ws = self.root / "workspace"
        ws.mkdir(exist_ok=True)
        for name, text in (files or {}).items():
            (ws / name).parent.mkdir(parents=True, exist_ok=True)
            (ws / name).write_text(text, encoding="utf-8")
        return ws

    def bundle(self, checks: list[dict], files: dict[str, str] | None = None) -> GraderBundle:
        src = None
        if files is not None:
            src = self.root / "bundle-src"
            src.mkdir(exist_ok=True)
            for name, text in files.items():
                (src / name).write_text(text, encoding="utf-8")
        return GraderBundle.build("t1", checks, src)

    def evaluate(self, bundle: GraderBundle, outputs: dict | None = None, workspace: Path | None = None,
                 limits: ProcessLimits = LIMITS):
        return evaluate_bundle(bundle, outputs or {}, workspace, backend=LocalBackend(), limits=limits,
                               snapshot_limits=SNAPSHOT, trial_id="trial-1")


class GraderBundleBuild(GraderCase):
    def test_hash_is_deterministic_and_covers_checks_and_files(self) -> None:
        checks = [{"type": "exact", "value": "x"}]
        a = self.bundle(checks, {"a.py": "1", "b.py": "2"})
        b = self.bundle(checks, {"a.py": "1", "b.py": "2"})
        self.assertEqual(a.hash, b.hash)
        self.assertEqual(len(a.hash), 64)
        self.assertEqual([name for name, _ in a.files], ["a.py", "b.py"])
        self.assertNotEqual(a.hash, self.bundle(checks, {"a.py": "1", "b.py": "3"}).hash)
        self.assertNotEqual(a.hash, self.bundle([{"type": "exact", "value": "y"}], {"a.py": "1", "b.py": "2"}).hash)

    def test_files_are_read_at_build_time(self) -> None:
        bundle = self.bundle([{"type": "exact", "value": "x"}], {"a.py": "original"})
        (self.root / "bundle-src" / "a.py").write_text("changed", encoding="utf-8")
        self.assertEqual(dict(bundle.files)["a.py"], b"original")

    def test_absent_files_dir_means_no_files(self) -> None:
        bundle = GraderBundle.build("t1", [{"type": "exact", "value": "x"}], None)
        self.assertEqual(bundle.files, ())
        self.assertEqual(bundle.version, "1")

    def test_materialize_writes_read_only_files_and_directories(self) -> None:
        bundle = self.bundle([{"type": "exact", "value": "x"}], {"a.py": "1"})
        target = self.root / "mat"
        bundle.materialize(target)
        self.assertEqual((target / "a.py").read_text(encoding="utf-8"), "1")
        self.assertEqual(stat.S_IMODE((target / "a.py").stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o555)


class SnapshotWorkspace(GraderCase):
    def test_valid_tree_is_copied_and_digest_returned(self) -> None:
        ws = self.workspace({"a.txt": "alpha", "sub/b.txt": "beta"})
        dest = self.root / "snap"
        digest = snapshot_workspace(ws, dest, SNAPSHOT)
        self.assertEqual((dest / "sub" / "b.txt").read_text(encoding="utf-8"), "beta")
        self.assertEqual(digest, digest_path(dest))

    def test_rejects_symlink(self) -> None:
        ws = self.workspace({"a.txt": "alpha"})
        os.symlink("a.txt", ws / "link")
        with self.assertRaises(SnapshotError):
            snapshot_workspace(ws, self.root / "snap", SNAPSHOT)

    def test_rejects_symlink_to_outside_file(self) -> None:
        secret = self.root / "secret.txt"
        secret.write_text(CANARY, encoding="utf-8")
        ws = self.workspace()
        os.symlink(secret, ws / "leak")
        with self.assertRaises(SnapshotError):
            snapshot_workspace(ws, self.root / "snap", SNAPSHOT)
        self.assertFalse((self.root / "snap" / "leak").exists())

    def test_rejects_hardlink(self) -> None:
        ws = self.workspace({"a.txt": "alpha"})
        os.link(ws / "a.txt", ws / "b.txt")
        with self.assertRaises(SnapshotError):
            snapshot_workspace(ws, self.root / "snap", SNAPSHOT)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs os.mkfifo")
    def test_rejects_fifo(self) -> None:
        ws = self.workspace({"a.txt": "alpha"})
        os.mkfifo(ws / "pipe")
        with self.assertRaises(SnapshotError):
            snapshot_workspace(ws, self.root / "snap", SNAPSHOT)

    def test_rejects_too_many_entries(self) -> None:
        ws = self.workspace({f"f{i}.txt": "x" for i in range(5)})
        with self.assertRaises(SnapshotError):
            snapshot_workspace(ws, self.root / "snap", SnapshotLimits(max_files=4, max_total_bytes=1_000_000))
        snapshot_workspace(ws, self.root / "snap2", SnapshotLimits(max_files=5, max_total_bytes=1_000_000))

    def test_rejects_too_many_bytes(self) -> None:
        ws = self.workspace({"a.txt": "x" * 50, "b.txt": "y" * 50})
        with self.assertRaises(SnapshotError):
            snapshot_workspace(ws, self.root / "snap", SnapshotLimits(max_files=10, max_total_bytes=99))
        snapshot_workspace(ws, self.root / "snap2", SnapshotLimits(max_files=10, max_total_bytes=100))

    def test_rejects_workspace_that_is_a_symlink(self) -> None:
        real = self.workspace({"a.txt": "alpha"})
        os.symlink(real, self.root / "ws-link")
        with self.assertRaises(SnapshotError):
            snapshot_workspace(self.root / "ws-link", self.root / "snap", SNAPSHOT)

    def test_rejects_destination_inside_workspace(self) -> None:
        ws = self.workspace({"a.txt": "alpha"})
        with self.assertRaises(SnapshotError):
            snapshot_workspace(ws, ws / "snap", SNAPSHOT)

    def test_snapshot_error_is_a_runtime_error(self) -> None:
        self.assertTrue(issubclass(SnapshotError, RuntimeError))

    def test_snapshot_rejects_symlink_hardlink_fifo_and_limits(self) -> None:
        for name, tweak in (
            ("symlink", lambda ws: os.symlink("a.txt", ws / "x")),
            ("hardlink", lambda ws: os.link(ws / "a.txt", ws / "x")),
            ("fifo", lambda ws: os.mkfifo(ws / "x")),
        ):
            with self.subTest(name):
                ws = self.root / f"ws-{name}"
                ws.mkdir()
                (ws / "a.txt").write_text("alpha", encoding="utf-8")
                tweak(ws)
                with self.assertRaises(SnapshotError):
                    snapshot_workspace(ws, self.root / f"snap-{name}", SNAPSHOT)
        ws = self.workspace({"a.txt": "alpha", "b.txt": "beta"})
        with self.assertRaises(SnapshotError):
            snapshot_workspace(ws, self.root / "snap-files", SnapshotLimits(max_files=1, max_total_bytes=1_000))
        with self.assertRaises(SnapshotError):
            snapshot_workspace(ws, self.root / "snap-bytes", SnapshotLimits(max_files=10, max_total_bytes=5))


class EvaluateBundle(GraderCase):
    def test_rewritten_check_script_in_artifact_does_not_matter(self) -> None:
        bundle = self.bundle([{"id": "c", "type": "command", "argv": ["{python}", "-I", "check.py"]}],
                             {"check.py": "import sys; sys.exit(1)\n"})
        before = bundle.hash
        ws = self.workspace({"check.py": "import sys; sys.exit(1)\n"})
        first = self.evaluate(bundle, {}, ws)
        self.assertEqual(first.status, EvaluationStatus.FAILED)
        (ws / "check.py").write_text("pass\n", encoding="utf-8")  # the "solver" rewrites the grader
        second = self.evaluate(bundle, {}, ws)
        self.assertEqual(second.status, EvaluationStatus.FAILED)
        self.assertEqual(first.rubric_hash, before)
        self.assertEqual(second.rubric_hash, before)
        self.assertEqual(bundle.hash, before)

    def test_missing_grader_executable_is_evaluation_error_without_score(self) -> None:
        bundle = self.bundle([{"id": "g", "type": "command", "argv": ["/nonexistent/grader"]}])
        result = self.evaluate(bundle, {}, self.workspace())
        self.assertEqual(result.status, EvaluationStatus.EVALUATION_ERROR)
        self.assertIsNone(result.score)
        self.assertEqual(result.checks[0]["status"], "error")
        self.assertFalse(result.checks[0]["passed"])

    def test_snapshot_failure_is_evaluation_error(self) -> None:
        ws = self.workspace({"a.txt": "alpha"})
        os.symlink("a.txt", ws / "link")
        result = self.evaluate(self.bundle([{"type": "exact", "value": "x"}]), {"answer": "x"}, ws)
        self.assertEqual(result.status, EvaluationStatus.EVALUATION_ERROR)
        self.assertIsNone(result.score)
        self.assertIsNone(result.artifact_hash)
        self.assertTrue(result.error)

    def test_artifact_hash_is_the_snapshot_digest(self) -> None:
        ws = self.workspace({"a.txt": "alpha"})
        result = self.evaluate(self.bundle([{"type": "file_exists", "path": "a.txt"}]), {}, ws)
        self.assertEqual(result.status, EvaluationStatus.PASSED)
        self.assertEqual(result.artifact_hash, digest_path(ws))
        none_result = self.evaluate(self.bundle([{"type": "exact", "value": "x"}]), {"answer": "x"}, None)
        self.assertIsNone(none_result.artifact_hash)

    def test_empty_bundle_is_evaluation_error(self) -> None:
        result = self.evaluate(GraderBundle.build("t1", [], None), {}, None)
        self.assertEqual(result.status, EvaluationStatus.EVALUATION_ERROR)
        self.assertIsNone(result.score)

    def test_bundle_is_materialized_read_only_and_only_after_solver_finished(self) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can write into read-only directories")
        ws = self.workspace({"a.txt": "alpha"})
        listing_before = sorted(p.name for p in ws.iterdir())
        self.assertNotIn("bundle", listing_before)
        bundle = self.bundle(
            [{"id": "w", "type": "command",
              "argv": py("import sys; open(sys.argv[1] + '/injected', 'w')", "{bundle}")}],
            {"g.py": "pass\n"})
        result = self.evaluate(bundle, {}, ws)
        self.assertEqual(result.status, EvaluationStatus.FAILED)  # the write failed: nonzero exit, not an error
        self.assertEqual(result.checks[0]["status"], "failed")
        self.assertEqual(sorted(p.name for p in ws.iterdir()), listing_before)

    def test_output_checks_cannot_be_forged_by_artifact_contents(self) -> None:
        ws = self.workspace({"result.json": '{"checks": [{"passed": true}]}', "stdout.txt": "PASS"})
        bundle = self.bundle([{"id": "a", "type": "exact", "target": "answer", "value": "CORRECT"}])
        result = self.evaluate(bundle, {"answer": "WRONG"}, ws)
        self.assertEqual(result.status, EvaluationStatus.FAILED)
        self.assertEqual(result.score, 0.0)

    def test_oracle_passes_only_on_supervisor_side_comparison(self) -> None:
        write = py("import json, sys; json.dump({'sum': 6}, open(sys.argv[1] + '/observed.json', 'w'))", "{out}")

        def oracle(expected: object, argv: list[str]) -> dict:
            return {"id": "o", "type": "oracle", "argv": argv, "observed": "observed.json",
                    "expected": expected, "compare": "json_equal"}

        passed = self.evaluate(self.bundle([oracle({"sum": 6}, write)]), {}, None)
        self.assertEqual(passed.status, EvaluationStatus.PASSED)
        self.assertEqual(passed.checks[0]["status"], "passed")
        failed = self.evaluate(self.bundle([oracle({"sum": 7}, write)]), {}, None)
        self.assertEqual(failed.status, EvaluationStatus.FAILED)
        self.assertEqual(failed.checks[0]["status"], "failed")
        silent = self.evaluate(self.bundle([oracle({"sum": 6}, py("pass"))]), {}, None)
        self.assertEqual(silent.status, EvaluationStatus.EVALUATION_ERROR)
        self.assertIn("oracle protocol violation", silent.checks[0]["detail"])
        self.assertIsNone(silent.score)

    def test_score_is_weighted_fraction_only_for_usable_statuses(self) -> None:
        hit = {"id": "hit", "type": "exact", "target": "answer", "value": "A", "weight": 3}
        miss = {"id": "miss", "type": "exact", "target": "other", "value": "B", "weight": 1}
        self.assertEqual(self.evaluate(self.bundle([hit]), {"answer": "A"}).score, 1.0)
        mixed = self.evaluate(self.bundle([hit, miss]), {"answer": "A", "other": "Z"})
        self.assertEqual(mixed.status, EvaluationStatus.FAILED)
        self.assertEqual(mixed.score, 0.75)
        none = self.evaluate(self.bundle([hit, dict(miss, id="miss2", target="other")]), {"answer": "Z", "other": "Z"})
        self.assertEqual(none.score, 0.0)
        broken = {"id": "broken", "type": "command", "argv": ["/nonexistent/grader"]}
        errored = self.evaluate(self.bundle([hit, broken]), {"answer": "A"})
        self.assertEqual(errored.status, EvaluationStatus.EVALUATION_ERROR)
        self.assertIsNone(errored.score)

    def test_grader_does_not_inherit_parent_secret_or_python_variables(self) -> None:
        code = ("import os, sys\n"
                "names = [n for n in os.environ if n.startswith('PYTHON') and n not in "
                "('PYTHONNOUSERSITE', 'PYTHONDONTWRITEBYTECODE', 'PYTHONSAFEPATH')]\n"
                "sys.exit(0 if 'HARNESS_TEST_CANARY' not in os.environ and not names "
                "and 'HARNESS_OUT' in os.environ else 1)")
        bundle = self.bundle([{"id": "env", "type": "command", "argv": py(code)}])
        with mock.patch.dict(os.environ, {"HARNESS_TEST_CANARY": CANARY, "PYTHONPATH": str(self.root),
                                          "MY_API_KEY": CANARY}):
            result = self.evaluate(bundle, {}, None)
        self.assertEqual(result.status, EvaluationStatus.PASSED, result.checks)

    def test_command_runs_in_bundle_and_sees_artifact_placeholder(self) -> None:
        ws = self.workspace({"seed.txt": "original"})
        code = ("import sys, pathlib; "
                "sys.exit(0 if pathlib.Path(sys.argv[1], 'seed.txt').read_text() == 'original' "
                "and pathlib.Path('g.txt').is_file() else 1)")
        bundle = self.bundle([{"id": "c", "type": "command", "argv": py(code, "{artifact}")}], {"g.txt": "x"})
        self.assertEqual(self.evaluate(bundle, {}, ws).status, EvaluationStatus.PASSED)


class ChecksDirect(GraderCase):
    def env(self, artifact_files: dict[str, str] | None = None) -> EvaluationEnvironment:
        base = self.root / "env"
        dirs = {name: base / name for name in ("artifact", "bundle", "out", "home", "tmp")}
        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        for name, text in (artifact_files or {}).items():
            (dirs["artifact"] / name).write_text(text, encoding="utf-8")
        return EvaluationEnvironment(dirs["artifact"], dirs["bundle"], dirs["out"], dirs["home"], dirs["tmp"],
                                     LocalBackend(), LIMITS)

    def test_file_checks_resolve_inside_artifact(self) -> None:
        env = self.env({"a.txt": "alpha"})
        (self.root / "env" / "secret.txt").write_text(CANARY, encoding="utf-8")
        results = evaluate_checks([
            {"id": "e", "type": "file_exists", "path": "a.txt"},
            {"id": "q", "type": "file_equals", "path": "a.txt", "value": "alpha"},
            {"id": "n", "type": "file_equals", "path": "a.txt", "value": "beta"},
            {"id": "m", "type": "file_exists", "path": "missing.txt"},
            {"id": "x", "type": "file_exists", "path": "../secret.txt"},
            {"id": "abs", "type": "file_exists", "path": str(self.root / "env" / "secret.txt")},
        ], {}, env)
        by_id = {r["id"]: r for r in results}
        self.assertEqual([by_id[k]["status"] for k in ("e", "q", "n", "m", "x", "abs")],
                         ["passed", "passed", "failed", "failed", "error", "error"])
        self.assertTrue(all(r["passed"] == (r["status"] == "passed") for r in results))
        self.assertEqual(set(results[0]), {"id", "type", "weight", "status", "passed", "detail"})

    def test_file_check_without_environment_is_error(self) -> None:
        result = evaluate_checks([{"type": "file_exists", "path": "a.txt"}], {}, None)[0]
        self.assertEqual(result["status"], "error")

    def test_output_checks_work_without_environment(self) -> None:
        result = evaluate_checks([{"type": "exact", "value": "x"}], {"answer": "x"}, None)[0]
        self.assertEqual(result["status"], "passed")

    def test_missing_output_or_invalid_json_is_failed_not_error(self) -> None:
        results = evaluate_checks([
            {"id": "m", "type": "exact", "target": "answer", "value": "x"},
            {"id": "j", "type": "json_equal", "target": "doc", "value": {"a": 1}},
        ], {"doc": "not json"}, None)
        self.assertEqual([r["status"] for r in results], ["failed", "failed"])

    def test_non_environment_argument_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            evaluate_checks([{"type": "exact", "value": "x"}], {"answer": "x"}, self.root)  # type: ignore[arg-type]

    def test_command_exit_code_and_stdout_regex(self) -> None:
        env = self.env()
        results = evaluate_checks([
            {"id": "ok", "type": "command", "argv": py("print('hello 42')"), "stdout_regex": r"hello \d+"},
            {"id": "nomatch", "type": "command", "argv": py("print('hello')"), "stdout_regex": r"\d+"},
            {"id": "exit3", "type": "command", "argv": py("import sys; sys.exit(3)"), "exit_code": 3},
            {"id": "badre", "type": "command", "argv": py("pass"), "stdout_regex": "("},
        ], {}, env)
        self.assertEqual([r["status"] for r in results], ["passed", "failed", "passed", "error"])

    def test_command_timeout_and_output_limit_are_errors(self) -> None:
        env = self.env()
        results = evaluate_checks([
            {"id": "slow", "type": "command", "argv": py("import time; time.sleep(30)"), "timeout_seconds": 0.5},
            {"id": "loud", "type": "command", "argv": py("print('x' * 2000000)")},
        ], {}, EvaluationEnvironment(env.artifact, env.bundle, env.out, env.home, env.tmp, env.backend,
                                     ProcessLimits(timeout_seconds=30, max_output_bytes=1000)))
        self.assertEqual([r["status"] for r in results], ["error", "error"])

    def test_oracle_observed_file_protocol(self) -> None:
        env = self.env()

        def check(argv: list[str], observed: str = "observed.txt", **extra: object) -> dict:
            return {"id": "o", "type": "oracle", "argv": argv, "observed": observed, "expected": "42", **extra}

        write = py("import sys; open(sys.argv[1] + '/observed.txt', 'w').write('42')", "{out}")
        self.assertEqual(evaluate_checks([check(write)], {}, env)[0]["status"], "passed")
        # exit code is diagnostic only: a failing exit after writing the right value still compares by value
        write_then_fail = py("import sys; open(sys.argv[1] + '/observed.txt', 'w').write('42'); sys.exit(5)", "{out}")
        self.assertEqual(evaluate_checks([check(write_then_fail)], {}, env)[0]["status"], "passed")
        wrong = py("import sys; open(sys.argv[1] + '/observed.txt', 'w').write('41')", "{out}")
        self.assertEqual(evaluate_checks([check(wrong)], {}, env)[0]["status"], "failed")
        # a stale observed file from an earlier check must not satisfy a check that writes nothing
        self.assertEqual(evaluate_checks([check(py("pass"))], {}, env)[0]["status"], "error")

    def test_oracle_protocol_violations_are_errors(self) -> None:
        env = self.env()
        cases = {
            "directory": (py("import sys, os; os.mkdir(sys.argv[1] + '/observed.txt')", "{out}"), "observed.txt"),
            "symlink": (py("import sys, os; os.symlink(sys.argv[2], sys.argv[1] + '/observed.txt')",
                           "{out}", str(self.root / "secret.txt")), "observed.txt"),
            "escape": (py("pass"), "../secret.txt"),
            "timeout": (py("import time; time.sleep(30)"), "observed.txt"),
        }
        (self.root / "secret.txt").write_text("42", encoding="utf-8")
        (self.root / "env" / "secret.txt").write_text("42", encoding="utf-8")
        tight = EvaluationEnvironment(env.artifact, env.bundle, env.out, env.home, env.tmp, env.backend,
                                      ProcessLimits(timeout_seconds=1))
        for name, (argv, observed) in cases.items():
            with self.subTest(name):
                stale = env.out / "observed.txt"
                if stale.is_dir() and not stale.is_symlink():
                    stale.rmdir()
                elif stale.exists() or stale.is_symlink():
                    stale.unlink()
                result = evaluate_checks(
                    [{"id": "o", "type": "oracle", "argv": argv, "observed": observed, "expected": "42"}],
                    {}, tight)[0]
                self.assertEqual(result["status"], "error", result)
                self.assertIn("oracle protocol violation", result["detail"])
                if name == "escape":  # the supervisor must not delete a file outside out while "cleaning"
                    self.assertTrue((self.root / "env" / "secret.txt").exists())

    def test_oracle_stale_cleanup_never_deletes_outside_out(self) -> None:
        env = self.env()
        victim_dir = self.root / "victim"
        victim_dir.mkdir()
        victim = victim_dir / "v.txt"
        victim.write_text("keep", encoding="utf-8")
        plant = {"id": "plant", "type": "command",
                 "argv": py("import sys, os; os.symlink(sys.argv[2], sys.argv[1] + '/d')", "{out}", str(victim_dir))}
        self.assertEqual(evaluate_checks([plant], {}, env)[0]["status"], "passed")
        self.assertTrue((env.out / "d").is_symlink())
        for observed in ("d/v.txt", "d/../victim/v.txt", "../victim/v.txt", str(victim)):
            with self.subTest(observed):
                result = evaluate_checks(
                    [{"id": "o", "type": "oracle", "argv": py("pass"), "observed": observed, "expected": "keep"}],
                    {}, env)[0]
                self.assertEqual(result["status"], "error", result)
                self.assertIn("oracle protocol violation", result["detail"])
                self.assertTrue(victim.exists(), f"supervisor deleted a file outside out via {observed!r}")
                self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def test_oracle_stale_unlink_failure_is_an_error(self) -> None:
        env = self.env()
        (env.out / "observed.txt").write_text("42", encoding="utf-8")
        check = {"id": "o", "type": "oracle", "argv": py("pass"), "observed": "observed.txt", "expected": "42"}
        with mock.patch("pathlib.Path.unlink", side_effect=PermissionError("denied")):
            result = evaluate_checks([check], {}, env)[0]
        self.assertEqual(result["status"], "error", result)
        self.assertIn("oracle protocol violation", result["detail"])

    def test_oracle_exact_compare_reads_text(self) -> None:
        env = self.env()
        argv = py("import sys; open(sys.argv[1] + '/o.txt', 'w').write('done\\n')", "{out}")
        same = {"id": "a", "type": "oracle", "argv": argv, "observed": "o.txt", "expected": "done\n"}
        self.assertEqual(evaluate_checks([same], {}, env)[0]["status"], "passed")
        self.assertEqual(evaluate_checks([dict(same, expected="done")], {}, env)[0]["status"], "failed")

    def test_oracle_invalid_json_in_json_equal_mode_is_error(self) -> None:
        env = self.env()
        argv = py("import sys; open(sys.argv[1] + '/o.json', 'w').write('NaN')", "{out}")
        check = {"id": "a", "type": "oracle", "argv": argv, "observed": "o.json", "expected": 1,
                 "compare": "json_equal"}
        result = evaluate_checks([check], {}, env)[0]
        self.assertEqual(result["status"], "error")
        self.assertIn("oracle protocol violation", result["detail"])


class RunControls(GraderCase):
    """``run_controls``: the positive control must pass and every negative must fail."""

    def task(self, controls: Controls | None, checks: list[dict] | None = None) -> TaskSpec:
        from harness_workbench.core.executor import Task

        frozen = checks or [{"id": "answer", "type": "exact", "target": "answer", "value": "CORRECT"}]
        bundle = GraderBundle.build("t1", frozen, None)
        return TaskSpec("t1", Task.from_text("controls", task_id="t1"), None, "t1", tuple(frozen), None,
                        bundle.hash, bundle, None, None, "fingerprint", False, controls, frozenset())

    def controls(self, task: TaskSpec) -> list[str]:
        return run_controls(task, backend=LocalBackend(), limits=LIMITS, snapshot_limits=SNAPSHOT)

    def test_controls_positive_must_pass_and_negatives_must_fail(self) -> None:
        good = Controls(ControlCase("pos", {"answer": "CORRECT"}), (ControlCase("neg", {"answer": "WRONG"}),))
        self.assertEqual(self.controls(self.task(good)), [])
        leaky_negative = Controls(ControlCase("pos", {"answer": "CORRECT"}),
                                  (ControlCase("neg-a", {"answer": "WRONG"}), ControlCase("neg-b", {"answer": "CORRECT"})))
        self.assertEqual(self.controls(self.task(leaky_negative)),
                         ["control neg-b of task t1: expected failed, got passed"])
        failing_positive = Controls(ControlCase("pos", {"answer": "WRONG"}), (ControlCase("neg", {"answer": "WRONG"}),))
        self.assertEqual(self.controls(self.task(failing_positive)),
                         ["control pos of task t1: expected passed, got failed"])

    def test_control_evaluation_error_is_a_problem(self) -> None:
        ws = self.workspace({"a.txt": "alpha"})
        os.symlink("a.txt", ws / "link")  # the snapshot refuses it: the control cannot be evaluated
        controls = Controls(ControlCase("pos", {"answer": "CORRECT"}),
                            (ControlCase("neg", {"answer": "WRONG"}, ws),))
        self.assertEqual(self.controls(self.task(controls)),
                         ["control neg of task t1: expected failed, got evaluation_error"])
        broken_positive = Controls(ControlCase("pos", {}, ws), (ControlCase("neg", {"answer": "WRONG"}),))
        problems = self.controls(self.task(broken_positive))
        self.assertIn("control pos of task t1: expected passed, got evaluation_error", problems)

    def test_control_artifact_directory_is_the_workspace(self) -> None:
        good = self.workspace({"result.txt": "42"})
        bad = self.root / "bad-artifact"
        bad.mkdir()
        (bad / "result.txt").write_text("41", encoding="utf-8")
        checks = [{"id": "f", "type": "file_equals", "path": "result.txt", "value": "42"}]
        controls = Controls(ControlCase("pos", {}, good), (ControlCase("neg", {}, bad),))
        self.assertEqual(self.controls(self.task(controls, checks)), [])
        self.assertEqual((good / "result.txt").read_text(encoding="utf-8"), "42")  # the source is not modified

    def test_task_without_controls_reports_a_problem(self) -> None:
        self.assertEqual(self.controls(self.task(None)), ["task t1 has no controls"])


if __name__ == "__main__":
    unittest.main()
