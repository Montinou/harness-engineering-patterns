"""Independent-unit statistics: paired deltas, family units, cluster bootstrap, t bound, non-inferiority."""
from __future__ import annotations

import math
import random
import statistics
import unittest

from harness_workbench.experiments.contracts import (
    EvaluationResult,
    ExecutionResult,
    Measurement,
    TrialRecord,
)
from harness_workbench.experiments.stats import (
    METHODS,
    ConflictingEvidence,
    IncompleteEvidence,
    Unit,
    cluster_percentile_ci_v1,
    cluster_t_v1,
    family_units,
    lower_bound,
    noninferiority,
    paired_task_deltas,
    reference_lower_bound,
    t_quantile,
    upper_bound,
    zero_variance,
)
from harness_workbench.experiments.status import EvaluationStatus, ExecutionStatus


def trial(variant: str, task_id: str, repetition: int, score: float | None, *, attempt: int = 1,
          family: str = "fam") -> TrialRecord:
    trial_id = f"{variant}-{task_id}-{repetition}-{attempt}"
    usable = score is not None
    execution = ExecutionResult(
        trial_id=trial_id,
        status=ExecutionStatus.COMPLETED if usable else ExecutionStatus.EXECUTION_ERROR,
        outputs={}, exit_code=0 if usable else None, stdout_tail="", stderr_tail="",
        tokens=Measurement.unknown(), latency_ms=Measurement.measured(1.0),
        adapter_latency_ms=Measurement.unknown(), returned_model=None, artifact_hash=None,
        error=None if usable else "crashed",
    )
    evaluation = EvaluationResult(
        trial_id=trial_id,
        status=EvaluationStatus.PASSED if usable and score >= 0.5 else
        (EvaluationStatus.FAILED if usable else EvaluationStatus.NOT_EVALUATED),
        rubric_hash="r", artifact_hash=None, checks=[], score=score, error=None,
    )
    return TrialRecord(
        trial_id=trial_id, variant_id=variant, task_id=task_id, split="validation", family=family,
        repetition=repetition, seed=3 + repetition, attempt=attempt, execution=execution,
        evaluation=evaluation, implementation_hash="impl",
    )


class PairedDeltaTests(unittest.TestCase):
    def test_paired_deltas_require_complete_pairs_and_use_last_attempt(self):
        records = []
        for rep in range(2):
            records.append(trial("baseline", "t1", rep, 0.5))
            records.append(trial("cand", "t1", rep, 0.7))
        # A later attempt replaces the earlier one for repetition 0 of the candidate.
        records.append(trial("cand", "t1", 0, 0.9, attempt=2))
        deltas = paired_task_deltas(records, variant_id="cand", task_ids=["t1"], repetitions=2)
        self.assertAlmostEqual(deltas["t1"], ((0.9 - 0.5) + (0.7 - 0.5)) / 2)

        # An earlier attempt listed after the later one never wins.
        shuffled = [records[-1]] + records[:-1]
        again = paired_task_deltas(shuffled, variant_id="cand", task_ids=["t1"], repetitions=2)
        self.assertAlmostEqual(again["t1"], deltas["t1"])

        missing = [r for r in records if not (r.variant_id == "cand" and r.repetition == 1)]
        with self.assertRaises(IncompleteEvidence) as ctx:
            paired_task_deltas(missing, variant_id="cand", task_ids=["t1"], repetitions=2)
        self.assertIn("t1", str(ctx.exception))
        self.assertIn("cand", str(ctx.exception))

        unusable = [r for r in records if not (r.variant_id == "baseline" and r.repetition == 1)]
        unusable.append(trial("baseline", "t1", 1, None))
        with self.assertRaises(IncompleteEvidence) as ctx:
            paired_task_deltas(unusable, variant_id="cand", task_ids=["t1"], repetitions=2)
        self.assertIn("baseline", str(ctx.exception))

        # The last attempt is the one that counts even when an earlier one was usable.
        crashed_last = records + [trial("cand", "t1", 1, None, attempt=3)]
        with self.assertRaises(IncompleteEvidence):
            paired_task_deltas(crashed_last, variant_id="cand", task_ids=["t1"], repetitions=2)

    def test_two_records_for_one_attempt_conflict_unless_they_are_the_same_trial(self):
        records = [trial("baseline", "t1", 0, 0.5), trial("cand", "t1", 0, 0.7)]
        twin = trial("cand", "t1", 0, 0.1)
        twin.trial_id = "another-trial"
        with self.assertRaises(ConflictingEvidence) as ctx:
            paired_task_deltas(records + [twin], variant_id="cand", task_ids=["t1"], repetitions=1)
        self.assertIsInstance(ctx.exception, IncompleteEvidence)
        self.assertIn("t1", str(ctx.exception))
        # The same trial listed twice is not a conflict.
        deltas = paired_task_deltas(records + [records[1]], variant_id="cand", task_ids=["t1"], repetitions=1)
        self.assertAlmostEqual(deltas["t1"], 0.2)

    def test_units_are_families_not_repetitions_or_checks(self):
        records = []
        for task in ("a", "b", "c"):
            for rep in range(5):
                records.append(trial("baseline", task, rep, 0.4))
                records.append(trial("cand", task, rep, 0.4 + {"a": 0.1, "b": 0.2, "c": 0.3}[task]))
        deltas = paired_task_deltas(records, variant_id="cand", task_ids=["a", "b", "c"], repetitions=5)
        units = family_units(deltas, {"a": "f", "b": "f", "c": "f"})
        self.assertEqual(len(units), 1)
        self.assertIsInstance(units[0], Unit)
        self.assertEqual(units[0].id, "f")
        self.assertEqual(units[0].tasks, 3)
        self.assertAlmostEqual(units[0].value, 0.2)

        two = family_units({"a": 0.1, "b": 0.3, "c": 0.5}, {"a": "x", "b": "x", "c": "y"})
        self.assertEqual({u.id: (round(u.value, 6), u.tasks) for u in two}, {"x": (0.2, 2), "y": (0.5, 1)})


