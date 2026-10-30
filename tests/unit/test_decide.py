"""The decision function: fail-closed budgets, integrity blocks and non-inferiority, in a fixed order.

Records are synthetic (``tests.support.make_records``): nothing runs. The confirmatory fixture has 51
validation tasks, 80 regression tasks and 2 development tasks, each its own family, so it meets the
``binary_v1`` profile floors (50 validation and 80 regression independent units) and every status is
reachable from data.
"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from typing import Any

from harness_workbench.experiments import ExperimentSpec, compare
from harness_workbench.experiments.contracts import EvaluationResult, ExperimentDecision, Measurement
from harness_workbench.experiments.profiles import PROFILES
from harness_workbench.experiments.protocol import build_plan, freeze_selection
from harness_workbench.experiments.stats import decide
from harness_workbench.experiments.status import DecisionStatus, EvaluationStatus, Provenance
from tests.support import exact_task, make_records, write_manifest

CONTROLS = {"positive": {"outputs": {"answer": "CORRECT"}}, "negatives": [{"outputs": {"answer": "INCORRECT"}}]}
N_UNITS = 51          # validation units: 34 of 51 candidate wins give the 2/3 estimate
N_REGRESSION = 80
DIAGNOSTIC_KEYS = (
    "n_units", "n_regression_units", "repetitions", "method", "estimate", "lower_bound", "zero_variance",
    "token_ratio", "latency_ratio", "latency_gate", "task_deltas", "noninferiority", "group_alpha",
    "unknown_usage_trials",
)

_TMP: tempfile.TemporaryDirectory | None = None
SPEC: ExperimentSpec
SMALL: ExperimentSpec
EXPLORATORY: ExperimentSpec


def _holdout(task_id: str, split: str, **extra: Any) -> dict:
    kind = "validation" if split == "validation" else "regression"
    return exact_task(task_id, f"{kind} scenario {task_id}", split=split, runs_candidate_code=False,
                      controls=CONTROLS, **extra)


def _confirmatory(root: Path, tasks: list[dict], **overrides: Any) -> ExperimentSpec:
    manifest = write_manifest(
        root, tasks=tasks, variants=[{"id": "candidate", "system_prompt": "cand"}], schema_version=2,
        mode="confirmatory", family="decide", dataset={"id": "decide", "version": "1"}, repetitions=5,
        environment={"mode": "trusted_local", "trusted_solver_acknowledged": True}, **overrides)
    return ExperimentSpec.load(manifest)


def setUpModule() -> None:
    global _TMP, SPEC, SMALL, EXPLORATORY
    _TMP = tempfile.TemporaryDirectory()
    root = Path(_TMP.name)
    tasks = [exact_task(f"d{i}", f"development scenario {i}", split="development", runs_candidate_code=False)
             for i in range(2)]
    tasks += [_holdout(f"v{i}", "validation") for i in range(N_UNITS)]
    tasks += [_holdout(f"r{i}", "regression") for i in range(N_REGRESSION)]
    SPEC = _confirmatory(root / "full", tasks)
    SMALL = _confirmatory(root / "small", [_holdout("v0", "validation"), _holdout("r0", "regression")])
    EXPLORATORY = ExperimentSpec.load(write_manifest(
        root / "exploratory", tasks=[exact_task(f"t{i}", f"legacy scenario {i}") for i in range(3)],
        variants=[{"id": "candidate", "system_prompt": "cand"}], repetitions=5))


def tearDownModule() -> None:
    if _TMP is not None:
        _TMP.cleanup()


def index(task_id: str) -> int:
    return int(task_id[1:])


def good_score(variant: str, task_id: str, _rep: int) -> float:
    """Baseline always 0; the candidate wins on 34 of the 51 validation tasks and ties on regression."""
    if variant == "baseline" or not task_id.startswith("v"):
        return 0.0
    return 1.0 if index(task_id) % 3 else 0.0


def with_policy(spec: ExperimentSpec, **changes: Any) -> ExperimentSpec:
    return dataclasses.replace(spec, statistics=dataclasses.replace(spec.statistics, **changes))


def with_budget(spec: ExperimentSpec, **changes: Any) -> ExperimentSpec:
    return dataclasses.replace(spec, budget=dataclasses.replace(spec.budget, **changes))


def run_decide(spec: ExperimentSpec, records: list, *, plan: Any = None, selection: Any = None,
               integrity: list[str] | None = None, reason: str | None = None) -> ExperimentDecision:
    return decide(plan or build_plan(spec), records, spec=spec, selection=selection,
                  integrity_problems=list(integrity or []), exploratory_reason=reason)


def replace_record(records: list, trial_id: str, **changes: Any) -> list:
    return [dataclasses.replace(record, **changes) if record.trial_id == trial_id else record
            for record in records]


class DecideCase(unittest.TestCase):
    def reasons(self, decision: ExperimentDecision) -> str:
        return " | ".join(decision.reasons)

    def assertStatus(self, decision: ExperimentDecision, status: DecisionStatus, *fragments: str) -> None:
        self.assertEqual(decision.status, status, self.reasons(decision))
        for fragment in fragments:
            self.assertIn(fragment, self.reasons(decision))


class BudgetTests(DecideCase):
    def test_unknown_baseline_tokens_block_as_insufficient(self) -> None:   # A4
        records = make_records(SPEC, score=good_score, tokens=lambda v, t, r: (
            Measurement.unknown() if v == "baseline" else Measurement.reported(100, Provenance.ADAPTER_REPORTED)))
        decision = run_decide(SPEC, records)
        self.assertStatus(decision, DecisionStatus.INSUFFICIENT_EVIDENCE, "token usage unknown for 655 trials")
        self.assertEqual(decision.diagnostics["unknown_usage_trials"], 655)

    def test_provenance_outside_the_accepted_set_is_unknown(self) -> None:
        records = make_records(SPEC, score=good_score, tokens=Measurement.reported(100, Provenance.ESTIMATED))
        self.assertStatus(run_decide(SPEC, records), DecisionStatus.INSUFFICIENT_EVIDENCE,
                          "token usage unknown for 1310 trials")
        accepting = with_budget(SPEC, accepted_provenance=("estimated",))
        self.assertStatus(run_decide(accepting, records), DecisionStatus.ELIGIBLE)

    def test_zero_baseline_tokens_without_absolute_policy_is_undefined_ratio(self) -> None:
        records = make_records(SPEC, score=good_score, tokens=lambda v, t, r: Measurement.reported(
            0 if v == "baseline" else 100, Provenance.ADAPTER_REPORTED))
        decision = run_decide(SPEC, records)
        self.assertStatus(decision, DecisionStatus.INSUFFICIENT_EVIDENCE,
                          "token ratio undefined: baseline usage is zero")
        self.assertIsNone(decision.diagnostics["token_ratio"])
        absolute = with_budget(SPEC, absolute_token_limit=1000)
        self.assertStatus(run_decide(absolute, records), DecisionStatus.ELIGIBLE)
        heavy = make_records(SPEC, score=good_score, tokens=lambda v, t, r: Measurement.reported(
            0 if v == "baseline" else 5000, Provenance.ADAPTER_REPORTED))
        self.assertStatus(run_decide(absolute, heavy), DecisionStatus.REJECTED, "absolute_token_limit 1000")

    def test_token_ratio_over_the_limit_rejects_and_no_gate_ignores_usage(self) -> None:
        records = make_records(SPEC, score=good_score, tokens=lambda v, t, r: Measurement.reported(
            100 if v == "baseline" else 200, Provenance.ADAPTER_REPORTED))
        decision = run_decide(SPEC, records)
        self.assertStatus(decision, DecisionStatus.REJECTED, "token ratio 2.000 exceeds max_token_ratio 1.5")
        self.assertEqual(decision.diagnostics["token_ratio"], 2.0)
        unknown = make_records(SPEC, score=good_score, tokens=Measurement.unknown())
        ungated = with_budget(SPEC, max_token_ratio=None, max_latency_ratio=None)
        self.assertStatus(run_decide(ungated, unknown), DecisionStatus.ELIGIBLE)
        self.assertEqual(run_decide(ungated, unknown).diagnostics["unknown_usage_trials"], 1310)

    def test_latency_not_applicable_on_both_sides_skips_gate_but_unknown_blocks(self) -> None:
        not_applicable = make_records(SPEC, score=good_score, latency=Measurement.not_applicable())
        decision = run_decide(SPEC, not_applicable)
        self.assertStatus(decision, DecisionStatus.ELIGIBLE)
        self.assertEqual(decision.diagnostics["latency_gate"], "not_applicable: in-process executor")
        self.assertIsNone(decision.diagnostics["latency_ratio"])
        unknown = make_records(SPEC, score=good_score, latency=lambda v, t, r: (
            Measurement.unknown() if (v, t, r) == ("baseline", "v1", 0) else Measurement.measured(10.0)))
        self.assertStatus(run_decide(SPEC, unknown), DecisionStatus.INSUFFICIENT_EVIDENCE, "latency unknown for 1 trials")
        mixed = make_records(SPEC, score=good_score, latency=lambda v, t, r: (
            Measurement.not_applicable() if v == "baseline" else Measurement.measured(10.0)))
        self.assertStatus(run_decide(SPEC, mixed), DecisionStatus.INSUFFICIENT_EVIDENCE, "latency not applicable")

    def test_latency_ratio_over_the_limit_rejects_and_zero_baseline_is_undefined(self) -> None:
        slow = make_records(SPEC, score=good_score, latency=lambda v, t, r: Measurement.measured(
            10.0 if v == "baseline" else 30.0))
        decision = run_decide(SPEC, slow)
        self.assertStatus(decision, DecisionStatus.REJECTED, "latency ratio 3.000 exceeds max_latency_ratio 1.5")
        self.assertEqual(decision.diagnostics["latency_ratio"], 3.0)
        zero = make_records(SPEC, score=good_score, latency=lambda v, t, r: Measurement.measured(
            0.0 if v == "baseline" else 30.0))
        self.assertStatus(run_decide(SPEC, zero), DecisionStatus.INSUFFICIENT_EVIDENCE,
                          "latency ratio undefined: baseline latency is zero")


class IntegrityTests(DecideCase):
    def test_grader_error_invalidates(self) -> None:   # A5 at decision level
        records = make_records(SPEC, score=good_score)
        broken = EvaluationResult("baseline.v1.0", EvaluationStatus.EVALUATION_ERROR, "r", None, [], None,
                                  "grader could not run")
        decision = run_decide(SPEC, replace_record(records, "baseline.v1.0", evaluation=broken))
        self.assertStatus(decision, DecisionStatus.INVALID_EXPERIMENT,
                          "unresolved execution/evaluation error in trial baseline.v1.0")

    def test_an_unusable_trial_outside_the_finalist_comparison_is_ignored(self) -> None:
        records = make_records(SPEC, score=good_score)
        records += make_records(SPEC, score=None, variants=["other"])               # another candidate
        records += make_records(SPEC, score=None, splits=("development",))          # development phase
        self.assertStatus(run_decide(SPEC, records), DecisionStatus.ELIGIBLE)

    def test_integrity_problems_come_first(self) -> None:
        records = make_records(SPEC, score=good_score)
        decision = run_decide(SPEC, records, integrity=["control positive of task v1 failed"],
                              reason="holdout already consumed")
        self.assertStatus(decision, DecisionStatus.INVALID_EXPERIMENT, "control positive of task v1 failed")
        self.assertNotIn("holdout already consumed", self.reasons(decision))

    def test_critical_check_failure_rejects(self) -> None:
        tasks = [_holdout("v0", "validation", checks=[
            {"id": "answer", "type": "exact", "target": "answer", "value": "CORRECT"},
            {"id": "safety", "type": "exact", "target": "safe", "value": "yes", "critical": True}]),
            _holdout("r0", "regression")]
        spec = _confirmatory(Path(_TMP.name) / "critical", tasks)
        self.assertEqual(spec.tasks_in("validation")[0].critical_check_ids, frozenset({"safety"}))

        def checks(variant: str, task_id: str, rep: int) -> list[dict]:
            failing = variant == "candidate" and task_id == "v0" and rep == 2
            return [{"id": "answer", "type": "exact", "weight": 1.0, "status": "passed", "passed": True, "detail": ""},
                    {"id": "safety", "type": "exact", "weight": 1.0, "status": "failed" if failing else "passed",
                     "passed": not failing, "detail": ""}]

        decision = run_decide(spec, make_records(spec, score=good_score, checks=checks))
        self.assertStatus(decision, DecisionStatus.REJECTED, "critical invariant safety failed in task v0")
        # the same failure on the baseline is not a finalist invariant
        baseline_fails = make_records(spec, score=good_score, checks=lambda v, t, r: [
            {"id": "answer", "type": "exact", "weight": 1.0, "status": "passed", "passed": True, "detail": ""},
            {"id": "safety", "type": "exact", "weight": 1.0, "status": "failed" if v == "baseline" else "passed",
             "passed": v != "baseline", "detail": ""}])
        self.assertNotIn("critical invariant", self.reasons(run_decide(spec, baseline_fails)))
        # an unresolved trial is checked first: the critical failure does not hide it
        records = make_records(spec, score=good_score, checks=checks)
        gone = replace_record(records, "baseline.v0.0", evaluation=EvaluationResult(
            "baseline.v0.0", EvaluationStatus.NOT_EVALUATED, "r", None, [], None, "x"))
        self.assertEqual(run_decide(spec, gone).status, DecisionStatus.INVALID_EXPERIMENT)

    def test_a_failing_critical_result_is_not_hidden_by_a_same_id_passing_result(self) -> None:
        # P5 review fix: fail closed even when the results carry a duplicate id (the loader rejects such a manifest)
        tasks = [_holdout("v0", "validation", checks=[
            {"id": "answer", "type": "exact", "target": "answer", "value": "CORRECT"},
            {"id": "safety", "type": "exact", "target": "safe", "value": "yes", "critical": True}]),
            _holdout("r0", "regression")]
        spec = _confirmatory(Path(_TMP.name) / "critical-duplicate", tasks)

        def checks(variant: str, task_id: str, rep: int) -> list[dict]:
            failing = variant == "candidate" and task_id == "v0" and rep == 2
            return [{"id": "safety", "type": "file_exists", "weight": 1.0, "status": "passed", "passed": True, "detail": ""},
                    {"id": "safety", "type": "exact", "weight": 1.0, "status": "failed" if failing else "passed",
                     "passed": not failing, "detail": ""}]

        decision = run_decide(spec, make_records(spec, score=good_score, checks=checks))
        self.assertStatus(decision, DecisionStatus.REJECTED, "critical invariant safety failed in task v0")

    def test_conflicting_records_for_one_attempt_invalidate(self) -> None:
        records = make_records(SPEC, score=good_score)
        twin = dataclasses.replace(records[0], trial_id="twin")
        decision = run_decide(SPEC, [*records, twin])
        self.assertStatus(decision, DecisionStatus.INVALID_EXPERIMENT, "conflicting records")


class EvidenceTests(DecideCase):
    def test_one_task_many_repetitions_is_insufficient(self) -> None:   # A3
        decision = run_decide(SMALL, make_records(SMALL, score=good_score))
        self.assertStatus(decision, DecisionStatus.INSUFFICIENT_EVIDENCE,
                          "independent units 1 is below min_independent_units 50",
                          "regression units 1 is below min_regression_units 80")
        self.assertEqual(decision.n_units, 1)
        self.assertEqual(decision.diagnostics["n_units"], 1)
        self.assertEqual(decision.diagnostics["repetitions"], 5)

    def test_missing_pairs_are_insufficient(self) -> None:
        records = [r for r in make_records(SPEC, score=good_score)
                   if not (r.variant_id == "candidate" and r.task_id == "v3" and r.repetition == 4)]
        self.assertStatus(run_decide(SPEC, records), DecisionStatus.INSUFFICIENT_EVIDENCE,
                          "missing trial for variant 'candidate' task 'v3' repetition 4")
        self.assertStatus(run_decide(SPEC, []), DecisionStatus.INSUFFICIENT_EVIDENCE, "missing trial")

    def test_later_attempt_replaces_an_earlier_one(self) -> None:
        records = make_records(SPEC, score=good_score)
        failed = replace_record(records, "baseline.v1.0", evaluation=EvaluationResult(
            "baseline.v1.0", EvaluationStatus.NOT_EVALUATED, "r", None, [], None, "x"))
        retry = dataclasses.replace(next(r for r in records if r.trial_id == "baseline.v1.0"),
                                    trial_id="baseline.v1.0.retry", attempt=2)
        self.assertEqual(run_decide(SPEC, [*failed, retry]).status, DecisionStatus.ELIGIBLE)

    def test_bound_below_margin_is_rejected_not_insufficient(self) -> None:
        def noise(variant: str, task_id: str, _rep: int) -> float:
            if variant == "baseline" or task_id.startswith("r"):
                return 0.0
            return 0.1 if index(task_id) % 2 else -0.1

        decision = run_decide(SPEC, make_records(SPEC, score=noise))
        self.assertStatus(decision, DecisionStatus.REJECTED, "improvement not supported: lower bound")
        self.assertLessEqual(decision.lower_bound, SPEC.statistics.practical_margin)
        self.assertEqual(decision.n_units, N_UNITS)

    def test_zero_variance_is_insufficient_unless_allowed_with_justification(self) -> None:
        uniform = make_records(SPEC, score=lambda v, t, r: 0.0 if v == "baseline" or t.startswith("r") else 1.0)
        blocked = run_decide(SPEC, uniform)
        self.assertStatus(blocked, DecisionStatus.INSUFFICIENT_EVIDENCE,
                          "degenerate interval: zero variance across units")
        self.assertTrue(blocked.diagnostics["zero_variance"])
        allowed_spec = with_policy(SPEC, allow_zero_variance=True,
                                   zero_variance_justification="deterministic oracle graded by exact match")
        allowed = run_decide(allowed_spec, uniform)
        self.assertStatus(allowed, DecisionStatus.ELIGIBLE)
        self.assertEqual(allowed.diagnostics["zero_variance_justification"],
                         "deterministic oracle graded by exact match")


class RegressionGroupTests(DecideCase):
    @staticmethod
    def regression_score(delta_for: Any) -> Any:
        def score(variant: str, task_id: str, rep: int) -> float:
            if variant == "baseline":
                return 0.5 if task_id.startswith("r") else 0.0
            if task_id.startswith("r"):
                return 0.5 + delta_for(index(task_id))
            return good_score(variant, task_id, rep)
        return score

    def test_regression_group_demonstrated_regression_rejects_and_wide_uncertainty_is_insufficient(self) -> None:
        worse = self.regression_score(lambda i: -0.4 if i % 2 else -0.6)
        decision = run_decide(SPEC, make_records(SPEC, score=worse))
        self.assertStatus(decision, DecisionStatus.REJECTED, "regression group regression: demonstrated regression")
        wide = self.regression_score(lambda i: 1.0 if i % 2 else -1.0)
        decision = run_decide(SPEC, make_records(SPEC, score=wide))
        self.assertStatus(decision, DecisionStatus.INSUFFICIENT_EVIDENCE,
                          "regression group regression: non-inferiority undecided")
        fine = self.regression_score(lambda i: 0.0)
        self.assertEqual(run_decide(SPEC, make_records(SPEC, score=fine)).status, DecisionStatus.ELIGIBLE)

    def test_bonferroni_across_groups(self) -> None:   # 2 groups -> group_alpha == 0.025
        families = [f"r{i}" for i in range(N_REGRESSION)]
        plan = dataclasses.replace(build_plan(SPEC), regression_groups={
            "first": tuple(families[:40]), "second": tuple(families[40:])})
        decision = run_decide(SPEC, make_records(SPEC, score=good_score), plan=plan)
        self.assertStatus(decision, DecisionStatus.ELIGIBLE)
        self.assertEqual(decision.diagnostics["group_alpha"], 0.025)
        self.assertEqual(sorted(decision.diagnostics["noninferiority"]), ["first", "second"])
        for group in decision.diagnostics["noninferiority"].values():
            self.assertEqual(group["units"], 40)
            self.assertEqual(group["outcome"], "non_inferior")
            self.assertIn("lower_bound", group)
            self.assertIn("upper_bound", group)

    def test_default_group_covers_all_regression_families_with_the_full_alpha(self) -> None:
        plan = dataclasses.replace(build_plan(SPEC), regression_groups={})
        decision = run_decide(SPEC, make_records(SPEC, score=good_score), plan=plan)
        self.assertStatus(decision, DecisionStatus.ELIGIBLE)
        self.assertEqual(decision.diagnostics["group_alpha"], SPEC.statistics.alpha)
        self.assertEqual(sorted(decision.diagnostics["noninferiority"]), ["regression"])

    def test_a_regression_family_outside_every_group_is_insufficient(self) -> None:
        plan = dataclasses.replace(build_plan(SPEC), regression_groups={
            "partial": tuple(f"r{i}" for i in range(N_REGRESSION - 1))})
        decision = run_decide(SPEC, make_records(SPEC, score=good_score), plan=plan)
        self.assertStatus(decision, DecisionStatus.INSUFFICIENT_EVIDENCE, "not covered by any regression group: r79")


class ModeTests(DecideCase):
    def test_exploratory_and_exposure_paths(self) -> None:
        records = make_records(EXPLORATORY, splits=None, score=good_score_legacy)
        legacy = run_decide(EXPLORATORY, records)
        self.assertStatus(legacy, DecisionStatus.EXPLORATORY_ONLY, "mode is exploratory", "schema v1 manifest")
        self.assertEqual(legacy.diagnostics["n_units"], 3)
        self.assertEqual(legacy.diagnostics["per_variant"]["candidate"]["n_units"], 3)
        self.assertEqual(legacy.diagnostics["estimate"], 1.0)
        self.assertEqual(legacy.candidate_id, "candidate")
        self.assertEqual(sorted(legacy.evidence_ids), sorted(r.trial_id for r in records))
        # a confirmatory spec with a blocked holdout is exploratory even on eligible data
        blocked = run_decide(SPEC, make_records(SPEC, score=good_score), reason="holdout task abc already consumed")
        self.assertStatus(blocked, DecisionStatus.EXPLORATORY_ONLY, "holdout task abc already consumed")
        self.assertNotEqual(blocked.diagnostics["lower_bound"], None)
        # exploratory beats an unusable trial
        unusable = replace_record(make_records(SPEC, score=good_score), "baseline.v1.0", evaluation=EvaluationResult(
            "baseline.v1.0", EvaluationStatus.NOT_EVALUATED, "r", None, [], None, "x"))
        self.assertEqual(run_decide(SPEC, unusable, reason="blocked").status, DecisionStatus.EXPLORATORY_ONLY)

    def test_a_confirmatory_spec_with_several_candidates_needs_a_frozen_selection(self) -> None:
        many = dataclasses.replace(SPEC, variants=(*SPEC.variants, dataclasses.replace(SPEC.variants[1], id="other")))
        decision = run_decide(many, make_records(SPEC, score=good_score))
        self.assertStatus(decision, DecisionStatus.INVALID_EXPERIMENT, "no frozen selection")


def good_score_legacy(variant: str, _task: str, _rep: int) -> float:
    return 0.0 if variant == "baseline" else 1.0


class EligibleTests(DecideCase):
    def test_eligible_path(self) -> None:
        plan = build_plan(SPEC)
        selection = freeze_selection(SPEC, plan, "candidate", evidence={})
        records = make_records(SPEC, score=good_score)
        decision = run_decide(SPEC, records, plan=plan, selection=selection)
        self.assertStatus(decision, DecisionStatus.ELIGIBLE)
        self.assertEqual(decision.candidate_id, "candidate")
        self.assertEqual(decision.candidate_hash, selection.finalist_hash)
        self.assertEqual(decision.baseline_hash, selection.baseline_hash)
        self.assertEqual(decision.baseline_id, SPEC.variants[0].harness.id)
        self.assertEqual(decision.baseline_version, SPEC.variants[0].harness.version)
        self.assertEqual(decision.plan_hash, plan.plan_hash)
        self.assertEqual(decision.n_units, N_UNITS)
        self.assertAlmostEqual(decision.estimate, 2 / 3, places=9)
        self.assertGreater(decision.lower_bound, SPEC.statistics.practical_margin)
        self.assertEqual(decision.pending_requirements, [])
        # evidence: the last-attempt holdout trials of the baseline and the candidate, all real records
        holdout = {r.trial_id for r in records}
        self.assertEqual(set(decision.evidence_ids), holdout)
        self.assertEqual(len(decision.evidence_ids), 2 * (N_UNITS + N_REGRESSION) * 5)
        for key in DIAGNOSTIC_KEYS:
            self.assertIn(key, decision.diagnostics)
        self.assertEqual(decision.diagnostics["n_regression_units"], N_REGRESSION)
        self.assertEqual(decision.diagnostics["method"], SPEC.statistics.method)
        self.assertEqual(decision.diagnostics["unknown_usage_trials"], 0)
        self.assertEqual(decision.diagnostics["token_ratio"], 1.0)
        self.assertEqual(decision.diagnostics["latency_ratio"], 1.0)
        self.assertEqual(len(decision.diagnostics["task_deltas"]["primary"]), N_UNITS)
        self.assertEqual(decision.diagnostics["group_alpha"], SPEC.statistics.alpha)
        # the decision survives the strict contract round trip that the store applies
        self.assertEqual(ExperimentDecision.from_dict(decision.to_dict()).status, DecisionStatus.ELIGIBLE)

    def test_the_t_method_decides_too(self) -> None:
        # The manifest must match the published profile's method (T23), so the profile table is switched to the
        # t method for the construction of this spec; the decision rule under test is unchanged.
        t_profile = dataclasses.replace(PROFILES["binary_v1"], method="cluster_t_v1")
        with mock.patch.dict(PROFILES, {"binary_v1": t_profile}):
            spec = with_policy(SPEC, method="cluster_t_v1")
        decision = run_decide(spec, make_records(spec, score=good_score))
        self.assertStatus(decision, DecisionStatus.ELIGIBLE)
        self.assertEqual(decision.diagnostics["method"], "cluster_t_v1")


class CompareOrderTests(unittest.TestCase):
    def test_recommended_sort_survives_a_token_tie_with_an_unknown_mean(self) -> None:
        spec = ExperimentSpec.load(write_manifest(
            Path(_TMP.name) / "order", tasks=[exact_task(f"t{i}", f"order scenario {i}") for i in range(3)],
            variants=[{"id": "a", "system_prompt": "a"}, {"id": "b", "system_prompt": "b"}], repetitions=5))
        records = make_records(
            spec, splits=None, variants=["baseline", "a", "b"],
            score=lambda v, t, r: 0.0 if v == "baseline" else 1.0,
            tokens=lambda v, t, r: Measurement.unknown() if v == "a" else Measurement.reported(
                100, Provenance.ADAPTER_REPORTED))
        summary = compare(spec, records)
        self.assertTrue(summary["contrasts"]["a"]["eligible"] and summary["contrasts"]["b"]["eligible"])
        self.assertIsNone(summary["variants"]["a"]["mean_tokens"])
        # both improve by the same amount: the variant with a known token mean ranks first, never a TypeError
        self.assertEqual(summary["recommended"], ["b"])


if __name__ == "__main__":
    unittest.main()
