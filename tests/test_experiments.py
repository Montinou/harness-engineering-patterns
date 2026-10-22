"""Experiments and promotion safety regression suite (stdlib only)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from harness_workbench.core.executor import BehavioralReport, Task
from harness_workbench.core.harness import Harness
from harness_workbench.core.improver import Improver
from harness_workbench.core.proposer import RuleBasedProposer
from harness_workbench.core.store import load_versions
from harness_workbench.experiments import ExperimentSpec, run_experiment
from harness_workbench.experiments.checks import evaluate_checks


class FakeCaseExecutor:
    """Emulates an agent adapter consuming independent workspaces and seeds."""
    def __init__(self) -> None:
        self.workspaces: list[str] = []
        self.seeds: dict[int, set[int]] = {}

    def execute_case(self, harness: Harness, task: Task, context) -> BehavioralReport:
        self.seeds.setdefault(context.repetition, set()).add(context.seed)
        if context.workspace:
            self.workspaces.append(str(context.workspace))
            # Must not leak mutations into the next repetition.
            assert (context.workspace / "seed.txt").read_text() == "original"
            (context.workspace / "output.txt").write_text("verified", encoding="utf-8")
            (context.workspace / "seed.txt").write_text("changed", encoding="utf-8")
        success = "verified" in harness.system_prompt
        if task.id == "regression":
            success = not success
        return BehavioralReport(
            harness_id=harness.id, harness_version=harness.version, task_id=task.id,
            score=0.99 if not success else 0.2,  # Judge conflicts with objective.
            outputs={"answer": "CORRECT" if success else "INCORRECT"},
            cost_tokens=100, latency_ms=10,
        )


class FlatCaseExecutor:
    def execute(self, harness: Harness, task: Task) -> BehavioralReport:
        return BehavioralReport(
            harness_id=harness.id, harness_version=harness.version, task_id=task.id,
            score=0.9 if "verified" in harness.system_prompt else 0.1,
            outputs={"answer": "SAME"}, cost_tokens=100, latency_ms=10,
        )


class ExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        (self.fixture / "seed.txt").write_text("original", encoding="utf-8")
        self.base_path = self.root / "base.json"
        self.edited_path = self.root / "edited.json"
        self.base_path.write_text(Harness(id="sample", version="1", system_prompt="base").to_json())
        self.edited_path.write_text(Harness(id="sample", version="2", system_prompt="verified").to_json())

    def manifest(self, *, regression: bool = False, fixture: bool = True) -> Path:
        checks = [{"id": "answer", "type": "exact", "target": "answer", "value": "CORRECT"}]
        if fixture:
            checks.append({"id": "file", "type": "file_equals", "path": "output.txt", "value": "verified"})
        tasks = [{"id": "positive", "description": "return correct answer", "checks": checks}]
        if regression:
            tasks.append({"id": "regression", "description": "regression task", "checks": checks})
        for task in tasks:
            if fixture:
                task["fixture"] = "fixture"
        content = {
            "name": "unit-experiment", "baseline": "base.json",
            "variants": [{"id": "candidate", "harness": "edited.json"}],
            "tasks": tasks, "repetitions": 5, "seed": 123,
            "bootstrap_iterations": 300,
        }
        path = self.root / "experiment.json"
        path.write_text(json.dumps(content))
        return path

    def test_isolated_paired_repetitions_and_reports(self) -> None:
        spec = ExperimentSpec.load(self.manifest())
        fake = FakeCaseExecutor()
        result = run_experiment(spec, fake, self.root)
        self.assertEqual(len(result["runs"]), 10)
        self.assertTrue(result["summary"]["contrasts"]["candidate"]["eligible"])
        self.assertEqual(result["summary"]["recommended"], ["candidate"])
        self.assertEqual(result["summary"]["contrasts"]["candidate"]["confidence_interval"], [0.5, 0.5])
        self.assertEqual(len(set(fake.workspaces)), 10)
        self.assertTrue(all(len(values) == 1 for values in fake.seeds.values()))
        self.assertEqual((self.fixture / "seed.txt").read_text(), "original")
        self.assertTrue(Path(result["results_path"]).is_file())
        html = Path(result["report_path"]).read_text()
        self.assertIn("unit-experiment", html)
        self.assertIn("Paired delta CI", html)
        # Judge gave weaker candidate a higher behavioral score; it is ignored.
        self.assertTrue(result["summary"]["contrasts"]["candidate"]["eligible"])

    def test_detect_task_specific_regression(self) -> None:
        spec = ExperimentSpec.load(self.manifest(regression=True))
        result = run_experiment(spec, FakeCaseExecutor(), self.root)
        decision = result["summary"]["contrasts"]["candidate"]
        self.assertFalse(decision["eligible"])
        self.assertIn("regression", decision["regressions"])

    def test_behavioral_judge_cannot_promote_identical_outputs(self) -> None:
        spec = ExperimentSpec.load(self.manifest(fixture=False))
        result = run_experiment(spec, FlatCaseExecutor(), self.root)
        self.assertFalse(result["summary"]["contrasts"]["candidate"]["eligible"])
        self.assertEqual(result["summary"]["contrasts"]["candidate"]["mean_delta"], 0)

    def test_fails_closed_when_checks_missing(self) -> None:
        path = self.manifest()
        data = json.loads(path.read_text())
        data["tasks"][0]["checks"] = []
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            ExperimentSpec.load(path)

    def test_checks_cannot_read_outside_workspace(self) -> None:
        check = {"type": "file_exists", "path": "../secret.txt"}
        self.assertFalse(evaluate_checks([check], {}, self.fixture)[0]["passed"])

    def test_command_verifier_runs_in_fixture(self) -> None:
        result = evaluate_checks(
            [{"type": "command", "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('seed.txt').read_text() == 'original'"]}],
            {}, self.fixture,
        )
        self.assertTrue(result[0]["passed"])

    def test_fixture_fingerprint_change_is_detected(self) -> None:
        spec = ExperimentSpec.load(self.manifest())
        (self.fixture / "seed.txt").write_text("tampered")
        result = run_experiment(spec, FakeCaseExecutor(), self.root)
        self.assertFalse(result["summary"]["contrasts"]["candidate"]["eligible"])
        self.assertTrue(any(r["error"] for r in result["runs"]))


class CombinedFailureExecutor:
    def execute(self, harness: Harness, task: Task) -> BehavioralReport:
        flags = int(bool(harness.guardrails)) + int("search" in harness.tool_names())
        score = 0.4 if flags == 0 else (0.85 if flags == 1 else 0.1)
        return BehavioralReport(harness.id, harness.version, task.id, score)


class ComposeAndStoreTests(unittest.TestCase):
    def test_bad_composition_is_rolled_back(self) -> None:
        harness = Harness(id="sample", version="1", system_prompt="Answer")
        task = Task.from_text("research question", domain="research")
        from harness_workbench.core.proposer import FileProposer
        edits = {"edits": [
            {"id": "verify", "kind": "invariant", "component": "guardrails",
             "op": "add_guardrail", "payload": {"text": "Verify claims"},
             "rationale": "add verification"},
            {"id": "search", "kind": "domain", "component": "tools",
             "op": "add_tool", "payload": {"name": "search", "description": "Search sources"},
             "rationale": "add retrieval"}
        ]}
        result = Improver(FileProposer(json.dumps(edits)), CombinedFailureExecutor()).improve(harness, task)
        self.assertIs(result.adopted, harness)
        self.assertEqual(result.adopted_report.score, 0.4)
        self.assertTrue(any(item.startswith("ROLLBACK") for item in result.lessons))

    def test_independent_gate_can_veto_behavioral_improvement(self) -> None:
        from harness_workbench.core.executor import MockExecutor
        harness = Harness(id="sample", version="1", system_prompt="Answer")
        task = Task.from_text("research question", domain="research")
        result = Improver(RuleBasedProposer(), MockExecutor(),
                          promotion_gate=lambda old, new: False).improve(harness, task)
        self.assertIs(result.adopted, harness)
        self.assertTrue(any(item.startswith("VALIDATION REJECT") for item in result.lessons))

    def test_improve_cli_can_require_experiment_validation(self) -> None:
        from harness_workbench.cli import main
        from contextlib import redirect_stdout
        from io import StringIO
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            args = [
                "improve", "--provider", "mock",
                "--harness", str(root / "examples/workbench/base_research_harness.json"),
                "--task", "Fact-check a claim using primary sources",
                "--domain", "research",
                "--validation-spec", str(root / "examples/experiments/local-command.json"),
                "--root", folder,
            ]
            with redirect_stdout(StringIO()):
                self.assertEqual(main(args), 0)
            self.assertTrue(list((Path(folder) / "experiments").glob("*/results.json")))
            self.assertTrue(list((Path(folder) / "library/harnesses/research-agent").glob("2.json")))

    def test_same_parent_can_create_two_immutable_children(self) -> None:
        from harness_workbench.core.executor import MockExecutor
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            harness = Harness(id="sample", version="1", system_prompt="Answer")
            task = Task.from_text("research question", domain="research")
            first = Improver(RuleBasedProposer(), MockExecutor()).improve(harness, task, root=root)
            first_file = (root / "library/harnesses/sample/2.json").read_bytes()
            second = Improver(RuleBasedProposer(), MockExecutor()).improve(harness, task, root=root)
            self.assertNotEqual(first.adopted.version, second.adopted.version)
            self.assertEqual(second.adopted.lineage.parent_version, "1")
            self.assertEqual((root / "library/harnesses/sample/2.json").read_bytes(), first_file)
            self.assertEqual(len(load_versions(root, "sample")), 3)


if __name__ == "__main__":
    unittest.main()
