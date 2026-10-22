"""Experiment-layer regression tests. No provider calls or credentials required."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_workbench.core.executor import BehavioralReport, Task
from harness_workbench.core.harness import Harness
from harness_workbench.experiments import Case, ExperimentRunner, ExperimentSpec, ExperimentStore, Policy, Variant, load_spec
from harness_workbench.experiments.cli import main
from harness_workbench.experiments.evaluation import evaluate
from harness_workbench.experiments.execution import Context, build_executor, run_command
from harness_workbench.experiments.models import canonical, digest, relative_path, snapshot
from harness_workbench.experiments.proposals import config_dict, propose_development
from harness_workbench.experiments.reporting import write_reports
from harness_workbench.experiments.statistics import paired_statistics, pareto_frontier


def specification(validation_tasks=6, repetitions=2):
    base = Harness("agent", "1", "Solve the task")
    good = base.copy()
    good.guardrails = ["Verify the result"]
    cases = []
    for split, count in (("development", 2), ("validation", validation_tasks), ("regression", 2)):
        for index in range(count):
            cases.append(Case(Task(f"{split}-{index}", f"Resolve case {split} number {index}"), split,
                              [{"kind": "exact", "expected": "OK"}], {"input.txt": str(index)}))
    return ExperimentSpec("test-experiment", base, [Variant("verified", good)], cases,
                          Policy(repetitions=repetitions, bootstrap_samples=200, max_workers=2), {"kind": "custom"})


def response(harness, task, context, *, output=None, score=0.0):
    if output is None:
        output = "OK" if harness.guardrails or task.id.startswith("regression") else "WRONG"
    return BehavioralReport(harness.id, harness.version, task.id, score,
                            cost_tokens=10, output=output, metadata={"usage_known": True})


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ExperimentStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def run_spec(self, spec=None, executor=response):
        return ExperimentRunner(self.store, executor=executor).run(spec or specification())

    def test_improvement_is_validated_and_promoted_immutably(self):
        result = self.run_spec()
        self.assertTrue(result["promote"], result["reasons"])
        self.assertEqual(result["total_runs"], 40)
        self.assertEqual(result["total_tokens"], 400)
        self.assertEqual(result["statistics"]["tasks"], 6)
        self.assertLess(result["statistics"]["sign_p"], 0.05)
        info = self.store.inspect(result["experiment_id"])
        adopted = self.store.object(info["promoted_object"])
        self.assertEqual(adopted["lineage"]["parent_version"], "1")
        self.assertEqual(adopted["version"], result["experiment_id"])
        self.assertTrue(adopted["guardrails"])

    def test_diagnostic_score_cannot_promote_wrong_answers(self):
        result = self.run_spec(executor=lambda h, t, c: response(h, t, c, output="WRONG", score=1 if h.guardrails else 0))
        self.assertFalse(result["promote"])
        self.assertEqual(result["total_runs"], 8)
        self.assertIn("no_development_improvement", result["reasons"])

    def test_repetitions_do_not_inflate_independent_task_count(self):
        result = self.run_spec(specification(validation_tasks=1, repetitions=20))
        self.assertFalse(result["promote"])
        self.assertEqual(result["statistics"]["tasks"], 1)
        self.assertIn("insufficient_independent_validation_tasks", result["reasons"])

    def test_regression_blocks_an_otherwise_good_candidate(self):
        def execute(h, t, c):
            return response(h, t, c, output="BAD" if h.guardrails and t.id == "regression-0" else None)
        result = self.run_spec(executor=execute)
        self.assertFalse(result["promote"])
        self.assertIn("regression_regression:regression-0", result["reasons"])

    def test_no_runner_up_after_failed_validation(self):
        spec = specification()
        spec.variants[0].label = "a"
        spec.variants.append(Variant("b", spec.variants[0].harness.copy()))
        spec.variants[1].harness.notes = "runner up"
        def execute(h, t, c):
            fail = t.id.startswith("validation") and h.guardrails and not h.notes
            return response(h, t, c, output="WRONG" if fail else None)
        result = self.run_spec(spec, execute)
        self.assertEqual(result["nominee"], "a")
        self.assertFalse(result["promote"])
        rows = self.store.records(result["experiment_id"])
        self.assertFalse(any(r["label"] == "b" and r["split"] != "development" for r in rows))
        kinds = [e["kind"] for e in self.store.inspect(result["experiment_id"])["events"]]
        self.assertEqual(kinds[:3], ["phase_started", "phase_finished", "selection_frozen"])

    def test_error_trials_are_retained_and_block_promotion(self):
        def execute(h, t, c):
            if h.guardrails and t.id == "validation-0" and c.repetition == 0:
                raise RuntimeError("untrusted error text")
            return response(h, t, c)
        result = self.run_spec(executor=execute)
        self.assertFalse(result["promote"])
        errors = [r for r in self.store.records(result["experiment_id"]) if r["status"] == "execution_error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["evaluation"]["score"], 0)
        self.assertNotIn("untrusted error text", canonical(errors))
        self.assertIn("validation_execution_or_evaluation_error", result["reasons"])

    def test_fresh_workspaces_and_matched_seeds(self):
        workspaces = []
        def execute(h, t, c):
            self.assertFalse((c.workspace / "mutation.txt").exists())
            self.assertEqual((c.workspace / "input.txt").read_text(), t.id.rsplit("-", 1)[1])
            (c.workspace / "mutation.txt").write_text("local only")
            workspaces.append(str(c.workspace))
            return response(h, t, c)
        result = self.run_spec(executor=execute)
        self.assertTrue(result["promote"])
        self.assertEqual(len(set(workspaces)), len(workspaces))
        self.assertTrue(all(not Path(p).exists() for p in workspaces))
        rows = self.store.records(result["experiment_id"])
        grouped = {}
        for row in rows:
            grouped.setdefault((row["task_id"], row["repetition"]), []).append(row["seed"])
        self.assertTrue(all(len(set(values)) == 1 for values in grouped.values()))

    def test_mutating_harness_is_an_execution_error(self):
        def execute(h, t, c):
            h.guardrails.append("contamination")
            return response(h, t, c)
        result = self.run_spec(executor=execute)
        self.assertFalse(result["promote"])
        self.assertTrue(all(row["status"] == "execution_error" for row in self.store.records(result["experiment_id"])))

    def test_task_and_harness_are_not_mutated_in_caller(self):
        spec = specification()
        before = canonical(spec.to_dict())
        self.run_spec(spec)
        self.assertEqual(canonical(spec.to_dict()), before)

    def test_token_budget_interrupts_and_never_promotes(self):
        spec = specification()
        spec.policy.max_total_tokens = 15
        result = self.run_spec(spec)
        self.assertFalse(result["promote"])
        self.assertLessEqual(result["total_runs"], spec.policy.max_workers)
        self.assertIn("budget_exhausted_or_usage_unknown", result["reasons"])

    def test_cost_ratio_is_a_real_gate(self):
        spec = specification()
        spec.policy.max_tokens_ratio = 1.1
        def execute(h, t, c):
            report = response(h, t, c)
            report.cost_tokens = 30 if h.guardrails else 10
            return report
        result = self.run_spec(spec, execute)
        self.assertFalse(result["promote"])
        self.assertIn("validation_token_budget_regression", result["reasons"])

    def test_unknown_usage_cannot_satisfy_cost_gate(self):
        spec = specification()
        spec.policy.max_tokens_ratio = 2
        def execute(h, t, c):
            report = response(h, t, c)
            report.metadata["usage_known"] = False
            return report
        result = self.run_spec(spec, execute)
        self.assertIn("validation_token_usage_unknown", result["reasons"])

    def test_model_drift_blocks_promotion(self):
        def execute(h, t, c):
            report = response(h, t, c)
            report.metadata["solver_returned"] = "changed" if c.repetition else "original"
            return report
        result = self.run_spec(executor=execute)
        self.assertIn("solver_model_changed_between_trials", result["reasons"])
        self.assertFalse(result["promote"])

    def test_synthetic_results_are_not_promoted(self):
        def execute(h, t, c):
            report = response(h, t, c)
            report.metadata["synthetic"] = True
            return report
        result = self.run_spec(executor=execute)
        self.assertFalse(result["promote"])
        self.assertIn("synthetic_backend_not_production_evidence", result["reasons"])

    def test_repeated_experiments_have_distinct_ids_and_events(self):
        a, b = self.run_spec(), self.run_spec()
        self.assertNotEqual(a["experiment_id"], b["experiment_id"])
        ar = {r["run_id"] for r in self.store.records(a["experiment_id"])}
        br = {r["run_id"] for r in self.store.records(b["experiment_id"])}
        self.assertFalse(ar & br)
        self.assertEqual(len(self.store.list()), 2)

    def test_run_and_decision_records_cannot_be_overwritten(self):
        result = self.run_spec()
        row = self.store.records(result["experiment_id"])[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record(result["experiment_id"], row)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish(result["experiment_id"], result)

    def test_manifest_tampering_is_detected(self):
        result = self.run_spec()
        with self.store.connection() as db:
            db.execute("UPDATE experiments SET manifest='{}' WHERE id=?", (result["experiment_id"],))
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.store.inspect(result["experiment_id"])

    def test_reports_escape_html_and_preserve_full_output(self):
        result = self.run_spec()
        report = write_reports(self.store, result["experiment_id"], self.root / "report")
        self.assertTrue(all(Path(path).exists() for path in report.values()))
        self.assertEqual(len(json.loads(Path(report["json"]).read_text())["runs"]), 40)
        self.assertIn("Content-Security-Policy", Path(report["html"]).read_text())
        self.assertIn("Held-out paired statistics", Path(report["md"]).read_text())

    def test_long_outputs_are_not_truncated(self):
        text = "<script>alert(1)</script>" + "a" * 8000
        def execute(h, t, c):
            return response(h, t, c, output=text)
        result = self.run_spec(executor=execute)
        paths = write_reports(self.store, result["experiment_id"], self.root / "reports")
        self.assertEqual(self.store.records(result["experiment_id"])[0]["report"]["output"], text)
        web = Path(paths["html"]).read_text()
        self.assertNotIn("<script>alert", web)
        self.assertIn("&lt;script&gt;", web)

    def test_grader_opt_in_is_checked_before_any_run(self):
        spec = specification()
        spec.cases[0].checks = [{"kind": "command", "command": ["{python}", "-c", "pass"]}]
        with self.assertRaisesRegex(ValueError, "allow-commands"):
            self.run_spec(spec)
        self.assertEqual(self.store.list(), [])

    def test_python_api_custom_executor_receives_no_expected_answer(self):
        def execute(h, t, c):
            self.assertEqual(set(t.to_dict()), {"id", "description", "domain", "constraints"})
            self.assertNotIn("expected", canonical(t.to_dict()))
            self.assertEqual(set(snapshot(c.workspace)), {"input.txt"})
            return response(h, t, c)
        self.assertTrue(self.run_spec(executor=execute)["promote"])


class ModelAndEvaluatorTests(unittest.TestCase):
    def test_invalid_paths_are_rejected(self):
        for path in ("../secret", "/absolute", "a/../../b", "C:\\tmp", ".git/config", ".runtime/key"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                relative_path(path)

    def test_nonfinite_policy_values_are_rejected(self):
        for key in ("margin", "regression_tolerance", "alpha", "max_tokens_ratio", "max_latency_ratio"):
            spec = specification()
            setattr(spec.policy, key, float("nan"))
            with self.subTest(key=key), self.assertRaises(ValueError):
                spec.validate()

    def test_invalid_repetitions_are_rejected(self):
        for value in (0, -1, 1.5, True):
            spec = specification()
            spec.policy.repetitions = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                spec.validate()

    def test_duplicate_tasks_and_missing_splits_are_rejected(self):
        spec = specification()
        spec.cases[1] = copy.deepcopy(spec.cases[0])
        with self.assertRaises(ValueError):
            spec.validate()
        spec = specification()
        spec.cases = [c for c in spec.cases if c.split != "regression"]
        with self.assertRaises(ValueError):
            spec.validate()

    def test_duplicate_content_under_a_new_id_is_rejected(self):
        spec = specification()
        case = copy.deepcopy(spec.cases[0])
        case.task.id = "different-id"
        case.split = "validation"
        spec.cases.append(case)
        with self.assertRaisesRegex(ValueError, "duplicate case"):
            spec.validate()

    def test_reference_comparison_is_not_python_bool_equality(self):
        report = evaluate("true", {}, [{"kind": "json_equals", "expected": 1}])
        self.assertFalse(report.success)

    def test_objective_checks_use_only_observed_artifacts(self):
        report = evaluate("I tested everything", {"result.txt": "bad"}, [{"kind": "file_equals", "target": "result.txt", "expected": "good"}])
        self.assertEqual(report.score, 0)
        self.assertFalse(report.error)

    def test_weighted_checks_and_success_are_separate(self):
        report = evaluate("ok", {}, [{"kind": "exact", "expected": "ok", "weight": 3},
                                     {"kind": "contains", "expected": "missing", "weight": 1}])
        self.assertEqual(report.score, .75)
        self.assertFalse(report.success)

    def test_hidden_command_tests_are_injected_after_solver(self):
        artifacts = {"answer.txt": "42", "check.py": "raise RuntimeError('solver fake test')"}
        checks = [{"kind": "command", "command": ["{python}", "check.py"],
                   "files": {"check.py": "from pathlib import Path\nassert Path('answer.txt').read_text() == '42'"}}]
        report = evaluate("", artifacts, checks, allow_commands=True)
        self.assertTrue(report.success)
        self.assertIn("solver fake", artifacts["check.py"])

    def test_grader_failure_differs_from_grader_error(self):
        failed = evaluate("", {}, [{"kind": "command", "command": ["{python}", "-c", "raise SystemExit(1)"]}], allow_commands=True)
        error = evaluate("", {}, [{"kind": "command", "command": ["/no/such/binary"]}], allow_commands=True)
        self.assertFalse(failed.success)
        self.assertFalse(failed.error)
        self.assertTrue(error.error)

    def test_unknown_config_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            data = config_dict(specification())
            data["silent_typo"] = True
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "unknown"):
                load_spec(path)

    def test_config_roundtrip_freezes_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            data = config_dict(specification())
            path.write_text(json.dumps(data))
            loaded = load_spec(path)
            self.assertEqual(loaded.cases[0].fixtures, {"input.txt": "0"})
            self.assertEqual(loaded.baseline.to_dict(), data["baseline"])

    def test_seeds_and_bootstrap_are_reproducible(self):
        kwargs = dict(margin=.05, alpha=.05, samples=100, seed=1)
        a = paired_statistics([.2, .3, .4, .1, .2, .3], **kwargs)
        self.assertEqual(a, paired_statistics([.2, .3, .4, .1, .2, .3], **kwargs))
        self.assertEqual(a["sign_p"], 1/64)
        self.assertEqual(paired_statistics([.05] * 6, **kwargs)["sign_p"], 1)

    def test_pareto_frontier_does_not_reward_unknown_costs(self):
        rows = {"a": {"score": .9, "mean_tokens": 10, "mean_latency_ms": 2, "usage_known": True, "errors": 0},
                "b": {"score": .8, "mean_tokens": 11, "mean_latency_ms": 3, "usage_known": True, "errors": 0}}
        self.assertEqual(pareto_frontier(rows), ["a"])
        rows["a"]["usage_known"] = False
        self.assertEqual(pareto_frontier(rows), ["b"])

    def test_proposer_sees_only_development_tasks(self):
        seen = []
        from harness_workbench.core.proposer import RuleBasedProposer
        class Spy:
            def propose(self, task, harness, report, *, k):
                seen.append(task.id)
                return RuleBasedProposer().propose(task, harness, report, k=k)
        spec = propose_development(specification(), Spy())
        self.assertTrue(spec.variants)
        self.assertTrue(all(s.startswith("development") for s in seen))
        self.assertTrue(any(v.label.startswith("composition-") for v in spec.variants))


class CommandAndCliTests(unittest.TestCase):
    def test_command_adapter_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError, "allow-commands"):
            build_executor({"kind": "command", "command": ["python"]})

    def test_command_protocol_and_environment_allowlist(self):
        script = "import json,sys,os; r=json.load(sys.stdin); print(json.dumps({'output':str('CHECK_SECRET' in os.environ), 'cost_tokens':3}))"
        backend = {"kind": "command", "command": ["{python}", "-c", script]}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CHECK_SECRET": "sensitive"}):
            context = Context(Path(tmp), "run", 3, 0)
            executor = build_executor(backend, allow_commands=True)
            report = executor(Harness("a", "1", "solve"), Task("x", "question"), context)
            self.assertEqual(report.output, "False")
            self.assertEqual(report.cost_tokens, 3)
            backend["env_allow"] = ["CHECK_SECRET"]
            report = build_executor(backend, allow_commands=True)(Harness("a", "1", "solve"), Task("x", "question"), context)
            self.assertEqual(report.output, "True")

    def test_timeout_terminates_command(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(subprocess.TimeoutExpired):
                run_command(["{python}", "-c", "import time;time.sleep(10)"], Path(tmp), {}, timeout=.05)

    def test_excessive_command_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "2 MiB"):
                run_command(["{python}", "-c", "print('x'*2200000)"], Path(tmp), {}, timeout=5)

    def test_non_json_command_output_is_rejected(self):
        backend = {"kind": "command", "command": ["{python}", "-c", "print('not-json')"]}
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            build_executor(backend, allow_commands=True)(Harness("a", "1", "solve"), Task("x", "q"), Context(Path(tmp), "run", 0, 0))

    def test_cli_offline_run_report_list_and_rejected_export(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = config_dict(specification())
            data["backend"] = {"kind": "mock"}
            config = root / "spec.json"
            config.write_text(json.dumps(data))
            out = StringIO()
            with redirect_stdout(out):
                code = main(["run", "--config", str(config), "--root", str(root / "store"), "--json"])
            self.assertEqual(code, 0)
            result = json.loads(out.getvalue())
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["list", "--root", str(root / "store")]), 0)
                self.assertEqual(main(["report", "--root", str(root / "store"), "--experiment-id", result["experiment_id"]]), 0)
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["export", "--root", str(root / "store"), "--experiment-id", result["experiment_id"], "--out", str(root / "export.json")]), 2)
            self.assertFalse((root / "export.json").exists())

    def test_network_backend_needs_opt_in_without_spending_tokens(self):
        from contextlib import redirect_stderr
        from io import StringIO
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            data = config_dict(specification())
            data["backend"] = {"kind": "anthropic", "solver_model": "frozen-solver", "judge_model": "frozen-judge"}
            path.write_text(json.dumps(data))
            with redirect_stderr(StringIO()) as error:
                self.assertEqual(main(["run", "--config", str(path), "--root", str(Path(tmp) / "store")]), 2)
            self.assertIn("allow-network", error.getvalue())


if __name__ == "__main__":
    unittest.main()
