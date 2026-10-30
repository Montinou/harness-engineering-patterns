"""Development-only finalist selection, the frozen finalist and the holdout fingerprints."""
from __future__ import annotations

import copy
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness_workbench.core.db import WorkbenchDB
from harness_workbench.core.harness import Harness
from harness_workbench.experiments.contracts import (
    EvaluationResult,
    ExecutionResult,
    Measurement,
    PrivateExperimentPlan,
    TrialRecord,
)
from harness_workbench.experiments.manifest import ExperimentSpec
from harness_workbench.experiments.protocol import (
    FrozenSelection,
    build_plan,
    dev_effects,
    freeze_selection,
    holdout_fingerprints,
    select_finalist,
    verify_frozen,
)
from harness_workbench.experiments.status import EvaluationStatus, ExecutionStatus
from harness_workbench.experiments.validation import ValidationError

REPETITIONS = 2
DESCRIPTIONS = {
    "d1": "Summarize the quarterly incident report for the operations team",
    "d2": "Translate the shipping manifest into a sorted inventory table",
    "d3": "Compute the median latency of the attached request samples",
    "v1": "Explain why the nightly backup job exited with a nonzero status",
    "r1": "List the open pull requests that touch the billing module",
}
SPLITS = {"d1": "development", "d2": "development", "d3": "development", "v1": "validation", "r1": "regression"}
FAMILIES = {"d1": "f1", "d2": "f2", "d3": "f2", "v1": "f3", "r1": "f4"}


def task_dict(task_id: str, **overrides: Any) -> dict[str, Any]:
    data = {
        "id": task_id, "description": DESCRIPTIONS[task_id], "split": SPLITS[task_id], "family": FAMILIES[task_id],
        "runs_candidate_code": False,
        "checks": [{"id": "answer", "type": "exact", "target": "answer", "value": "CORRECT"}],
    }
    data.update(overrides)
    return data


class ProtocolCase(unittest.TestCase):
    candidates = ("a", "b", "c")

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.spec = self.make_spec()

    def write_harness(self, name: str, prompt: str) -> None:
        (self.root / f"{name}.json").write_text(Harness(id="h", version="1", system_prompt=prompt).to_json())

    def make_spec(self, tasks: list[dict[str, Any]] | None = None) -> ExperimentSpec:
        self.write_harness("base", "base")
        for name in self.candidates:
            self.write_harness(name, f"candidate {name}")
        data = {
            "schema_version": 2, "name": "protocol-unit", "mode": "exploratory", "family": "unit",
            "dataset": {"id": "unit-set", "version": "1"},
            "baseline": "base.json", "variants": [{"id": name, "harness": f"{name}.json"} for name in self.candidates],
            "tasks": tasks if tasks is not None else [task_dict(task_id) for task_id in DESCRIPTIONS],
            "repetitions": REPETITIONS, "seed": 3,
            "environment": {"mode": "trusted_local"},
            "executor": {"type": "command", "argv": ["{python}", "-I", "agent.py"]},
        }
        return ExperimentSpec.from_dict(data, self.root)

    def trial(self, variant: str, task_id: str, repetition: int, score: float | None, *,
              tokens: float | None = 100.0, attempt: int = 1) -> TrialRecord:
        trial_id = f"{variant}-{task_id}-{repetition}-{attempt}"
        usable = score is not None
        execution = ExecutionResult(
            trial_id=trial_id,
            status=ExecutionStatus.COMPLETED if usable else ExecutionStatus.EXECUTION_ERROR,
            outputs={}, exit_code=0 if usable else None, stdout_tail="", stderr_tail="",
            tokens=Measurement.unknown() if tokens is None else Measurement.measured(tokens),
            latency_ms=Measurement.measured(1.0), adapter_latency_ms=Measurement.unknown(),
            returned_model=None, artifact_hash=None, error=None if usable else "crashed",
        )
        evaluation = EvaluationResult(
            trial_id=trial_id,
            status=EvaluationStatus.PASSED if usable and score >= 0.5 else
            (EvaluationStatus.FAILED if usable else EvaluationStatus.NOT_EVALUATED),
            rubric_hash="r", artifact_hash=None, checks=[], score=score, error=None,
        )
        return TrialRecord(
            trial_id=trial_id, variant_id=variant, task_id=task_id, split=SPLITS[task_id],
            family=FAMILIES[task_id], repetition=repetition, seed=3 + repetition, attempt=attempt,
            execution=execution, evaluation=evaluation, implementation_hash="impl",
        )

    def records(self, scores: dict[str, dict[str, float | None]], *, tokens: dict[str, float | None] | None = None,
                ) -> list[TrialRecord]:
        """``scores[variant][task_id]`` repeated over every repetition; the baseline defaults to 0.5."""
        out: list[TrialRecord] = []
        tokens = tokens or {}
        for variant, per_task in scores.items():
            for task_id, score in per_task.items():
                for repetition in range(REPETITIONS):
                    out.append(self.trial(variant, task_id, repetition, score,
                                          tokens=tokens.get(variant, 100.0)))
        return out

    def dev(self, **deltas: dict[str, float | None]) -> dict[str, dict[str, float | None]]:
        """Baseline 0.5 on the development tasks, candidate score = 0.5 + delta (a ``None`` delta is a crash)."""
        scores: dict[str, dict[str, float | None]] = {"baseline": {t: 0.5 for t in ("d1", "d2", "d3")}}
        for variant, per_task in deltas.items():
            scores[variant] = {t: (None if d is None else 0.5 + d) for t, d in per_task.items()}
        return scores


