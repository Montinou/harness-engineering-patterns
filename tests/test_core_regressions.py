"""Regression coverage for the defects found during the repository review."""
from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harness_workbench.core.anthropic_backend import AnthropicClient, AnthropicError, parse_judge, render_judge_user
from harness_workbench.core.executor import BehavioralReport, Task
from harness_workbench.core.harness import Harness
from harness_workbench.core.improver import Improver
from harness_workbench.core.proposer import FileProposer
from harness_workbench.core.store import load_versions, persist_result


def proposer():
    return FileProposer(json.dumps({"edits": [
        {"id": "a", "kind": "invariant", "op": "add_guardrail", "payload": {"text": "a"}},
        {"id": "b", "kind": "domain", "op": "add_guardrail", "payload": {"text": "b"}},
    ]}))


class Scored:
    def __init__(self, composed=.2):
        self.composed = composed

    def execute(self, harness, task):
        score = .5 if not harness.guardrails else .8 if len(harness.guardrails) == 1 else self.composed
        return BehavioralReport(harness.id, harness.version, task.id, score, cost_tokens=10)


class CompositionTests(unittest.TestCase):
    def test_individually_good_edits_do_not_force_bad_composition(self):
        base = Harness("a", "1", "Answer")
        result = Improver(proposer(), Scored()).improve(base, Task("q", "question"))
        self.assertIs(result.adopted, base)
        self.assertEqual(result.steps[0].composition_report.score, .2)
        self.assertEqual(result.steps[0].total_probe_tokens, 40)
        self.assertEqual(result.to_dict()["total_probe_tokens"], 40)
        self.assertFalse(any(line.startswith("ADOPT") for line in result.lessons))
        self.assertTrue(any(line.startswith("REJECT composition") for line in result.lessons))

    def test_good_composition_is_adopted_after_its_own_probe(self):
        result = Improver(proposer(), Scored(.9)).improve(Harness("a", "1", "Answer"), Task("q", "question"))
        self.assertEqual(result.adopted.version, "2")
        self.assertEqual(result.adopted.lineage.metrics["total_probe_tokens"], 40)
        self.assertTrue(any(line.startswith("ADOPT") for line in result.lessons))

    def test_nonfinite_composition_cannot_be_adopted(self):
        result = Improver(proposer(), Scored(float("nan"))).improve(Harness("a", "1", "Answer"), Task("q", "question"))
        self.assertEqual(result.adopted.version, "1")
        self.assertIn("invalid_score", result.steps[0].composition_report.failures)
        json.dumps(result.to_dict(), allow_nan=False)

    def test_invalid_baseline_fails_closed(self):
        class Bad:
            def execute(self, h, t):
                return BehavioralReport(h.id, h.version, t.id, float("inf"))
        with self.assertRaisesRegex(ValueError, "baseline"):
            Improver(proposer(), Bad()).improve(Harness("a", "1", "Answer"), Task("q", "question"))

    def test_nonfinite_margin_rejected(self):
        with self.assertRaises(ValueError):
            Improver(margin=float("nan"))

    def test_rejected_composition_evidence_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = Improver(proposer(), Scored()).improve(Harness("a", "1", "Answer"), Task("q", "question"), root=tmp)
            reports = json.loads((result.run_dir / "reports.json").read_text())
            self.assertEqual(reports[0]["composition_report"]["score"], .2)
            self.assertEqual(reports[0]["total_probe_tokens"], 40)


class StorageTests(unittest.TestCase):
    def test_version_collision_preserves_old_version_and_new_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = Harness("a", "1", "Answer")
            first = Improver(proposer(), Scored(.9)).improve(base, Task("q", "question"), root=root)
            alias = root / "library" / "harnesses" / "a" / "2.json"
            before = alias.read_bytes()
            second = Improver(proposer(), Scored(.95)).improve(base, Task("q", "another question"))
            with self.assertRaisesRegex(ValueError, "Immutable version conflict"):
                persist_result(root, second)
            self.assertEqual(alias.read_bytes(), before)
            self.assertEqual(len(list((root / "runs").iterdir())), 2)
            self.assertTrue(first.run_dir.exists())

    def test_concurrent_identical_versions_do_not_corrupt_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = Improver(proposer(), Scored(.9)).improve(Harness("a", "1", "Answer"), Task("q", "question"))
            with ThreadPoolExecutor(max_workers=5) as pool:
                paths = list(pool.map(lambda _: persist_result(root, result), range(10)))
            self.assertEqual(len(set(paths)), 10)
            index = json.loads((root / "library" / "index.json").read_text())
            self.assertEqual(len(index["harnesses"]["a"]["versions"]), 2)
            self.assertEqual(len(load_versions(root, "a")), 2)

    def test_path_traversal_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = Improver(proposer(), Scored(.9)).improve(Harness("../escape", "1", "Answer"), Task("q", "question"))
            with self.assertRaisesRegex(ValueError, "unsafe"):
                persist_result(Path(tmp), result)


class EvaluationSeparationTests(unittest.TestCase):
    def test_same_output_gets_identical_judge_input_under_different_harnesses(self):
        task = Task("q", "Answer the question")
        a = render_judge_user("Verify and plan", task, "identical response")
        b = render_judge_user("Do nothing", task, "identical response")
        self.assertEqual(a, b)
        self.assertNotIn("HARNESS", a)
        self.assertNotIn("Verify and plan", a)

    def test_full_answer_is_available_to_judge(self):
        answer = "a" * 10000
        self.assertEqual(json.loads(render_judge_user("ignored", Task("a", "q"), answer))["answer"], answer)

    def test_invalid_judge_numbers_and_failure_shape_are_rejected(self):
        for payload in ('{"score":NaN}', '{"score":Infinity}', '{"score":true}', '{"score":.5}', '{"score":0.5,"failures":"wrong"}'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_judge(payload)

    def test_api_error_redacts_the_actual_secret(self):
        client = AnthropicClient("secret-value", transport=lambda *args: (401, b'{"error":"secret-value"}'))
        with self.assertRaises(AnthropicError) as caught:
            client.complete(model="test", user="hi")
        self.assertNotIn("secret-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
