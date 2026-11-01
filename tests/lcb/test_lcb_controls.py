"""The controls builder keeps a problem only when its positive control passes and both negatives fail.

Everything is offline: the Claude Code adapter is replaced by a fake command that writes a prepared
``solution.py`` and reports a fixed cost, the problems are synthetic, and the grading is the real hash-only
grader through the package backend (trusted local execution here; the srt backend is covered elsewhere).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

from benchmarks.livecodebench import make_controls
from harness_workbench.experiments.backends import LocalBackend
from harness_workbench.experiments.manifest import EnvironmentConfig
from tests.lcb import test_lcb_import as importer_tests

FAKE_COST_USD = 0.25

FAKE_ADAPTER = textwrap.dedent('''
    import argparse, json, os, sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--solutions", required=True)
    parser.add_argument("--calls", required=True)
    args = parser.parse_args()
    with open(args.task, encoding="utf-8") as handle:
        title = json.load(handle)["description"].split("\\n\\n")[0]
    with open(args.calls, "a", encoding="utf-8") as handle:
        handle.write(title + "\\n")
    with open(args.solutions, encoding="utf-8") as handle:
        source = json.load(handle).get(title)
    if source is not None:
        with open(os.path.join(args.workspace, "solution.py"), "w", encoding="utf-8") as handle:
            handle.write(source)
    if title == "Broken Adapter":
        with open(args.result, "w", encoding="utf-8") as handle:
            handle.write("this is not json")
        sys.exit(0)
    details = {"subtype": "success", "num_turns": 3, "total_cost_usd": %s, "is_error": False}
    with open(args.result, "w", encoding="utf-8") as handle:
        json.dump({"outputs": {"answer": "done", "details": details}, "tokens": 10, "model": "fake"}, handle)
''' % FAKE_COST_USD)

SUM_SOLUTION = "a, b = map(int, input().split())\nprint(a + b)\n"
WRONG_SOLUTION = "print(0)\n"
DOUBLE_SOLUTION = (
    "from typing import *\n"
    "class Solution:\n"
    "    def doubleAll(self, nums: List[int], k: int) -> List[int]:\n"
    "        return [2 * x for x in nums] + [2 * k]\n")


class RecordingBackend:
    """Delegates to ``LocalBackend`` and records ``(program name, network)`` of every process."""

    name = "recording"

    def __init__(self):
        self._inner = LocalBackend()
        self.python = self._inner.python
        self.calls = []
        self._lock = threading.Lock()

    def capabilities(self):
        return self._inner.capabilities()

    def run(self, argv, *, cwd, env, limits, mounts, network=False):
        with self._lock:
            self.calls.append((Path(argv[2]).name if len(argv) > 2 else argv[0], network))
        return self._inner.run(argv, cwd=cwd, env=env, limits=limits, mounts=mounts, network=network)


class FlakyBackend:
    """Delegates to ``LocalBackend`` until ``broken`` is set, then every process raises (a sandbox that died)."""

    name = "flaky"

    def __init__(self):
        self._inner = LocalBackend()
        self.python = self._inner.python
        self.broken = False

    def capabilities(self):
        return self._inner.capabilities()

    def run(self, argv, *, cwd, env, limits, mounts, network=False):
        if self.broken:
            raise OSError("sandbox unavailable")
        return self._inner.run(argv, cwd=cwd, env=env, limits=limits, mounts=mounts, network=network)


class ControlsTestCase(importer_tests.ImportTestCase):
    def setUp(self):
        super().setUp()
        self.work = self.root / "work"
        self.work.mkdir()
        self.adapter = self.work / "fake_adapter.py"
        self.adapter.write_text(FAKE_ADAPTER, encoding="utf-8")
        self.solutions_file = self.work / "solutions.json"
        self.calls_file = self.work / "calls.txt"
        self.calls_file.write_text("", encoding="utf-8")
        self.environment = EnvironmentConfig(mode="trusted_local")

    def adapter_argv(self):
        return ["{python}", "-I", str(self.adapter), "--task", "{task_json}", "--workspace", "{workspace}",
                "--result", "{result_json}", "--solutions", str(self.solutions_file),
                "--calls", str(self.calls_file)]

    def set_solutions(self, by_title):
        self.solutions_file.write_text(json.dumps(by_title), encoding="utf-8")

    def calls(self):
        return [line for line in self.calls_file.read_text(encoding="utf-8").splitlines() if line]

    def build(self, **overrides):
        options = dict(model="anthropic/claude-opus-5.5", environment=self.environment,
                       adapter_argv=self.adapter_argv(), backend=LocalBackend(), log=lambda message: None)
        options.update(overrides)
        return make_controls.build_controls(self.out, **options)

    def control_report(self):
        return json.loads((self.out / "controls_report.json").read_text(encoding="utf-8"))

    def three_problems(self):
        rows = [
            importer_tests.stdin_row("abc900_a", title="Sum Two"),
            importer_tests.functional_row("3001", title="Double Them", func_name="doubleAll"),
            importer_tests.stdin_row("abc900_c", title="Sum Again", content="Print the sum of two integers."),
        ]
        self.run_import(rows)
        return rows


class BuildControlsTests(ControlsTestCase):
    def test_kept_only_when_pass_passes_and_negatives_fail(self):
        self.three_problems()
        # abc900_a and 3001 get a correct solution, abc900_c a wrong one
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": WRONG_SOLUTION})
        report = self.build()
        self.assertEqual(sorted(report["kept"]), ["3001", "abc900_a"])
        self.assertEqual([item["id"] for item in report["excluded"]], ["abc900_c"])
        self.assertEqual(report["excluded"][0]["reason"], "no_passing_solution")
        controls = self.out / "controls"
        for qid in ("abc900_a", "3001"):
            self.assertTrue((controls / qid / "pass" / "solution.py").is_file(), qid)
            self.assertTrue((controls / qid / "examples" / "solution.py").is_file(), qid)
        self.assertEqual((controls / "abc900_a" / "pass" / "solution.py").read_text(), SUM_SOLUTION)
        # an excluded problem leaves no control to be mistaken for a kept one
        self.assertFalse((controls / "abc900_c" / "pass").exists())
        self.assertFalse((controls / "abc900_c" / "examples").exists())
        self.assertEqual((controls / "empty" / "solution.py").read_text(), "")
        self.assertEqual(self.control_report(), report)

    def test_kept_controls_behave_as_controls_under_the_real_grader(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION})
        self.build()
        by_id = {entry["id"]: entry for entry in self.pool()}
        for qid, entry in by_id.items():
            if qid == "abc900_c":
                continue
            expected = entry["expected_hashes"]
            bundle = self.out / "graders" / qid
            observed = {}
            for label, path in (("pass", self.out / "controls" / qid / "pass"),
                                ("examples", self.out / "controls" / qid / "examples"),
                                ("empty", self.out / "controls" / "empty")):
                out = self.work / ("observed-%s-%s" % (qid, label))
                subprocess.run([sys.executable, "-I", "grader.py", str(path), str(out)],
                               cwd=str(bundle), check=True, timeout=120)
                observed[label] = json.loads((out / "observed.json").read_text())["hashes"]
            self.assertEqual(observed["pass"], expected, qid)
            self.assertNotEqual(observed["examples"], expected, qid)
            self.assertNotEqual(observed["empty"], expected, qid)
            # the public examples are the first tests: the examples negative reproduces exactly those
            n_public = len(entry["public_examples"])
            self.assertEqual(observed["examples"][:n_public], expected[:n_public], qid)
            for got, want in zip(observed["examples"][n_public:], expected[n_public:]):
                self.assertNotEqual(got, want, qid)  # nothing (stdin) or a crash (functional) on the private tests

    def test_examples_only_negative_that_passes_excludes_problem(self):
        # every test is a public example, so printing the examples' outputs passes the whole grader
        only_public = importer_tests.stdin_row(
            "abc900_p", title="All Public",
            public=[{"input": "1 2\n", "output": "3\n", "testtype": "stdin"}], private=[])
        self.run_import([only_public, importer_tests.stdin_row("abc900_a", title="Sum Two")])
        self.set_solutions({"All Public": SUM_SOLUTION, "Sum Two": SUM_SOLUTION})
        report = self.build()
        self.assertEqual(report["kept"], ["abc900_a"])
        self.assertEqual(report["excluded"], [{"id": "abc900_p", "reason": "examples_passed"}])
        self.assertFalse((self.out / "controls" / "abc900_p" / "pass").exists())
        # the negatives are checked first, so no model money is spent on the excluded problem
        self.assertEqual(self.calls(), ["Sum Two"])

    def test_empty_solution_that_passes_excludes_problem(self):
        # a problem whose expected output is empty is passed by an empty solution
        silent = importer_tests.stdin_row(
            "abc900_s", title="Print Nothing",
            public=[{"input": "1\n", "output": "", "testtype": "stdin"}],
            private=[{"input": "2\n", "output": "", "testtype": "stdin"}])
        self.run_import([silent])
        self.set_solutions({"Print Nothing": "pass\n"})
        report = self.build()
        self.assertEqual(report["kept"], [])
        self.assertEqual(report["excluded"], [{"id": "abc900_s", "reason": "empty_passed"}])
        self.assertEqual(self.calls(), [])

    def test_report_counts_and_cost(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": WRONG_SOLUTION})
        report = self.build()
        # one generation per problem, kept or not
        self.assertEqual(sorted(self.calls()), ["Double Them", "Sum Again", "Sum Two"])
        self.assertAlmostEqual(report["total_cost_usd"], 3 * FAKE_COST_USD)
        self.assertEqual(report["pool_size"], 3)
        self.assertEqual(report["kept_count"], 2)
        self.assertEqual(report["excluded_count"], 1)
        self.assertEqual(report["exclusion_counts"], {"no_passing_solution": 1})
        self.assertEqual(report["model"], "anthropic/claude-opus-5.5")

    def test_rerun_skips_kept(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": WRONG_SOLUTION})
        first = self.build()
        kept_file = self.out / "controls" / "abc900_a" / "pass" / "solution.py"
        before = kept_file.stat().st_mtime_ns
        self.calls_file.write_text("", encoding="utf-8")
        second = self.build()
        # kept controls are verified again but not regenerated; the excluded problem is retried
        self.assertEqual(self.calls(), ["Sum Again"])
        self.assertEqual(kept_file.stat().st_mtime_ns, before)
        self.assertEqual(sorted(second["kept"]), sorted(first["kept"]))
        self.assertEqual(second["excluded"], first["excluded"])
        self.assertAlmostEqual(second["run_cost_usd"], FAKE_COST_USD)
        # the total counts every generation ever paid for in this pool
        self.assertAlmostEqual(second["total_cost_usd"], 4 * FAKE_COST_USD)

    def test_rerun_retries_a_kept_control_that_no_longer_verifies(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": SUM_SOLUTION})
        self.build()
        (self.out / "controls" / "abc900_a" / "pass" / "solution.py").write_text(WRONG_SOLUTION)
        self.calls_file.write_text("", encoding="utf-8")
        report = self.build()
        self.assertEqual(self.calls(), ["Sum Two"])
        self.assertIn("abc900_a", report["kept"])
        self.assertEqual((self.out / "controls" / "abc900_a" / "pass" / "solution.py").read_text(), SUM_SOLUTION)

    def test_grader_failure_while_reverifying_keeps_the_controls_and_reports_grader_error(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": SUM_SOLUTION})
        backend = FlakyBackend()
        first = self.build(backend=backend)
        self.assertEqual(len(first["kept"]), 3)
        controls = self.out / "controls" / "abc900_a"
        before = (controls / "pass" / "solution.py").read_bytes()
        self.calls_file.write_text("", encoding="utf-8")
        backend.broken = True
        second = self.build(backend=backend)
        # nothing was regenerated, nothing deleted, and the flake is visible in the report
        self.assertEqual(self.calls(), [])
        self.assertEqual((controls / "pass" / "solution.py").read_bytes(), before)
        self.assertTrue((controls / "examples" / "solution.py").is_file())
        self.assertEqual(second["kept"], [])
        self.assertEqual(second["exclusion_counts"], {"grader_error": 3})
        self.assertEqual(second["grader_errors"], 3)
        self.assertAlmostEqual(second["run_cost_usd"], 0.0)
        # the sandbox recovers: the same controls are kept again, still without a new generation
        backend.broken = False
        third = self.build(backend=backend)
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(third["kept"]), 3)
        self.assertEqual(third["grader_errors"], 0)
        self.assertEqual((controls / "pass" / "solution.py").read_bytes(), before)

    def test_a_definite_mismatch_while_reverifying_still_removes_the_controls(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": SUM_SOLUTION})
        self.build()
        controls = self.out / "controls" / "abc900_a"
        (controls / "pass" / "solution.py").write_text(WRONG_SOLUTION)
        self.set_solutions({"Sum Two": WRONG_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": SUM_SOLUTION})
        report = self.build()
        self.assertNotIn("abc900_a", report["kept"])
        self.assertFalse((controls / "pass").exists())
        self.assertEqual(report["exclusion_counts"], {"no_passing_solution": 1})
        self.assertEqual(report["grader_errors"], 0)

    def test_first_run_grader_error_is_counted_in_the_report(self):
        self.run_import([importer_tests.stdin_row("abc900_a", title="Sum Two")])
        self.set_solutions({"Sum Two": SUM_SOLUTION})
        backend = FlakyBackend()
        backend.broken = True
        report = self.build(backend=backend)
        self.assertEqual(report["kept"], [])
        self.assertEqual(report["exclusion_counts"], {"grader_error": 1})
        self.assertEqual(report["grader_errors"], 1)
        self.assertEqual(report["execution_errors"], 0)

    def test_limit_processes_only_the_first_problems(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": SUM_SOLUTION})
        report = self.build(limit=2)
        self.assertEqual(sorted(report["kept"]), ["3001", "abc900_a"])
        self.assertEqual(sorted(self.calls()), ["Double Them", "Sum Two"])
        self.assertEqual(report["pool_size"], 3)
        self.assertEqual(report["attempted"], 2)
        # a partial report says so, and what limited it
        self.assertIs(report["complete"], False)
        self.assertEqual(report["limit"], 2)
        self.assertIs(self.control_report()["complete"], False)

    def test_a_whole_pool_run_is_complete(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": SUM_SOLUTION})
        report = self.build()
        self.assertIs(report["complete"], True)
        self.assertIsNone(report["limit"])
        # a limit that covers the pool is still the whole pool
        self.assertIs(self.build(limit=10)["complete"], True)

    def test_an_interrupted_run_writes_an_incomplete_report(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": SUM_SOLUTION})
        original = make_controls.ControlsBuilder.process
        seen = []

        def interrupt_on_the_second(builder, entry):
            seen.append(entry["id"])
            if len(seen) == 2:
                raise KeyboardInterrupt
            return original(builder, entry)

        with mock.patch.object(make_controls.ControlsBuilder, "process", interrupt_on_the_second):
            with self.assertRaises(KeyboardInterrupt):
                self.build()
        report = self.control_report()  # written in the finally block
        self.assertIs(report["complete"], False)
        self.assertLess(report["attempted"], report["pool_size"])

    def test_parallel_run_gives_the_same_report(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": WRONG_SOLUTION})
        report = self.build(parallelism=3)
        self.assertEqual(report["kept"], ["abc900_a", "3001"])  # pool order, not completion order
        self.assertEqual([item["id"] for item in report["excluded"]], ["abc900_c"])

    def test_generation_is_a_solver_run_and_grading_never_has_network(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": WRONG_SOLUTION})
        backend = RecordingBackend()
        self.build(backend=backend)
        networked = [name for name, network in backend.calls if network]
        offline = [name for name, network in backend.calls if not network]
        self.assertEqual(networked, ["fake_adapter.py"] * 3)
        self.assertTrue(offline)
        self.assertTrue(all(name == "grader.py" for name in offline), offline)
        # every problem reached all three gradings: empty, examples, pass
        self.assertEqual(len(offline), 3 * 3)

    def test_expected_hashes_never_reach_the_controls(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": WRONG_SOLUTION})
        self.build()
        hashes = {h for entry in self.pool() for h in entry["expected_hashes"]}
        self.assertTrue(hashes)
        for path in (self.out / "controls").rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                for digest in hashes:
                    self.assertNotIn(digest, text, str(path))

    def test_adapter_protocol_error_is_recorded_not_scored(self):
        self.run_import([importer_tests.stdin_row("abc900_b", title="Broken Adapter")])
        self.set_solutions({"Broken Adapter": SUM_SOLUTION})
        report = self.build()
        self.assertEqual(report["kept"], [])
        excluded = report["excluded"][0]
        self.assertEqual((excluded["id"], excluded["reason"]), ("abc900_b", "no_passing_solution"))
        self.assertIn("protocol", excluded["detail"])
        self.assertEqual(report["execution_errors"], 1)

    def test_a_solution_that_is_a_symlink_is_never_kept(self):
        self.run_import([importer_tests.stdin_row("abc900_a", title="Sum Two")])
        secret = self.work / "elsewhere.py"
        secret.write_text(SUM_SOLUTION, encoding="utf-8")
        link_adapter = self.work / "link_adapter.py"
        link_adapter.write_text(textwrap.dedent('''
            import argparse, json, os
            parser = argparse.ArgumentParser()
            for name in ("--task", "--workspace", "--result", "--target"):
                parser.add_argument(name)
            args = parser.parse_args()
            os.symlink(args.target, os.path.join(args.workspace, "solution.py"))
            with open(args.result, "w") as handle:
                json.dump({"outputs": {"answer": "", "details": {"total_cost_usd": 0.0}}}, handle)
        '''), encoding="utf-8")
        argv = ["{python}", "-I", str(link_adapter), "--task", "{task_json}", "--workspace", "{workspace}",
                "--result", "{result_json}", "--target", str(secret)]
        report = self.build(adapter_argv=argv)
        self.assertEqual(report["kept"], [])
        self.assertEqual(report["excluded"][0]["reason"], "no_passing_solution")

    def test_unsafe_pool_id_is_refused(self):
        self.run_import([importer_tests.stdin_row("abc900_a")])
        pool = self.pool()
        pool[0]["id"] = "../escape"
        (self.out / "pool.json").write_text(json.dumps(pool), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.build()


class ExamplesSolutionTests(unittest.TestCase):
    def run_source(self, source, stdin):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "solution.py"
            path.write_text(source, encoding="utf-8")
            return subprocess.run([sys.executable, "-I", str(path)], input=stdin, capture_output=True,
                                  text=True, timeout=60)

    def test_stdin_examples_solution_prints_only_the_examples(self):
        entry = {"mode": "stdin", "func_name": None, "public_examples": [
            {"input": "1 2\n", "output": "3\n"}, {"input": "é \"q\"\n", "output": "x\\y\n"}]}
        source = make_controls.examples_solution(entry)
        self.assertEqual(self.run_source(source, "1 2\n").stdout, "3\n")
        self.assertEqual(self.run_source(source, "é \"q\"\n").stdout, "x\\y\n")
        self.assertEqual(self.run_source(source, "5 6\n").stdout, "")

    def test_functional_examples_solution_answers_only_the_examples(self):
        entry = {"mode": "functional", "func_name": "doubleAll", "public_examples": [
            {"input": "[[1, 2], 3]", "output": "[2, 4, 6]", "raw_input": "[1, 2]\n3"}]}
        source = make_controls.examples_solution(entry)
        namespace = {}
        exec(compile(source, "solution.py", "exec"), namespace)
        method = namespace["Solution"]().doubleAll
        self.assertEqual(method([1, 2], 3), [2, 4, 6])
        with self.assertRaises(KeyError):
            method([9], 1)


class CommandLineTests(ControlsTestCase):
    def run_cli(self, *extra):
        script = Path(make_controls.__file__)
        argv = [sys.executable, str(script), "--pool", str(self.out), "--model", "anthropic/claude-opus-5.5",
                "--adapter-argv", json.dumps(self.adapter_argv()), *extra]
        return subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd=str(self.root))

    def test_environment_inline_and_file_and_report(self):
        self.three_problems()
        self.set_solutions({"Sum Two": SUM_SOLUTION, "Double Them": DOUBLE_SOLUTION,
                            "Sum Again": WRONG_SOLUTION})
        done = self.run_cli("--environment", '{"mode": "trusted_local"}', "--limit", "1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.control_report()["kept"], ["abc900_a"])
        environment_file = self.work / "environment.json"
        environment_file.write_text('{"mode": "trusted_local", "parallelism": 2}', encoding="utf-8")
        done = self.run_cli("--environment", str(environment_file))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(self.control_report()["kept"]), ["3001", "abc900_a"])
        self.assertIn("kept 2", done.stdout)

    def test_bad_environment_stops_before_any_work(self):
        self.three_problems()
        done = self.run_cli("--environment", '{"parallelism": 2}')
        self.assertEqual(done.returncode, 2)
        self.assertFalse((self.out / "controls_report.json").exists())

    def test_execution_error_gives_a_nonzero_exit_but_still_writes_the_report(self):
        self.run_import([importer_tests.stdin_row("abc900_b", title="Broken Adapter")])
        self.set_solutions({})
        done = self.run_cli("--environment", '{"mode": "trusted_local"}')
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertEqual(self.control_report()["execution_errors"], 1)

    def test_grader_error_gives_a_nonzero_exit_but_still_writes_the_report(self):
        self.run_import([importer_tests.stdin_row("abc900_a", title="Sum Two")])
        self.set_solutions({"Sum Two": SUM_SOLUTION})
        shutil.rmtree(self.out / "graders" / "abc900_a")  # the grader cannot decide for this problem
        done = self.run_cli("--environment", '{"mode": "trusted_local"}')
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("grading(s) errored", done.stderr)
        report = self.control_report()
        self.assertEqual(report["grader_errors"], 1)
        self.assertEqual(report["kept"], [])

    def test_default_adapter_needs_a_claude_executable(self):
        self.three_problems()
        env = {k: v for k, v in os.environ.items() if k != "HARNESS_CLAUDE_PATH"}
        done = subprocess.run(
            [sys.executable, str(Path(make_controls.__file__)), "--pool", str(self.out),
             "--model", "anthropic/claude-opus-5.5", "--environment", '{"mode": "trusted_local"}'],
            capture_output=True, text=True, timeout=120, env=env, cwd=str(self.root))
        self.assertEqual(done.returncode, 2)
        self.assertIn("--claude", done.stderr)

    def test_default_adapter_needs_the_gateway_key(self):
        self.three_problems()
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        done = subprocess.run(
            [sys.executable, str(Path(make_controls.__file__)), "--pool", str(self.out),
             "--model", "anthropic/claude-opus-5.5", "--environment", '{"mode": "trusted_local"}',
             "--claude", "/nonexistent/claude"],
            capture_output=True, text=True, timeout=120, env=env, cwd=str(self.root))
        self.assertEqual(done.returncode, 2)
        self.assertIn("ANTHROPIC_API_KEY", done.stderr)
        self.assertFalse((self.out / "controls").exists())


class DefaultArgvTests(unittest.TestCase):
    def test_default_adapter_argv_follows_the_spec(self):
        argv = make_controls.default_adapter_argv(
            model="anthropic/claude-opus-5.5", claude="/opt/claude/2.1.316", claude_version="2.1.316",
            max_budget_usd="0.50")
        self.assertEqual(argv[:3], ["{python}", "-I", "{claude_code_agent}"])
        pairs = dict(zip(argv[3::2], argv[4::2]))
        self.assertEqual(pairs, {
            "--harness": "{harness_json}", "--task": "{task_json}", "--workspace": "{workspace}",
            "--result": "{result_json}", "--model": "anthropic/claude-opus-5.5",
            "--max-budget-usd": "0.50", "--claude": "/opt/claude/2.1.316", "--claude-version": "2.1.316"})

    def test_default_adapter_passes_the_gateway_credentials_through(self):
        environment = EnvironmentConfig(mode="trusted_local")
        widened = make_controls.with_gateway_passthrough(environment)
        self.assertEqual(widened.solver_env_passthrough, ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"))
        self.assertEqual(make_controls.with_gateway_passthrough(widened).solver_env_passthrough,
                         widened.solver_env_passthrough)


class OracleCheckTests(unittest.TestCase):
    def test_check_is_oracle_json_equal_with_the_hashes(self):
        check = make_controls.oracle_check(["a" * 64, None])
        self.assertEqual(check["type"], "oracle")
        self.assertEqual(check["compare"], "json_equal")
        self.assertEqual(check["observed"], "observed.json")
        self.assertEqual(check["expected"], {"hashes": ["a" * 64, None]})
        self.assertEqual(check["argv"][:3], ["{python}", "-I", "grader.py"])


if __name__ == "__main__":
    unittest.main()
