"""Adversarial grader manipulation against the real evaluator (LocalBackend + evaluate_bundle).

The solver's artifact is untrusted: it can ship modules named after the standard
library, rewrite its own reports, exit early, or forge the observed file. None of
that may turn a failing solution into a passing one. Marker files live in a
tempfile directory outside the artifact, so a planted module that really ran is
detected even though the evaluator deletes its own snapshot.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_workbench.experiments.backends import LocalBackend, ProcessLimits
from harness_workbench.experiments.grader import GraderBundle, SnapshotLimits, evaluate_bundle
from harness_workbench.experiments.status import EvaluationStatus

LIMITS = ProcessLimits(timeout_seconds=30)
SNAPSHOT = SnapshotLimits(max_files=100, max_total_bytes=1_000_000)
RUN_TESTS = ["{python}", "-I", "run_tests.py"]

ANSWER_RUNNER = (
    "import json, os, sys, unittest\n"
    "with open(os.path.join(os.environ['HARNESS_ARTIFACT'], 'answer.txt'), encoding='utf-8') as handle:\n"
    "    sys.exit(0 if handle.read() == '42' else 1)\n"
)

# The planted module records that it was imported, then reports success.
PLANTED = "import sys\nopen({marker!r}, 'a').write('{name}\\n')\nsys.exit(0)\n"

SOLUTION_RUNNER = (
    "import json, os, sys\n"
    "sys.path.insert(0, os.environ['HARNESS_ARTIFACT'])\n"
    "import solution\n"
    "with open(os.path.join(os.environ['HARNESS_OUT'], 'observed.json'), 'w', encoding='utf-8') as handle:\n"
    "    json.dump({'sum': solution.total()}, handle)\n"
)


class AdversarialCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.marker = self.root / "markers" / "pwned"
        self.marker.parent.mkdir()

    def write(self, directory: str, files: dict[str, str]) -> Path:
        base = self.root / directory
        base.mkdir(exist_ok=True)
        for name, text in files.items():
            (base / name).write_text(text, encoding="utf-8")
        return base

    def bundle(self, checks: list[dict], files: dict[str, str]) -> GraderBundle:
        return GraderBundle.build("t1", checks, self.write("bundle-src", files))

    def evaluate(self, bundle: GraderBundle, workspace: Path, outputs: dict | None = None):
        return evaluate_bundle(bundle, outputs or {}, workspace, backend=LocalBackend(), limits=LIMITS,
                               snapshot_limits=SNAPSHOT, trial_id="trial-1")

    def planted(self, *names: str) -> dict[str, str]:
        return {name: PLANTED.format(marker=str(self.marker), name=name) for name in names}

    def assertNoMarker(self) -> None:
        self.assertFalse(self.marker.exists(), "a module planted in the artifact was executed")


class ShadowedModules(AdversarialCase):
    def answer_bundle(self) -> GraderBundle:
        return self.bundle([{"id": "tests", "type": "command", "argv": RUN_TESTS}], {"run_tests.py": ANSWER_RUNNER})

    def test_shadowed_stdlib_modules_in_artifact_are_not_imported(self) -> None:
        planted = self.planted("unittest.py", "json.py", "sitecustomize.py", "conftest.py")
        bad = self.write("artifact-bad", {**planted, "answer.txt": "41"})
        result = self.evaluate(self.answer_bundle(), bad)
        self.assertEqual(result.status, EvaluationStatus.FAILED)
        self.assertNoMarker()
        good = self.write("artifact-good", {**planted, "answer.txt": "42"})
        self.assertEqual(self.evaluate(self.answer_bundle(), good).status, EvaluationStatus.PASSED)  # grader still runs
        self.assertNoMarker()

    def test_pythonpath_and_startup_env_are_not_inherited(self) -> None:
        hostile = self.write("hostile", {"unittest.py": PLANTED.format(marker=str(self.marker), name="unittest"),
                                         "startup.py": PLANTED.format(marker=str(self.marker), name="startup")})
        artifact = self.write("artifact", {"answer.txt": "41"})
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(hostile), "PYTHONSTARTUP": str(hostile / "startup.py"),
                                          "PYTHONHOME": str(self.root / "no-such-home")}):
            result = self.evaluate(self.answer_bundle(), artifact)
        self.assertEqual(result.status, EvaluationStatus.FAILED)
        self.assertNoMarker()


class RunnerTampering(AdversarialCase):
    def test_printing_pass_and_exit_zero_without_running_checks_does_not_pass(self) -> None:
        bundle = self.bundle(
            [{"id": "sum", "type": "command", "argv": RUN_TESTS}],
            {"run_tests.py": (
                "import os, sys\n"
                "path = os.path.join(os.environ['HARNESS_ARTIFACT'], 'sum.txt')\n"
                "sys.exit(0 if os.path.isfile(path) and open(path, encoding='utf-8').read() == '6' else 1)\n")})
        faking = self.write("artifact-faking", {"report.txt": "PASS"})
        outputs = {"answer": "PASS", "stdout": "PASS", "exit_code": 0}
        result = self.evaluate(bundle, faking, outputs)
        self.assertEqual(result.status, EvaluationStatus.FAILED)
        honest = self.write("artifact-honest", {"report.txt": "PASS", "sum.txt": "6"})
        self.assertEqual(self.evaluate(bundle, honest, outputs).status, EvaluationStatus.PASSED)

    def oracle_bundle(self) -> GraderBundle:
        check = {"id": "oracle", "type": "oracle", "argv": RUN_TESTS, "observed": "observed.json",
                 "expected": {"sum": 6}, "compare": "json_equal"}
        return self.bundle([check], {"run_tests.py": SOLUTION_RUNNER})

    def test_candidate_code_calling_os_exit_zero_cannot_pass_an_oracle_check(self) -> None:
        artifact = self.write("artifact", {"solution.py": "import os\nos._exit(0)\n\ndef total():\n    return 6\n"})
        result = self.evaluate(self.oracle_bundle(), artifact)
        self.assertNotEqual(result.status, EvaluationStatus.PASSED)
        self.assertEqual(result.status, EvaluationStatus.EVALUATION_ERROR)
        self.assertEqual(result.checks[0]["status"], "error")
        self.assertIn("oracle protocol violation", result.checks[0]["detail"])

    def test_candidate_code_calling_os_exit_zero_passes_a_plain_command_check(self) -> None:
        """Documents the limitation that makes ``oracle`` mandatory for runs_candidate_code tasks (ADR 0001)."""
        artifact = self.write("artifact", {"solution.py": "import os\nos._exit(0)\n\ndef total():\n    return 7\n"})
        bundle = self.bundle(
            [{"id": "tests", "type": "command", "argv": RUN_TESTS}],
            {"run_tests.py": ("import os, sys\nsys.path.insert(0, os.environ['HARNESS_ARTIFACT'])\n"
                              "import solution\nsys.exit(0 if solution.total() == 6 else 1)\n")})
        result = self.evaluate(bundle, artifact)
        self.assertEqual(result.status, EvaluationStatus.PASSED)

    def test_forged_observed_with_wrong_content_fails_and_correct_solution_passes(self) -> None:
        forged = self.write("artifact-forged", {"solution.py": (
            "import json, os\n"
            "with open(os.path.join(os.environ['HARNESS_OUT'], 'observed.json'), 'w') as handle:\n"
            "    json.dump({'sum': 6}, handle)\n"  # forged correct value, overwritten by the grader's own observation
            "\ndef total():\n    return 7\n")})
        result = self.evaluate(self.oracle_bundle(), forged)
        self.assertEqual(result.status, EvaluationStatus.FAILED)
        self.assertEqual(result.checks[0]["status"], "failed")
        wrong = self.write("artifact-wrong", {"solution.py": "def total():\n    return 7\n"})
        self.assertEqual(self.evaluate(self.oracle_bundle(), wrong).status, EvaluationStatus.FAILED)
        correct = self.write("artifact-correct", {"solution.py": "def total():\n    return 6\n"})
        self.assertEqual(self.evaluate(self.oracle_bundle(), correct).status, EvaluationStatus.PASSED)
        self.assertEqual(json.loads('{"sum": 6}'), {"sum": 6})  # sanity: expected value is plain JSON


if __name__ == "__main__":
    unittest.main()
