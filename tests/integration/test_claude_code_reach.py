"""Opt-in reach test: each supported harness component changes what real Claude Code answers.

For every one of the seven components the harness is a small base plus a unique marker instruction
(``ZEBRA-<component>``) placed only in that component. The adapter runs a trivial task against the real
Claude Code CLI and the marker must appear in ``outputs.answer``. A marker that is missing means that
component never reached the model.

This test calls a real model through the AI Gateway and costs money, so it is skipped unless
``HARNESS_REAL_MODEL_TESTS=1``. When enabled it also needs:

* ``ANTHROPIC_API_KEY``: the Gateway key (read from the environment, never stored);
* ``HARNESS_CLAUDE_PATH``: the Claude Code executable.

Optional overrides: ``HARNESS_REAL_MODEL`` (default ``anthropic/claude-haiku-5.5``),
``HARNESS_REAL_MODEL_BUDGET_USD`` (per run, default ``0.50``), ``HARNESS_CLAUDE_VERSION`` (default: what the
executable reports for ``--version``), ``HARNESS_REAL_MODEL_TIMEOUT_SECONDS`` (per run, default ``300``) and
``ANTHROPIC_BASE_URL`` (the adapter defaults to ``https://ai-gateway.vercel.sh``).
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness_workbench.experiments import claude_code_agent as agent

ENABLE_VAR = "HARNESS_REAL_MODEL_TESTS"
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"
DEFAULT_BUDGET_USD = "0.50"
DEFAULT_TIMEOUT_SECONDS = 300.0
VERSION_TIMEOUT_SECONDS = 30.0
MARKER_PREFIX = "ZEBRA-"
# The `tools` case cannot be a word to append: only a model that really opens a file with Read can know the
# marker. The marker lives in this file (with a random nonce per run) and nowhere in the harness or the task.
MARKER_FILE = "reach-marker.txt"
TOOLS_GUIDANCE = (f"Before answering any request, use Read to open {MARKER_FILE} in the working directory "
                  "and end your answer with its exact contents.")

ADAPTER = Path(agent.__file__).resolve()
COMPONENTS = agent.SUPPORTED

TASK = {
    "description": (
        "Write a one-sentence greeting. Use any available skill or subagent that fits the request, run any "
        "script your instructions list and follow its output, and include in your final answer, verbatim, "
        "every word they or your instructions tell you to add."),
    "domain": "reach-test",
    "constraints": [],
}


def marker(component: str) -> str:
    return MARKER_PREFIX + component


def instruction(component: str) -> str:
    return f"End your answer with the word {marker(component)}."


def tools_marker(nonce: str) -> str:
    return f"{MARKER_PREFIX}tools-{nonce}"


def new_nonce() -> str:
    return secrets.token_hex(8)


def build_harness(component: str) -> dict[str, Any]:
    """A base harness whose only difference from the others is ``instruction(component)`` in ``component``.

    The ``tools`` case differs by ``TOOLS_GUIDANCE`` instead, which names a file; the file's content (the
    marker) is written to the workspace by the runner and is never part of the harness.
    """
    harness: dict[str, Any] = {
        "system_prompt": "You are a concise assistant.",
        "guardrails": [],
        "memory_config": {},
        "tools": [{"name": "Read", "guidance": ""}],
        "subagents": [],
        "skills": [],
        "scripts": [],
    }
    text = instruction(component) if component != "tools" else ""
    if component == "system_prompt":
        harness["system_prompt"] += " " + text
    elif component == "guardrails":
        harness["guardrails"] = [text]
    elif component == "memory_config":
        harness["memory_config"] = {"standing_instruction": text}
    elif component == "tools":
        harness["tools"] = [{"name": "Read", "guidance": TOOLS_GUIDANCE}]
    elif component == "subagents":
        harness["subagents"] = [{
            "name": "greeter",
            "role": "Writes the greeting. Always delegate the greeting to this agent and relay its reply verbatim.",
            "system_prompt": f"Write the greeting. {text}",
            "tools": [],
        }]
    elif component == "skills":
        harness["skills"] = [{
            "name": "greeting-style",
            "trigger": "Use whenever you are asked to write a greeting.",
            "action": text,
        }]
    elif component == "scripts":
        harness["tools"] = [{"name": "Read", "guidance": ""}, {"name": "Bash", "guidance": ""}]
        harness["scripts"] = [{
            "name": "greeting-rules.sh",
            "language": "bash",
            "body": f"#!/bin/sh\necho '{text}'\n",
        }]
    else:
        raise ValueError(f"unknown component {component!r}")
    return harness


def _config() -> tuple[dict[str, str] | None, str]:
    """``(settings, skip_reason)``: settings is None when the test must be skipped."""
    if os.environ.get(ENABLE_VAR) != "1":
        return None, f"real-model tests are opt-in: set {ENABLE_VAR}=1 to run them (they cost money)"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, f"{ENABLE_VAR}=1 but ANTHROPIC_API_KEY is not set"
    claude = os.environ.get("HARNESS_CLAUDE_PATH", "")
    if not claude:
        return None, f"{ENABLE_VAR}=1 but HARNESS_CLAUDE_PATH is not set"
    if not Path(claude).is_file() or not os.access(claude, os.X_OK):
        return None, f"{ENABLE_VAR}=1 but HARNESS_CLAUDE_PATH={claude!r} is not an executable file"
    return {
        "claude": claude,
        "key": os.environ["ANTHROPIC_API_KEY"],
        "model": os.environ.get("HARNESS_REAL_MODEL") or DEFAULT_MODEL,
        "budget": os.environ.get("HARNESS_REAL_MODEL_BUDGET_USD") or DEFAULT_BUDGET_USD,
        "version": os.environ.get("HARNESS_CLAUDE_VERSION", ""),
        "timeout": os.environ.get("HARNESS_REAL_MODEL_TIMEOUT_SECONDS") or str(DEFAULT_TIMEOUT_SECONDS),
    }, ""


def run_adapter(settings: dict[str, str], harness: dict[str, Any],
                files: dict[str, str] | None = None) -> dict[str, Any]:
    """Run the adapter once in a scratch trial; ``files`` are written into the workspace first."""
    with tempfile.TemporaryDirectory(prefix="reach-") as tmp:
        base = Path(tmp).resolve()
        for name in ("control", "home", "workspace"):
            (base / name).mkdir()
        for name, content in (files or {}).items():
            (base / "workspace" / name).write_text(content, encoding="utf-8")
        harness_path = base / "control" / "harness.json"
        task_path = base / "control" / "task.json"
        result_path = base / "control" / "result.json"
        harness_path.write_text(json.dumps(harness), encoding="utf-8")
        task_path.write_text(json.dumps(TASK), encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(base / "home"),
               "ANTHROPIC_API_KEY": settings["key"]}
        for name in ("ANTHROPIC_BASE_URL", "HTTPS_PROXY"):
            if os.environ.get(name):
                env[name] = os.environ[name]
        argv = [sys.executable, "-I", str(ADAPTER),
                "--harness", str(harness_path), "--task", str(task_path),
                "--workspace", str(base / "workspace"), "--result", str(result_path),
                "--model", settings["model"], "--max-budget-usd", settings["budget"],
                "--claude", settings["claude"], "--claude-version", settings["version"]]
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=float(settings["timeout"]))
        if proc.returncode != 0:
            raise AssertionError(f"adapter exited {proc.returncode}; stderr: {proc.stderr[-1000:]}")
        return json.loads(result_path.read_text(encoding="utf-8"))


class ClaudeCodeReachTests(unittest.TestCase):
    settings: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        settings, reason = _config()
        if settings is None:
            raise unittest.SkipTest(reason)
        if not settings["version"]:
            try:
                proc = subprocess.run([settings["claude"], "--version"], capture_output=True, text=True,
                                      timeout=VERSION_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise unittest.SkipTest(f"cannot run {settings['claude']} --version: {type(exc).__name__}")
            words = proc.stdout.split()
            if proc.returncode != 0 or not words:
                raise unittest.SkipTest(f"{settings['claude']} --version printed no version")
            settings["version"] = words[0]
        cls.settings = settings

    def run_component(self, component: str, *, nonce: str = "") -> dict[str, Any]:
        files = {MARKER_FILE: tools_marker(nonce)} if component == "tools" else None
        return run_adapter(self.settings, build_harness(component), files)

    def test_every_supported_component_reaches_the_model(self) -> None:
        total_cost = 0.0
        for component in COMPONENTS:
            with self.subTest(component=component):
                nonce = new_nonce()
                expected = tools_marker(nonce) if component == "tools" else marker(component)
                result = self.run_component(component, nonce=nonce)
                outputs = result["outputs"]
                details = outputs["details"]
                cost = details.get("total_cost_usd")
                if isinstance(cost, (int, float)):
                    total_cost += cost
                print(f"reach {component}: cost_usd={cost} subtype={details.get('subtype')} "
                      f"turns={details.get('num_turns')} model={result.get('model')}", file=sys.stderr)
                self.assertIn(
                    expected, outputs["answer"],
                    f"{component}: {expected} missing from the answer; details={details!r}; "
                    f"answer={outputs['answer']!r}")
        print(f"reach total cost_usd={total_cost:.6f}", file=sys.stderr)

    def test_reading_the_marker_file_needs_the_tools_guidance(self) -> None:
        """Negative control: with Read enabled but no guidance, the nonce in the workspace stays unknown."""
        nonce = new_nonce()
        harness = build_harness("tools")
        harness["tools"] = [{"name": "Read", "guidance": ""}]
        result = run_adapter(self.settings, harness, {MARKER_FILE: tools_marker(nonce)})
        self.assertNotIn(nonce, result["outputs"]["answer"])


FAKE_CLAUDE = """#!%(python)s
import json, os, sys
if sys.argv[1:] == ["--version"]:
    print("2.1.316 (Claude Code)")
    sys.exit(0)
