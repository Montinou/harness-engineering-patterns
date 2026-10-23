"""Published statistical profiles and the Monte Carlo simulator over the real decision functions.

Everything here is synthetic: Bernoulli and Beta outcomes drawn through the real record contracts. No model,
no subprocess and no network run. The simulator calls the real ``select_finalist`` and ``decide``; these
tests check that, the arithmetic around them and the manifest enforcement of the published profile.

The smoke tests use a point of the pre-registered calibration grid (not the provisional ``binary_v1`` numbers,
which T24 rewrites from the grid result) and 1000 bootstrap iterations, so they stay fast and do not depend on
the value T24 selects.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import random
import unittest
from unittest import mock

from harness_workbench.experiments import simulate
from harness_workbench.experiments.contracts import TrialRecord
from harness_workbench.experiments.profiles import CALIBRATION_GRID, PROFILES, StatisticalProfile
from harness_workbench.experiments.simulate import (
    MATRIX_FULL,
    MATRIX_SMOKE,
    RECREATIONS,
    Scenario,
    run_protocol,
    run_scenario,
    simulate_records,
    wilson_interval,
)
from harness_workbench.experiments.status import DecisionStatus, ExecutionStatus, Provenance
from tests.unit import test_manifest as manifest_tests

BASE = PROFILES["binary_v1"]
# A pre-registered grid point with enough regression units to have power, and a fast bootstrap.
FAST = dataclasses.replace(
    BASE, bootstrap_iterations=1000, min_independent_units=40, min_regression_units=60, regression_margin=0.10)


def scenario_by_id(matrix, scenario_id):
    return next(item for item in matrix if item.id == scenario_id)


class ProfileTests(unittest.TestCase):
    def test_binary_v1_is_published_with_the_brief_values(self):
        self.assertIsInstance(BASE, StatisticalProfile)
        self.assertEqual(
            dataclasses.astuple(BASE),
            ("binary_v1", "cluster_percentile_ci_v1", 0.05, 0.05, 0.05, 30, 30, 5, 10000, "binary", (0.5, 0.7),
             "docs/statistical-validation.md"))

    def test_the_calibration_grid_is_the_pre_registered_one(self):
        self.assertEqual(CALIBRATION_GRID["method"], ("cluster_percentile_ci_v1", "cluster_t_v1"))
        self.assertEqual(CALIBRATION_GRID["min_independent_units"], (30, 40, 50, 60))
        self.assertEqual(CALIBRATION_GRID["min_regression_units"], (30, 40, 60, 80))
        self.assertEqual(CALIBRATION_GRID["regression_margin"], (0.05, 0.10))
        self.assertEqual(CALIBRATION_GRID["repetitions"], (5,))
        self.assertNotIn("alpha", CALIBRATION_GRID)
        self.assertNotIn("practical_margin", CALIBRATION_GRID)
        self.assertEqual(len(simulate.grid_points()), 2 * 4 * 4 * 2)

    def test_a_profile_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            dataclasses.replace(BASE, method="bootstrap_v9")
        with self.assertRaises(ValueError):
            dataclasses.replace(BASE, alpha=0.0)
        with self.assertRaises(ValueError):
            dataclasses.replace(BASE, min_independent_units=0)


class ProfileEnforcementTests(manifest_tests.ManifestCase):
    def test_profile_mismatch_is_rejected_at_load(self):
        self.load_dict(manifest_tests.v2_dict())  # the profile's own values load
        message = self.assert_confirmatory_problem(
            manifest_tests.v2_dict(statistics={"method": "cluster_t_v1"}), "method", "cluster_t_v1", "binary_v1")
        self.assertIn(BASE.method, message)
        self.assert_confirmatory_problem(
            manifest_tests.v2_dict(statistics={"regression_margin": 0.10}), "regression_margin", "binary_v1")

    def test_a_manifest_above_the_profile_floors_still_loads(self):
        self.load_dict(manifest_tests.v2_dict(
            repetitions=8, statistics={"min_independent_units": 50, "min_regression_units": 80,
                                       "bootstrap_iterations": 20000}))

    def test_floors_are_read_from_the_profile_table(self):
        raised = dataclasses.replace(BASE, min_independent_units=50)
        with mock.patch.dict(PROFILES, {"binary_v1": raised}):
            self.assert_confirmatory_problem(manifest_tests.v2_dict(), "min_independent_units", "50")

    def test_an_exploratory_manifest_is_not_held_to_the_profile(self):
        self.load_dict(manifest_tests.v2_dict(mode="exploratory", statistics={"method": "cluster_t_v1"}))


class WilsonTests(unittest.TestCase):
    def test_wilson_interval_known_values(self):
        # Reference values from an independent 40-digit decimal evaluation of the Wilson formula.
        low, high = wilson_interval(0, 100)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.0369948075, places=8)
        low, high = wilson_interval(50, 100)
        self.assertAlmostEqual(low, 0.4038298286, places=8)
        self.assertAlmostEqual(high, 0.5961701714, places=8)
        low, high = wilson_interval(100, 100)
        self.assertAlmostEqual(low, 0.9630051925, places=8)
        self.assertEqual(high, 1.0)
        low, high = wilson_interval(7, 200, z=1.0)
        self.assertAlmostEqual(low, 0.0241457908, places=8)
        self.assertAlmostEqual(high, 0.0504810749, places=8)

    def test_wilson_interval_rejects_impossible_counts(self):
        for k, n in ((1, 0), (-1, 5), (6, 5)):
            with self.subTest(k=k, n=n):
                with self.assertRaises(ValueError):
                    wilson_interval(k, n)


class BootstrapMemoTests(unittest.TestCase):
    def test_the_lower_and_upper_bounds_of_one_set_of_units_share_one_bootstrap(self):
        from harness_workbench.experiments import stats

        values = [0.1, 0.4, -0.2, 0.3, 0.0, 0.5, 0.2, 0.1]
        stats._sorted_bootstrap_means.cache_clear()
        lower = stats.lower_bound(values, method="cluster_percentile_ci_v1", alpha=0.05, iterations=300, seed=9)
        upper = stats.upper_bound(values, method="cluster_percentile_ci_v1", alpha=0.05, iterations=300, seed=9)
        info = stats._sorted_bootstrap_means.cache_info()
        self.assertEqual((info.misses, info.hits), (1, 1))
        # memoizing changes nothing: the independent implementation still agrees with the cached one
        self.assertAlmostEqual(
            lower[1], stats.reference_lower_bound(values, alpha=0.05, iterations=300, seed=9), delta=0.02)
        self.assertGreater(upper, lower[1])
        # different units or a different seed are different computations
        stats.lower_bound(values[:-1], method="cluster_percentile_ci_v1", alpha=0.05, iterations=300, seed=9)
        stats.lower_bound(values, method="cluster_percentile_ci_v1", alpha=0.05, iterations=300, seed=10)
        self.assertEqual(stats._sorted_bootstrap_means.cache_info().misses, 3)


class SyntheticRecordTests(unittest.TestCase):
    def test_records_go_through_the_real_contracts_and_are_labeled_synthetic(self):
        scenario = Scenario(
            id="shape", kind="power", n_units=4, regression_units=3, repetitions=2, dev_candidates=3, dev_units=5,
            outcome="binary", p_base=0.5, effect=0.2)
        records, plan, spec = simulate_records(scenario, random.Random(1))
        self.assertTrue(all(isinstance(record, TrialRecord) for record in records))
        self.assertEqual({record.variant_id for record in records}, {variant.id for variant in spec.variants})
        self.assertEqual(len(spec.variants), 1 + 3)
        # baseline + 3 candidates on 5 development units, baseline + 3 on 4 + 3 holdout units, 2 repetitions each.
        self.assertEqual(len(records), 4 * (5 + 4 + 3) * 2)
        self.assertEqual({r.split for r in records}, {"development", "validation", "regression"})
        for record in records:
            self.assertEqual(record.execution.tokens.value, 100)
            self.assertEqual(record.execution.tokens.provenance, Provenance.ADAPTER_REPORTED)
            self.assertEqual(record.execution.latency_ms.value, 10.0)
            self.assertEqual(record.execution.latency_ms.provenance, Provenance.MEASURED)
            self.assertIn(record.objective_score, (0.0, 1.0))
        self.assertEqual(plan.repetitions, 2)
        self.assertEqual(len(plan.splits["validation"]), 4)
        self.assertEqual(spec.mode, "confirmatory")

    def test_the_synthetic_structure_is_a_valid_confirmatory_spec_at_the_profile_plan(self):
        target = simulate.target_scenario(BASE)
        _, plan, spec = simulate_records(target, random.Random(2))
        self.assertEqual(spec.validate_confirmatory(), [])
        self.assertEqual(len(plan.splits["validation"]), BASE.min_independent_units)
        self.assertEqual(len(plan.splits["regression"]), BASE.min_regression_units)

    def test_continuous_scores_are_bounded_and_not_all_binary(self):
        scenario = Scenario(
            id="cont", kind="power", n_units=6, regression_units=3, repetitions=3, dev_candidates=1, dev_units=3,
            outcome="continuous", p_base=0.5, effect=0.2)
        records, _, _ = simulate_records(scenario, random.Random(3))
        scores = [record.objective_score for record in records]
        self.assertTrue(all(score is not None and 0.0 <= score <= 1.0 for score in scores))
        self.assertTrue(any(0.0 < score < 1.0 for score in scores))

    def test_family_size_groups_tasks_into_one_independent_unit(self):
        scenario = Scenario(
            id="fam", kind="null", n_units=4, regression_units=2, repetitions=1, dev_candidates=1, dev_units=2,
            outcome="binary", p_base=0.5, effect=0.0, family_size=3, family_correlation=0.5)
        records, plan, _ = simulate_records(scenario, random.Random(4))
        self.assertEqual(len(plan.splits["validation"]), 4 * 3)
        self.assertEqual(len({family for task, family in plan.families.items() if task in plan.splits["validation"]}), 4)

    def test_error_rate_marks_trials_as_execution_errors(self):
        scenario = Scenario(
            id="err", kind="power", n_units=10, regression_units=10, repetitions=2, dev_candidates=1, dev_units=2,
            outcome="binary", p_base=0.5, effect=0.2, error_rate=0.5)
        records, _, _ = simulate_records(scenario, random.Random(5))
        errors = [r for r in records if r.execution.status is ExecutionStatus.EXECUTION_ERROR]
        self.assertTrue(errors)
        self.assertTrue(all(not r.usable and r.objective_score is None for r in errors))
        self.assertLess(len(errors), len(records))

    def test_unknown_usage_switch_marks_usage_unknown(self):
        scenario = simulate.target_scenario(FAST)
        records, _, _ = simulate_records(scenario, random.Random(6), profile_like=FAST, unknown_usage=True)
        self.assertTrue(all(r.execution.tokens.provenance is Provenance.UNKNOWN for r in records))

    def test_the_same_rng_state_gives_the_same_records(self):
        scenario = simulate.target_scenario(FAST)
        first = [r.to_dict() for r in simulate_records(scenario, random.Random(7), profile_like=FAST)[0]]
        second = [r.to_dict() for r in simulate_records(scenario, random.Random(7), profile_like=FAST)[0]]
        self.assertEqual(first, second)


class ProtocolTests(unittest.TestCase):
    def test_simulated_records_go_through_real_decide(self):
        scenario = dataclasses.replace(simulate.target_scenario(FAST), dev_candidates=3)
        with mock.patch.object(simulate, "decide", wraps=simulate.decide) as decide_spy, \
                mock.patch.object(simulate, "select_finalist", wraps=simulate.select_finalist) as select_spy:
            result = run_scenario(scenario, FAST, simulations=3, seed=11, method=FAST.method)
        self.assertEqual(decide_spy.call_count, 3)
        self.assertEqual(select_spy.call_count, 3)
        self.assertEqual(result["simulations"], 3)
        for call in decide_spy.call_args_list:
            self.assertIsNotNone(call.kwargs["selection"])
            self.assertEqual(call.kwargs["integrity_problems"], [])

    def test_run_protocol_returns_a_decision_status(self):
        status = run_protocol(simulate.target_scenario(FAST), FAST, random.Random(12), method=FAST.method)
        self.assertIsInstance(status, DecisionStatus)

    def test_null_smoke_rate_is_small_and_power_smoke_rate_is_large(self):
        null = run_scenario(scenario_by_id(simulate.build_matrix("smoke", FAST), "null-d5"), FAST,
                            simulations=200, seed=1, method=FAST.method)
        power = run_scenario(simulate.target_scenario(FAST), FAST, simulations=200, seed=1, method=FAST.method)
        self.assertLess(null["upper_95"], 0.15)
        self.assertGreater(power["rate"], 0.5)
        for result in (null, power):
            self.assertEqual(sum(result["status_counts"].values()), 200)
            self.assertLessEqual(result["lower_95"], result["rate"])
            self.assertGreaterEqual(result["upper_95"], result["rate"])

    def test_small_configurations_are_insufficient_not_promoted(self):
        a3 = scenario_by_id(RECREATIONS, "A3")
        result = run_scenario(a3, FAST, simulations=30, seed=2, method=FAST.method)
        self.assertEqual(result["promoted"], 0)
        self.assertEqual(result["status_counts"], {"insufficient_evidence": 30})
        self.assertTrue(result["reason_counts"])

    def test_unknown_usage_switch_blocks(self):
        target = simulate.target_scenario(FAST)
        result = run_scenario(target, FAST, simulations=10, seed=3, method=FAST.method, unknown_usage=True)
        self.assertEqual(result["promoted"], 0)
        self.assertEqual(result["status_counts"], {"insufficient_evidence": 10})
        self.assertTrue(any("token usage unknown" in reason for reason in result["reason_counts"]))

    def test_infrastructure_errors_fail_closed(self):
        scenario = dataclasses.replace(simulate.target_scenario(FAST), error_rate=0.05)
        result = run_scenario(scenario, FAST, simulations=10, seed=4, method=FAST.method)
        self.assertEqual(result["promoted"], 0)
        self.assertEqual(set(result["status_counts"]), {"invalid_experiment"})

    def test_a_demonstrated_regression_is_not_promoted(self):
        scenario = dataclasses.replace(simulate.target_scenario(FAST), effect=0.3, regression_effect=-0.3)
        result = run_scenario(scenario, FAST, simulations=20, seed=5, method=FAST.method)
        self.assertEqual(result["promoted"], 0)
        self.assertIn("rejected", result["status_counts"])

    def test_results_are_labeled_synthetic_and_reproducible(self):
        scenario = dataclasses.replace(simulate.target_scenario(FAST), dev_candidates=2)
        first = run_scenario(scenario, FAST, simulations=6, seed=21, method=FAST.method)
        second = run_scenario(scenario, FAST, simulations=6, seed=21, method=FAST.method)
        other = run_scenario(scenario, FAST, simulations=6, seed=22, method=FAST.method)
        self.assertEqual(first, second)
        self.assertTrue(first["synthetic"])
        self.assertEqual(first["scenario"]["id"], scenario.id)
        self.assertEqual(
            set(first), {"synthetic", "scenario", "simulations", "promoted", "rate", "lower_95", "upper_95",
                         "status_counts", "reason_counts", "method", "seed"})
        self.assertNotEqual(first["seed"], other["seed"])

    def test_workers_do_not_change_the_result(self):
        scenario = dataclasses.replace(simulate.target_scenario(FAST), dev_candidates=2)
        serial = run_scenario(scenario, FAST, simulations=8, seed=31, method=FAST.method, workers=1)
        parallel = run_scenario(scenario, FAST, simulations=8, seed=31, method=FAST.method, workers=2)
        self.assertEqual(serial, parallel)


class MatrixTests(unittest.TestCase):
    def test_smoke_matrix_has_six_scenarios(self):
        self.assertEqual(len(MATRIX_SMOKE), 6)
        self.assertEqual(len({item.id for item in MATRIX_SMOKE}), 6)

    def test_full_matrix_has_every_required_dimension_and_the_target_scenario(self):
        matrix = MATRIX_FULL
        self.assertEqual(len({item.id for item in matrix}), len(matrix))
        factorial = {(s.dev_candidates, s.n_units, s.repetitions, s.outcome) for s in matrix}
        for candidates in (1, 5, 20):
            for units in (10, BASE.min_independent_units):
                for repetitions in (1, 3, 5, 10):
                    for outcome in ("binary", "continuous"):
                        self.assertIn((candidates, units, repetitions, outcome), factorial)
        self.assertTrue(any(s.difficulty_spread == 0.2 for s in matrix))
        self.assertTrue(any(s.family_correlation == 0.5 and s.family_size == 3 for s in matrix))
        self.assertTrue(any(s.effect_concentration == 0.25 for s in matrix))
        self.assertTrue(any(s.kind == "null" and s.effect == 0.0 for s in matrix))
        self.assertTrue(any(s.kind == "null" and s.effect == BASE.practical_margin for s in matrix))
        self.assertTrue(any(s.regression_effect == -0.10 for s in matrix))
        self.assertTrue(any(s.error_rate == 0.05 for s in matrix))
        target = simulate.target_scenario(BASE)
        self.assertIn(target, matrix)
        self.assertEqual(
            (target.kind, target.outcome, target.p_base, target.effect, target.n_units, target.regression_units,
             target.regression_effect, target.repetitions, target.dev_candidates),
            ("power", "binary", 0.5, 0.2, BASE.min_independent_units, BASE.min_regression_units, 0.0,
             BASE.repetitions, 5))
        self.assertTrue(all(item.kind in ("null", "power") for item in matrix))

    def test_the_matrix_follows_the_profile_it_is_built_for(self):
        matrix = simulate.build_matrix("full", FAST)
        target = simulate.target_scenario(FAST)
        self.assertIn(target, matrix)
        self.assertEqual((target.n_units, target.regression_units), (40, 60))

    def test_recreations_are_a2_a3_a8(self):
        self.assertEqual([item.id for item in RECREATIONS], ["A2", "A3", "A8"])
        a2, a3, a8 = RECREATIONS
        self.assertEqual((a2.n_units, a2.dev_candidates, a2.effect, a2.kind), (3, 5, 0.0, "null"))
        self.assertEqual((a3.n_units, a3.repetitions), (1, 5))
        self.assertEqual((a8.n_units, a8.p_base, a8.effect), (5, 0.5, 0.2))


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = simulate.main(list(argv))
        return code, out.getvalue()

    def test_smoke_json_prints_a_rate_for_every_scenario(self):
        code, text = self.run_cli(
            "--profile", "binary_v1", "--matrix", "smoke", "--simulations", "2", "--seed", "1",
            "--iterations", "200", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertTrue(payload["synthetic"])
        self.assertEqual(payload["profile"], "binary_v1")
        self.assertEqual(payload["seed"], 1)
        self.assertEqual(payload["method"], BASE.method)
        self.assertEqual([item["scenario"]["id"] for item in payload["scenarios"]],
                         [item.id for item in MATRIX_SMOKE])
        for item in payload["scenarios"]:
            self.assertIn("rate", item)
            self.assertEqual(item["simulations"], 2)
        for key in ("overrides", "implementation_hash", "python", "wall_seconds"):
            self.assertIn(key, payload)
        self.assertEqual(payload["overrides"]["bootstrap_iterations"], 200)

    def test_overrides_reach_the_profile_and_the_output_file(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "results.json")
            code, text = self.run_cli(
                "--matrix", "recreations", "--simulations", "2", "--seed", "3", "--iterations", "200",
                "--method", "cluster_t_v1", "--units", "40", "--regression-units", "60",
                "--regression-margin", "0.1", "--out", target)
            self.assertEqual(code, 0)
            with open(target, encoding="utf-8") as handle:
                payload = json.load(handle)
        self.assertEqual(payload["method"], "cluster_t_v1")
        self.assertEqual(payload["overrides"], {
            "method": "cluster_t_v1", "min_independent_units": 40, "min_regression_units": 60,
            "regression_margin": 0.1, "bootstrap_iterations": 200})
        self.assertIn("A3", text)
        self.assertIn("synthetic", text.lower())

    def test_null_and_power_counts_are_separate(self):
        code, text = self.run_cli(
            "--matrix", "recreations", "--null-simulations", "3", "--power-simulations", "2", "--seed", "1",
            "--iterations", "200", "--json")
        self.assertEqual(code, 0)
        counts = {item["scenario"]["id"]: item["simulations"] for item in json.loads(text)["scenarios"]}
        self.assertEqual(counts, {"A2": 3, "A3": 3, "A8": 2})

    def test_missing_simulation_counts_are_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            simulate.main(["--matrix", "smoke", "--seed", "1"])

    def test_unknown_usage_flag_reaches_the_protocol(self):
        code, text = self.run_cli(
            "--matrix", "smoke", "--simulations", "2", "--seed", "1", "--iterations", "200", "--unknown-usage",
            "--json")
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertTrue(payload["unknown_usage"])
        self.assertTrue(all(item["promoted"] == 0 for item in payload["scenarios"]))


if __name__ == "__main__":
    unittest.main()
