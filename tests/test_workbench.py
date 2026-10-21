"""Offline tests for the Propose–Probe–Compose loop."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from harness_workbench.cli import main
from harness_workbench.core.executor import BehavioralReport, MockExecutor, Task, TraceStep
from harness_workbench.core.harness import Harness, Lineage, ToolSpec, harness_from_dict, load_harness
from harness_workbench.core.improver import Improver
from harness_workbench.core.proposer import LLMProposer, ProposerError, RuleBasedProposer, apply_edit
from harness_workbench.core.store import lineage_chain, load_versions

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "examples" / "workbench" / "base_research_harness.json"


def weak_research() -> Harness:
    return Harness(
        id="research-agent",
        version="1",
        name="Research agent",
        domain="research",
        system_prompt="Answer the research question.",
        tools=[ToolSpec("calc", "Basic calculator.")],
        memory_config={"window": 2, "strategy": "recent"},
        lineage=Lineage(source="seed", created_at="2026-10-21T00:00:00Z"),
    )


def research_task() -> Task:
    return Task.from_text(
        "Fact-check a claim with primary sources and counterevidence",
        domain="research",
        task_id="fact-check",
    )


class FlatExecutor:
    """Every harness looks the same, so compose must keep the base."""

    def execute(self, harness: Harness, task: Task) -> BehavioralReport:
        return BehavioralReport(
            harness_id=harness.id,
            harness_version=harness.version,
            task_id=task.id,
            score=0.5,
            traces=[TraceStep("flat", "no contrast", True)],
            failures=[],
            cost_tokens=1,
            latency_ms=1,
        )


class HarnessModelTests(unittest.TestCase):
    def test_json_roundtrip_and_seed_file(self) -> None:
        loaded = load_harness(SEED)
        self.assertEqual(loaded.id, "research-agent")
        self.assertEqual(loaded.version, "1")
        self.assertEqual(harness_from_dict(loaded.to_dict()), loaded)

    def test_fork_records_parent(self) -> None:
        child = weak_research().fork(source="compose")
        self.assertEqual(child.version, "2")
        self.assertEqual(child.lineage.parent_version, "1")
        self.assertEqual(child.lineage.parent_id, "research-agent")
        self.assertEqual(child.lineage.source, "compose")
        self.assertEqual(child.system_prompt, weak_research().system_prompt)


class ProposerTests(unittest.TestCase):
    def test_rule_proposer_emits_invariant_and_domain(self) -> None:
        harness = weak_research()
        before = harness.to_dict()
        candidates = RuleBasedProposer().propose(research_task(), harness, k=1)
        self.assertEqual(harness.to_dict(), before)
        kinds = [candidate.edit.kind for candidate in candidates]
        self.assertEqual(kinds, ["invariant", "domain"])
        self.assertIn("Verify claims", candidates[0].harness.guardrails[0])
        self.assertIn("search", candidates[1].harness.tool_names())
        self.assertNotEqual(candidates[0].edit.diff, "(no change)")
        self.assertIn("---", candidates[0].edit.diff)

    def test_apply_edit_does_not_mutate_and_is_idempotent(self) -> None:
        harness = weak_research()
        candidates = RuleBasedProposer().propose(research_task(), harness, k=1)
        original_tools = list(harness.tools)
        once = apply_edit(harness, candidates[1].edit)
        twice = apply_edit(once, candidates[1].edit)
        self.assertEqual(harness.tools, original_tools)
        self.assertEqual([tool.name for tool in once.tools], [tool.name for tool in twice.tools])

    def test_llm_proposer_parses_fenced_json(self) -> None:
        def completer(prompt: str) -> str:
            self.assertIn("fact-check", prompt)
            return """```json
            {"edits":[{"id":"llm-verify","kind":"invariant","component":"guardrails",
              "op":"add_guardrail","payload":{"text":"Verify claims against a second source before answering."},
              "rationale":"model said verify"}]}
            ```"""

        candidates = LLMProposer(completer).propose(research_task(), weak_research(), k=1)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].edit.kind, "invariant")
        self.assertIn("Verify claims", candidates[0].harness.guardrails[0])

    def test_llm_proposer_rejects_garbage(self) -> None:
        with self.assertRaises(ProposerError):
            LLMProposer(lambda _prompt: "not json").propose(research_task(), weak_research())


class ExecutorTests(unittest.TestCase):
    def test_mock_is_deterministic_and_prefers_a_richer_harness(self) -> None:
        executor = MockExecutor()
        task = research_task()
        base = weak_research()
        first = executor.execute(base, task)
        second = executor.execute(base, task)
        self.assertEqual(first, second)
        self.assertIn("missing_verification", first.failures)
        self.assertTrue(any(item.startswith("missing_domain_tool") for item in first.failures))
        edited = RuleBasedProposer().propose(task, base, first, k=1)[0].harness
        self.assertGreater(executor.execute(edited, task).score, first.score)


class ImproverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_improve_adopts_refined_harness_and_records_lessons(self) -> None:
        base = weak_research()
        before = base.to_dict()
        result = Improver().improve(base, research_task(), k=1, root=self.root)
        self.assertEqual(base.to_dict(), before)
        adopted = result.adopted
        self.assertEqual(adopted.version, "2")
        self.assertEqual(adopted.lineage.parent_version, "1")
        self.assertEqual(adopted.lineage.parent_id, base.id)
        self.assertIn("Verify claims", " ".join(adopted.guardrails))
        self.assertIn("step-by-step", adopted.system_prompt.lower())
        self.assertIn("search", adopted.tool_names())
        self.assertGreater(result.adopted_report.score, result.base_report.score)
        self.assertTrue(any(lesson.startswith("ADOPT") for lesson in result.lessons))
        self.assertTrue((self.root / "library" / "harnesses" / "research-agent" / "1.json").is_file())
        self.assertTrue((self.root / "library" / "harnesses" / "research-agent" / "2.json").is_file())
        experience = (self.root / "library" / "EXPERIENCE.md").read_text(encoding="utf-8")
        self.assertIn("Fact-check a claim", experience)
        self.assertIn("ADOPT", experience)
        self.assertIsNotNone(result.run_dir)
        assert result.run_dir is not None
        self.assertTrue((result.run_dir / "lessons.md").is_file())
        self.assertTrue((result.run_dir / "reports.json").is_file())

        chain = lineage_chain(load_versions(self.root, "research-agent"))
        self.assertEqual([item.version for item in chain], ["1", "2"])

    def test_second_round_extends_lineage(self) -> None:
        first = Improver().improve(weak_research(), research_task(), root=self.root)
        second = Improver().improve(first.adopted, research_task(), root=self.root)
        self.assertEqual(second.adopted.version, "3")
        self.assertEqual(second.adopted.lineage.parent_version, "2")
        self.assertGreaterEqual(second.adopted.memory_config["window"], 4)
        chain = lineage_chain(load_versions(self.root, "research-agent"))
        self.assertEqual([item.version for item in chain], ["1", "2", "3"])

    def test_flat_executor_keeps_the_base(self) -> None:
        base = weak_research()
        result = Improver(executor=FlatExecutor()).improve(base, research_task(), root=self.root)
        self.assertIs(result.adopted, base)
        self.assertEqual(result.adopted.version, "1")
        self.assertTrue(any("base harness kept" in lesson for lesson in result.lessons))
        self.assertEqual(len(list((self.root / "library" / "harnesses" / "research-agent").glob("*.json"))), 1)

    def test_sequential_carries_evidence_forward(self) -> None:
        result = Improver().improve(weak_research(), research_task(), k=2, mode="sequential", root=self.root)
        self.assertEqual([item.version for item in result.versions], ["1", "2", "3"])
        self.assertGreaterEqual(result.adopted.memory_config["window"], 4)
        self.assertEqual(result.adopted.lineage.parent_version, "2")

    def test_parallel_k2_skips_edits_under_the_margin(self) -> None:
        result = Improver().improve(weak_research(), research_task(), k=2)
        skipped = [lesson for lesson in result.lessons if lesson.startswith("SKIP")]
        adopted = [lesson for lesson in result.lessons if lesson.startswith("ADOPT")]
        self.assertGreaterEqual(len(adopted), 2)
        self.assertTrue(skipped)
        self.assertNotIn("cite_primary", {skill.name for skill in result.adopted.skills})

    def test_noisy_task_adopts_reflection_skill(self) -> None:
        harness = weak_research()
        harness.system_prompt = "Answer the research question. Plan step-by-step, then verify intermediate results."
        harness.guardrails = ["Verify claims against a second source before answering."]
        harness.memory_config = {"window": 6, "strategy": "recent"}
        harness.tools.append(ToolSpec("search", "Search for primary sources and counterevidence."))
        task = Task.from_text(
            "Fact-check a claim when the search tool is noisy and sometimes malformed",
            domain="research",
            task_id="noisy",
        )
        result = Improver().improve(harness, task, k=1)
        self.assertTrue(any(skill.kind == "reflection" for skill in result.adopted.skills))
        self.assertGreater(result.adopted_report.score, result.base_report.score)


def run_cli(argv: list[str]) -> int:
    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        return main(argv)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_propose_probe_improve_report(self) -> None:
        common = [
            "--harness",
            str(SEED),
            "--task",
            "Fact-check a claim with primary sources and counterevidence",
            "--domain",
            "research",
            "--task-id",
            "fact-check",
        ]
        self.assertEqual(run_cli(["propose", *common]), 0)
        self.assertEqual(run_cli(["probe", *common, "--json"]), 0)
        self.assertEqual(run_cli(["improve", *common, "--root", str(self.root), "--k", "1"]), 0)
        self.assertEqual(run_cli(["report", "--root", str(self.root), "--harness-id", "research-agent"]), 0)
        saved = load_harness(self.root / "library" / "harnesses" / "research-agent" / "2.json")
        self.assertEqual(saved.lineage.parent_version, "1")
        self.assertIn("search", saved.tool_names())

    def test_missing_harness_is_an_error(self) -> None:
        code = run_cli(
            [
                "probe",
                "--harness",
                str(self.root / "missing.json"),
                "--task",
                "anything",
            ]
        )
        self.assertEqual(code, 2)

    def test_edits_file_is_the_offline_llm_path(self) -> None:
        edits = self.root / "edits.json"
        edits.write_text(
            json.dumps(
                {
                    "edits": [
                        {
                            "id": "file-search",
                            "kind": "domain",
                            "component": "tools",
                            "op": "add_tool",
                            "payload": {
                                "name": "search",
                                "description": "Search for primary sources and counterevidence.",
                            },
                            "rationale": "external model added search",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        code = run_cli(
            [
                "improve",
                "--harness",
                str(SEED),
                "--task",
                "Fact-check a claim",
                "--domain",
                "research",
                "--edits-file",
                str(edits),
                "--root",
                str(self.root),
            ]
        )
        self.assertEqual(code, 0)
        saved = load_harness(self.root / "library" / "harnesses" / "research-agent" / "2.json")
        self.assertIn("search", saved.tool_names())
        self.assertTrue(any("file-search" in lesson for lesson in saved.lineage.lessons))


class ExampleTests(unittest.TestCase):
    def test_existing_demos_still_run(self) -> None:
        for name in ("harness_learning_demo.py", "harness2_demo.py", "agent_reflex_demo.py"):
            completed = subprocess.run(
                [sys.executable, str(ROOT / "examples" / name)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
