"""Provenance record, drift detection and the adapter capability declaration (no network, no model)."""
from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness_workbench
from harness_workbench.core.anthropic_backend import JUDGE_SYSTEM, AnthropicHarnessExecutor, Completion
from harness_workbench.core.harness import ALL_COMPONENTS
from harness_workbench.experiments import ExperimentSpec, core, provenance
from harness_workbench.experiments.backends import LocalBackend
from harness_workbench.experiments.contracts import ExecutionResult, Measurement, PublicTrialRequest
from harness_workbench.experiments.core import run_experiment
from harness_workbench.experiments.manifest import EnvironmentConfig
from harness_workbench.experiments.provenance import (
    detect_drift, git_state, implementation_hash, provenance_record, returned_model_status, unsupported_components,
)
from harness_workbench.experiments.status import DecisionStatus, ExecutionStatus
from tests.support import ScriptedExecutor, exact_task, make_records, write_manifest
from tests.unit.test_run_experiment_phases import PhaseCase, confirmatory_manifest

PACKAGE = Path(harness_workbench.__file__).resolve().parent
SECRET_VALUE = "not-a-real-secret-value"


class ModelDriftExecutor:
    """Answers CORRECT and reports a different model on every other trial."""

    solver_model = "requested-model"
    usage_provenance = "adapter_reported"

    def __init__(self) -> None:
        self.calls = 0

    def execute_trial(self, request: PublicTrialRequest, *, workspace, backend, limits) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(
            trial_id=request.trial_id, status=ExecutionStatus.COMPLETED, outputs={"answer": "CORRECT"},
            exit_code=None, stdout_tail="", stderr_tail="", tokens=Measurement.unknown(),
            latency_ms=Measurement.measured(1), adapter_latency_ms=Measurement.unknown(),
            returned_model=f"model-{self.calls % 2}", artifact_hash=None, error=None, retryable=False)


class CountingExecutor(ScriptedExecutor):
    """A scripted executor with the capability declaration of the Anthropic backend."""

    supports = AnthropicHarnessExecutor.supports

    def execute_case(self, harness, task, context):
        raise AssertionError("the executor must not run when a component is unsupported")


class ImplementationHashTests(unittest.TestCase):
    def test_implementation_hash_changes_when_a_module_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "harness_workbench"
            shutil.copytree(PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__"))
            before = implementation_hash(copy)
            self.assertEqual(before, implementation_hash(copy))
            self.assertEqual(before, implementation_hash())  # the default is this very package
            target = copy / "experiments" / "provenance.py"
            target.write_text(target.read_text(encoding="utf-8") + "\n# patched\n", encoding="utf-8")
            self.assertNotEqual(before, implementation_hash(copy))

    def test_renaming_a_module_changes_the_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "pkg"
            package.mkdir()
            (package / "a.py").write_text("x = 1\n", encoding="utf-8")
            before = implementation_hash(package)
            (package / "a.py").rename(package / "b.py")
            self.assertNotEqual(before, implementation_hash(package))


class GitStateTests(unittest.TestCase):
    def test_git_state_outside_repo_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(git_state(Path(tmp)), {"commit": None, "dirty": None})

    def test_git_state_missing_directory_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(git_state(Path(tmp) / "absent"), {"commit": None, "dirty": None})

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_state_reports_commit_and_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args: str) -> None:
                subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                "-c", "commit.gpgsign=false", *args], cwd=root, check=True,
                               capture_output=True)

            git("init", "-q")
            (root / "f.txt").write_text("one", encoding="utf-8")
            git("add", "f.txt")
            git("commit", "-q", "-m", "init")
            clean = git_state(root)
            self.assertRegex(clean["commit"], r"^[0-9a-f]{40}$")
            self.assertIs(clean["dirty"], False)
            (root / "f.txt").write_text("two", encoding="utf-8")
            self.assertIs(git_state(root)["dirty"], True)


