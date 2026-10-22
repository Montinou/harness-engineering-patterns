"""Opt-in local command executor for real tool-enabled agent harnesses.

The configured command is trusted code, NOT a security sandbox. The runner
provides a fresh copied fixture/workspace and structured harness/task files.
The command must apply the harness it receives; this adapter does not pretend
that passing JSON automatically rewires the target agent.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_workbench.core.executor import BehavioralReport, Task, TraceStep
from harness_workbench.core.harness import Harness
from harness_workbench.experiments.core import RunContext


@dataclass
class CommandHarnessExecutor:
    argv: list[str]
    timeout_seconds: float = 300.0
    solver_model: str = "external-command"

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "CommandHarnessExecutor":
        argv = config.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise ValueError("command executor requires executor.argv (nonempty string array)")
        timeout = float(config.get("timeout_seconds", 300))
        if not 0 < timeout <= 3600:
            raise ValueError("command executor timeout_seconds must be in (0, 3600]")
        return cls(argv, timeout, str(config.get("model", "external-command")))

    def execute_case(self, harness: Harness, task: Task, context: RunContext) -> BehavioralReport:
        if context.workspace is None:
            raise RuntimeError("command executor needs a workspace path")
        workspace = context.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        # Keep context files outside the fixture so hashes are comparable.
        context_dir = workspace.parent / "control"
        context_dir.mkdir(exist_ok=True)
        harness_file = context_dir / "harness.json"
        task_file = context_dir / "task.json"
        output_file = context_dir / "result.json"
        prompt_file = context_dir / "prompt.txt"
        harness_file.write_text(harness.to_json(), encoding="utf-8")
        task_file.write_text(json.dumps(task.to_dict(), ensure_ascii=False), encoding="utf-8")
        prompt = (
            "Follow the complete harness specification in the attached harness JSON.\n"
            "Harness system prompt: " + harness.system_prompt +
            "\nGuardrails:\n" + "\n".join(harness.guardrails) +
            "\nTask: " + task.description +
            "\nConstraints: " + "; ".join(task.constraints)
        )
        prompt_file.write_text(prompt, encoding="utf-8")
        tokens = {
            "{workspace}": str(workspace), "{harness_json}": str(harness_file),
            "{task_json}": str(task_file), "{prompt_file}": str(prompt_file),
            "{result_json}": str(output_file), "{task}": task.description,
            "{prompt}": prompt, "{seed}": str(context.seed),
        }
        args = [self._expand(argument, tokens) for argument in self.argv]
        started = time.monotonic()
        env = os.environ.copy()
        env.update({
            "HARNESS_WORKSPACE": str(workspace),
            "HARNESS_SPEC_FILE": str(harness_file),
            "HARNESS_TASK_FILE": str(task_file),
            "HARNESS_RESULT_FILE": str(output_file),
            "HARNESS_SEED": str(context.seed),
        })
        finished = subprocess.run(
            args, cwd=workspace, env=env, shell=False,
            capture_output=True, text=True, check=False, timeout=self.timeout_seconds,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        if finished.returncode != 0:
            raise RuntimeError(
                f"agent command failed (exit {finished.returncode}): {finished.stderr[-500:]}"
            )
        data: dict[str, Any] = {}
        if output_file.exists():
            loaded = json.loads(output_file.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("result.json must contain an object")
            data = loaded
        outputs = data.get("outputs", {"answer": finished.stdout})
        if not isinstance(outputs, dict):
            raise ValueError("result.json outputs must be a JSON object")
        return BehavioralReport(
            harness_id=harness.id, harness_version=harness.version, task_id=task.id,
            score=float(data.get("behavioral_score", 0)),
            traces=[TraceStep("command", f"exit 0; stdout={len(finished.stdout)} bytes", True)],
            cost_tokens=int(data.get("tokens", 0)), latency_ms=int(data.get("latency_ms", latency_ms)),
            notes="tool-enabled command adapter; behavioral score is not an acceptance verdict",
            outputs=outputs,
        )

    @staticmethod
    def _expand(template: str, values: dict[str, str]) -> str:
        for key, value in values.items():
            template = template.replace(key, value)
        return template
