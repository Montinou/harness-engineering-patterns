import copy
import dataclasses
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from harness_workbench.cli import main
from harness_workbench.core.harness import ALL_COMPONENTS, Harness
from harness_workbench.experiments import ExperimentSpec as ExportedSpec
from harness_workbench.experiments.manifest import (
    SCHEMA_VERSION,
    ALL_COMPONENTS as MANIFEST_ALL_COMPONENTS,
    BudgetPolicy,
    Case,
    EnvironmentConfig,
    ExecutorConfig,
    ExperimentSpec,
    ManifestError,
    Split,
    StatisticsPolicy,
    TaskSpec,
    migrate_manifest,
)
from harness_workbench.experiments.validation import ValidationError

DESCRIPTIONS = {
    "dev-1": "Summarize the quarterly incident report for the operations team",
    "val-1": "Translate the shipping manifest into a sorted inventory table",
    "val-2": "Compute the median latency of the attached request samples",
    "reg-1": "Explain why the nightly backup job exited with a nonzero status",
}
SPLITS = {"dev-1": "development", "val-1": "validation", "val-2": "validation", "reg-1": "regression"}


def controls_block(task_id):
    return {
        "positive": {"id": "pass", "outputs": {"answer": "CORRECT"}},
        "negatives": [{"id": "wrong", "outputs": {"answer": "INCORRECT"}}],
    }


def v2_dict(**overrides):
    tasks = []
    for task_id, description in DESCRIPTIONS.items():
        task = {
            "id": task_id, "description": description, "split": SPLITS[task_id],
            "runs_candidate_code": False,
            "checks": [{"id": "answer", "type": "exact", "target": "answer", "value": "CORRECT"}],
        }
        if SPLITS[task_id] != "development":
            task["controls"] = controls_block(task_id)
        tasks.append(task)
    data = {
        "schema_version": 2, "name": "unit-v2", "mode": "confirmatory", "family": "unit",
        "dataset": {"id": "unit-set", "version": "1"},
        "baseline": "base.json", "variants": [{"id": "cand", "harness": "cand.json"}],
        "tasks": tasks, "repetitions": 5, "seed": 3,
        "environment": {"mode": "trusted_local", "trusted_solver_acknowledged": True},
        "executor": {"type": "command", "argv": ["{python}", "-I", "agent.py"]},
    }
    data.update(overrides)
    return data


class ManifestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "base.json").write_text(Harness(id="h", version="1", system_prompt="base").to_json())
        (self.root / "cand.json").write_text(Harness(id="h", version="2", system_prompt="candidate").to_json())
        (self.root / "fixture").mkdir()
        (self.root / "fixture" / "seed.txt").write_text("seed")
        (self.root / "graders").mkdir()
        (self.root / "graders" / "check.py").write_text("print('ok')\n")

    def write(self, data, name="experiment.json"):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def load_dict(self, data):
        return ExperimentSpec.from_dict(data, self.root)

    def v1(self, **overrides):
        data = {
            "name": "legacy", "baseline": "base.json",
            "variants": [{"id": "cand", "harness": "cand.json"}],
            "tasks": [
                {"id": "t1", "description": "return the answer one", "checks": [
                    {"type": "exact", "target": "answer", "value": "CORRECT"}]},
                {"id": "t2", "description": "return the answer two", "checks": [
                    {"type": "exact", "target": "answer", "value": "CORRECT"}]},
            ],
            "repetitions": 5, "seed": 1, "min_samples": 3,
            "confidence": 0.9, "min_improvement": 0.02, "max_regression": 0.07,
            "max_cost_ratio": 2, "max_latency_ratio": 3, "bootstrap_iterations": 300,
        }
        data.update(overrides)
        return data

    def assert_confirmatory_problem(self, data, *fragments):
        with self.assertRaises(ManifestError) as caught:
            self.load_dict(data)
        message = str(caught.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)
        return message