class ProvenanceRecordTests(PhaseCase):
    def test_provenance_record_keys_and_no_env_values(self) -> None:
        manifest = write_manifest(self.root / "m", tasks=[exact_task("t1")],
                                  variants=[{"id": "candidate", "system_prompt": "cand"}])
        spec = dataclasses.replace(ExperimentSpec.load(manifest), environment=EnvironmentConfig(
            mode="trusted_local", solver_env_passthrough=("PROVENANCE_TEST_TOKEN",),
            trusted_solver_acknowledged=True))
        with mock.patch.dict("os.environ", {"PROVENANCE_TEST_TOKEN": SECRET_VALUE}):
            record = provenance_record(spec, AnthropicHarnessExecutor(client=None), LocalBackend(), root=self.root)
        self.assertEqual(
            set(record),
            {"commit", "dirty", "implementation_hash", "schema_version", "python", "platform", "executable",
             "executor", "adapter_argv", "adapter_supports", "backend", "model_requested", "environment",
             "seed_scope", "dependency_scope"})
        self.assertEqual(record["implementation_hash"], implementation_hash())
        self.assertEqual(record["executable"], sys.executable)
        self.assertEqual(record["executor"], "AnthropicHarnessExecutor")
        self.assertEqual(record["adapter_supports"], ["guardrails", "memory_config", "system_prompt"])
        self.assertEqual(set(record["backend"]), {"name", "capabilities", "image", "image_digest"})
        self.assertEqual(record["backend"]["name"], "trusted_local")
        self.assertIsNone(record["backend"]["image"])
        self.assertEqual(record["environment"]["solver_env_passthrough"], ["PROVENANCE_TEST_TOKEN"])
        self.assertEqual(record["environment"]["mode"], "trusted_local")
        self.assertIn("timeout_seconds", record["environment"]["limits"])
        self.assertEqual(record["seed_scope"], "paired per repetition; honored only if the adapter honors it")
        self.assertEqual(record["dependency_scope"], "stdlib only; adapter dependencies not hashed")
        self.assertEqual(record["schema_version"], spec.schema_version)
        self.assertNotIn(SECRET_VALUE, json.dumps(record))

    def test_payload_provenance_carries_the_record(self) -> None:
        manifest = write_manifest(self.root / "m", tasks=[exact_task("t1")],
                                  variants=[{"id": "candidate", "system_prompt": "cand"}])
        payload = run_experiment(ExperimentSpec.load(manifest),
                                 ScriptedExecutor({"base": lambda t: True, "cand": lambda t: True}),
                                 self.library, backend=LocalBackend())
        self.assertEqual(payload["provenance"]["implementation_hash"], implementation_hash())
        self.assertEqual({run["implementation_hash"] for run in payload["runs"]}, {implementation_hash()})
        self.assertEqual(payload["provenance"]["adapter_supports"], sorted(ALL_COMPONENTS))
        self.assertIn("manifest_sha256", payload["provenance"])


