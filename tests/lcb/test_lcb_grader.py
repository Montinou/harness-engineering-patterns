"""The grader records output hashes of a candidate; it never decides pass or fail.

Each test builds a bundle in a temp directory (``canonical.py``, ``grader.py``,
``call.py`` and ``tests.json`` side by side) and runs the grader the way the check
does: ``python -I grader.py <artifact> <out>`` with the bundle directory as cwd.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from benchmarks.livecodebench.canonical import (
    canonical_stdout,
    canonical_value,
    digest,
)
from tests.lcb.lcb_reference import functional_outputs_match

SOURCE_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "livecodebench"
BUNDLE_FILES = ("canonical.py", "grader.py", "call.py")


def stdout_hash(text):
    return digest(canonical_stdout(text))


def value_hash(value):
    return digest(canonical_value(value))


class GraderCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lcb-grader-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bundle = self.root / "bundle"
        self.work = self.root / "work"
        self.out = self.root / "out"
        for directory in (self.bundle, self.work, self.out):
            directory.mkdir()

    def make_bundle(self, mode, inputs, func_name=None):
        for name in BUNDLE_FILES:
            shutil.copy(SOURCE_DIR / name, self.bundle / name)
        (self.bundle / "tests.json").write_text(
            json.dumps({"mode": mode, "func_name": func_name, "inputs": inputs}),
            encoding="utf-8",
        )

    def write_solution(self, source):
        path = self.work / "solution.py"
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return path

    def run_grader(self, artifact=None, env=None, timeout=60):
        artifact = self.work / "solution.py" if artifact is None else artifact
        full_env = dict(os.environ)
        full_env.update(env or {})
        result = subprocess.run(
            [sys.executable, "-I", "grader.py", str(artifact), str(self.out)],
            cwd=str(self.bundle),
            env=full_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result

    def observed(self):
        return json.loads((self.out / "observed.json").read_text(encoding="utf-8"))


class StdinAndFunctionalTests(GraderCase):
    def test_correct_stdin_solution_hashes_match(self):
        self.make_bundle("stdin", ["1 2\n", "10 20\n", "-5 5\n"])
        self.write_solution(
            """
            a, b = map(int, input().split())
            print(a + b)
            """
        )
        result = self.run_grader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.observed(),
            {"hashes": [stdout_hash("3\n"), stdout_hash("30\n"), stdout_hash("0\n")]},
        )

    def test_functional_solution_uses_call_py(self):
        self.make_bundle("functional", ["[[1, 2, 3], 2]", "[[4, 5], 9]"], "addAll")
        self.write_solution(
            """
            class Solution:
                def addAll(self, nums, k):
                    print("noise that must not reach the result")
                    return [n + k for n in nums]
            """
        )
        result = self.run_grader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.observed(),
            {"hashes": [value_hash([3, 4, 5]), value_hash([13, 14])]},
        )

    def test_functional_unhashable_results_record_null(self):
        self.make_bundle("functional", ["[0]", "[1]", "[2]"], "f")
        self.write_solution(
            """
            class Solution:
                def f(self, n):
                    if n == 0:
                        return float("nan")
                    if n == 1:
                        return 10 ** 5000
                    return [n]
            """
        )
        result = self.run_grader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.observed(), {"hashes": [None, None, value_hash([2])]})


class FunctionalVerdictTests(GraderCase):
    """Through the grader, a functional result passes exactly when LiveCodeBench's
    own comparison accepts it: the child hashes the real Python object, before any
    JSON round trip can turn a tuple into a list or an int key into a string."""

    def verdict(self, returned_source, expected_text):
        self.make_bundle("functional", ["[0]"], "f")
        self.write_solution(
            """
            class Solution:
                def f(self, n):
                    return %s
            """ % returned_source
        )
        result = self.run_grader()
        self.assertEqual(result.returncode, 0, result.stderr)
        (observed,) = self.observed()["hashes"]
        expected_hash = value_hash(json.loads(expected_text))
        return observed is not None and observed == expected_hash

    def check_matches_reference(self, returned_source, expected_text):
        prediction = eval(returned_source)
        reference = functional_outputs_match(prediction, json.loads(expected_text))
        self.assertEqual(
            self.verdict(returned_source, expected_text),
            reference,
            "%s vs %s" % (returned_source, expected_text),
        )
        return reference

    def test_nested_tuple_is_not_a_list(self):
        self.assertFalse(self.check_matches_reference("[(1, 2)]", "[[1, 2]]"))

    def test_int_dict_key_is_not_a_string_key(self):
        self.assertFalse(self.check_matches_reference('{1: "a"}', '{"1": "a"}'))

    def test_outermost_tuple_is_a_list(self):
        self.assertTrue(self.check_matches_reference("(1, 2)", "[1, 2]"))

    def test_plain_results_still_match(self):
        self.assertTrue(self.check_matches_reference("[[1, 2]]", "[[1, 2]]"))
        self.assertTrue(self.check_matches_reference('{"a": [1.0, True]}', '{"a": [1, 1]}'))
        self.assertFalse(self.check_matches_reference("[[2, 1]]", "[[1, 2]]"))


class IsolationTests(GraderCase):
    def test_timeout_test_records_null_and_continues(self):
        self.make_bundle("stdin", ["fast\n", "slow\n", "fast\n"])
        self.write_solution(
            """
            import sys, time
            word = sys.stdin.read().strip()
            if word == "slow":
                time.sleep(30)
            print(word)
            """
        )
        started = time.monotonic()
        result = self.run_grader(env={"LCB_TEST_TIMEOUT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(
            self.observed(),
            {"hashes": [stdout_hash("fast"), None, stdout_hash("fast")]},
        )

    def test_crash_and_huge_output_record_null(self):
        self.make_bundle("stdin", ["ok\n", "crash\n", "huge\n", "ok\n"])
        self.write_solution(
            """
            import sys
            word = sys.stdin.read().strip()
            if word == "crash":
                raise RuntimeError("boom")
            if word == "huge":
                sys.stdout.write("x" * (17 * 1024 * 1024))
            else:
                print(word)
            """
        )
        result = self.run_grader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.observed(),
            {"hashes": [stdout_hash("ok"), None, None, stdout_hash("ok")]},
        )

    def test_missing_solution_records_all_null(self):
        self.make_bundle("stdin", ["1\n", "2\n", "3\n"])
        result = self.run_grader(artifact=self.work / "does-not-exist.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.observed(), {"hashes": [None, None, None]})

    def test_total_budget_records_remaining_null(self):
        self.make_bundle("stdin", ["a\n"] * 6)
        self.write_solution(
            """
            import sys, time
            time.sleep(1)
            print(sys.stdin.read().strip())
            """
        )
        started = time.monotonic()
        result = self.run_grader(env={"LCB_TOTAL_SECONDS": "2"})
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 10)
        hashes = self.observed()["hashes"]
        self.assertEqual(len(hashes), 6)
        self.assertEqual(hashes[-3:], [None, None, None])
        self.assertEqual(hashes[0], stdout_hash("a"))

    def test_solution_writing_observed_json_cannot_pass(self):
        inputs = ["1\n", "2\n"]
        truth = [stdout_hash("right 1"), stdout_hash("right 2")]
        forged = json.dumps({"hashes": truth})
        target = self.out / "observed.json"
        self.make_bundle("stdin", inputs)
        self.write_solution(
            """
            import os, sys, time
            FORGED = %r
            PATHS = ["../out/observed.json", %r]

            def forge():
                for path in PATHS:
                    try:
                        with open(path, "w") as handle:
                            handle.write(FORGED)
                    except OSError:
                        pass

            sys.stdin.read()
            forge()
            if os.fork() == 0:
                # drop the inherited pipes so the grader's reader sees EOF
                # without waiting for this child; only the group kill stops it
                os.close(1)
                os.close(2)
                time.sleep(2)
                forge()
                os._exit(0)
            print("wrong")
            """
            % (forged, str(target))
        )
        result = self.run_grader()
        self.assertEqual(result.returncode, 0, result.stderr)
        # a forked child would rewrite the file two seconds after the solution ran
        time.sleep(3.5)
        observed = self.observed()
        self.assertNotEqual(observed["hashes"], truth)
        self.assertEqual(observed, {"hashes": [stdout_hash("wrong")] * 2})

    def test_grader_never_imports_solution(self):
        self.make_bundle("stdin", ["1\n", "2\n"])
        self.write_solution("raise SystemExit\n")
        result = self.run_grader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.observed()["hashes"]), 2)
        self.assertEqual(self.observed()["hashes"], [stdout_hash("")] * 2)

    def test_grader_never_imports_functional_solution(self):
        self.make_bundle("functional", ["[1]", "[2]"], "f")
        self.write_solution("raise SystemExit\n")
        result = self.run_grader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.observed(), {"hashes": [None, None]})


if __name__ == "__main__":
    unittest.main()
