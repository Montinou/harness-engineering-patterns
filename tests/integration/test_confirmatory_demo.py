"""The synthetic confirmatory demo benchmark: generator, committed files and the end-to-end decision.

The demo is synthetic: its agent is a deterministic script and its tasks are template variants, so nothing
here is evidence about a real agent. It exists so the confirmatory path (controls, development selection,
frozen finalist, holdout, decision, promotion) can be exercised offline with no API key.
"""
from __future__ import annotations

import filecmp
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from harness_workbench.cli import main
from harness_workbench.experiments import ExperimentSpec, run_experiment
from harness_workbench.experiments.command import CommandHarnessExecutor
from harness_workbench.experiments.profiles import PROFILES

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"
GENERATOR = EXAMPLES / "experiments" / "make_confirmatory_demo.py"
MANIFEST = EXAMPLES / "experiments" / "confirmatory-demo.json"
SANDBOXED = EXAMPLES / "experiments" / "sandboxed-demo.json"
BASELINE = EXAMPLES / "workbench" / "base_research_harness.json"
PROFILE = PROFILES["binary_v1"]
GENERATOR_TIMEOUT_SECONDS = 120
SUPPORT_FILES = ("verified_research_harness.json", "noop_research_harness.json")


def _mirror(root: Path) -> Path:
    """A copy of the files a manifest references, laid out like ``examples/``; returns its experiments dir."""
    (root / "workbench").mkdir(parents=True)
    shutil.copy2(BASELINE, root / "workbench" / BASELINE.name)
    experiments = root / "experiments"
    experiments.mkdir()
    shutil.copytree(EXAMPLES / "experiments" / "fixture", experiments / "fixture")
    for name in SUPPORT_FILES:
        shutil.copy2(EXAMPLES / "experiments" / name, experiments / name)
    return experiments


def _same_tree(left: Path, right: Path) -> list[str]:
    """Relative paths that differ between two directory trees (empty when identical)."""
    differences: list[str] = []
    comparison = filecmp.dircmp(left, right)
    differences += [str(Path(left.name) / name) for name in comparison.left_only + comparison.right_only]
    differences += [str(Path(left.name) / name) for name in filecmp.cmpfiles(
        left, right, comparison.common_files, shallow=False)[1]]
    for name in comparison.common_dirs:
        differences += _same_tree(left / name, right / name)
    return differences


def _generate(experiments: Path, *flags: str, out: str) -> Path:
    target = experiments / out
    proc = subprocess.run([sys.executable, "-I", str(GENERATOR), "--out", str(target), *flags],
                          capture_output=True, text=True, timeout=GENERATOR_TIMEOUT_SECONDS)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return target


class GeneratorTests(unittest.TestCase):
    def test_generator_is_deterministic_and_matches_committed_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            experiments = _mirror(Path(folder) / "examples")
            first = _generate(experiments, out="confirmatory-demo.json")
            self.assertEqual(first.read_bytes(), MANIFEST.read_bytes())
            self.assertEqual(_same_tree(experiments / "controls", MANIFEST.parent / "controls"), [])
            sandboxed = _generate(experiments, "--sandboxed", out="sandboxed-demo.json")
            self.assertEqual(sandboxed.read_bytes(), SANDBOXED.read_bytes())
            # A second run changes nothing.
            again = _generate(experiments, out="confirmatory-demo.json")
            self.assertEqual(again.read_bytes(), first.read_bytes())
            self.assertEqual(_same_tree(experiments / "controls", MANIFEST.parent / "controls"), [])

    def test_manifests_have_the_published_shape(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["mode"], "confirmatory")
        self.assertEqual(data["family"], "demo-research-agent")
        self.assertEqual(data["dataset"], {"id": "synthetic-demo", "version": "1"})
        self.assertEqual(data["name"], "synthetic-confirmatory-demo (not evidence about real agents)")
        self.assertEqual(data["environment"], {"mode": "trusted_local", "trusted_solver_acknowledged": True})
        self.assertEqual(data["executor"]["argv"], ["{python}", "-I", "{demo_agent}"])
        self.assertEqual(data["statistics"]["profile"], "binary_v1")
        self.assertEqual(json.loads(SANDBOXED.read_text(encoding="utf-8"))["environment"], {"mode": "sandboxed"})
        splits = [task["split"] for task in data["tasks"]]
        self.assertEqual(splits.count("development"), 6)
        self.assertEqual(splits.count("validation"), PROFILE.min_independent_units)
        self.assertEqual(splits.count("regression"), PROFILE.min_regression_units)
        words = [task["checks"][0]["value"] for task in data["tasks"]]
        self.assertEqual(len(set(words)), len(words), "every task takes a distinct word")

    def test_both_manifests_load_as_valid_confirmatory_specs(self) -> None:
        for path in (MANIFEST, SANDBOXED):
            spec = ExperimentSpec.load(path)
            self.assertEqual(spec.mode, "confirmatory")
            self.assertEqual([variant.id for variant in spec.variants], ["baseline", "verification", "noop"])
            self.assertEqual(spec.validate_confirmatory(), [])
            self.assertNotEqual(spec.variants[1].config_hash, spec.variants[2].config_hash)


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_confirmatory_demo_loads_and_is_eligible_end_to_end(self) -> None:
        spec = ExperimentSpec.load(MANIFEST)
        executor = CommandHarnessExecutor.from_config(spec.executor, spec.environment)
        started = time.monotonic()
        result = run_experiment(spec, executor, self.root)
        print(f"\nconfirmatory demo end-to-end: {time.monotonic() - started:.1f}s on "
              f"python {sys.version.split()[0]}", file=sys.stderr)
        decision = result["decision"]
        self.assertEqual(decision["status"], "eligible", decision)
        self.assertEqual(result["selection"]["finalist_id"], "verification")
        self.assertEqual(decision["candidate_id"], "verification")
        self.assertEqual(decision["diagnostics"]["n_units"], PROFILE.min_independent_units)
        self.assertEqual(decision["diagnostics"]["n_regression_units"], PROFILE.min_regression_units)
        self.assertTrue(decision["diagnostics"]["zero_variance"])
        self.assertEqual(result["summary"]["recommended"], ["verification"])

    def test_improve_with_confirmatory_demo_persists_version_2_via_promotion(self) -> None:
        args = [
            "improve", "--provider", "mock", "--harness", str(BASELINE),
            "--task", "Fact-check a claim using primary sources", "--domain", "research",
            "--validation-spec", str(MANIFEST), "--root", str(self.root), "--json",
        ]
        out, err = StringIO(), StringIO()
        started = time.monotonic()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        print(f"\nimprove with the confirmatory demo: {time.monotonic() - started:.1f}s on "
              f"python {sys.version.split()[0]}", file=sys.stderr)
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["adopted"]["version"], "2")
        self.assertTrue(payload["decision_id"])
        self.assertEqual(payload["validation_evidence"][0]["decision"]["status"], "eligible")
        self.assertTrue((self.root / "library" / "harnesses" / "research-agent" / "2.json").is_file())
        self.assertTrue(list((self.root / "experiments").glob("*/results.json")))
        conn = sqlite3.connect(str(self.root / "library" / "workbench.sqlite3"))
        try:
            rows = conn.execute("SELECT decision_id, harness_id, version FROM promotions").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(payload["decision_id"], "research-agent", "2")])


if __name__ == "__main__":
    unittest.main()