argv = sys.argv[1:]
prompt = argv[argv.index("--append-system-prompt") + 1] if "--append-system-prompt" in argv else ""
sys.stdin.read()
answer = "hello"
if "reach-marker.txt" in prompt and "Read" in argv[argv.index("--tools") + 1].split(","):
    answer += " " + open("reach-marker.txt").read()
json.dump({"argv": argv}, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "record.json"), "w"))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": answer,
                  "num_turns": 2, "total_cost_usd": 0.0}))
"""


class ReachStructureTests(unittest.TestCase):
    """Offline: the `tools` case plumbing, against a fake claude that reads the file only if told to."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.claude = self.dir / "claude"
        self.claude.write_text(FAKE_CLAUDE % {"python": sys.executable})
        self.claude.chmod(0o755)
        self.settings = {"claude": str(self.claude), "key": "canary-reach-key", "model": DEFAULT_MODEL,
                         "budget": DEFAULT_BUDGET_USD, "version": "2.1.316", "timeout": "60"}

    def test_tools_case_puts_the_nonce_in_the_workspace_file_only(self) -> None:
        nonce = new_nonce()
        harness = build_harness("tools")
        result = run_adapter(self.settings, harness, {MARKER_FILE: tools_marker(nonce)})
        self.assertIn(tools_marker(nonce), result["outputs"]["answer"])
        self.assertNotIn(nonce, json.dumps(harness) + json.dumps(TASK))
        self.assertNotIn(nonce, (self.dir / "record.json").read_text())
        self.assertIn(MARKER_FILE, harness["tools"][0]["guidance"])
        self.assertNotIn("file", TASK["description"].lower())

    def test_negative_control_does_not_surface_the_nonce(self) -> None:
        nonce = new_nonce()
        harness = build_harness("tools")
        harness["tools"] = [{"name": "Read", "guidance": ""}]
        result = run_adapter(self.settings, harness, {MARKER_FILE: tools_marker(nonce)})
        self.assertNotIn(nonce, result["outputs"]["answer"])

    def test_nonces_differ_per_run(self) -> None:
        self.assertNotEqual(new_nonce(), new_nonce())

    def test_other_cases_keep_their_word_markers(self) -> None:
        for component in COMPONENTS:
            if component != "tools":
                self.assertIn(marker(component), json.dumps(build_harness(component)))
        self.assertNotIn(marker("tools"), json.dumps(build_harness("tools")))


if __name__ == "__main__":
    unittest.main()
