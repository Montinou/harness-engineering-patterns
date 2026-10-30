import hashlib
import json
import unittest

from harness_workbench.experiments.contracts import (
    EvaluationResult,
    ExecutionResult,
    ExperimentDecision,
    Measurement,
    PrivateExperimentPlan,
    PublicTrialRequest,
    TrialRecord,
)
from harness_workbench.experiments.status import (
    USABLE_EVALUATION,
    USABLE_EXECUTION,
    DecisionStatus,
    EvaluationStatus,
    ExecutionStatus,
    Provenance,
)
from harness_workbench.experiments.validation import ValidationError, json_dumps_strict


def make_execution(status=ExecutionStatus.COMPLETED, error=None, **overrides):
    fields = dict(
        trial_id="t1",
        status=status,
        outputs={"answer": "42"},
        exit_code=0,
        stdout_tail="out",
        stderr_tail="",
        tokens=Measurement.reported(120, Provenance.PROVIDER_REPORTED),
        latency_ms=Measurement.measured(35.5),
        adapter_latency_ms=Measurement.unknown(),
        returned_model="model-x",
        artifact_hash="a" * 8,
        error=error,
    )
    fields.update(overrides)
    return ExecutionResult(**fields)


def make_evaluation(status=EvaluationStatus.PASSED, score=1.0, error=None):
    return EvaluationResult(
        trial_id="t1",
        status=status,
        rubric_hash="r" * 8,
        artifact_hash="a" * 8,
        checks=[{"name": "c", "ok": True}],
        score=score,
        error=error,
    )


def make_record(execution=None, evaluation=None):
    return TrialRecord(
        trial_id="t1",
        variant_id="v1",
        task_id="task-1",
        split="dev",
        family="fam",
        repetition=0,
        seed=7,
        attempt=1,
        execution=execution or make_execution(),
        evaluation=evaluation or make_evaluation(),
        implementation_hash="i" * 8,
    )


def make_plan(**overrides):
    fields = dict(
        experiment_name="exp",
        mode="confirmatory",
        family="fam",
        dataset_id="ds",
        dataset_version="1",
        splits={"dev": ("t1", "t2"), "test": ("t3",)},
        families={"t1": "fam", "t2": "fam", "t3": "fam"},
        regression_groups={"g": ("fam",)},
        variant_hashes={"base": "h1", "cand": "h2"},
        rubric_hashes={"fam": "rh"},
        task_fingerprints={"t1": "f1", "t2": "f2", "t3": "f3"},
        policy={"margin": 0.05},
        budget={"max_tokens": 1000},
        repetitions=3,
        seed=11,
        environment_mode="sandbox",
    )
    fields.update(overrides)
    return PrivateExperimentPlan(**fields)


class StatusTests(unittest.TestCase):
    def test_enum_values_and_str_behaviour(self):
        self.assertEqual(ExecutionStatus.TASK_FAILURE.value, "task_failure")
        self.assertEqual(
            [m.value for m in ExecutionStatus],
            ["completed", "task_failure", "execution_error", "timeout", "resource_limit", "cancelled"],
        )
        self.assertEqual(EvaluationStatus.NOT_EVALUATED.value, "not_evaluated")
        self.assertEqual(DecisionStatus.INSUFFICIENT_EVIDENCE.value, "insufficient_evidence")
        self.assertEqual(Provenance.NOT_APPLICABLE.value, "not_applicable")
        self.assertIsInstance(ExecutionStatus.COMPLETED, str)

    def test_usable_sets(self):
        self.assertEqual(USABLE_EXECUTION, {ExecutionStatus.COMPLETED, ExecutionStatus.TASK_FAILURE})
        self.assertEqual(USABLE_EVALUATION, {EvaluationStatus.PASSED, EvaluationStatus.FAILED})


