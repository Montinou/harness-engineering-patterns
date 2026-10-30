"""A09: the proposer never sees the holdout tasks or their results.

The promotion gate confirms the final composed harness on the validation and regression tasks. Those
tasks, and what the harnesses produced on them, must never reach the proposer that writes the next edit.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from harness_workbench.core.db import WorkbenchDB
from harness_workbench.core.executor import BehavioralReport, MockExecutor, Task
from harness_workbench.core.harness import Harness
from harness_workbench.core.improver import GateVerdict, Improver
from harness_workbench.core.proposer import RuleBasedProposer
from harness_workbench.experiments import ExperimentSpec, run_experiment
from harness_workbench.experiments.backends import LocalBackend
from harness_workbench.experiments.contracts import ExperimentDecision
from harness_workbench.experiments.manifest import Variant
from harness_workbench.experiments.status import DecisionStatus
from tests.support import exact_task
from tests.unit.test_run_experiment_phases import CONTROLS, DEVELOPMENT, confirmatory_manifest

HOLDOUT_MARKER = "HOLDOUT-ONLY"
STEPS = 3
SPLITS = (("validation", 3), ("regression", 1))


def tasks() -> list[dict[str, Any]]:
    items = [exact_task(task_id, split="development", runs_candidate_code=False) for task_id in DEVELOPMENT]
    number = 0
    for split, count in SPLITS:
        for _ in range(count):
            number += 1
            items.append(exact_task(f"h{number}", f"{HOLDOUT_MARKER}-{number}", split=split,
                                    runs_candidate_code=False, controls=CONTROLS))
    return items


class SpyProposer:
    """Wraps the rule-based proposer and records everything it is shown."""

    name = "spy"

    def __init__(self) -> None:
        self.inner = RuleBasedProposer()
        self.seen: list[tuple[str, str, BehavioralReport]] = []

    def propose(self, task: Task, harness: Harness, report: BehavioralReport, *, k: int = 1):
        self.seen.append((task.id, task.description, report))
        return self.inner.propose(task, harness, report, k=k)


class HoldoutLeakingExecutor:
    """Solves a task only for harnesses that differ from the baseline, and echoes the holdout marker."""

    solver_model = "scripted"
    usage_provenance = "adapter_reported"

    def __init__(self, baseline: Harness) -> None:
        self.baseline_hash = baseline.execution_config_hash()

    def execute_case(self, harness: Harness, task: Task, context: Any) -> BehavioralReport:
        correct = harness.execution_config_hash() != self.baseline_hash
        return BehavioralReport(
            harness_id=harness.id, harness_version=harness.version, task_id=task.id, score=1.0,
            outputs={"answer": "CORRECT" if correct else "INCORRECT", "echo": task.description},
            cost_tokens=100, latency_ms=10)


def eligible(plan, records, *, spec, selection, integrity_problems, exploratory_reason) -> ExperimentDecision:
    return ExperimentDecision(
        DecisionStatus.ELIGIBLE, [], 0.5, 0.1, 4, plan.plan_hash, selection.finalist_id,
        selection.finalist_hash, selection.baseline_hash, spec.variants[0].harness.id,
        spec.variants[0].harness.version, [record.trial_id for record in records], [], {})


class ProposerIsolationTests(unittest.TestCase):
    def test_proposer_never_receives_holdout_tasks_or_results(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            spec = ExperimentSpec.load(confirmatory_manifest(root / "manifest", ("candidate",), tasks=tasks()))
            base = Harness(id="sample", version="1", system_prompt="Answer")
            executor = HoldoutLeakingExecutor(base)
            library = root / "library-root"
            payloads: list[dict[str, Any]] = []

            def gate(before: Harness, candidate: Harness) -> GateVerdict:
                def variant(identity: str, harness: Harness) -> Variant:
                    return Variant(identity, harness, Path("(dynamically-composed)"),
                                   harness.execution_config_hash(), harness.execution_config_hash())

                controlled = replace(spec, variants=(variant("baseline", before), variant("candidate", candidate)))
                payload = run_experiment(controlled, executor, library, backend=LocalBackend(), decide=eligible)
                payloads.append(payload)
                return GateVerdict(payload["decision"]["status"] == DecisionStatus.ELIGIBLE.value,
                                   payload["decision_id"])

            proposer = SpyProposer()
            task = Task.from_text("research question", domain="research")
            result = Improver(proposer, MockExecutor(), promotion_gate=gate).improve(
                base, task, k=STEPS, mode="sequential", root=library)

            # The confirmation really ran on the holdout, once, and promoted the candidate it approved.
            self.assertEqual(len(payloads), 1)
            holdout_ids = {item["id"] for item in tasks() if HOLDOUT_MARKER in item["description"]}
            self.assertEqual({run["task_id"] for run in payloads[0]["runs"]} & holdout_ids, holdout_ids)
            leaked = [run for run in payloads[0]["runs"] if HOLDOUT_MARKER in json.dumps(run)]
            self.assertTrue(leaked, "the scripted executor must have produced holdout-bearing outputs")
            self.assertEqual(result.decision_id, payloads[0]["decision_id"])
            db = WorkbenchDB.open(library)
            self.addCleanup(db.close)
            self.assertEqual([h.version for h in db.versions("sample")], ["1", "2"])

            # The proposer was consulted for every step and saw none of it.
            self.assertEqual(len(proposer.seen), STEPS)
            for task_id, description, report in proposer.seen:
                self.assertNotIn(HOLDOUT_MARKER, description)
                self.assertNotIn(task_id, holdout_ids)
                self.assertNotIn(HOLDOUT_MARKER, json.dumps(report.to_dict()))


if __name__ == "__main__":
    unittest.main()