class DriftTests(PhaseCase):
    def records(self):
        spec = self.spec("drift", ("a",))
        return spec, make_records(spec, repetitions=1)

    def test_clean_records_have_no_drift(self) -> None:
        spec, records = self.records()
        rubrics = {task.id: task.bundle_hash for task in spec.tasks}
        self.assertEqual(detect_drift(records, rubrics), [])

    def test_differing_implementation_hash_is_drift(self) -> None:
        spec, records = self.records()
        records[0].implementation_hash = "other"
        rubrics = {task.id: task.bundle_hash for task in spec.tasks}
        (problem,) = detect_drift(records, rubrics)
        self.assertIn("implementation drift", problem)

    def test_changed_rubric_is_drift(self) -> None:
        spec, records = self.records()
        rubrics = {task.id: task.bundle_hash for task in spec.tasks}
        rubrics[records[0].task_id] = "different-rubric"
        problems = detect_drift(records, rubrics)
        self.assertTrue(problems)
        self.assertTrue(all("rubric drift" in problem for problem in problems))

    def test_two_returned_models_are_drift_and_none_is_ignored(self) -> None:
        spec, records = self.records()
        rubrics = {task.id: task.bundle_hash for task in spec.tasks}
        records[0].execution.returned_model = "m1"
        self.assertEqual(detect_drift(records, rubrics), [])  # one model and the rest unreported
        records[1].execution.returned_model = "m2"
        (problem,) = detect_drift(records, rubrics)
        self.assertIn("model drift", problem)

    def test_drift_in_model_or_rubric_invalidates(self) -> None:
        spec = self.spec("m", ("a",))
        payload = self.run_exp(spec, ModelDriftExecutor())
        decision = payload["decision"]
        self.assertEqual(decision["status"], DecisionStatus.INVALID_EXPERIMENT.value)
        self.assertTrue(any("model drift" in reason for reason in decision["reasons"]), decision["reasons"])

    def test_rubric_drift_in_a_run_invalidates(self) -> None:
        spec = self.spec("r", ("a",))
        real = core.evaluate_bundle
        calls = []

        def tampered(*args, **kwargs):
            result = real(*args, **kwargs)
            calls.append(result)
            return dataclasses.replace(result, rubric_hash="tampered") if len(calls) == 1 else result

        with mock.patch.object(core, "evaluate_bundle", tampered):
            payload = self.run_exp(spec, ScriptedExecutor({"base": lambda t: False, "a": lambda t: True}))
        self.assertEqual(payload["decision"]["status"], DecisionStatus.INVALID_EXPERIMENT.value)
        self.assertTrue(any("rubric drift" in reason for reason in payload["decision"]["reasons"]))

    def test_code_edited_during_a_run_invalidates_the_confirmation(self) -> None:
        spec = self.spec("code", ("a",))
        hashes = iter(["hash-at-start"])

        def hash_source(*_args, **_kwargs) -> str:  # the first read is the start; every later read is after an edit
            return next(hashes, "hash-after-edit")

        executor = ScriptedExecutor({"base": lambda t: False, "a": lambda t: True})
        with mock.patch.object(provenance, "implementation_hash", hash_source), \
                mock.patch.object(core, "implementation_hash", hash_source, create=True):
            payload = self.run_exp(spec, executor)
        decision = payload["decision"]
        self.assertEqual(decision["status"], DecisionStatus.INVALID_EXPERIMENT.value)
        self.assertTrue(any("implementation drift" in reason for reason in decision["reasons"]), decision["reasons"])


class CapabilityTests(PhaseCase):
    def test_anthropic_backend_declares_its_supported_components(self) -> None:
        self.assertEqual(AnthropicHarnessExecutor.supports, frozenset({"system_prompt", "guardrails", "memory_config"}))

    def test_unsupported_components_lists_changes_outside_supports(self) -> None:
        spec = self.spec("u", ("a",))
        self.assertEqual(unsupported_components(spec, frozenset(ALL_COMPONENTS)), [])
        # The candidate only differs in its system prompt.
        self.assertEqual(unsupported_components(spec, frozenset({"system_prompt"})), [])
        self.assertEqual(len(unsupported_components(spec, frozenset({"guardrails"}))), 1)
        (problem,) = unsupported_components(spec, frozenset({"guardrails"}))
        self.assertIn("system_prompt", problem)
        self.assertIn("a", problem)

    def test_unsupported_component_change_is_rejected_before_execution_in_confirmatory_mode(self) -> None:
        path = confirmatory_with_tool(self.root / "t")
        spec = ExperimentSpec.load(path)
        executor = CountingExecutor({"base": lambda t: False, "a": lambda t: True})
        payload = self.run_exp(spec, executor)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(payload["runs"], [])
        decision = payload["decision"]
        self.assertEqual(decision["status"], DecisionStatus.INVALID_EXPERIMENT.value)
        self.assertTrue(any("tools" in reason for reason in decision["reasons"]), decision["reasons"])
        db = self.db()
        self.assertEqual(db.trials(payload["experiment_id"]), [])

    def test_supported_change_still_runs_in_confirmatory_mode(self) -> None:
        spec = self.spec("s", ("a",))
        executor = ScriptedExecutor({"base": lambda t: False, "a": lambda t: True})
        executor.supports = frozenset({"system_prompt"})
        payload = self.run_exp(spec, executor)
        self.assertGreater(len(payload["runs"]), 0)
        self.assertFalse(any("does not declare support" in r for r in payload["decision"]["reasons"]))