class MeasurementTests(unittest.TestCase):
    def test_measurement_value_and_provenance_agree(self):
        with self.assertRaises(ValidationError):
            Measurement(None, Provenance.MEASURED)
        with self.assertRaises(ValidationError):
            Measurement(5.0, Provenance.UNKNOWN)
        with self.assertRaises(ValidationError):
            Measurement(5.0, Provenance.NOT_APPLICABLE)
        self.assertEqual(Measurement.measured(0).value, 0)  # zero is a known zero

    def test_measurement_rejects_negative_nan_inf_and_bool(self):
        for bad in (-1, float("nan"), float("inf"), True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    Measurement.measured(bad)

    def test_constructors(self):
        self.assertIsNone(Measurement.unknown().value)
        self.assertEqual(Measurement.unknown().provenance, Provenance.UNKNOWN)
        self.assertEqual(Measurement.not_applicable().provenance, Provenance.NOT_APPLICABLE)
        self.assertEqual(Measurement.measured(3).provenance, Provenance.MEASURED)
        reported = Measurement.reported(9, Provenance.ADAPTER_REPORTED)
        self.assertEqual((reported.value, reported.provenance), (9, Provenance.ADAPTER_REPORTED))

    def test_measurement_roundtrip_and_unknown_provenance_rejected(self):
        for m in (Measurement.unknown(), Measurement.not_applicable(), Measurement.measured(2.5)):
            self.assertEqual(Measurement.from_dict(m.to_dict()), m)
        self.assertEqual(Measurement.measured(2.5).to_dict(), {"value": 2.5, "provenance": "measured"})
        with self.assertRaises(ValidationError):
            Measurement.from_dict({"value": 1, "provenance": "guessed"})


class EvaluationResultTests(unittest.TestCase):
    def test_evaluation_result_score_only_when_usable(self):
        with self.assertRaises(ValidationError):
            EvaluationResult("t", EvaluationStatus.EVALUATION_ERROR, "h", None, [], 0.0, "boom")
        with self.assertRaises(ValidationError):
            EvaluationResult("t", EvaluationStatus.PASSED, "h", None, [], None, None)
        with self.assertRaises(ValidationError):
            EvaluationResult("t", EvaluationStatus.NOT_EVALUATED, "h", None, [], 1.0, None)
        ok = EvaluationResult("t", EvaluationStatus.FAILED, "h", None, [], 0.0, None)
        self.assertEqual(ok.score, 0.0)

    def test_evaluation_result_rejects_nan_score(self):
        with self.assertRaises(ValidationError):
            EvaluationResult("t", EvaluationStatus.PASSED, "h", None, [], float("nan"), None)

    def test_evaluation_roundtrip_and_unknown_status(self):
        e = make_evaluation()
        data = e.to_dict()
        self.assertEqual(data["status"], "passed")
        self.assertEqual(EvaluationResult.from_dict(data), e)
        data["status"] = "bogus"
        with self.assertRaises(ValidationError):
            EvaluationResult.from_dict(data)


class ExecutionResultTests(unittest.TestCase):
    def test_roundtrip_to_dict_from_dict(self):
        e = make_execution()
        data = e.to_dict()
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["tokens"], {"value": 120.0, "provenance": "provider_reported"})
        self.assertEqual(data["adapter_latency_ms"], {"value": None, "provenance": "unknown"})
        self.assertIs(data["retryable"], False)
        json.loads(json_dumps_strict(data, sort_keys=True))
        self.assertEqual(ExecutionResult.from_dict(data), e)

    def test_retryable_roundtrips(self):
        e = make_execution(ExecutionStatus.EXECUTION_ERROR, error="spawn", retryable=True)
        self.assertTrue(ExecutionResult.from_dict(e.to_dict()).retryable)

    def test_to_dict_rejects_non_finite_outputs(self):
        e = make_execution(outputs={"score": float("nan")})
        with self.assertRaises(ValidationError):
            e.to_dict()

    def test_from_dict_rejects_unknown_status_and_missing_field(self):
        data = make_execution().to_dict()
        bad = dict(data, status="exploded")
        with self.assertRaises(ValidationError):
            ExecutionResult.from_dict(bad)
        missing = dict(data)
        del missing["outputs"]
        with self.assertRaises(ValidationError):
            ExecutionResult.from_dict(missing)
        with self.assertRaises(ValidationError):
            ExecutionResult.from_dict("not a dict")


class TrialRecordTests(unittest.TestCase):
    def test_trial_record_usable_and_compat_keys(self):
        broken = make_record(
            execution=make_execution(ExecutionStatus.EXECUTION_ERROR, error="crash"),
            evaluation=make_evaluation(EvaluationStatus.NOT_EVALUATED, score=None),
        )
        self.assertFalse(broken.usable)
        self.assertIsNone(broken.objective_score)

        failed = make_record(
            execution=make_execution(ExecutionStatus.TASK_FAILURE),
            evaluation=make_evaluation(EvaluationStatus.FAILED, score=0.0),
        )
        self.assertTrue(failed.usable)
        self.assertEqual(failed.objective_score, 0.0)
        d = failed.to_dict()
        self.assertEqual(d["case_id"], "task-1")
        self.assertIs(d["verified"], True)
        self.assertIs(d["passed"], False)
        self.assertIsNone(d["error"])
        self.assertEqual(d["objective_score"], 0.0)
        self.assertEqual(d["tokens"], 120.0)
        self.assertEqual(d["latency_ms"], 35.5)
        self.assertEqual(d["execution"]["status"], "task_failure")
        self.assertEqual(d["evaluation"]["status"], "failed")

        bd = broken.to_dict()
        self.assertIs(bd["verified"], False)
        self.assertIs(bd["passed"], False)
        self.assertEqual(bd["error"], "crash")
        self.assertIsNone(bd["objective_score"])

    def test_error_prefers_execution_then_evaluation(self):
        rec = make_record(
            execution=make_execution(),
            evaluation=make_evaluation(EvaluationStatus.EVALUATION_ERROR, score=None, error="judge down"),
        )
        self.assertEqual(rec.to_dict()["error"], "judge down")
        self.assertIs(rec.to_dict()["passed"], False)

    def test_passed_compat_key(self):
        self.assertIs(make_record().to_dict()["passed"], True)

    def test_unknown_tokens_serialize_as_none(self):
        rec = make_record(execution=make_execution(tokens=Measurement.unknown()))
        self.assertIsNone(rec.to_dict()["tokens"])

    def test_roundtrip_to_dict_from_dict(self):
        rec = make_record()
        data = json.loads(json_dumps_strict(rec.to_dict(), sort_keys=True))
        self.assertEqual(TrialRecord.from_dict(data), rec)