class SelectionTests(ProtocolCase):
    def test_select_finalist_uses_development_only_and_breaks_ties_deterministically(self):
        # a and b tie on development effect (family mean 0.15); c is lower on development but would win on
        # validation, whose records must be ignored.
        scores = self.dev(
            a={"d1": 0.3, "d2": 0.0, "d3": 0.0},
            b={"d1": 0.3, "d2": 0.0, "d3": 0.0},
            c={"d1": 0.1, "d2": 0.1, "d3": 0.1},
        )
        scores["baseline"]["v1"] = 0.0
        scores["c"]["v1"] = 1.0
        scores["a"]["v1"] = 0.0
        scores["b"]["v1"] = 0.0
        # The tie goes to the lower mean token count, not to listing order.
        records = self.records(scores, tokens={"a": 300.0, "b": 200.0, "c": 10.0})
        effects = dev_effects(records, self.spec)
        self.assertEqual(sorted(effects), ["a", "b", "c"])
        self.assertAlmostEqual(effects["a"], 0.15)
        self.assertAlmostEqual(effects["b"], 0.15)
        self.assertAlmostEqual(effects["c"], 0.1)
        chosen, evidence = select_finalist(self.spec, records)
        self.assertEqual(chosen, "b")
        self.assertEqual(evidence["selection"], "development")
        self.assertAlmostEqual(evidence["effects"]["c"], 0.1)
        self.assertEqual(evidence["tie_break"], "tokens")
        # Same inputs in another order give the same answer.
        self.assertEqual(select_finalist(self.spec, list(reversed(records)))[0], "b")

        # Equal effects and equal tokens fall back to the lexical id.
        scores_equal = self.dev(
            a={"d1": 0.2, "d2": 0.2, "d3": 0.2}, b={"d1": 0.2, "d2": 0.2, "d3": 0.2},
            c={"d1": 0.0, "d2": 0.0, "d3": 0.0},
        )
        tied = self.records(scores_equal)
        chosen, evidence = select_finalist(self.spec, tied)
        self.assertEqual(chosen, "a")
        self.assertEqual(evidence["tie_break"], "id")
        # Unknown token counts cannot break a tie: the id decides, even when b is the cheaper one.
        unknown = self.records(scores_equal, tokens={"a": None, "b": 1.0, "c": 1.0})
        self.assertEqual(select_finalist(self.spec, unknown)[0], "a")

    def test_select_finalist_excludes_incomplete_candidates_and_reports_all_incomplete(self):
        scores = self.dev(
            a={"d1": 0.4, "d2": 0.4, "d3": 0.4},
            b={"d1": 0.1, "d2": 0.1, "d3": 0.1},
            c={"d1": None, "d2": 0.4, "d3": 0.4},  # one crashed pair, the best remaining candidate on paper
        )
        records = self.records(scores)
        effects = dev_effects(records, self.spec)
        self.assertIsNone(effects["c"])
        self.assertAlmostEqual(effects["a"], 0.4)
        chosen, evidence = select_finalist(self.spec, records)
        self.assertEqual(chosen, "a")
        self.assertIn("c", evidence["excluded"])
        self.assertIn("incomplete", evidence["excluded"]["c"])

        # A missing pair (no record at all) is incomplete too.
        missing = [r for r in records if not (r.variant_id == "b" and r.task_id == "d3" and r.repetition == 1)]
        self.assertIsNone(dev_effects(missing, self.spec)["b"])
        # A missing baseline trial makes every candidate incomplete.
        no_base = [r for r in records if not (r.variant_id == "baseline" and r.task_id == "d2")]
        self.assertEqual(dev_effects(no_base, self.spec), {"a": None, "b": None, "c": None})
        self.assertEqual(select_finalist(self.spec, no_base),
                         (None, {"problem": "development evidence incomplete"}))

        everything = self.dev(a={"d1": None, "d2": 0.1, "d3": 0.1}, b={"d1": None, "d2": 0.1, "d3": 0.1},
                              c={"d1": None, "d2": 0.1, "d3": 0.1})
        self.assertEqual(select_finalist(self.spec, self.records(everything)),
                         (None, {"problem": "development evidence incomplete"}))
        # No development records at all.
        self.assertEqual(select_finalist(self.spec, []), (None, {"problem": "development evidence incomplete"}))

    def test_single_candidate_is_the_finalist_without_development(self):
        self.candidates = ("only",)
        spec = self.make_spec()
        self.assertEqual(select_finalist(spec, []), ("only", {"selection": "single_candidate"}))
        # Even broken development evidence does not matter when there is nothing to choose between.
        bad = [self.trial("only", "d1", 0, None)]
        self.assertEqual(select_finalist(spec, bad), ("only", {"selection": "single_candidate"}))

    def test_latest_attempt_replaces_an_earlier_one(self):
        scores = self.dev(a={"d1": 0.2, "d2": 0.2, "d3": 0.2}, b={"d1": 0.0, "d2": 0.0, "d3": 0.0},
                          c={"d1": 0.0, "d2": 0.0, "d3": 0.0})
        records = self.records(scores)
        # A crashed first attempt of a, retried successfully, is complete evidence.
        crashed = [r for r in records if not (r.variant_id == "a" and r.task_id == "d1" and r.repetition == 0)]
        crashed.append(self.trial("a", "d1", 0, None, attempt=1))
        crashed.append(self.trial("a", "d1", 0, 0.7, attempt=2))
        # Every repetition of every development task now pairs a usable candidate trial with the baseline.
        self.assertAlmostEqual(dev_effects(crashed, self.spec)["a"], 0.2)
        # The earlier attempt arriving last in the list does not matter.
        self.assertAlmostEqual(dev_effects(list(reversed(crashed)), self.spec)["a"], 0.2)
        # A later attempt that crashes makes the pair unusable again.
        crashed.append(self.trial("a", "d1", 0, None, attempt=3))
        self.assertIsNone(dev_effects(crashed, self.spec)["a"])