class BoundTests(unittest.TestCase):
    def test_percentile_bound_matches_reference_within_tolerance(self):
        rng = random.Random(123)
        values = [rng.gauss(0.1, 0.5) for _ in range(40)]
        estimate, bound = cluster_percentile_ci_v1(values, alpha=0.05, iterations=10000, seed=7)
        reference = reference_lower_bound(values, alpha=0.05, iterations=10000, seed=99)
        self.assertAlmostEqual(estimate, statistics.fmean(values))
        self.assertLess(abs(bound - reference), 0.01)

    def test_t_quantile_known_values(self):
        self.assertAlmostEqual(t_quantile(0.95, 9), 1.8331, delta=1e-3)
        self.assertAlmostEqual(t_quantile(0.975, 29), 2.0452, delta=1e-3)
        self.assertAlmostEqual(t_quantile(0.95, 1), 6.3138, delta=1e-3)
        self.assertAlmostEqual(t_quantile(0.5, 5), 0.0, delta=1e-6)
        self.assertAlmostEqual(t_quantile(0.05, 9), -1.8331, delta=1e-3)

    def test_t_bound_matches_known_value(self):
        values = [float(v) for v in range(1, 11)]
        estimate, bound = cluster_t_v1(values, alpha=0.05)
        sd = statistics.stdev(values)
        self.assertAlmostEqual(estimate, 5.5)
        self.assertAlmostEqual(bound, 5.5 - 1.8331 * sd / math.sqrt(10), delta=1e-3)
        self.assertEqual(lower_bound(values, method="cluster_t_v1", alpha=0.05, iterations=1, seed=0), (estimate, bound))
        upper = upper_bound(values, method="cluster_t_v1", alpha=0.05, iterations=1, seed=0)
        self.assertAlmostEqual(upper, 5.5 + 1.8331 * sd / math.sqrt(10), delta=1e-3)

    def test_non_finite_values_are_incomplete_evidence_for_every_bound(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            values = [0.1, bad, 0.2, 0.3]
            for method in METHODS:
                kwargs = dict(method=method, alpha=0.05, iterations=200, seed=1)
                with self.assertRaises(IncompleteEvidence, msg=f"lower {method} {bad}"):
                    lower_bound(values, **kwargs)
                with self.assertRaises(IncompleteEvidence, msg=f"upper {method} {bad}"):
                    upper_bound(values, **kwargs)
                with self.assertRaises(IncompleteEvidence, msg=f"noninferiority {method} {bad}"):
                    noninferiority(values, margin=0.1, **kwargs)
            with self.assertRaises(IncompleteEvidence):
                reference_lower_bound(values, alpha=0.05, iterations=200, seed=1)

    def test_bound_needs_two_units(self):
        for method in METHODS:
            with self.assertRaises(IncompleteEvidence):
                lower_bound([0.3], method=method, alpha=0.05, iterations=100, seed=1)
            with self.assertRaises(IncompleteEvidence):
                upper_bound([], method=method, alpha=0.05, iterations=100, seed=1)
        with self.assertRaises(ValueError):
            lower_bound([1.0, 2.0], method="nope", alpha=0.05, iterations=100, seed=1)

    def test_bootstrap_is_seeded_and_reproducible(self):
        values = [0.1, -0.2, 0.3, 0.05, 0.4, -0.1, 0.2]
        first = lower_bound(values, method="cluster_percentile_ci_v1", alpha=0.05, iterations=2000, seed=5)
        second = lower_bound(values, method="cluster_percentile_ci_v1", alpha=0.05, iterations=2000, seed=5)
        other = lower_bound(values, method="cluster_percentile_ci_v1", alpha=0.05, iterations=2000, seed=6)
        self.assertEqual(first, second)
        self.assertNotEqual(first[1], other[1])
        self.assertLessEqual(first[1], first[0])
        self.assertGreaterEqual(
            upper_bound(values, method="cluster_percentile_ci_v1", alpha=0.05, iterations=2000, seed=5), first[0])

    def test_zero_variance(self):
        self.assertTrue(zero_variance([0.2, 0.2, 0.2]))
        self.assertFalse(zero_variance([0.2, 0.2, 0.3]))
        self.assertTrue(zero_variance([0.2]))


class NoninferiorityTests(unittest.TestCase):
    def test_noninferiority_three_outcomes(self):
        for method in METHODS:
            kwargs = dict(margin=0.1, alpha=0.05, iterations=2000, seed=3, method=method)
            ok_estimate, ok = noninferiority([0.01, -0.01, 0.02, 0.0, 0.015, -0.005], **kwargs)
            self.assertTrue(ok, method)
            self.assertAlmostEqual(ok_estimate, 0.005, places=3)
            _, regressed = noninferiority([-0.4, -0.45, -0.38, -0.42, -0.4, -0.41], **kwargs)
            self.assertIs(regressed, False, method)
            _, unknown = noninferiority([0.3, -0.5, 0.2, -0.4, 0.1, -0.2], **kwargs)
            self.assertIsNone(unknown, method)

    def test_reference_lower_bound_needs_two_units(self):
        with self.assertRaises(IncompleteEvidence):
            reference_lower_bound([1.0], alpha=0.05, iterations=100, seed=1)


if __name__ == "__main__":
    unittest.main()
