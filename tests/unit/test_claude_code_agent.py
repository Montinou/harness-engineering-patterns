"""Claude Code adapter: argv, generated plugin, allowlisted environment and result mapping.

Every test drives the adapter against a fake ``claude`` executable that records what it was given and
prints canned JSON. Nothing here touches the network or needs a key; the key is a literal canary.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness_workbench.experiments import claude_code_agent as agent
from harness_workbench.experiments.backends import SRT_REQUIRED, LocalBackend, ProcessLimits
from harness_workbench.experiments.command import CLAUDE_CODE_AGENT, CommandHarnessExecutor
from harness_workbench.experiments.contracts import PublicTrialRequest
from harness_workbench.experiments.status import ExecutionStatus

ADAPTER = Path(agent.__file__).resolve()
KEY_CANARY = "canary-gateway-key-0000"
PINNED = "2.1.316"
MODEL = "anthropic/claude-haiku-5.5"

FAKE_CLAUDE = """#!%(python)s
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
behavior = json.load(open(os.path.join(here, "behavior.json")))
if sys.argv[1:] == ["--version"]:
    print(behavior.get("version", "%(pinned)s") + " (Claude Code)")
    sys.exit(0)
record = {"argv": sys.argv[1:], "env": dict(os.environ), "stdin": sys.stdin.read(), "cwd": os.getcwd()}
json.dump(record, open(os.path.join(here, "record.json"), "w"))
if "sleep" in behavior:
    time.sleep(behavior["sleep"])