class PlanAndHoldoutTests(ProtocolCase):
    def test_build_plan_fills_every_field_from_the_spec(self):
        plan = build_plan(self.spec)
        self.assertIsInstance(plan, PrivateExperimentPlan)
        self.assertEqual(plan.splits["development"], ("d1", "d2", "d3"))
        self.assertEqual(plan.splits["validation"], ("v1",))
        self.assertEqual(plan.splits["regression"], ("r1",))
        self.assertEqual(plan.families["d3"], "f2")
        self.assertEqual(plan.task_fingerprints, {t.id: t.content_fingerprint for t in self.spec.tasks})
        self.assertEqual(plan.variant_hashes["baseline"], self.spec.variants[0].harness.execution_config_hash())
        self.assertEqual(plan.rubric_hashes, {t.id: t.bundle_hash for t in self.spec.tasks})
        self.assertEqual((plan.repetitions, plan.seed, plan.environment_mode), (REPETITIONS, 3, "trusted_local"))
        self.assertEqual(plan.policy["profile"], "binary_v1")
        self.assertEqual(plan.regression_groups, {"regression": ("f4",)})
        self.assertEqual(plan.plan_hash, build_plan(self.spec).plan_hash)
        self.assertEqual(PrivateExperimentPlan.from_dict(plan.to_dict()).plan_hash, plan.plan_hash)

    def test_holdout_fingerprints_ignore_task_ids_families_and_development_split(self):
        fingerprints = holdout_fingerprints(self.spec)
        self.assertEqual(sorted(fingerprints), ["r1", "v1"])
        by_id = {t.id: t.content_fingerprint for t in self.spec.tasks}
        self.assertEqual(fingerprints, {"v1": by_id["v1"], "r1": by_id["r1"]})

        # Renaming ids and families of the same content gives the same fingerprint values.
        renamed = []
        for task_id in DESCRIPTIONS:
            renamed.append(task_dict(task_id, id=f"renamed-{task_id}", family=f"other-{task_id}"))
        other = self.make_spec(renamed)
        other_fingerprints = holdout_fingerprints(other)
        self.assertEqual(sorted(other_fingerprints), ["renamed-r1", "renamed-v1"])
        self.assertEqual(sorted(other_fingerprints.values()), sorted(fingerprints.values()))

        # Changing a development task changes nothing; changing a holdout task changes its fingerprint.
        edited = [task_dict(t, description="Completely different development wording") if t == "d1"
                  else task_dict(t) for t in DESCRIPTIONS]
        self.assertEqual(holdout_fingerprints(self.make_spec(edited)), fingerprints)
        changed = [task_dict(t, description=DESCRIPTIONS[t] + " today") if t == "v1" else task_dict(t)
                   for t in DESCRIPTIONS]
        changed_fp = holdout_fingerprints(self.make_spec(changed))
        self.assertNotEqual(changed_fp["v1"], fingerprints["v1"])
        self.assertEqual(changed_fp["r1"], fingerprints["r1"])
        # Moving a validation task into development removes it from the holdout set.
        moved = [task_dict(t, split="development") if t == "v1" else task_dict(t) for t in DESCRIPTIONS]
        self.assertEqual(sorted(holdout_fingerprints(self.make_spec(moved))), ["r1"])


