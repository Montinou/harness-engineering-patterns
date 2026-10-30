"""The promotion gate confirms the final composed harness once; promotion is tied to the persisted decision."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from harness_workbench.core.db import PromotionConflict, WorkbenchDB
from harness_workbench.core.executor import MockExecutor, Task
from harness_workbench.core.harness import Harness
from harness_workbench.core.improver import GateVerdict, Improver
from harness_workbench.core.proposer import RuleBasedProposer
from harness_workbench.core.store import persist_result
from tests.unit.test_contracts import make_plan
from tests.unit.test_db_experiments import make_decision

STEPS = 3


def base_harness() -> Harness:
    return Harness(id="sample", version="1", system_prompt="Answer")


def research_task() -> Task:
    return Task.from_text("research question", domain="research")


def record_decision(root: Path, base: Harness, candidate: Harness) -> str:
    """Persist an eligible decision for exactly ``candidate`` against ``base`` and return its id."""
    root.mkdir(parents=True, exist_ok=True)
    db = WorkbenchDB.open(root)
    try:
        experiment_id = uuid.uuid4().hex
        db.start_experiment(
            experiment_id, name="exp", family="fam", mode="confirmatory", schema_version=1,
            plan=make_plan(), implementation_hash="impl")
        return db.record_decision(experiment_id, make_decision(
            candidate_hash=candidate.execution_config_hash(), baseline_hash=base.execution_config_hash(),
            baseline_id=base.id, baseline_version=base.version))
    finally:
        db.close()


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.harness = base_harness()
        self.task = research_task()

    def improve(self, gate: Any = None, *, root: Path | None = None, k: int = STEPS, mode: str = "sequential"):
        return Improver(RuleBasedProposer(), MockExecutor(), promotion_gate=gate).improve(
            self.harness, self.task, k=k, mode=mode, root=root)

    def stored(self) -> list[Harness]:
        db = WorkbenchDB.open(self.root)
        try:
            return db.versions("sample")
        finally:
            db.close()


class GateCallTests(GateCase):
    def test_gate_is_called_once_with_the_final_composed_harness_in_sequential_mode(self) -> None:
        calls: list[tuple[Harness, Harness]] = []

        def spy(before: Harness, candidate: Harness) -> bool:
            calls.append((before, candidate))
            return True

        result = self.improve(spy)
        self.assertEqual(len(result.steps), STEPS)
        self.assertEqual(len(calls), 1)
        before, candidate = calls[0]
        self.assertIs(before, self.harness)
        self.assertIs(candidate, result.adopted)
        self.assertEqual(candidate.version, "3")

    def test_gate_is_not_called_when_nothing_was_adopted(self) -> None:
        calls: list[Any] = []
        result = Improver(RuleBasedProposer(), MockExecutor(), margin=1.0,
                          promotion_gate=lambda old, new: calls.append(new) or True).improve(
            self.harness, self.task, k=STEPS, mode="sequential")
        self.assertEqual(calls, [])
        self.assertIs(result.adopted, self.harness)
        self.assertEqual([h.version for h in result.versions], ["1"])

    def test_rejected_final_candidate_rolls_back_whole_chain(self) -> None:
        for name, gate in (("bool", lambda old, new: False), ("verdict", lambda old, new: GateVerdict(False))):
            with self.subTest(name):
                result = self.improve(gate)
                self.assertEqual(len(result.steps), STEPS)
                self.assertIs(result.adopted, self.harness)
                self.assertIs(result.steps[-1].adopted, self.harness)
                self.assertEqual(result.adopted_report.score, result.base_report.score)
                self.assertEqual([h.version for h in result.versions], ["1"])
                self.assertIsNone(result.decision_id)
                self.assertTrue(any(item.startswith("VALIDATION REJECT") for item in result.lessons))

    def test_rejected_chain_persists_only_the_base(self) -> None:
        self.improve(lambda old, new: False, root=self.root)
        self.assertEqual([h.version for h in self.stored()], ["1"])

    def test_gate_exception_rolls_back_with_an_error_lesson(self) -> None:
        def broken(old: Harness, new: Harness) -> bool:
            raise RuntimeError("gate unavailable")

        result = self.improve(broken, root=self.root)
        self.assertIs(result.adopted, self.harness)
        self.assertEqual([h.version for h in result.versions], ["1"])
        self.assertTrue(any(item.startswith("VALIDATION ERROR: RuntimeError: gate unavailable")
                            for item in result.lessons))
        self.assertEqual([h.version for h in self.stored()], ["1"])

    def test_a_gate_that_returns_neither_bool_nor_verdict_fails_closed(self) -> None:
        result = self.improve(lambda old, new: {"approved": True})
        self.assertIs(result.adopted, self.harness)
        self.assertTrue(any(item.startswith("VALIDATION ERROR: TypeError") for item in result.lessons))

    def test_parallel_mode_gate_is_called_once_and_can_veto(self) -> None:
        calls: list[Any] = []
        result = self.improve(lambda old, new: calls.append(new) or False, k=1, mode="parallel")
        self.assertEqual(len(calls), 1)
        self.assertIs(result.adopted, self.harness)


class ApprovedChainTests(GateCase):
    def test_approved_sequential_result_persists_base_and_final_only_with_direct_parent(self) -> None:
        decisions: list[str] = []

        def gate(before: Harness, candidate: Harness) -> GateVerdict:
            decisions.append(record_decision(self.root, before, candidate))
            return GateVerdict(True, decisions[-1], {"note": "confirmed"})

        result = self.improve(gate, root=self.root)
        self.assertEqual(len(result.steps), STEPS)
        self.assertEqual(result.decision_id, decisions[0])
        self.assertEqual([h.version for h in self.stored()], ["1", "2"])
        final = self.stored()[-1]
        self.assertEqual(final.lineage.parent_version, "1")
        self.assertEqual(final.execution_config_hash(), result.adopted.execution_config_hash())
        self.assertEqual(result.adopted.version, "2")
        self.assertEqual(result.adopted.lineage.parent_version, "1")
        self.assertEqual([h.version for h in result.versions], ["1", "2"])
        # The intermediate adoptions stay as evidence, not as library versions.
        candidates = json.loads((result.run_dir / "candidates.json").read_text(encoding="utf-8"))
        self.assertEqual(len(candidates), STEPS)
        summary = json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["decision_id"], decisions[0])
        self.assertEqual(result.to_dict()["decision_id"], decisions[0])
        db = WorkbenchDB.open(self.root)
        self.addCleanup(db.close)
        rows = db._conn.execute("SELECT decision_id, version FROM promotions").fetchall()
        self.assertEqual([(row["decision_id"], row["version"]) for row in rows], [(decisions[0], "2")])

    def test_bool_gate_still_supported(self) -> None:
        result = self.improve(lambda old, new: True, root=self.root)
        self.assertIsNone(result.decision_id)
        self.assertEqual([h.version for h in result.versions], ["1", "2"])
        self.assertEqual([h.version for h in self.stored()], ["1", "2"])
        self.assertEqual(self.stored()[-1].lineage.parent_version, "1")

    def test_without_a_gate_every_adopted_step_is_still_persisted(self) -> None:
        result = self.improve(None, root=self.root)
        self.assertEqual([h.version for h in result.versions], ["1", "2", "3"])
        self.assertEqual([h.version for h in self.stored()], ["1", "2", "3"])
        self.assertIsNone(result.decision_id)


class PersistByDecisionTests(GateCase):
    def result(self):
        return self.improve(None, k=1, mode="parallel")

    def test_persist_with_decision_id_is_idempotent_promotion(self) -> None:
        first, second = self.result(), self.result()
        decision_id = record_decision(self.root, first.base, first.adopted)
        persist_result(self.root, first, decision_id=decision_id)
        persist_result(self.root, second, decision_id=decision_id)
        self.assertEqual([h.version for h in self.stored()], ["1", "2"])
        self.assertEqual((first.adopted.version, second.adopted.version), ("2", "2"))
        self.assertEqual(self.stored()[-1].lineage.parent_version, "1")
        db = WorkbenchDB.open(self.root)
        self.addCleanup(db.close)
        self.assertEqual(db._conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0], 1)

    def test_persist_with_decision_for_other_candidate_conflicts(self) -> None:
        result = self.result()
        other = result.adopted.copy()
        other.system_prompt = "a different candidate"
        decision_id = record_decision(self.root, result.base, other)
        with self.assertRaises(PromotionConflict):
            persist_result(self.root, result, decision_id=decision_id)
        self.assertEqual([h.version for h in self.stored()], [])
        self.assertFalse((self.root / "runs").exists())

    def test_persist_with_unknown_decision_conflicts(self) -> None:
        result = self.result()
        with self.assertRaises(PromotionConflict):
            persist_result(self.root, result, decision_id="0" * 32)
        self.assertEqual([h.version for h in self.stored()], [])

    def test_a_decision_promotes_exactly_one_candidate(self) -> None:
        result = self.improve(None, k=STEPS)  # ungated chain: three versions
        decision_id = record_decision(self.root, result.base, result.adopted)
        with self.assertRaises(ValueError):
            persist_result(self.root, result, decision_id=decision_id)
        self.assertEqual([h.version for h in self.stored()], [])


if __name__ == "__main__":
    unittest.main()
