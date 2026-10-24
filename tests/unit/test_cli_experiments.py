"""The CLI: a confirmatory-only validation gate for ``improve`` and decision-aware ``experiment`` output."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from harness_workbench import cli
from harness_workbench.core.db import WorkbenchDB
from harness_workbench.core.executor import MockExecutor
from harness_workbench.core.proposer import RuleBasedProposer
from tests.support import ScriptedExecutor, exact_task, write_manifest
from tests.unit.test_contracts import make_plan
from tests.unit.test_db_experiments import make_decision
from tests.unit.test_improver_gate import base_harness

ROOT = Path(__file__).resolve().parents[2]
BASE_HARNESS = ROOT / "examples/workbench/base_research_harness.json"
TASK_TEXT = "Fact-check a claim using primary sources"
SCRIPTED_TOKENS = 100


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class SpyExecutor:
    """Counts every way an executor can be driven; nothing may run before the spec is accepted."""

    usage_provenance = "adapter_reported"

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, harness: Any, task: Any) -> Any:
        self.calls += 1
        raise AssertionError("executor must not run")

    execute_case = execute_trial = execute


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.library = self.tmp / "library-root"

    def legacy_manifest(self, name: str = "legacy", **overrides: Any) -> Path:
        return write_manifest(
            self.tmp / name, tasks=[exact_task("t1"), exact_task("t2")],
            variants=[{"id": "cand", "system_prompt": "cand"}], repetitions=3, **overrides)

    def patch_backend(self, executor: Any):
        return mock.patch.object(cli, "_backend", return_value=(RuleBasedProposer(), executor))

    def improve_args(self, spec: Path) -> list[str]:
        return ["improve", "--provider", "mock", "--harness", str(BASE_HARNESS), "--task", TASK_TEXT,
                "--domain", "research", "--validation-spec", str(spec), "--root", str(self.library)]


class ExploratoryRefusalTests(CliCase):
    def test_improve_refuses_exploratory_validation_spec_before_running(self) -> None:
        spy = SpyExecutor()
        with self.patch_backend(spy):
            code, out, err = run_cli(*self.improve_args(self.legacy_manifest()))
        self.assertEqual(code, 2)
        self.assertEqual(spy.calls, 0)
        self.assertIn("migrate-manifest", err)
        self.assertIn("validation spec is exploratory", err)
        self.assertFalse((self.library / "experiments").exists())
        self.assertFalse((self.library / "library" / "harnesses").exists())

    def test_explicit_exploratory_v2_spec_is_refused_too(self) -> None:
        spec = self.legacy_manifest(
            "v2", schema_version=2, mode="exploratory", family="f", dataset={"id": "d", "version": "1"},
            environment={"mode": "trusted_local", "trusted_solver_acknowledged": True})
        spy = SpyExecutor()
        with self.patch_backend(spy):
            code, _out, err = run_cli(*self.improve_args(spec))
        self.assertEqual((code, spy.calls), (2, 0))
        self.assertIn("migrate-manifest", err)


class ExperimentOutputTests(CliCase):
    def run_experiment_cli(self, *extra: str) -> tuple[int, str, str]:
        spec = self.legacy_manifest()
        executor = ScriptedExecutor({"base": lambda _t: False, "cand": lambda _t: True}, tokens=SCRIPTED_TOKENS)
        with self.patch_backend(executor):
            return run_cli("experiment", "--spec", str(spec), "--root", str(self.library), *extra)

    def test_experiment_json_output_has_decision_and_status(self) -> None:
        code, out, err = self.run_experiment_cli("--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(
            set(payload),
            {"experiment_id", "mode", "decision", "decision_id", "summary", "report_path", "results_path"})
        self.assertEqual(payload["mode"], "exploratory")
        self.assertEqual(payload["decision"]["status"], "exploratory_only")
        self.assertTrue(payload["decision_id"])
        self.assertTrue(Path(payload["results_path"]).is_file())
        self.assertEqual(payload["summary"]["recommended"], [])

    def test_text_output_prints_decision_reasons_and_unit_aware_variants(self) -> None:
        code, out, err = self.run_experiment_cli()
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertIn("decision exploratory_only", lines)
        self.assertTrue(any(line.strip() == "mode is exploratory" or line.strip() == "schema v1 manifest"
                            for line in lines), out)
        variant_lines = [line for line in lines if "quality=" in line]
        self.assertEqual(len(variant_lines), 2)
        self.assertTrue(all(f"tokens={SCRIPTED_TOKENS}" in line for line in variant_lines), variant_lines)
        self.assertNotIn("ELIGIBLE", out)

    def test_text_output_states_statistics_mode_and_exploratory_caveat(self) -> None:
        code, out, err = self.run_experiment_cli()
        self.assertEqual(code, 0, err)
        lines = [line.strip() for line in out.splitlines()]
        self.assertIn("mode exploratory", lines)
        self.assertIn("Exploratory only: not promotion evidence", lines)
        self.assertTrue(any(line.startswith("estimate ") and "lower_bound " in line and "n_units " in line
                            for line in lines), out)
        self.assertNotIn("Synthetic data", out)

    def test_text_output_labels_synthetic_runs(self) -> None:
        result = {
            "experiment_id": "e", "mode": "exploratory", "report_path": "r", "results_path": "d",
            "provenance": {"executor": "MockExecutor", "model_requested": "x"},
            "decision": {"status": "invalid_experiment", "reasons": ["why"], "estimate": None,
                         "lower_bound": None, "n_units": 0, "diagnostics": {}},
            "summary": {"variants": {}},
        }
        out = StringIO()
        with redirect_stdout(out):
            cli._print_experiment(result, "name")
        text = out.getvalue()
        self.assertIn("Synthetic data", text)
        self.assertIn("decision invalid_experiment", text)
        self.assertIn("estimate unavailable  lower_bound unavailable  n_units 0", text)

    def test_unknown_tokens_print_as_unknown(self) -> None:
        payload_summary = {"variants": {"x": {"mean_score": None, "pass_rate": None, "mean_tokens": None}}}
        text = cli._variant_line("x", payload_summary["variants"]["x"])
        self.assertIn("tokens=unknown", text)
        self.assertIn("quality=unknown", text)

    def test_provider_command_requires_a_command_executor_manifest(self) -> None:
        code, _out, err = run_cli("experiment", "--spec", str(self.legacy_manifest()), "--provider", "command",
                                  "--root", str(self.library))
        self.assertEqual(code, 2)
        self.assertIn("--provider command requires executor.type 'command'", err)
        self.assertFalse((self.library / "experiments").exists())


class BackendUnavailableTests(CliCase):
    def sandboxed_manifest(self) -> Path:
        return self.legacy_manifest(
            "sandboxed", schema_version=2, mode="exploratory", family="f", dataset={"id": "d", "version": "1"},
            environment={"mode": "sandboxed"})

    def test_backend_unavailable_is_a_clean_error(self) -> None:
        empty_path = self.tmp / "empty-bin"
        empty_path.mkdir()
        executor = ScriptedExecutor({"base": lambda _t: False, "cand": lambda _t: True})
        with mock.patch.dict(os.environ, {"PATH": str(empty_path)}), self.patch_backend(executor):
            code, out, err = run_cli("experiment", "--spec", str(self.sandboxed_manifest()),
                                     "--root", str(self.library))
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: "), err)
        self.assertIn("docker", err.lower())
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")

    def test_store_busy_and_migration_required_exit_two(self) -> None:
        from harness_workbench.core.db import StoreBusyError
        from harness_workbench.core.store import MigrationRequired
        for error in (StoreBusyError("busy"), MigrationRequired("migrate first")):
            with mock.patch("harness_workbench.experiments.run_experiment", side_effect=error):
                executor = ScriptedExecutor({"base": lambda _t: False, "cand": lambda _t: True})
                with self.patch_backend(executor):
                    code, _out, err = run_cli("experiment", "--spec", str(self.legacy_manifest()),
                                              "--root", str(self.library))
            self.assertEqual(code, 2)
            self.assertIn(str(error), err)


class ListIncompleteTests(CliCase):
    def start(self, experiment_id: str) -> None:
        self.library.mkdir(parents=True, exist_ok=True)
        db = WorkbenchDB.open(self.library)
        try:
            db.start_experiment(experiment_id, name="e", family="f", mode="exploratory", schema_version=1,
                                plan=make_plan(), implementation_hash="impl")
        finally:
            db.close()

    def test_list_incomplete(self) -> None:
        self.start("exp-interrupted")
        code, out, err = run_cli("experiment", "--root", str(self.library), "--list-incomplete")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.split(), ["exp-interrupted"])

    def test_list_incomplete_json_and_empty(self) -> None:
        code, out, _err = run_cli("experiment", "--root", str(self.library), "--list-incomplete", "--json")
        self.assertEqual((code, json.loads(out)), (0, []))
        self.start("exp-interrupted")
        code, out, _err = run_cli("experiment", "--root", str(self.library), "--list-incomplete", "--json")
        self.assertEqual(json.loads(out), ["exp-interrupted"])

    def test_spec_is_required_without_list_incomplete(self) -> None:
        code, _out, err = run_cli("experiment", "--root", str(self.library))
        self.assertEqual(code, 2)
        self.assertIn("--spec", err)


class GateTests(CliCase):
    """The improve gate trusts the persisted decision status, never the interim contrast verdict."""

    def confirmatory_spec(self) -> Path:
        from tests.unit.test_run_experiment_phases import confirmatory_manifest
        return confirmatory_manifest(self.tmp / "confirmatory", ("cand",))

    def fake_run_experiment(self, status: str, seen: list[Any], *, record: bool):
        def fake(spec: Any, executor: Any, root: Any, **kwargs: Any) -> dict:
            seen.append(spec)
            before, candidate = spec.variants[0].harness, spec.variants[1].harness
            decision_id = uuid.uuid4().hex
            if record:
                Path(root).mkdir(parents=True, exist_ok=True)
                db = WorkbenchDB.open(Path(root))
                try:
                    experiment_id = uuid.uuid4().hex
                    db.start_experiment(experiment_id, name="exp", family="fam", mode="confirmatory",
                                        schema_version=2, plan=make_plan(), implementation_hash="impl")
                    decision_id = db.record_decision(experiment_id, make_decision(
                        candidate_hash=candidate.execution_config_hash(),
                        baseline_hash=before.execution_config_hash(),
                        baseline_id=before.id, baseline_version=before.version))
                finally:
                    db.close()
            return {
                "experiment_id": "e1", "mode": "confirmatory", "decision_id": decision_id,
                "decision": {"status": status, "reasons": []},
                # the interim contrast says eligible; the gate must ignore it
                "summary": {"contrasts": {"candidate": {"eligible": True, "reasons": []}}, "variants": {}},
                "report_path": "r.html", "results_path": "r.json",
            }
        return fake

    def improve_json(self, fake: Any) -> tuple[int, dict, str]:
        spec = self.confirmatory_spec()
        with mock.patch("harness_workbench.experiments.run_experiment", side_effect=fake), \
                self.patch_backend(MockExecutor()):
            code, out, err = run_cli(*self.improve_args(spec), "--json")
        return code, (json.loads(out) if out.strip() else {}), err

    def test_gate_ignores_contrast_eligibility_when_decision_is_not_eligible(self) -> None:
        seen: list[Any] = []
        code, payload, err = self.improve_json(self.fake_run_experiment("insufficient_evidence", seen, record=False))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(seen), 1)
        self.assertEqual(payload["adopted"]["version"], payload["base"]["version"])
        self.assertIsNone(payload["decision_id"])
        self.assertTrue(any(lesson.startswith("VALIDATION REJECT") for lesson in payload["lessons"]))
        evidence = payload["validation_evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(set(evidence[0]), {"report", "results", "decision"})
        self.assertEqual(evidence[0]["decision"]["status"], "insufficient_evidence")

    def test_eligible_decision_promotes_by_decision_id_with_baseline_identity(self) -> None:
        seen: list[Any] = []
        code, payload, err = self.improve_json(self.fake_run_experiment("eligible", seen, record=True))
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["decision_id"])
        self.assertNotEqual(payload["adopted"]["version"], payload["base"]["version"])
        self.assertEqual(payload["validation_evidence"][0]["decision"]["status"], "eligible")
        controlled = seen[0]
        baseline = controlled.variants[0]
        self.assertEqual((baseline.harness.id, baseline.harness.version),
                         (payload["base"]["id"], payload["base"]["version"]))
        self.assertEqual(baseline.config_hash, baseline.harness.execution_config_hash())
        self.assertEqual([variant.id for variant in controlled.variants], ["baseline", "candidate"])
        harness_dir = self.library / "library" / "harnesses" / payload["base"]["id"]
        self.assertTrue((harness_dir / f"{payload['adopted']['version']}.json").is_file())


if __name__ == "__main__":
    unittest.main()
