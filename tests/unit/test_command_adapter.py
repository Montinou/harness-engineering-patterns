"""Command adapter: public trial request, allowlisted environment, supervisor-measured usage.

Solver scripts run as real child processes through ``LocalBackend`` (trusted local
execution). Canaries are literal strings, never real secrets, and no test prints
an environment.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness_workbench.core.executor import Task
from harness_workbench.core.harness import ALL_COMPONENTS, Harness
from harness_workbench.experiments.backends import LocalBackend, Mounts, ProcessLimits
from harness_workbench.experiments.command import CommandHarnessExecutor
from harness_workbench.experiments.contracts import PublicTrialRequest
from harness_workbench.experiments.core import RunContext
from harness_workbench.experiments.manifest import EnvironmentConfig, ExecutorConfig, ProcessLimitsConfig
from harness_workbench.experiments.status import ExecutionStatus, Provenance
from tests.support import ENV_CANARY

DEMO_AGENT = Path(__file__).resolve().parents[2] / "harness_workbench" / "experiments" / "demo_agent.py"
TRIAL_ID = "9f1c2ab34d5e"


def _harness(guardrails: list[str] | None = None) -> Harness:
    harness = Harness(id="secret-id", version="7", system_prompt="Answer carefully.",
                      notes="internal-note", guardrails=list(guardrails or []))
    harness.lineage.parent_version = "6"
    return harness


def _request(harness: Harness | None = None, *, description: str = "do the task", seed: int = 11,
             trial_id: str = TRIAL_ID) -> PublicTrialRequest:
    return PublicTrialRequest(
        trial_id=trial_id, task_description=description, domain="research", constraints=("be brief",),
        harness_public=(harness or _harness()).public_components(), fixture_hash=None, seed=seed, budget={})


def _result_writer(raw: str) -> str:
    """Solver code that writes ``raw`` verbatim as result.json."""
    return f"import os; open(os.environ['HARNESS_RESULT_FILE'], 'w').write({raw!r})"


def _result_json(obj: dict) -> str:
    return _result_writer(json.dumps(obj))


class RecordingBackend:
    """Delegates to LocalBackend and remembers what it was asked to run."""

    name = "recording"
    python = LocalBackend.python

    def __init__(self) -> None:
        self.inner = LocalBackend()
        self.argv: list[str] = []
        self.mounts: Mounts | None = None
        self.env: dict[str, str] = {}
        self.cwd: Path | None = None

    def capabilities(self):
        return self.inner.capabilities()

    def run(self, argv, *, cwd, env, limits, mounts):
        self.argv, self.mounts, self.env, self.cwd = list(argv), mounts, dict(env), cwd
        return self.inner.run(argv, cwd=cwd, env=env, limits=limits, mounts=mounts)


class AdapterCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "trial"
        self.workspace = self.base / "workspace"
        self.control = self.base / "control"
        self.home = self.base / "home"

    def run_code(self, code: str, *, request: PublicTrialRequest | None = None, passthrough: tuple[str, ...] = (),
                 limits: ProcessLimits | None = None, backend=None):
        executor = CommandHarnessExecutor([sys.executable, "-c", code], env_passthrough=passthrough)
        return self.run_executor(executor, request=request, limits=limits, backend=backend)

    def run_executor(self, executor: CommandHarnessExecutor, *, request: PublicTrialRequest | None = None,
                     limits: ProcessLimits | None = None, backend=None):
        return executor.execute_trial(
            request or _request(), workspace=self.workspace, backend=backend or LocalBackend(),
            limits=limits or ProcessLimits(timeout_seconds=30))


class PublicRequestTests(AdapterCase):
    def test_solver_sees_only_public_components_and_opaque_trial_id(self) -> None:
        code = ("import os, json; "
                "h = json.load(open(os.environ['HARNESS_SPEC_FILE'])); t = json.load(open(os.environ['HARNESS_TASK_FILE'])); "
                "json.dump({'outputs': {'harness_keys': sorted(h), 'task_keys': sorted(t), "
                "'trial_id': t['trial_id'], 'env_trial_id': os.environ['HARNESS_TRIAL_ID'], "
                "'seed': os.environ['HARNESS_SEED']}}, open(os.environ['HARNESS_RESULT_FILE'], 'w'))")
        result = self.run_code(code)
        self.assertEqual(result.status, ExecutionStatus.COMPLETED, result.error)
        self.assertEqual(result.outputs["harness_keys"], sorted(ALL_COMPONENTS))
        for private in ("lineage", "version", "notes", "id", "name"):
            self.assertNotIn(private, result.outputs["harness_keys"])
        self.assertEqual(result.outputs["task_keys"], ["constraints", "description", "domain", "trial_id"])
        self.assertNotIn("split", result.outputs["task_keys"])
        self.assertEqual(result.outputs["trial_id"], TRIAL_ID)
        self.assertEqual(result.outputs["env_trial_id"], TRIAL_ID)
        self.assertEqual(result.outputs["seed"], "11")
        self.assertEqual(result.trial_id, TRIAL_ID)
        # harness.json is exactly the public components; nothing private is on disk anywhere in the control dir.
        self.assertEqual(json.loads((self.control / "harness.json").read_text()), _request().harness_public)
        for path in self.control.iterdir():
            text = path.read_text()
            for private in ("secret-id", "internal-note"):
                self.assertNotIn(private, text, path.name)

    def test_control_files_never_contain_environment_values(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_TEST_CANARY": ENV_CANARY}):
            result = self.run_code("print('ok')", passthrough=("HARNESS_TEST_CANARY",))
        self.assertEqual(result.status, ExecutionStatus.COMPLETED, result.error)
        for path in self.control.iterdir():
            self.assertNotIn(ENV_CANARY, path.read_text(), path.name)
        self.assertNotIn(ENV_CANARY, json.dumps(result.to_dict()))


class EnvironmentTests(AdapterCase):
    CODE = ("import os, json; json.dump({'outputs': {'answer': os.environ.get('HARNESS_TEST_CANARY', '')}}, "
            "open(os.environ['HARNESS_RESULT_FILE'], 'w'))")

    def test_parent_canary_not_inherited_unless_passthrough(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_TEST_CANARY": ENV_CANARY}):
            default = self.run_code(self.CODE)
            declared = self.run_code(self.CODE, passthrough=("HARNESS_TEST_CANARY",))
        self.assertEqual(default.status, ExecutionStatus.COMPLETED, default.error)
        self.assertEqual(default.outputs["answer"], "")
        self.assertEqual(declared.outputs["answer"], ENV_CANARY, "positive control: a declared name is passed")

    def test_python_variables_in_parent_do_not_reach_the_solver(self) -> None:
        code = ("import os, json; json.dump({'outputs': {'present': sorted(k for k in os.environ if k.startswith('PYTHON'))}}, "
                "open(os.environ['HARNESS_RESULT_FILE'], 'w'))")
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/x", "PYTHONSTARTUP": "/y"}):
            result = self.run_code(code)
        self.assertEqual(result.outputs["present"], ["PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", "PYTHONSAFEPATH"])

    def test_fresh_home_per_trial(self) -> None:
        code = ("import os, json; open(os.path.join(os.environ['HOME'], 'marker'), 'w').write('m'); "
                "open(os.path.join(os.environ['TMPDIR'], 'scratch'), 'w').write('s'); "
                "json.dump({'outputs': {'home': os.environ['HOME'], 'tmp': os.environ['TMPDIR']}}, "
                "open(os.environ['HARNESS_RESULT_FILE'], 'w'))")
        result = self.run_code(code)
        self.assertEqual(result.status, ExecutionStatus.COMPLETED, result.error)
        self.assertEqual(Path(result.outputs["home"]), self.base / "home")
        self.assertEqual(Path(result.outputs["tmp"]), self.base / "tmp")
        self.assertTrue((self.base / "home" / "marker").is_file())
        self.assertTrue((self.base / "tmp" / "scratch").is_file())
        self.assertFalse((self.workspace / "marker").exists())
        # A second trial in the same parent starts from an empty home.
        again = self.run_code("import os; print(sorted(os.listdir(os.environ['HOME'])))")
        self.assertEqual(again.outputs["answer"].strip(), "[]")

    def test_declared_passthrough_cannot_use_python_names(self) -> None:
        for name in ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONINSPECT", "pythonpath", "PYTHONFOO"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, name):
                    CommandHarnessExecutor([sys.executable], env_passthrough=(name,))

    def test_declared_passthrough_cannot_replace_the_fresh_home_and_tmp(self) -> None:
        for name in ("HOME", "TMPDIR", "XDG_CACHE_HOME"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, name):
                    CommandHarnessExecutor([sys.executable], env_passthrough=(name,))

    def test_declared_passthrough_must_be_environment_variable_names(self) -> None:
        for name in ("BAD NAME", "1X", "A=B", ""):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    CommandHarnessExecutor([sys.executable], env_passthrough=(name,))

    def test_from_config_refuses_python_passthrough_declared_in_the_manifest(self) -> None:
        environment = EnvironmentConfig(mode="trusted_local", solver_env_passthrough=("PYTHONPATH",))
        with self.assertRaisesRegex(ValueError, "PYTHONPATH"):
            CommandHarnessExecutor.from_config(ExecutorConfig(type="command", argv=(sys.executable,)), environment)


class MeasurementTests(AdapterCase):
    def test_supervisor_latency_wins_over_adapter_latency(self) -> None:
        code = ("import os, json, time; time.sleep(0.3); "
                "json.dump({'latency_ms': 0, 'outputs': {'answer': 'x'}}, open(os.environ['HARNESS_RESULT_FILE'], 'w'))")
        result = self.run_code(code)
        self.assertEqual(result.status, ExecutionStatus.COMPLETED, result.error)
        self.assertGreaterEqual(result.latency_ms.value, 250)
        self.assertEqual(result.latency_ms.provenance, Provenance.MEASURED)
        self.assertEqual(result.adapter_latency_ms.value, 0)
        self.assertEqual(result.adapter_latency_ms.provenance, Provenance.ADAPTER_REPORTED)

    def test_adapter_latency_absent_is_unknown(self) -> None:
        result = self.run_code(_result_json({"outputs": {"answer": "x"}}))
        self.assertIsNone(result.adapter_latency_ms.value)
        self.assertEqual(result.adapter_latency_ms.provenance, Provenance.UNKNOWN)
        self.assertEqual(result.latency_ms.provenance, Provenance.MEASURED)

    def test_tokens_absent_is_unknown_and_zero_is_known_zero(self) -> None:
        absent = self.run_code(_result_json({"outputs": {"answer": "x"}}))
        self.assertIsNone(absent.tokens.value)
        self.assertEqual(absent.tokens.provenance, Provenance.UNKNOWN)
        self.assertEqual(self.run_code("print('no result file')").tokens.provenance, Provenance.UNKNOWN)
        zero = self.run_code(_result_json({"tokens": 0}))
        self.assertEqual(zero.tokens.value, 0)
        self.assertEqual(zero.tokens.provenance, Provenance.ADAPTER_REPORTED)
        top = self.run_code(_result_json({"tokens": 10**12}))
        self.assertEqual(top.tokens.value, 10**12)

    def _assert_protocol_errors(self, field: str, cases: dict[str, str]) -> None:
        for label, raw in cases.items():
            with self.subTest(case=label):
                result = self.run_code(_result_writer(raw))
                self.assertEqual(result.status, ExecutionStatus.EXECUTION_ERROR, result.error)
                self.assertFalse(result.retryable, "an adapter protocol error is not an infrastructure retry")
                self.assertIn("protocol", result.error)
                if "NaN" not in raw and "Infinity" not in raw:  # rejected while parsing, before any field is known
                    self.assertIn(field, result.error)
                self.assertEqual(result.tokens.provenance, Provenance.UNKNOWN)
                self.assertEqual(result.adapter_latency_ms.provenance, Provenance.UNKNOWN)

    def test_tokens_negative_string_bool_and_out_of_range_are_protocol_errors(self) -> None:
        self._assert_protocol_errors("tokens", {
            "negative": '{"tokens": -1}', "string": '{"tokens": "5"}', "bool": '{"tokens": true}',
            "above 10**12": '{"tokens": 1000000000001}', "non-integer": '{"tokens": 1.5}',
            "integral float": '{"tokens": 5.0}', "null": '{"tokens": null}',
            "NaN": '{"tokens": NaN}', "overflow literal": '{"tokens": 1e400}',
        })

    def test_latency_negative_string_bool_and_out_of_range_are_protocol_errors(self) -> None:
        self._assert_protocol_errors("latency_ms", {
            "negative": '{"latency_ms": -1}', "string": '{"latency_ms": "5"}', "bool": '{"latency_ms": false}',
            "above 10**12": '{"latency_ms": 1000000000001}', "non-integer": '{"latency_ms": 0.5}',
            "Infinity": '{"latency_ms": Infinity}', "overflow literal": '{"latency_ms": 1e400}',
        })

    def test_overflow_literal_anywhere_in_adapter_output_is_a_protocol_error(self) -> None:
        for label, raw in {
            "in outputs": '{"outputs": {"score": 1e400}}',
            "negative overflow": '{"outputs": {"nested": [{"x": -1e999}]}}',
            "in an unrelated key": '{"extra": 1e400}',
        }.items():
            with self.subTest(case=label):
                result = self.run_code(_result_writer(raw))
                self.assertEqual(result.status, ExecutionStatus.EXECUTION_ERROR, result.error)
                self.assertFalse(result.retryable)
                self.assertIn("protocol", result.error)

    def test_boundary_values_are_accepted(self) -> None:
        result = self.run_code(_result_json({"tokens": 10**12, "latency_ms": 10**12, "outputs": {"answer": "x"}}))
        self.assertEqual(result.status, ExecutionStatus.COMPLETED, result.error)
        self.assertEqual(result.adapter_latency_ms.value, 10**12)

    def test_returned_model_is_a_string_or_none(self) -> None:
        self.assertEqual(self.run_code(_result_json({"model": "synthetic-fixture-agent"})).returned_model,
                         "synthetic-fixture-agent")
        self.assertIsNone(self.run_code(_result_json({"model": 5})).returned_model)
        self.assertIsNone(self.run_code(_result_json({})).returned_model)


class ProtocolTests(AdapterCase):
    def test_invalid_shapes_are_protocol_errors(self) -> None:
        for label, raw in {
            "invalid json": "{not json", "empty file": "", "array": "[1, 2]", "scalar": "5",
            "outputs is a list": '{"outputs": []}', "outputs is a string": '{"outputs": "x"}',
        }.items():
            with self.subTest(case=label):
                result = self.run_code(_result_writer(raw))
                self.assertEqual(result.status, ExecutionStatus.EXECUTION_ERROR, result.error)
                self.assertFalse(result.retryable)
                self.assertIn("protocol", result.error)
                self.assertEqual(result.outputs, {})

    def test_result_file_that_is_a_symlink_is_not_followed(self) -> None:
        secret = self.base / "elsewhere.txt"
        code = ("import os; os.symlink(os.environ['HARNESS_TEST_TARGET'], os.environ['HARNESS_RESULT_FILE'])")
        self.base.mkdir(parents=True)
        secret.write_text('{"outputs": {"answer": "from-a-file-the-solver-should-not-choose"}}', encoding="utf-8")
        with mock.patch.dict(os.environ, {"HARNESS_TEST_TARGET": str(secret)}):
            result = self.run_code(code, passthrough=("HARNESS_TEST_TARGET",))
        self.assertEqual(result.status, ExecutionStatus.EXECUTION_ERROR, result.error)
        self.assertIn("protocol", result.error)
        self.assertNotIn("from-a-file", json.dumps(result.to_dict()))

    def test_oversized_result_file_is_a_protocol_error(self) -> None:
        code = "import os; open(os.environ['HARNESS_RESULT_FILE'], 'w').write('{\"outputs\": {\"a\": \"' + 'x' * 5000 + '\"}}')"
        result = self.run_code(code, limits=ProcessLimits(timeout_seconds=30, max_output_bytes=1000))
        self.assertEqual(result.status, ExecutionStatus.EXECUTION_ERROR, result.error)
        self.assertIn("protocol", result.error)

    def test_stale_result_file_from_an_earlier_trial_is_ignored(self) -> None:
        self.control.mkdir(parents=True)
        (self.control / "result.json").write_text('{"outputs": {"answer": "stale"}, "tokens": 999}', encoding="utf-8")
        result = self.run_code("print('fresh')")
        self.assertEqual(result.outputs, {"answer": "fresh\n"})
        self.assertEqual(result.tokens.provenance, Provenance.UNKNOWN)

    def test_default_outputs_are_stdout_and_tails_are_bounded(self) -> None:
        result = self.run_code("import sys; sys.stdout.write('a' * 5000 + 'END'); sys.stderr.write('b' * 5000 + 'ERR')")
        self.assertEqual(result.outputs, {"answer": "a" * 5000 + "END"})
        self.assertEqual(len(result.stdout_tail), 2000)
        self.assertTrue(result.stdout_tail.endswith("END"))
        self.assertEqual(len(result.stderr_tail), 2000)
        self.assertTrue(result.stderr_tail.endswith("ERR"))
        self.assertEqual(result.exit_code, 0)


class OutcomeMappingTests(AdapterCase):
    def test_nonzero_exit_is_task_failure_still_evaluable_and_spawn_failure_is_retryable_execution_error(self) -> None:
        failed = self.run_code("import sys; print('partial answer'); sys.exit(3)")
        self.assertEqual(failed.status, ExecutionStatus.TASK_FAILURE)
        self.assertEqual(failed.exit_code, 3)
        self.assertEqual(failed.outputs, {"answer": "partial answer\n"})
        self.assertFalse(failed.retryable)
        spawn = self.run_executor(CommandHarnessExecutor(["/nonexistent/agent-binary-for-test"]))
        self.assertEqual(spawn.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertTrue(spawn.retryable)
        self.assertIn("could not start", spawn.error)
        self.assertIsNone(spawn.exit_code)

    def test_output_overflow_is_resource_limit(self) -> None:
        code = "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()\n"
        started = time.monotonic()
        result = self.run_code(code, limits=ProcessLimits(timeout_seconds=30, max_output_bytes=100_000))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.status, ExecutionStatus.RESOURCE_LIMIT)
        self.assertFalse(result.retryable)
        self.assertIn("100000", result.error)
        self.assertLessEqual(len(result.stdout_tail), 2000)
        self.assertEqual(result.outputs, {})

    def test_timeout_keeps_status_and_error(self) -> None:
        result = self.run_code("import time; time.sleep(30)", limits=ProcessLimits(timeout_seconds=0.5))
        self.assertEqual(result.status, ExecutionStatus.TIMEOUT)
        self.assertIn("0.5", result.error)
        self.assertEqual(result.latency_ms.provenance, Provenance.MEASURED)
        self.assertEqual(result.outputs, {})

    def test_failed_run_does_not_trust_a_result_file(self) -> None:
        code = ("import os, sys; open(os.environ['HARNESS_RESULT_FILE'], 'w').write('{\"tokens\": 5}'); "
                "sys.stdout.write('x' * 500000)")
        result = self.run_code(code, limits=ProcessLimits(timeout_seconds=30, max_output_bytes=1000))
        self.assertEqual(result.status, ExecutionStatus.RESOURCE_LIMIT)
        self.assertEqual(result.tokens.provenance, Provenance.UNKNOWN)


class PlaceholderTests(AdapterCase):
    def test_python_and_demo_agent_placeholders_resolve_to_trusted_paths(self) -> None:
        backend = RecordingBackend()
        harness = _harness(["Verify every claim."])
        executor = CommandHarnessExecutor(["{python}", "{demo_agent}", "{trial_id}"])
        result = self.run_executor(executor, request=_request(harness), backend=backend)
        self.assertEqual(result.status, ExecutionStatus.COMPLETED, result.error)
        self.assertEqual(backend.argv, [sys.executable, str(DEMO_AGENT), TRIAL_ID])
        self.assertTrue(DEMO_AGENT.is_file())
        self.assertEqual(backend.mounts.read_only, (DEMO_AGENT,))
        self.assertEqual(backend.mounts.writable, (self.workspace, self.control, self.home, self.base / "tmp"))
        self.assertEqual(backend.cwd, self.workspace)
        # The synthetic agent ran for real, saw only public components, and reports its (synthetic) model.
        self.assertEqual(result.outputs, {"answer": "CORRECT"})
        self.assertEqual(result.returned_model, "synthetic-fixture-agent")
        self.assertEqual((self.workspace / "report.txt").read_text(), "PASS")
        self.assertEqual(result.tokens.value, 10)

    def test_backend_python_is_what_the_placeholder_resolves_to(self) -> None:
        class OtherPython(RecordingBackend):
            python = "/opt/trusted/python-for-test"

        backend = OtherPython()
        self.run_executor(CommandHarnessExecutor(["{python}", "-c", "pass"]), backend=backend)
        self.assertEqual(backend.argv[0], "/opt/trusted/python-for-test")

    def test_task_text_cannot_inject_placeholders(self) -> None:
        backend = RecordingBackend()
        request = _request(description="{python} {demo_agent} {workspace}")
        executor = CommandHarnessExecutor(["{python}", "-c", "import sys; print(sys.argv[1])", "{task}", "{seed}-{trial_id}"])
        result = self.run_executor(executor, request=request, backend=backend)
        self.assertEqual(backend.argv[3], "{python} {demo_agent} {workspace}")
        self.assertEqual(backend.argv[4], f"11-{TRIAL_ID}")
        self.assertEqual(result.outputs["answer"].strip(), "{python} {demo_agent} {workspace}")

    def test_legacy_file_and_workspace_placeholders_still_expand(self) -> None:
        backend = RecordingBackend()
        executor = CommandHarnessExecutor(["{python}", "-c", "pass", "{workspace}", "{harness_json}", "{task_json}",
                                           "{prompt_file}", "{result_json}", "{prompt}"])
        self.run_executor(executor, backend=backend)
        self.assertEqual(backend.argv[3:8], [str(self.workspace), str(self.control / "harness.json"),
                                             str(self.control / "task.json"), str(self.control / "prompt.txt"),
                                             str(self.control / "result.json")])
        self.assertIn("Answer carefully.", backend.argv[8])
        self.assertIn("do the task", backend.argv[8])
        self.assertEqual((self.control / "prompt.txt").read_text(), backend.argv[8])

    def test_environment_given_to_the_backend_is_minimal_plus_harness_variables(self) -> None:
        backend = RecordingBackend()
        with mock.patch.dict(os.environ, {"HARNESS_TEST_CANARY": ENV_CANARY}):
            self.run_executor(CommandHarnessExecutor([sys.executable, "-c", "pass"]), backend=backend)
        self.assertNotIn("HARNESS_TEST_CANARY", backend.env)
        self.assertEqual(backend.env["HARNESS_WORKSPACE"], str(self.workspace))
        self.assertEqual(backend.env["HARNESS_SPEC_FILE"], str(self.control / "harness.json"))
        self.assertEqual(backend.env["HARNESS_TASK_FILE"], str(self.control / "task.json"))
        self.assertEqual(backend.env["HARNESS_RESULT_FILE"], str(self.control / "result.json"))
        self.assertEqual(backend.env["HARNESS_SEED"], "11")
        self.assertEqual(backend.env["HARNESS_TRIAL_ID"], TRIAL_ID)
        self.assertEqual(backend.env["HOME"], str(self.home))


class FromConfigTests(unittest.TestCase):
    def test_from_config_reads_executor_and_environment(self) -> None:
        executor_config = ExecutorConfig(type="command", argv=("{python}", "{demo_agent}"), timeout_seconds=30.0,
                                         model="synthetic-fixture-agent", supports=frozenset({"guardrails"}))
        environment = EnvironmentConfig(mode="trusted_local", solver_env_passthrough=("ANTHROPIC_API_KEY",),
                                        limits=ProcessLimitsConfig(timeout_seconds=300.0, max_output_bytes=4321))
        executor = CommandHarnessExecutor.from_config(executor_config, environment)
        self.assertEqual(executor.argv, ["{python}", "{demo_agent}"])
        self.assertEqual(executor.timeout_seconds, 30.0)
        self.assertEqual(executor.solver_model, "synthetic-fixture-agent")
        self.assertEqual(executor.env_passthrough, ("ANTHROPIC_API_KEY",))
        self.assertEqual(executor.supports, frozenset({"guardrails"}))
        self.assertEqual(executor.limits.max_output_bytes, 4321)
        self.assertEqual(executor.limits.timeout_seconds, 30.0)

    def test_the_stricter_timeout_wins(self) -> None:
        executor = CommandHarnessExecutor.from_config(
            ExecutorConfig(type="command", argv=("x",), timeout_seconds=300.0),
            EnvironmentConfig(mode="trusted_local", limits=ProcessLimitsConfig(timeout_seconds=5.0)))
        self.assertEqual(executor.limits.timeout_seconds, 5.0)

    def test_defaults_match_the_contract(self) -> None:
        executor = CommandHarnessExecutor(["x"])
        self.assertEqual(executor.timeout_seconds, 300.0)
        self.assertEqual(executor.solver_model, "external-command")
        self.assertEqual(executor.env_passthrough, ())
        self.assertEqual(executor.supports, frozenset(ALL_COMPONENTS))

    def test_empty_argv_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            CommandHarnessExecutor([])


class LegacyWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def context(self) -> RunContext:
        return RunContext("experiment", "run0123456789", "case-with-split-name", "candidate", 0, 5,
                          self.root / "work" / "workspace")

    def test_execute_case_converts_the_trial_back_to_a_behavioral_report(self) -> None:
        executor = CommandHarnessExecutor(["{python}", "{demo_agent}"])
        report = executor.execute_case(_harness(["Verify every claim."]), Task.from_text("task", task_id="t1"),
                                       self.context())
        self.assertEqual(report.task_id, "t1")
        self.assertEqual(report.outputs, {"answer": "CORRECT"})
        self.assertEqual(report.cost_tokens, 10)
        self.assertAlmostEqual(report.score, 0.2)
        self.assertGreaterEqual(report.latency_ms, 0)

    def test_execute_case_uses_the_run_id_as_the_opaque_trial_id(self) -> None:
        code = ("import os; print(os.environ['HARNESS_TRIAL_ID'])")
        report = CommandHarnessExecutor([sys.executable, "-c", code]).execute_case(
            _harness(), Task.from_text("task", task_id="t1"), self.context())
        self.assertEqual(report.outputs["answer"].strip(), "run0123456789")

    def test_execute_case_raises_on_protocol_error_and_timeout(self) -> None:
        task = Task.from_text("task", task_id="t1")
        with self.assertRaisesRegex(RuntimeError, "protocol"):
            CommandHarnessExecutor([sys.executable, "-c", _result_writer("[]")]).execute_case(
                _harness(), task, self.context())
        with self.assertRaisesRegex(RuntimeError, "seconds"):
            CommandHarnessExecutor([sys.executable, "-c", "import time; time.sleep(30)"], 0.5).execute_case(
                _harness(), task, self.context())

    def test_execute_case_returns_a_bounded_report_on_output_overflow(self) -> None:
        limits = ProcessLimits(timeout_seconds=30, max_output_bytes=100_000)
        executor = CommandHarnessExecutor([sys.executable, "-c", "import sys; sys.stdout.write('x' * 20_000_000)"],
                                          limits=limits)
        report = executor.execute_case(_harness(), Task.from_text("task", task_id="t1"), self.context())
        self.assertEqual(report.outputs, {})
        self.assertIn("resource_limit", report.failures)

    def test_execute_case_needs_a_workspace(self) -> None:
        context = RunContext("e", "r", "c", "v", 0, 1, None)
        with self.assertRaises(RuntimeError):
            CommandHarnessExecutor(["x"]).execute_case(_harness(), Task.from_text("t"), context)


if __name__ == "__main__":
    unittest.main()