class FakeAnthropicClient:
    """Stands in for AnthropicClient: the solver answers CORRECT and the returned model comes from ``models``."""

    def __init__(self, models: list[str]) -> None:
        self.models = models
        self.solver_calls = 0

    def complete(self, *, model: str, user: str, system: str = "", max_tokens: int = 4096) -> Completion:
        if system == JUDGE_SYSTEM:
            return Completion(json.dumps({"score": 1.0, "failures": [], "notes": "ok"}), model, 1, 1, "end_turn")
        returned = self.models[self.solver_calls % len(self.models)]
        self.solver_calls += 1
        return Completion("CORRECT", returned, 1, 1, "end_turn")


class ReturnedModelTests(PhaseCase):
    """The provider's returned model ID must reach the trial, the drift check and the payload (spec section 9)."""

    def run_with(self, models: list[str]) -> dict:
        spec = self.spec("rm", ("a",))
        return self.run_exp(spec, AnthropicHarnessExecutor(FakeAnthropicClient(models)))

    def test_alternating_snapshots_invalidate_a_confirmatory_run(self) -> None:
        payload = self.run_with(["snapshot-0", "snapshot-1"])
        self.assertTrue(payload["runs"])
        self.assertTrue(all(run["execution"]["returned_model"] in {"snapshot-0", "snapshot-1"}
                            for run in payload["runs"]))
        decision = payload["decision"]
        self.assertEqual(decision["status"], DecisionStatus.INVALID_EXPERIMENT.value)
        self.assertTrue(any("model drift" in reason for reason in decision["reasons"]), decision["reasons"])
        self.assertEqual(payload["provenance"]["returned_model_status"], "mixed")
        self.assertEqual(payload["provenance"]["returned_models"], ["snapshot-0", "snapshot-1"])

    def test_one_stable_returned_model_is_verified(self) -> None:
        payload = self.run_with(["snapshot-0"])
        self.assertNotEqual(payload["decision"]["status"], DecisionStatus.INVALID_EXPERIMENT.value)
        self.assertEqual(payload["provenance"]["returned_model_status"], "verified")
        self.assertEqual(payload["provenance"]["returned_models"], ["snapshot-0"])
        self.assertTrue(all(run["execution"]["returned_model"] == "snapshot-0" for run in payload["runs"]))

    def test_a_provider_that_names_no_model_is_marked_unknown(self) -> None:
        payload = self.run_with([""])
        self.assertEqual(payload["provenance"]["returned_model_status"], "unknown")
        self.assertEqual(payload["provenance"]["returned_models"], [])
        self.assertNotEqual(payload["decision"]["status"], DecisionStatus.INVALID_EXPERIMENT.value)
        self.assertIn("returned_model_status", (Path(payload["report_path"]).read_text(encoding="utf-8")))

    def test_status_of_a_mix_of_reported_and_unreported_models_is_partial_not_drift(self) -> None:
        spec = self.spec("st", ("a",))
        records = make_records(spec, repetitions=1)
        rubrics = {task.id: task.bundle_hash for task in spec.tasks}
        self.assertEqual(returned_model_status(records), {"status": "unknown", "models": []})
        records[0].execution.returned_model = "m1"
        self.assertEqual(returned_model_status(records), {"status": "partial", "models": ["m1"]})
        self.assertEqual(detect_drift(records, rubrics), [])  # a crashed or silent trial is a limitation, not drift
        for record in records:
            record.execution.returned_model = "m1"
        self.assertEqual(returned_model_status(records), {"status": "verified", "models": ["m1"]})
        records[1].execution.returned_model = "m2"
        self.assertEqual(returned_model_status(records), {"status": "mixed", "models": ["m1", "m2"]})


def confirmatory_with_tool(root: Path) -> Path:
    manifest = confirmatory_manifest(root, ("a",))
    candidate = root / "a.json"
    data = json.loads(candidate.read_text(encoding="utf-8"))
    data["tools"] = [{"name": "search", "description": "look things up"}]
    candidate.write_text(json.dumps(data), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    unittest.main()