class PublicTrialRequestTests(unittest.TestCase):
    def test_roundtrip_restores_tuple(self):
        req = PublicTrialRequest(
            "t1", "do it", "research", ["a", "b"], {"prompt": "p"}, None, 3, {"max_tokens": 10}
        )
        self.assertEqual(req.constraints, ("a", "b"))
        data = json.loads(json_dumps_strict(req.to_dict()))
        self.assertEqual(data["constraints"], ["a", "b"])
        back = PublicTrialRequest.from_dict(data)
        self.assertEqual(back, req)
        self.assertIsInstance(back.constraints, tuple)


class ExperimentDecisionTests(unittest.TestCase):
    def test_roundtrip(self):
        d = ExperimentDecision(
            status=DecisionStatus.ELIGIBLE,
            reasons=["ok"],
            estimate=0.2,
            lower_bound=0.05,
            n_units=12,
            plan_hash="p",
            candidate_id="c",
            candidate_hash="ch",
            baseline_hash="bh",
            baseline_id="b",
            baseline_version="1",
            evidence_ids=["e1"],
            pending_requirements=[],
            diagnostics={"k": 1},
        )
        data = d.to_dict()
        self.assertEqual(data["status"], "eligible")
        self.assertEqual(ExperimentDecision.from_dict(json.loads(json_dumps_strict(data))), d)

    def test_rejects_non_finite_estimate(self):
        with self.assertRaises(ValidationError):
            ExperimentDecision(
                DecisionStatus.REJECTED, [], float("nan"), None, 0, "p", None, None, "bh", "b", "1", [], [], {}
            )


class PrivateExperimentPlanTests(unittest.TestCase):
    def test_plan_hash_is_stable_and_content_sensitive(self):
        a, b = make_plan(), make_plan()
        self.assertEqual(a.plan_hash, b.plan_hash)
        self.assertEqual(
            a.plan_hash,
            hashlib.sha256(json_dumps_strict(a.to_dict(), sort_keys=True).encode("utf-8")).hexdigest(),
        )
        changed = make_plan(splits={"dev": ("t1", "tX"), "test": ("t3",)})
        self.assertNotEqual(a.plan_hash, changed.plan_hash)

    def test_plan_hash_independent_of_dict_insertion_order(self):
        a = make_plan(variant_hashes={"base": "h1", "cand": "h2"})
        b = make_plan(variant_hashes={"cand": "h2", "base": "h1"})
        self.assertEqual(a.plan_hash, b.plan_hash)

    def test_roundtrip_restores_tuples(self):
        plan = make_plan()
        data = json.loads(json_dumps_strict(plan.to_dict(), sort_keys=True))
        self.assertEqual(data["splits"]["dev"], ["t1", "t2"])
        back = PrivateExperimentPlan.from_dict(data)
        self.assertEqual(back, plan)
        self.assertIsInstance(back.splits["dev"], tuple)
        self.assertIsInstance(back.regression_groups["g"], tuple)
        self.assertEqual(back.plan_hash, plan.plan_hash)

    def test_list_inputs_are_normalized_to_tuples(self):
        plan = make_plan(splits={"dev": ["t1", "t2"], "test": ["t3"]})
        self.assertIsInstance(plan.splits["dev"], tuple)

    def test_policy_and_budget_are_copied_at_construction(self):
        policy = {"margin": 0.05, "nested": {"k": [1, 2]}}
        budget = {"max_tokens": 1000}
        plan = make_plan(policy=policy, budget=budget)
        before_hash, before = plan.plan_hash, plan.to_dict()
        policy["margin"] = 0.5
        policy["nested"]["k"].append(3)
        budget["max_tokens"] = 1
        self.assertEqual(plan.plan_hash, before_hash)
        self.assertEqual(plan.to_dict(), before)

    def test_from_dict_copies_policy_and_budget(self):
        data = make_plan().to_dict()
        plan = PrivateExperimentPlan.from_dict(data)
        before_hash, before = plan.plan_hash, plan.to_dict()
        data["policy"]["margin"] = 0.9
        data["budget"]["max_tokens"] = 2
        self.assertEqual(plan.plan_hash, before_hash)
        self.assertEqual(plan.to_dict(), before)

    def test_to_dict_returns_copies(self):
        plan = make_plan()
        before_hash = plan.plan_hash
        out = plan.to_dict()
        out["policy"]["margin"] = 0.9
        out["budget"]["max_tokens"] = 2
        out["splits"]["dev"].append("x")
        self.assertEqual(plan.plan_hash, before_hash)
        self.assertEqual(plan.policy["margin"], 0.05)

    def test_rejects_non_finite_policy(self):
        with self.assertRaises(ValidationError):
            make_plan(policy={"margin": float("inf")}).plan_hash


if __name__ == "__main__":
    unittest.main()