class LegacyAndValidationTests(ManifestCase):
    def test_v1_manifest_loads_as_legacy_exploratory_with_mapped_policy(self):
        spec = ExperimentSpec.load(self.write(self.v1()))
        self.assertTrue(spec.legacy)
        self.assertEqual(spec.mode, "exploratory")
        self.assertEqual(spec.schema_version, 1)
        self.assertAlmostEqual(spec.statistics.alpha, 0.1)
        self.assertAlmostEqual(spec.statistics.practical_margin, 0.02)
        self.assertAlmostEqual(spec.statistics.regression_margin, 0.07)
        self.assertEqual(spec.statistics.bootstrap_iterations, 300)
        self.assertEqual(spec.budget.max_token_ratio, 2)
        self.assertEqual(spec.budget.max_latency_ratio, 3)
        self.assertEqual(spec.environment.mode, "trusted_local")
        self.assertTrue(any("min_samples" in w for w in spec.warnings))
        self.assertTrue(any("trusted_local" in w for w in spec.warnings))
        for task in spec.tasks:
            self.assertIsNone(task.split)
            self.assertEqual(task.family, task.id)
            self.assertIs(task.runs_candidate_code, False)
        # Compatibility properties keep run_experiment's decisions unchanged.
        self.assertIs(spec.cases, spec.tasks)
        self.assertAlmostEqual(spec.confidence, 0.9)
        self.assertEqual(spec.min_samples, spec.repetitions)
        self.assertEqual(spec.min_improvement, spec.statistics.practical_margin)
        self.assertEqual(spec.max_regression, spec.statistics.regression_margin)
        self.assertEqual(spec.max_cost_ratio, 2)
        self.assertEqual(spec.max_latency_ratio, 3)
        self.assertEqual(spec.executor_config["type"], "model")
        self.assertEqual(spec.variants[1].config_hash, spec.variants[1].harness.execution_config_hash())
        self.assertNotEqual(spec.variants[0].config_hash, spec.variants[1].config_hash)

    def test_v1_defaults_preserve_previous_runtime_values(self):
        data = self.v1()
        for key in ("confidence", "min_improvement", "max_regression", "max_cost_ratio",
                    "max_latency_ratio", "bootstrap_iterations", "min_samples"):
            del data[key]
        spec = ExperimentSpec.load(self.write(data))
        self.assertAlmostEqual(spec.confidence, 0.95)
        self.assertEqual(spec.min_improvement, 0.02)
        self.assertEqual(spec.max_regression, 0.05)
        self.assertEqual(spec.max_cost_ratio, 1.5)
        self.assertEqual(spec.bootstrap_iterations, 2000)

    def test_names_are_re_exported(self):
        from harness_workbench.experiments import core
        self.assertIs(ExportedSpec, ExperimentSpec)
        self.assertIs(core.ExperimentSpec, ExperimentSpec)
        self.assertIs(core.Case, TaskSpec)
        self.assertIs(Case, TaskSpec)
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assertEqual(MANIFEST_ALL_COMPONENTS, ALL_COMPONENTS)

    def test_nan_inf_bool_rejected_in_json_and_api(self):
        sentinel = "@@"
        for label, overrides in (
            ("repetitions-true", {"repetitions": True}),
            ("ratio-infinity", {"max_cost_ratio": sentinel}),
            ("alpha-nan", {"schema_version": 2, "mode": "exploratory", "statistics": {"alpha": sentinel},
                           "environment": {"mode": "trusted_local"}}),
        ):
            template = json.dumps(self.v1(**overrides))
            constants = ("NaN", "Infinity", "-Infinity") if sentinel in template else (None,)
            for constant in constants:
                text = template.replace(f'"{sentinel}"', constant) if constant else template
                path = self.root / "bad.json"
                path.write_text(text, encoding="utf-8")
                with self.subTest(label=label, constant=constant), self.assertRaises(ManifestError):
                    ExperimentSpec.load(path)
        for key, value in (("repetitions", True), ("seed", True), ("min_improvement", True),
                           ("max_cost_ratio", float("inf")), ("confidence", float("nan"))):
            with self.subTest(key=key, value=value), self.assertRaises(ManifestError):
                self.load_dict(self.v1(**{key: value}))
        data = v2_dict(mode="exploratory", statistics={"alpha": True})
        with self.assertRaises(ManifestError):
            self.load_dict(data)
        with self.assertRaises(ValidationError):
            StatisticsPolicy(alpha=float("nan"))
        with self.assertRaises(ValidationError):
            StatisticsPolicy(alpha=True)
        with self.assertRaises(ValidationError):
            BudgetPolicy(max_token_ratio=float("inf"))
        with self.assertRaises(ValidationError):
            StatisticsPolicy(allow_zero_variance=True)  # needs a justification
        self.assertEqual(
            StatisticsPolicy(allow_zero_variance=True, zero_variance_justification="deterministic").policy_hash,
            StatisticsPolicy(allow_zero_variance=True, zero_variance_justification="deterministic").policy_hash,
        )
        self.assertNotEqual(StatisticsPolicy().policy_hash, StatisticsPolicy(alpha=0.01).policy_hash)

    def test_ranges_are_enforced(self):
        bad = [
            {"repetitions": 0}, {"repetitions": 1001}, {"repetitions": 2.0},
            {"confidence": 0.5}, {"confidence": 1}, {"min_improvement": -0.1}, {"max_regression": -1},
            {"max_cost_ratio": 0.9}, {"max_latency_ratio": 0.5}, {"bootstrap_iterations": 199},
            {"bootstrap_iterations": 100001},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(ManifestError):
                self.load_dict(self.v1(**overrides))
        self.assertIsNone(self.load_dict(self.v1(max_cost_ratio=None)).budget.max_token_ratio)
        for statistics in ({"max_attempts": 0}, {"max_attempts": 4}, {"alpha": 0.5}, {"alpha": 0},
                           {"practical_margin": -0.01}, {"bootstrap_iterations": 100001}):
            with self.subTest(statistics=statistics), self.assertRaises(ManifestError):
                self.load_dict(v2_dict(mode="exploratory", statistics=statistics))

    def test_structural_validation(self):
        data = self.v1()
        data["tasks"][1]["id"] = "t1"
        with self.assertRaises(ManifestError):
            self.load_dict(data)
        data = self.v1(variants=[{"id": "a/b", "harness": "cand.json"}])
        with self.assertRaises(ManifestError):
            self.load_dict(data)
        data = self.v1(variants=[{"id": "x", "harness": "cand.json"}, {"id": "x", "harness": "cand.json"}])
        with self.assertRaises(ManifestError):
            self.load_dict(data)
        data = self.v1()
        data["tasks"][0]["fixture"] = "base.json"
        with self.assertRaises(ManifestError):
            self.load_dict(data)
        data = self.v1()
        data["tasks"][0]["checks"][0]["weight"] = 0
        with self.assertRaises(ManifestError):
            self.load_dict(data)
        data = self.v1()
        data["tasks"][0]["checks"] = []
        with self.assertRaises(ManifestError):
            self.load_dict(data)
        with self.assertRaises(ManifestError):
            self.load_dict(self.v1(schema_version=3))

    def test_v2_requires_explicit_environment_mode(self):
        data = v2_dict(mode="exploratory")
        data["environment"] = {"trusted_solver_acknowledged": True}
        with self.assertRaises(ManifestError) as caught:
            self.load_dict(data)
        self.assertIn("environment.mode", str(caught.exception))
        del data["environment"]
        with self.assertRaises(ManifestError):
            self.load_dict(data)
        data["environment"] = {"mode": "docker"}
        with self.assertRaises(ManifestError):
            self.load_dict(data)
        with self.assertRaises(ValidationError):
            EnvironmentConfig("nope")

    def test_v2_exploratory_loads_with_policies_and_executor(self):
        data = v2_dict(mode="exploratory", statistics={"alpha": 0.1, "practical_margin": 0.0},
                       budget={"max_token_ratio": None})
        data["executor"] = {"type": "command", "argv": ["{python}", "agent.py"], "supports": ["system_prompt"]}
        spec = self.load_dict(data)
        self.assertFalse(spec.legacy)
        self.assertEqual(spec.schema_version, 2)
        self.assertEqual(spec.statistics.practical_margin, 0.0)
        self.assertIsNone(spec.budget.max_token_ratio)
        self.assertEqual(spec.executor.supports, frozenset({"system_prompt"}))
        self.assertEqual(spec.executor_config["type"], "command")
        self.assertEqual(spec.executor_config["argv"], ["{python}", "agent.py"])
        self.assertEqual([t.id for t in spec.tasks_in(Split.VALIDATION)], ["val-1", "val-2"])
        self.assertEqual([t.id for t in spec.tasks_in("regression")], ["reg-1"])
        self.assertEqual(ExecutorConfig().supports, frozenset(ALL_COMPONENTS))
        self.assertEqual(spec.dataset_id, "unit-set")

    def test_bare_python_and_missing_grader_file_are_rejected(self):
        data = self.v1()
        data["tasks"][0]["checks"] = [{"type": "command", "argv": ["python", "check.py"]}]
        with self.assertRaises(ManifestError) as caught:
            self.load_dict(data)
        self.assertIn("use {python}", str(caught.exception))
        data["tasks"][0]["checks"] = [{"type": "command", "argv": ["python3", "-c", "pass"]}]
        with self.assertRaises(ManifestError):
            self.load_dict(data)
        data["tasks"][0]["checks"] = [{"type": "command", "argv": ["{python}", "check.py"]}]
        with self.assertRaises(ManifestError) as caught:
            self.load_dict(data)
        self.assertIn("grader file missing: check.py", str(caught.exception))
        data["tasks"][0]["grader_files"] = "graders"
        self.load_dict(data)  # check.py exists under graders/
        data["tasks"][0]["checks"] = [{"type": "command", "argv": ["{python}", "other.py"]}]
        with self.assertRaises(ManifestError) as caught:
            self.load_dict(data)
        self.assertIn("grader file missing: other.py", str(caught.exception))
        # Arguments that are not file names are not demanded as grader files.
        data["tasks"][0]["checks"] = [{"type": "command", "argv": [
            "{python}", "-c", "print(1.5)", "--flag=a.b", "{out}/x.txt", "0.5"]}]
        self.load_dict(data)
        data = self.v1(executor={"type": "command", "argv": ["python", "-m", "demo"]})
        with self.assertRaises(ManifestError) as caught:
            self.load_dict(data)
        self.assertIn("use {python}", str(caught.exception))

    def test_oracle_check_is_validated(self):
        oracle = {"type": "oracle", "argv": ["{python}", "check.py"], "observed": "result.json",
                  "expected": {"a": 1}, "compare": "json_equal"}
        base = self.v1()
        base["tasks"][0]["grader_files"] = "graders"
        for mutation in (
            {"observed": "../escape.json"}, {"observed": "/abs/path"}, {"observed": ""},
            {"argv": []}, {"argv": ["{python}", ""]}, {"compare": "fuzzy"},
        ):
            data = copy.deepcopy(base)
            data["tasks"][0]["checks"] = [{**oracle, **mutation}]
            with self.subTest(mutation=mutation), self.assertRaises(ManifestError):
                self.load_dict(data)
        data = copy.deepcopy(base)
        without_expected = {k: v for k, v in oracle.items() if k != "expected"}
        data["tasks"][0]["checks"] = [without_expected]
        with self.assertRaises(ManifestError):
            self.load_dict(data)
        data["tasks"][0]["checks"] = [oracle]
        spec = self.load_dict(data)
        self.assertEqual(spec.tasks[0].checks[0]["type"], "oracle")

    def test_content_fingerprint_ignores_ids_and_families(self):
        data = v2_dict(mode="exploratory")
        data["tasks"] = [copy.deepcopy(data["tasks"][1]), copy.deepcopy(data["tasks"][1])]
        data["tasks"][1]["id"] = "renamed"
        data["tasks"][1]["family"] = "other-family"
        data["tasks"][0]["split"] = "validation"
        data["tasks"][1]["split"] = "regression"
        a, b = self.load_dict(data).tasks
        self.assertEqual(a.content_fingerprint, b.content_fingerprint)
        self.assertNotEqual((a.id, a.family), (b.id, b.family))
        data["tasks"][1]["description"] += " with an extra clause"
        c = self.load_dict(data).tasks[1]
        self.assertNotEqual(a.content_fingerprint, c.content_fingerprint)
        self.assertEqual(len(a.bundle_hash), 64)

    def test_fixture_and_grader_files_change_the_fingerprint(self):
        data = self.v1()
        data["tasks"][0]["fixture"] = "fixture"
        before = self.load_dict(data).tasks[0]
        (self.root / "fixture" / "seed.txt").write_text("changed")
        after = self.load_dict(data).tasks[0]
        self.assertNotEqual(before.fixture_hash, after.fixture_hash)
        self.assertNotEqual(before.content_fingerprint, after.content_fingerprint)
        data["tasks"][0]["grader_files"] = "graders"
        first = self.load_dict(data).tasks[0].bundle_hash
        (self.root / "graders" / "check.py").write_text("print('other')\n")
        self.assertNotEqual(first, self.load_dict(data).tasks[0].bundle_hash)

    def test_critical_checks_and_runs_candidate_code(self):
        data = v2_dict(mode="exploratory")
        data["tasks"][0]["checks"][0]["critical"] = True
        data["tasks"][0]["runs_candidate_code"] = None
        task = self.load_dict(data).tasks[0]
        self.assertEqual(task.critical_check_ids, frozenset({"answer"}))
        self.assertIsNone(task.runs_candidate_code)
        data["tasks"][0]["runs_candidate_code"] = "yes"
        with self.assertRaises(ManifestError):
            self.load_dict(data)


    def test_duplicate_check_ids_in_one_task_are_rejected(self):   # P5 review fix: a duplicate id hides a critical check
        data = v2_dict(mode="exploratory")
        data["tasks"][0]["checks"].append(
            {"id": "answer", "type": "exact", "target": "answer", "value": "CORRECT", "critical": True})
        with self.assertRaises(ManifestError) as caught:
            self.load_dict(data)
        self.assertIn("duplicate check id", str(caught.exception))
        self.assertIn("answer", str(caught.exception))
        # an explicit id colliding with the implicit one of an id-less check is the same collision
        data = v2_dict(mode="exploratory")
        data["tasks"][0]["checks"] = [
            {"type": "exact", "target": "answer", "value": "CORRECT"},
            {"id": "check-1", "type": "exact", "target": "answer", "value": "CORRECT", "critical": True}]
        with self.assertRaises(ManifestError):
            self.load_dict(data)
        # the same id in two different tasks is fine
        data = v2_dict(mode="exploratory")
        self.assertEqual(data["tasks"][0]["checks"][0]["id"], data["tasks"][1]["checks"][0]["id"])
        self.load_dict(data)

class ConfirmatoryTests(ManifestCase):
    def test_valid_confirmatory_manifest_loads(self):
        spec = ExperimentSpec.load(self.write(v2_dict()))
        self.assertEqual(spec.mode, "confirmatory")
        self.assertEqual(spec.validate_confirmatory(), [])
        self.assertEqual(spec.tasks[1].controls.positive.id, "pass")
        self.assertEqual(len(spec.tasks[1].controls.negatives), 1)
        self.assertEqual(len(spec.source_hash), 64)

    def test_confirmatory_alpha_and_practical_margin_must_match_profile(self):
        spec = ExperimentSpec.load(self.write(v2_dict()))
        for key, value in (("alpha", 0.45), ("practical_margin", 0.0)):
            with self.assertRaisesRegex(ManifestError, rf"{key} .* differs from the binary_v1 profile value"):
                dataclasses.replace(
                    spec, statistics=dataclasses.replace(spec.statistics, **{key: value}))

    def test_exploratory_spec_reports_mode_problem(self):
        spec = self.load_dict(v2_dict(mode="exploratory"))
        problems = spec.validate_confirmatory()
        self.assertTrue(any("confirmatory" in p for p in problems))

    def test_confirmatory_requires_splits_controls_mode_and_isolated_python(self):
        data = v2_dict()
        data["tasks"] = [t for t in data["tasks"] if t["split"] != "regression"]
        del data["tasks"][1]["controls"]
        data["tasks"][0]["runs_candidate_code"] = None
        data["tasks"][2]["runs_candidate_code"] = True
        data["environment"] = {"mode": "trusted_local"}
        data["tasks"][0]["grader_files"] = "graders"
        data["tasks"][0]["checks"].append({"id": "script", "type": "command", "argv": ["{python}", "check.py"]})
        message = self.assert_confirmatory_problem(
            data, "regression", "-I", "runs_candidate_code", "environment.mode",
            "trusted_local lets the solver read host files")
        self.assertIn("task val-1", message)
        data = v2_dict()
        data["environment"] = {"mode": "trusted_local", "trusted_solver_acknowledged": True}
        data["tasks"] = [t for t in data["tasks"] if t["split"] != "development"]
        data["variants"].append({"id": "second", "harness": "cand.json"})
        self.assert_confirmatory_problem(data, "development")

    def test_confirmatory_requires_mode_and_two_variants(self):
        data = v2_dict(variants=[])
        self.assert_confirmatory_problem(data, "candidate")

    def test_confirmatory_rejects_duplicate_content_across_splits_and_unreviewed_near_duplicates(self):
        data = v2_dict()
        clone = copy.deepcopy(data["tasks"][1])
        clone["id"] = "val-clone"
        clone["split"] = "regression"
        clone["family"] = "elsewhere"
        data["tasks"].append(clone)
        self.assert_confirmatory_problem(data, "val-1", "val-clone", "same content")
        data = v2_dict()
        near = copy.deepcopy(data["tasks"][1])
        near["id"] = "val-near"
        near["family"] = "elsewhere"
        near["description"] = DESCRIPTIONS["val-1"] + " today"
        data["tasks"].append(near)
        self.assert_confirmatory_problem(data, "near-duplicate", "val-1", "val-near")
        data["duplicate_review"] = ["val-1:val-near"]
        self.assertEqual(self.load_dict(data).validate_confirmatory(), [])
        data["duplicate_review"] = ["val-near:val-1"]
        self.load_dict(data)
        # The same family is not a cross-family near duplicate.
        data["duplicate_review"] = []
        data["tasks"][-1]["family"] = data["tasks"][1].get("family", "val-1")
        self.load_dict(data)

    def test_confirmatory_rejects_a_family_spanning_splits(self):
        # Same family in development and validation, description differing by one word.
        data = v2_dict()
        data["tasks"][0]["family"] = "famA"
        data["tasks"][1]["family"] = "famA"
        data["tasks"][0]["description"] = DESCRIPTIONS["val-1"] + " now"
        message = self.assert_confirmatory_problem(data, "family famA", "more than one split", "dev-1", "val-1")
        self.assertIn("development", message)
        self.assertIn("validation", message)
        # Same prompt in two splits with a different expected value: different content fingerprint,
        # still the same family, still rejected.
        data = v2_dict()
        data["tasks"][0]["family"] = "famA"
        data["tasks"][1]["family"] = "famA"
        data["tasks"][0]["description"] = DESCRIPTIONS["val-1"]
        data["tasks"][0]["checks"][0]["value"] = "OTHER"
        self.assert_confirmatory_problem(data, "family famA", "more than one split")
        # A family confined to one split is fine, and so are distinct families across splits.
        data = v2_dict()
        data["tasks"][1]["family"] = "famV"
        data["tasks"][2]["family"] = "famV"
        self.assertEqual(self.load_dict(data).validate_confirmatory(), [])

    def test_near_duplicates_in_different_splits_are_not_skipped_by_a_shared_family(self):
        spec = self.load_dict(v2_dict(mode="exploratory"))
        tasks = list(spec.tasks)
        tasks[0] = dataclasses.replace(
            tasks[0], family="famA", task=dataclasses.replace(
                tasks[0].task, description=DESCRIPTIONS["val-1"] + " now"))
        tasks[1] = dataclasses.replace(tasks[1], family="famA")
        problems = dataclasses.replace(spec, mode="exploratory", tasks=tuple(tasks)).validate_confirmatory()
        self.assertTrue(any("near-duplicate" in p and "dev-1:val-1" in p for p in problems), problems)

    def test_confirmatory_rejects_repetitions_below_profile(self):
        self.assert_confirmatory_problem(v2_dict(repetitions=2), "repetitions", "binary_v1")
        for statistics in ({"min_independent_units": 10}, {"min_regression_units": 10},
                           {"bootstrap_iterations": 5000}):
            with self.subTest(statistics=statistics):
                self.assert_confirmatory_problem(v2_dict(statistics=statistics), next(iter(statistics)))
        self.load_dict(v2_dict(repetitions=5))
        self.load_dict(v2_dict(repetitions=8))

    def test_confirmatory_rejects_candidate_identical_to_baseline(self):
        (self.root / "same.json").write_text(
            Harness(id="renamed", version="9", system_prompt="base", notes="different notes").to_json())
        data = v2_dict(variants=[{"id": "cand", "harness": "cand.json"}, {"id": "twin", "harness": "same.json"}])
        message = self.assert_confirmatory_problem(data, "twin", "baseline")
        self.assertNotIn("candidate cand ", message)

    def test_runs_candidate_code_requires_sandboxed_and_oracle(self):
        data = v2_dict()
        for task in data["tasks"]:
            task["runs_candidate_code"] = True
        self.assert_confirmatory_problem(data, "sandboxed", "oracle")
        data["environment"] = {"mode": "sandboxed"}
        self.assert_confirmatory_problem(data, "oracle")
        for task in data["tasks"]:
            task["grader_files"] = "graders"
            task["checks"].append({"id": "o", "type": "oracle", "argv": ["{python}", "-I", "check.py"],
                                   "observed": "out.json", "expected": 1})
        self.assertEqual(self.load_dict(data).validate_confirmatory(), [])

    def test_trusted_local_requires_acknowledgment(self):
        data = v2_dict(environment={"mode": "trusted_local"})
        self.assert_confirmatory_problem(
            data, "trusted_local lets the solver read host files including grader files; "
                  "acknowledge or use sandboxed")
        self.load_dict(v2_dict(environment={"mode": "sandboxed"}))

    def test_finalists_must_be_one(self):
        self.assert_confirmatory_problem(v2_dict(finalists=2), "finalists")
        self.load_dict(v2_dict(finalists=1))
        self.assertEqual(self.load_dict(v2_dict(mode="exploratory", finalists=3)).finalists, 3)

    def test_all_problems_are_listed_together(self):
        data = v2_dict(repetitions=1, finalists=0)
        data["environment"] = {"mode": "trusted_local"}
        message = self.assert_confirmatory_problem(data, "repetitions", "finalists", "trusted_local")
        self.assertGreaterEqual(message.count(";"), 2)

    def test_tasks_without_split_are_reported(self):
        data = v2_dict()
        del data["tasks"][0]["split"]
        self.assert_confirmatory_problem(data, "dev-1", "split")


class ApiValidationTests(ManifestCase):
    def test_directly_built_spec_is_validated_like_the_json_path(self):
        spec = self.load_dict(v2_dict(mode="exploratory"))
        for key, value in (
            ("repetitions", True), ("repetitions", 0), ("repetitions", 1001), ("repetitions", 5.0),
            ("seed", float("nan")), ("seed", True), ("seed", -1), ("seed", "3"),
            ("mode", "other"), ("schema_version", 3), ("finalists", True),
            ("statistics", {"alpha": 0.05}), ("budget", None), ("environment", "sandboxed"),
            ("executor", {}),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ManifestError):
                dataclasses.replace(spec, **{key: value})
        with self.assertRaises(ManifestError):
            dataclasses.replace(spec, repetitions=True, seed=float("nan"))
        self.assertEqual(dataclasses.replace(spec, repetitions=7, seed=0).repetitions, 7)

    def test_replace_to_confirmatory_runs_the_confirmatory_checks(self):
        spec = self.load_dict(v2_dict(mode="exploratory"))
        with self.assertRaises(ManifestError) as caught:
            dataclasses.replace(spec, mode="confirmatory", repetitions=2)
        self.assertIn("confirmatory manifest is not valid", str(caught.exception))
        self.assertIn("repetitions", str(caught.exception))
        valid = dataclasses.replace(spec, mode="confirmatory")
        self.assertEqual(valid.validate_confirmatory(), [])
        # Review Focus 2: a candidate whose execution configuration equals the baseline's.
        twin = dataclasses.replace(valid.variants[1], harness=valid.variants[0].harness,
                                   config_hash=valid.variants[0].config_hash)
        with self.assertRaises(ManifestError) as caught:
            dataclasses.replace(valid, variants=(valid.variants[0], twin))
        self.assertIn("same execution configuration as the baseline", str(caught.exception))


class MigrationTests(ManifestCase):
    def test_migrate_manifest_maps_keys_and_marks_missing_splits(self):
        v1 = self.v1()
        out = migrate_manifest(v1, splits={"t1": "validation"})
        self.assertEqual(out["schema_version"], 2)
        self.assertEqual(out["mode"], "exploratory")
        self.assertEqual(out["tasks"][0]["split"], "validation")
        self.assertIsNone(out["tasks"][1]["split"])
        self.assertAlmostEqual(out["statistics"]["alpha"], 0.1)
        self.assertEqual(out["statistics"]["practical_margin"], 0.02)
        self.assertEqual(out["statistics"]["regression_margin"], 0.07)
        self.assertEqual(out["budget"]["max_token_ratio"], 2)
        self.assertEqual(out["budget"]["max_latency_ratio"], 3)
        self.assertNotIn("min_samples", out)
        self.assertEqual(out["environment"]["mode"], "trusted_local")
        notes = " ".join(out["_migration_notes"])
        for fragment in ("min_samples", "t2", "controls", "runs_candidate_code", "trusted_local"):
            self.assertIn(fragment, notes)
        self.assertEqual(v1["tasks"][0].get("split"), None)  # input is not mutated
        reloaded = self.load_dict(out)
        self.assertFalse(reloaded.legacy)
        self.assertEqual(reloaded.mode, "exploratory")
        self.assertEqual(reloaded.tasks[0].split, Split.VALIDATION)
        self.assertAlmostEqual(reloaded.statistics.alpha, 0.1)
        self.assertEqual(reloaded.repetitions, 5)

    def test_migration_replaces_bare_python_with_the_placeholder(self):
        v1 = self.v1(executor={"type": "command", "argv": ["python", "-m", "demo"]})
        v1["tasks"][0]["checks"] = [{"type": "command", "argv": ["python3", "-c", "pass"]}]
        out = migrate_manifest(v1)
        self.assertEqual(out["executor"]["argv"], ["{python}", "-m", "demo"])
        self.assertEqual(out["tasks"][0]["checks"][0]["argv"], ["{python}", "-c", "pass"])
        self.assertEqual(v1["executor"]["argv"][0], "python")  # input is not mutated
        self.assertTrue(any("{python}" in note for note in out["_migration_notes"]))
        self.load_dict(out)

    def test_migration_rejects_bad_input(self):
        with self.assertRaises(ManifestError):
            migrate_manifest(self.v1(), splits={"missing": "validation"})
        with self.assertRaises(ManifestError):
            migrate_manifest(self.v1(), splits={"t1": "holdout"})
        with self.assertRaises(ManifestError):
            migrate_manifest(v2_dict())
        with self.assertRaises(ManifestError):
            migrate_manifest(self.v1(confidence=float("nan")))

    def test_cli_migrate_manifest_dry_run_writes_nothing(self):
        spec_path = self.write(self.v1())
        out_path = self.root / "out" / "v2.json"
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["migrate-manifest", "--spec", str(spec_path), "--out", str(out_path), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(out_path.exists())
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed["schema_version"], 2)
        self.assertIn("warning", stderr.getvalue())

    def test_cli_migrate_manifest_writes_a_loadable_file(self):
        spec_path = self.write(self.v1())
        splits = self.root / "splits.json"
        splits.write_text(json.dumps({"t1": "development", "t2": "regression"}))
        out_path = self.root / "nested" / "v2.json"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["migrate-manifest", "--spec", str(spec_path), "--out", str(out_path),
                         "--splits", str(splits)])
        self.assertEqual(code, 0)
        migrated = json.loads(out_path.read_text())
        self.assertEqual(migrated["mode"], "exploratory")
        spec = ExperimentSpec.load(out_path)  # relative paths were rebased to the new directory
        self.assertEqual([t.split for t in spec.tasks], [Split.DEVELOPMENT, Split.REGRESSION])

    def test_cli_migrate_manifest_refuses_to_overwrite_input_and_bad_input(self):
        spec_path = self.write(self.v1())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["migrate-manifest", "--spec", str(spec_path), "--out", str(spec_path)]), 2)
            self.assertEqual(main(["migrate-manifest", "--spec", str(spec_path)]), 2)
            self.assertEqual(main(["migrate-manifest", "--spec", str(self.write(v2_dict(), "v2.json")),
                                   "--out", str(self.root / "x.json")]), 2)
        self.assertEqual(json.loads(spec_path.read_text())["name"], "legacy")


if __name__ == "__main__":
    unittest.main()