sys.stdout.write(behavior.get("stdout", ""))
sys.exit(behavior.get("exit", 0))
"""

OK_JSON = {
    "type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 6,
    "total_cost_usd": 0.0026,
    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30,
              "output_tokens": 40},
    "modelUsage": {MODEL: {"costUSD": 0.0026}},
}

HARNESS = {
    "system_prompt": "SYSTEM-TEXT",
    "guardrails": ["GUARD-ONE", "GUARD-TWO"],
    "memory_config": {"window": 3, "strategy": "recent"},
    "tools": [
        {"name": "Bash", "description": "run", "schema": {}, "guidance": "BASH-GUIDE"},
        {"name": "Read", "description": "read", "schema": {}, "guidance": ""},
    ],
    "subagents": [{"name": "checker", "role": "Checks the answer", "system_prompt": "CHECK-PROMPT",
                   "tools": ["Read"]}],
    "skills": [{"name": "verify-plan", "trigger": "When a plan is needed", "action": "Write the plan first.",
                "kind": "domain"}],
    "scripts": [{"name": "lint.sh", "language": "bash", "body": "echo lint\n"}],
    "plugins": [],
}
TASK = {"description": "Solve the problem.", "domain": "competitive-programming",
        "constraints": ["be brief"], "trial_id": "abc123"}


def _section_positions(prompt: str, markers: list[str]) -> list[int]:
    return [prompt.index(marker) for marker in markers]


class AdapterCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        for name in ("control", "home", "tmp", "workspace", "bin"):
            (self.base / name).mkdir()
        self.claude = self.base / "bin" / "claude"
        self.claude.write_text(FAKE_CLAUDE % {"python": sys.executable, "pinned": PINNED})
        self.claude.chmod(0o755)
        self.set_behavior(stdout=json.dumps(OK_JSON))
        self.harness = json.loads(json.dumps(HARNESS))
        self.task = dict(TASK)
        self.result = self.base / "control" / "result.json"

    def set_behavior(self, **behavior: object) -> None:
        (self.base / "bin" / "behavior.json").write_text(json.dumps(behavior))

    def record(self) -> dict:
        return json.loads((self.base / "bin" / "record.json").read_text())

    def run_adapter(self, *, extra_env: dict[str, str] | None = None, extra_args: list[str] | None = None,
                    timeout: float = 60.0, key: str | None = KEY_CANARY) -> subprocess.CompletedProcess:
        (self.base / "control" / "harness.json").write_text(json.dumps(self.harness))
        (self.base / "control" / "task.json").write_text(json.dumps(self.task))
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.base / "home"),
               "TMPDIR": str(self.base / "tmp"), "ANTHROPIC_BASE_URL": "https://gateway.example.test",
               "HTTPS_PROXY": "http://127.0.0.1:9", "LEAKED_PARENT_VAR": "must-not-reach-claude",
               "PYTHONPATH": "/nonexistent"}
        if key is not None:
            env["ANTHROPIC_API_KEY"] = key
        env.update(extra_env or {})
        argv = [sys.executable, "-I", str(ADAPTER),
                "--harness", str(self.base / "control" / "harness.json"),
                "--task", str(self.base / "control" / "task.json"),
                "--workspace", str(self.base / "workspace"), "--result", str(self.result),
                "--model", MODEL, "--max-budget-usd", "0.50", "--claude", str(self.claude),
                "--claude-version", PINNED, *(extra_args or [])]
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)

    def result_data(self) -> dict:
        return json.loads(self.result.read_text())


class InvocationTests(AdapterCase):
    def test_argv_has_isolation_flags_and_no_bare(self) -> None:
        proc = self.run_adapter()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.record()["argv"]
        self.assertNotIn("--bare", argv)
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--model") + 1], MODEL)
        # An empty --setting-sources value is its own argv element, not a missing argument.
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.50")

    def test_components_map_to_flags(self) -> None:
        proc = self.run_adapter()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record = self.record()
        argv = record["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Bash,Read,Skill,Agent")
        agents = json.loads(argv[argv.index("--agents") + 1])
        self.assertEqual(agents, {"checker": {"description": "Checks the answer", "prompt": "CHECK-PROMPT",
                                              "tools": ["Read"]}})
        plugin = Path(argv[argv.index("--plugin-dir") + 1])
        manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
        self.assertTrue(manifest["name"] and manifest["version"])
        skill = (plugin / "skills" / "verify-plan" / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\n"))
        self.assertIn('name: "verify-plan"', skill)
        self.assertIn('description: "When a plan is needed"', skill)
        self.assertTrue(skill.rstrip().endswith("Write the plan first."))
        script = plugin / "scripts" / "lint.sh"
        self.assertEqual(script.read_text(), "echo lint\n")
        prompt = argv[argv.index("--append-system-prompt") + 1]
        order = _section_positions(prompt, ["SYSTEM-TEXT", "GUARD-ONE", "GUARD-TWO", '"window"', "BASH-GUIDE",
                                            str(script)])
        self.assertEqual(order, sorted(order))
        self.assertEqual(len(set(order)), len(order))
        # The task goes through stdin: a variadic flag in front of it can never swallow it.
        self.assertIn("Solve the problem.", record["stdin"])
        self.assertIn("be brief", record["stdin"])
        self.assertEqual(record["cwd"], str(self.base / "workspace"))

    def test_scripts_wording_follows_the_enabled_tools(self) -> None:
        def prompt() -> str:
            self.assertEqual(self.run_adapter().returncode, 0)
            argv = self.record()["argv"]
            return argv[argv.index("--append-system-prompt") + 1]

        self.assertIn("run them with the Bash tool", prompt())
        self.harness["tools"] = [{"name": "Read", "guidance": ""}]
        text = prompt()
        self.assertNotIn("Bash", text)
        self.assertIn("read them with the Read tool", text)
        self.assertIn("lint.sh", text)
        self.harness["tools"] = [{"name": "Grep", "guidance": ""}]
        text = prompt()
        self.assertNotIn("Bash", text)
        self.assertNotIn("run them", text)
        self.assertIn("lint.sh", text)

    def test_no_skills_or_subagents_means_no_skill_or_agent_tool(self) -> None:
        self.harness["skills"], self.harness["subagents"], self.harness["scripts"] = [], [], []
        self.assertEqual(self.run_adapter().returncode, 0)
        argv = self.record()["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Bash,Read")
        self.assertNotIn("--agents", argv)
        self.assertNotIn("--plugin-dir", argv)

    def test_empty_tool_list_disables_tools(self) -> None:
        self.harness["tools"], self.harness["skills"], self.harness["subagents"] = [], [], []
        self.assertEqual(self.run_adapter().returncode, 0)
        argv = self.record()["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")

    def test_build_invocation_returns_argv_env_and_plugin_dir(self) -> None:
        import argparse

        args = argparse.Namespace(model=MODEL, max_budget_usd="0.50", claude="/x/claude",
                                  claude_version=PINNED)
        home = self.base / "home" / "h1"
        home.mkdir()
        argv, env, plugin = agent.build_invocation(self.harness, self.task, self.base / "workspace", home, args)
        self.assertEqual(argv[0], "/x/claude")
        self.assertTrue((plugin / "skills" / "verify-plan" / "SKILL.md").is_file())
        self.assertEqual(env["HOME"], str(home))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(home / ".claude"))

    def test_supported_components(self) -> None:
        self.assertEqual(agent.SUPPORTED, ("system_prompt", "guardrails", "memory_config", "tools",
                                           "subagents", "skills", "scripts"))

    def test_adapter_imports_nothing_from_the_package(self) -> None:
        tree = ast.parse(ADAPTER.read_text())
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        self.assertFalse([m for m in modules if m.split(".")[0] in ("harness_workbench", "tests")], modules)


class EnvironmentTests(AdapterCase):
    def test_env_is_allowlisted(self) -> None:
        proc = self.run_adapter()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = self.record()["env"]
        allowed = {
            "PATH", "HOME", "TMPDIR", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_TMPDIR", "ANTHROPIC_BASE_URL",
            "ANTHROPIC_API_KEY", "HTTPS_PROXY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
            "DISABLE_AUTOUPDATER", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
        }
        # Python may add LC_CTYPE when it coerces the C locale; the adapter never sets it.
        extra = set(env) - allowed - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
        self.assertEqual(extra, set())
        self.assertNotIn("LEAKED_PARENT_VAR", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], KEY_CANARY)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://gateway.example.test")
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:9")
        for name in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            self.assertEqual(env[name], MODEL)
        self.assertEqual(env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
        self.assertTrue(env["PATH"].split(os.pathsep)[0] == os.path.dirname(sys.executable))
        # HOME is fresh and inside the trial; the config dir and Claude's tmp live under it.
        trial_home = Path(env["HOME"])
        self.assertNotEqual(trial_home, self.base / "home")
        self.assertTrue(str(trial_home).startswith(str(self.base)))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(trial_home / ".claude"))
        tmp = Path(env["CLAUDE_CODE_TMPDIR"])
        self.assertTrue(str(tmp).startswith(str(self.base)))
        self.assertTrue(tmp.is_dir())

    def test_default_base_url_is_the_gateway_and_missing_proxy_is_not_invented(self) -> None:
        (self.base / "control").mkdir(exist_ok=True)
        proc = self.run_adapter(extra_env={"ANTHROPIC_BASE_URL": ""})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # An empty value is treated as unset and falls back to the pinned Gateway URL.
        self.assertEqual(self.record()["env"]["ANTHROPIC_BASE_URL"], agent.DEFAULT_BASE_URL)
        self.assertEqual(agent.DEFAULT_BASE_URL, "https://ai-gateway.vercel.sh")

    def test_missing_api_key_exits_3_without_starting_claude(self) -> None:
        record = self.base / "bin" / "record.json"
        proc = self.run_adapter(key=None)
        self.assertEqual(proc.returncode, 3)
        self.assertFalse(record.exists())
        self.assertFalse(self.result.exists())
        self.assertNotIn(KEY_CANARY, proc.stderr + proc.stdout)


class FailureTests(AdapterCase):
    def test_unknown_tool_exits_3(self) -> None:
        self.harness["tools"].append({"name": "WebFetch", "description": "", "schema": {}, "guidance": ""})
        proc = self.run_adapter()
        self.assertEqual(proc.returncode, 3)
        self.assertIn("WebFetch", proc.stderr)
        self.assertFalse(self.result.exists())
        self.assertFalse((self.base / "bin" / "record.json").exists())

    def test_unknown_subagent_tool_and_malformed_components_exit_3(self) -> None:
        cases = {
            "subagent tool": lambda h: h["subagents"][0].__setitem__("tools", ["Teleport"]),
            "skill name with a slash": lambda h: h["skills"][0].__setitem__("name", "../escape"),
            "script name with a slash": lambda h: h["scripts"][0].__setitem__("name", "a/b"),
            "duplicate skill": lambda h: h["skills"].append(dict(h["skills"][0])),
            "guardrails not a list": lambda h: h.__setitem__("guardrails", "text"),
            "tool not an object": lambda h: h["tools"].append("Bash"),
            "empty trigger": lambda h: h["skills"][0].__setitem__("trigger", ""),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self.harness = json.loads(json.dumps(HARNESS))
                mutate(self.harness)
                proc = self.run_adapter()
                self.assertEqual(proc.returncode, 3, proc.stderr)
                self.assertFalse(self.result.exists())

    def test_unreadable_harness_file_exits_3(self) -> None:
        self.run_adapter()  # writes the control files
        (self.base / "control" / "harness.json").write_text("{not json")
        proc = subprocess.run(
            [sys.executable, "-I", str(ADAPTER), "--harness", str(self.base / "control" / "harness.json"),
             "--task", str(self.base / "control" / "task.json"), "--workspace", str(self.base / "workspace"),
             "--result", str(self.result), "--model", MODEL, "--max-budget-usd", "0.5",
             "--claude", str(self.claude), "--claude-version", PINNED],
            env={"HOME": str(self.base / "home"), "ANTHROPIC_API_KEY": KEY_CANARY}, capture_output=True,
            text=True)
        self.assertEqual(proc.returncode, 3)

    def test_wrong_cli_version_exits_3(self) -> None:
        self.set_behavior(stdout=json.dumps(OK_JSON), version="2.1.999")
        proc = self.run_adapter()
        self.assertEqual(proc.returncode, 3)
        self.assertIn("2.1.999", proc.stderr)
        self.assertFalse(self.result.exists())
        self.assertFalse((self.base / "bin" / "record.json").exists())

    def test_missing_cli_exits_3(self) -> None:
        self.claude.unlink()
        proc = self.run_adapter()
        self.assertEqual(proc.returncode, 3)
        self.assertFalse(self.result.exists())

    def test_auth_error_exits_3(self) -> None:
        cases = [
            {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
             "result": "Invalid API key - Please run /login", "usage": {}, "modelUsage": {}},
            {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 402,
             "result": "Payment required", "usage": {}, "modelUsage": {}},
            {"type": "result", "subtype": "success", "is_error": True,
             "result": "Invalid API key - Please run /login", "usage": {}, "modelUsage": {}},
        ]
        for canned in cases:
            with self.subTest(canned["result"], status=canned.get("api_error_status")):
                self.set_behavior(stdout=json.dumps(canned), exit=1)
                proc = self.run_adapter()
                self.assertEqual(proc.returncode, 3)
                self.assertFalse(self.result.exists())
                self.assertNotIn(KEY_CANARY, proc.stderr)

    def test_non_json_stdout_exits_3(self) -> None:
        self.set_behavior(stdout="Segmentation noise", exit=1)
        proc = self.run_adapter()
        self.assertEqual(proc.returncode, 3)
        self.assertFalse(self.result.exists())

    def test_a_success_answer_that_mentions_auth_words_is_not_an_auth_error(self) -> None:
        canned = dict(OK_JSON, result="The Invalid API key error is unrelated to this unauthorized sketch")
        self.set_behavior(stdout=json.dumps(canned))
        self.assertEqual(self.run_adapter().returncode, 0)


class ResultMappingTests(AdapterCase):
    def test_success_result_file(self) -> None:
        proc = self.run_adapter()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.result_data()
        self.assertEqual(data["outputs"]["answer"], "done")
        details = data["outputs"]["details"]
        self.assertEqual((details["subtype"], details["num_turns"], details["total_cost_usd"],
                          details["is_error"]), ("success", 6, 0.0026, False))
        self.assertEqual(data["model"], MODEL)
        self.assertEqual(details["claude_version"], PINNED)
        self.assertEqual(details["claude_path"], str(self.claude))
        self.assertEqual(details["claude_sha256"], hashlib.sha256(self.claude.read_bytes()).hexdigest())

    def test_api_key_is_redacted_from_the_result_file(self) -> None:
        leaked = dict(OK_JSON, result=f"ANTHROPIC_API_KEY={KEY_CANARY}\nand again {KEY_CANARY}",
                      modelUsage={MODEL: {"costUSD": 0.0026}, KEY_CANARY: {}})
        self.set_behavior(stdout=json.dumps(leaked))
        proc = self.run_adapter()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = self.result.read_text()
        self.assertNotIn(KEY_CANARY, text)
        self.assertNotIn(KEY_CANARY, proc.stdout + proc.stderr)
        data = json.loads(text)
        self.assertEqual(data["outputs"]["answer"], "ANTHROPIC_API_KEY=[redacted]\nand again [redacted]")

    def test_error_subtypes_write_result(self) -> None:
        for subtype in ("error_max_turns", "error_max_budget_usd", "error_during_execution"):
            with self.subTest(subtype):
                canned = dict(OK_JSON, subtype=subtype, is_error=True)
                canned.pop("result")
                self.set_behavior(stdout=json.dumps(canned), exit=1)
                if self.result.exists():
                    self.result.unlink()
                proc = self.run_adapter()
                self.assertEqual(proc.returncode, 0, proc.stderr)
                data = self.result_data()
                self.assertEqual(data["outputs"]["details"]["subtype"], subtype)
                self.assertTrue(data["outputs"]["details"]["is_error"])
                self.assertEqual(data["outputs"]["answer"], "")

    def test_result_event_list_is_accepted(self) -> None:
        self.set_behavior(stdout=json.dumps([{"type": "system"}, OK_JSON]))
        self.assertEqual(self.run_adapter().returncode, 0)
        self.assertEqual(self.result_data()["outputs"]["answer"], "done")

    def test_model_reporting(self) -> None:
        one = agent.map_result(dict(OK_JSON, modelUsage={MODEL: {}}), MODEL)
        self.assertEqual(one["model"], MODEL)
        two = agent.map_result(dict(OK_JSON, modelUsage={MODEL: {}, "anthropic/claude-opus-5.5": {}}), MODEL)
        self.assertEqual(two["model"], "anthropic/claude-haiku-5.5+anthropic/claude-opus-5.5")
        other = agent.map_result(dict(OK_JSON, modelUsage={"claude-sonnet-5-5": {}}), MODEL)
        self.assertEqual(other["model"], "claude-sonnet-5-5")

    def test_tokens_sum_usage(self) -> None:
        self.assertEqual(agent.map_result(OK_JSON, MODEL)["tokens"], 100)
        partial = agent.map_result(dict(OK_JSON, usage={"input_tokens": 5, "output_tokens": 7}), MODEL)
        self.assertEqual(partial["tokens"], 12)
        missing = agent.map_result({k: v for k, v in OK_JSON.items() if k != "usage"}, MODEL)
        self.assertNotIn("tokens", missing)

    def test_non_finite_cost_is_not_written(self) -> None:
        mapped = agent.map_result(dict(OK_JSON, total_cost_usd=float("nan")), MODEL)
        self.assertIsNone(mapped["outputs"]["details"]["total_cost_usd"])
        json.dumps(mapped, allow_nan=False)


class TimeoutTests(AdapterCase):
    def test_stops_before_executor_timeout(self) -> None:
        self.set_behavior(stdout=json.dumps(OK_JSON), sleep=60)
        started = time.monotonic()
        proc = self.run_adapter(extra_env={"HARNESS_TIMEOUT_SECONDS": "7"},
                                extra_args=["--stop-margin-seconds", "5"], timeout=30)
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 7.0)
        data = self.result_data()
        self.assertEqual(data["outputs"]["details"]["subtype"], "adapter_timeout")
        self.assertTrue(data["outputs"]["details"]["is_error"])
        self.assertEqual(data["model"], MODEL)

    def test_default_margin_is_thirty_seconds(self) -> None:
        self.assertEqual(agent.STOP_MARGIN_SECONDS, 30.0)


class PlaceholderTests(unittest.TestCase):
    """The command executor expands ``{claude_code_agent}`` and mounts the file read-only."""

    class Recording:
        """Remembers what it was asked to run, then runs a no-op instead of the real command."""

        name = "recording"
        python = LocalBackend.python

        def __init__(self) -> None:
            self.inner = LocalBackend()
            self.argv: list[str] = []
            self.mounts = None
            self.env: dict[str, str] = {}

        def capabilities(self):
            # The adapter is only allowed on an isolating backend; this double runs a no-op locally, so it
            # declares the isolation the real backends would provide.
            return SRT_REQUIRED

        def run(self, argv, *, cwd, env, limits, mounts, network=False):
            self.argv, self.mounts, self.env = list(argv), mounts, dict(env)
            return self.inner.run([sys.executable, "-c", "pass"], cwd=cwd, env=env, limits=limits,
                                  mounts=mounts, network=network)

    def request(self) -> PublicTrialRequest:
        return PublicTrialRequest(
            trial_id="9f1c2ab34d5e", task_description="t", domain="d", constraints=(),
            harness_public={}, fixture_hash=None, seed=1, budget={})

    def run_executor(self, argv: list[str], *, timeout: float = 300.0):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.Recording()
            workspace = Path(tmp) / "workspace"
            executor = CommandHarnessExecutor(argv, timeout_seconds=timeout)
            result = executor.execute_trial(self.request(), workspace=workspace, backend=backend,
                                            limits=ProcessLimits(timeout_seconds=timeout))
        return backend, result

    def test_placeholder_expands_and_mounts_read_only(self) -> None:
        backend, result = self.run_executor(["{python}", "-I", "{claude_code_agent}", "--result", "{result_json}"])
        self.assertEqual(result.status, ExecutionStatus.COMPLETED, result.error)
        self.assertEqual(backend.argv[2], str(CLAUDE_CODE_AGENT))
        self.assertEqual(CLAUDE_CODE_AGENT, ADAPTER)
        self.assertTrue(CLAUDE_CODE_AGENT.is_file())
        self.assertIn(CLAUDE_CODE_AGENT, backend.mounts.read_only)
        self.assertNotIn(CLAUDE_CODE_AGENT, backend.mounts.writable)

    def test_other_commands_keep_the_demo_agent_mount_only(self) -> None:
        backend, _ = self.run_executor(["{python}", "{demo_agent}"])
        self.assertEqual(len(backend.mounts.read_only), 1)
        self.assertNotIn(CLAUDE_CODE_AGENT, backend.mounts.read_only)

    def test_executor_hands_the_adapter_its_timeout(self) -> None:
        backend, _ = self.run_executor(["{python}", "{claude_code_agent}"], timeout=123.0)
        self.assertEqual(backend.env["HARNESS_TIMEOUT_SECONDS"], "123.0")


class IsolatedLocalBackend(LocalBackend):
    """Runs locally but declares isolation, so the command executor admits the Claude Code adapter.

    These tests exercise the adapter's exit-code mapping through the executor, not isolation; the refusal on a
    non-isolating backend is tested in tests/unit/test_srt_backend_argv.py and real isolation in the opt-in
    integration tests.
    """

    def capabilities(self):
        return SRT_REQUIRED


class AdapterExitThroughExecutorTests(AdapterCase):
    """The real adapter run by the real command executor: exit 3 must not become a scored trial."""

    def request(self) -> PublicTrialRequest:
        return PublicTrialRequest(
            trial_id="9f1c2ab34d5e", task_description="Solve the problem.", domain="d", constraints=(),
            harness_public=json.loads(json.dumps(HARNESS)), fixture_hash=None, seed=1, budget={})

    def run_through_executor(self, *, claude_version: str, key: str | None = KEY_CANARY,
                             argv: list[str] | None = None):
        argv = argv or ["{python}", "-I", "{claude_code_agent}", "--harness", "{harness_json}",
                        "--task", "{task_json}", "--workspace", "{workspace}", "--result", "{result_json}",
                        "--model", MODEL, "--max-budget-usd", "0.50", "--claude", str(self.claude),
                        "--claude-version", claude_version]
        executor = CommandHarnessExecutor(
            argv, timeout_seconds=60.0,
            env_passthrough=("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"))
        env = {"ANTHROPIC_BASE_URL": "https://gateway.example.test"}
        if key is not None:
            env["ANTHROPIC_API_KEY"] = key
        old = {name: os.environ.get(name) for name in env}
        os.environ.update(env)
        try:
            return executor.execute_trial(
                self.request(), workspace=self.base / "trial" / "workspace", backend=IsolatedLocalBackend(),
                limits=ProcessLimits(timeout_seconds=60.0))
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_wrong_version_exit_3_is_a_non_retryable_execution_error(self) -> None:
        result = self.run_through_executor(claude_version="2.1.999")
        self.assertEqual(result.exit_code, 3, result.stderr_tail)
        self.assertIs(result.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertFalse(result.retryable)
        self.assertEqual(result.outputs, {})
        self.assertIn("exit", result.error or "")

    def test_missing_key_exit_3_is_a_non_retryable_execution_error(self) -> None:
        result = self.run_through_executor(claude_version=PINNED, key="")
        self.assertEqual(result.exit_code, 3, result.stderr_tail)
        self.assertIs(result.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertFalse(result.retryable)

    def test_success_through_the_executor_is_still_completed(self) -> None:
        result = self.run_through_executor(claude_version=PINNED)
        self.assertEqual(result.exit_code, 0, result.stderr_tail)
        self.assertIs(result.status, ExecutionStatus.COMPLETED)
        self.assertEqual(result.outputs["answer"], "done")

    def test_other_commands_keep_a_nonzero_exit_as_task_failure(self) -> None:
        result = self.run_through_executor(
            claude_version=PINNED, argv=["{python}", "-c", "import sys; sys.exit(3)"])
        self.assertIs(result.status, ExecutionStatus.TASK_FAILURE)


if __name__ == "__main__":
    unittest.main()