class FrozenSelectionTests(ProtocolCase):
    def freeze(self) -> tuple[FrozenSelection, PrivateExperimentPlan]:
        plan = build_plan(self.spec)
        return freeze_selection(self.spec, plan, "b"), plan

    def harnesses(self) -> tuple[Harness, Harness]:
        return (self.spec.variants[2].harness.copy(), self.spec.variants[0].harness.copy())

    def test_freeze_selection_binds_the_hashes_and_round_trips(self):
        selection, plan = self.freeze()
        self.assertEqual(selection.finalist_id, "b")
        self.assertEqual(selection.finalist_hash, self.spec.variants[2].harness.execution_config_hash())
        self.assertEqual(selection.baseline_hash, self.spec.variants[0].harness.execution_config_hash())
        self.assertEqual(selection.plan_hash, plan.plan_hash)
        self.assertEqual(selection.policy_hash, self.spec.statistics.policy_hash)
        self.assertTrue(selection.frozen_at)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            selection.finalist_id = "a"  # type: ignore[misc]
        back = FrozenSelection.from_dict(json.loads(json.dumps(selection.to_dict())))
        self.assertEqual(back, selection)
        self.assertEqual(back.selection_hash, selection.selection_hash)
        changed = dataclasses.replace(selection, finalist_hash="x" * 64)
        self.assertNotEqual(changed.selection_hash, selection.selection_hash)
        with_evidence = freeze_selection(self.spec, plan, "b", evidence={"selection": "development"})
        self.assertEqual(with_evidence.evidence, {"selection": "development"})

    def test_freeze_selection_refuses_the_baseline_an_unknown_variant_and_a_foreign_plan(self):
        plan = build_plan(self.spec)
        for finalist in ("baseline", "nope"):
            with self.assertRaises(ValueError):
                freeze_selection(self.spec, plan, finalist)
        foreign = dataclasses.replace(plan, variant_hashes={**plan.variant_hashes, "b": "0" * 64})
        with self.assertRaises(ValueError):
            freeze_selection(self.spec, foreign, "b")

    def test_from_dict_rejects_missing_and_mistyped_fields(self):
        selection, _ = self.freeze()
        data = selection.to_dict()
        for key in data:
            broken = copy.deepcopy(data)
            del broken[key]
            with self.assertRaises(ValidationError, msg=key):
                FrozenSelection.from_dict(broken)
        broken = copy.deepcopy(data)
        broken["finalist_hash"] = 5
        with self.assertRaises(ValidationError):
            FrozenSelection.from_dict(broken)
        broken = copy.deepcopy(data)
        broken["evidence"] = {"x": float("nan")}
        with self.assertRaises(ValidationError):
            FrozenSelection.from_dict(broken)

    def test_selection_is_accepted_by_the_database(self):
        selection, plan = self.freeze()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = WorkbenchDB.open(Path(tmp.name))
        self.addCleanup(db.close)
        db.start_experiment("exp1", name="exp", family="fam", mode="exploratory", schema_version=2,
                            plan=plan, implementation_hash="impl")
        db.record_selection("exp1", selection.to_dict())
        stored = db.selection("exp1")
        assert stored is not None
        self.assertEqual(stored["finalist_hash"], selection.finalist_hash)
        self.assertEqual(stored["plan_hash"], plan.plan_hash)

    def test_verify_frozen_accepts_the_frozen_objects(self):
        selection, plan = self.freeze()
        candidate, baseline = self.harnesses()
        self.assertEqual(verify_frozen(selection, candidate, baseline, plan), [])

    def test_verify_frozen_detects_any_parameter_change(self):
        selection, plan = self.freeze()
        candidate, baseline = self.harnesses()
        # A guardrail edited after the freeze.
        candidate.guardrails = [*candidate.guardrails, "added after freezing"]
        problems = verify_frozen(selection, candidate, baseline, plan)
        self.assertEqual(len(problems), 1)
        self.assertIn("finalist", problems[0])

        # A baseline change.
        candidate, baseline = self.harnesses()
        baseline.system_prompt = "edited baseline"
        problems = verify_frozen(selection, candidate, baseline, plan)
        self.assertEqual(len(problems), 1)
        self.assertIn("baseline", problems[0])

        # A plan change (even an irrelevant-looking one) and a policy change inside the plan.
        candidate, baseline = self.harnesses()
        edited_plan = dataclasses.replace(plan, seed=plan.seed + 1)
        problems = verify_frozen(selection, candidate, baseline, edited_plan)
        self.assertTrue(any("plan" in p for p in problems))
        looser = dataclasses.replace(plan, policy={**plan.policy, "alpha": 0.2})
        problems = verify_frozen(selection, candidate, baseline, looser)
        self.assertTrue(any("policy" in p for p in problems))
        self.assertTrue(any("plan" in p for p in problems))

        # A plan that does not know the finalist hash the selection froze.
        swapped = dataclasses.replace(plan, variant_hashes={**plan.variant_hashes, "b": "0" * 64})
        self.assertTrue(any("variant" in p for p in verify_frozen(selection, candidate, baseline, swapped)))

        # Several changes are all reported.
        candidate.system_prompt = "edited candidate"
        baseline.system_prompt = "edited baseline"
        self.assertGreaterEqual(len(verify_frozen(selection, candidate, baseline, edited_plan)), 3)

    def test_verify_frozen_ignores_administrative_fields(self):
        selection, plan = self.freeze()
        candidate, baseline = self.harnesses()
        candidate.notes = "renamed later"
        candidate.version = "99"
        self.assertEqual(verify_frozen(selection, candidate, baseline, plan), [])


if __name__ == "__main__":
    unittest.main()
