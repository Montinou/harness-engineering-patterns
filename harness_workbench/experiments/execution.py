"""Fresh execution contexts and explicit, opt-in subprocess adapters.

A temporary directory is isolation of state, NOT a security sandbox. Run untrusted
agents in containers/VMs supplied by your adapter. This module never claims that
filesystem paths, an environment allowlist, or shell=False are a security boundary.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from harness_workbench.core.executor import BehavioralReport, MockExecutor, Task, TraceStep
from harness_workbench.core.harness import Harness
from harness_workbench.experiments.models import canonical, relative_path

MAX_OUTPUT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Context:
    workspace: Path
    run_id: str
    seed: int
    repetition: int


TrialExecutor = Callable[[Harness, Task, Context], BehavioralReport]


def populate(directory: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        dest = directory / relative_path(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")


def run_command(argv: list[str], workspace: Path, payload: dict, *, timeout: float,
                env_allow: list[str] | None = None) -> tuple[int, str, str]:
    """No shell, bounded captured output, and a killed process group on POSIX timeout."""
    runtime = workspace / ".runtime"
    runtime.mkdir(exist_ok=True)
    home, tmp = runtime / "home", runtime / "tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL") if key in os.environ}
    env.update({key: os.environ[key] for key in (env_allow or []) if key in os.environ})
    # Reset home/cache/temp AFTER the allowlist to prevent accidental state sharing.
    env.update(HOME=str(home), USERPROFILE=str(home), TMPDIR=str(tmp), TMP=str(tmp), TEMP=str(tmp),
               XDG_CACHE_HOME=str(runtime / "cache"), PYTHONHASHSEED=str(payload.get("seed", 0) % (2**32)))
    argv = [part.replace("{python}", sys.executable).replace("{workspace}", str(workspace)) for part in argv]
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(argv, cwd=workspace, env=env, stdin=subprocess.PIPE,
                                stdout=stdout, stderr=stderr, start_new_session=(os.name == "posix"))
        try:
            data = canonical(payload).encode("utf-8")
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    proc.communicate(data, timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    data = None
                    if stdout.tell() > MAX_OUTPUT_BYTES or stderr.tell() > MAX_OUTPUT_BYTES:
                        raise ValueError("command output exceeded 2 MiB")
            # A successful parent must not leave background children reading the
            # next trial's artifacts. Windows requires an external job/container.
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except BaseException:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            proc.wait()
            raise
        streams = []
        for stream in (stdout, stderr):
            if stream.tell() > MAX_OUTPUT_BYTES:
                raise ValueError("command output exceeded 2 MiB")
            stream.seek(0)
            streams.append(stream.read().decode("utf-8", errors="replace"))
    return proc.returncode, streams[0], streams[1]


def build_executor(backend: dict, *, allow_commands: bool = False) -> TrialExecutor:
    kind = backend.get("kind", "mock")
    if kind == "command" and not allow_commands:
        raise ValueError("command execution requires --allow-commands; use only trusted code")
    if kind == "custom":
        raise ValueError("custom backends require a Python executor callable")

    def execute(harness: Harness, task: Task, context: Context) -> BehavioralReport:
        for filename, expected in backend.get("files", {}).items():
            if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != expected:
                raise ValueError("declared adapter code changed after manifest was frozen")
        if kind == "mock":
            report = MockExecutor().execute(harness, task)
            report.metadata.update(usage_known=False, synthetic=True)
            return report
        if kind == "anthropic":
            from harness_workbench.core.anthropic_backend import AnthropicClient, AnthropicHarnessExecutor
            # New client/executor per trial. No conversation or memory is reused.
            executor = AnthropicHarnessExecutor(
                AnthropicClient.from_env(), solver_model=backend["solver_model"],
                judge_model=backend["judge_model"], max_tokens=backend.get("max_tokens", 4096),
            )
            report = executor.execute(harness, task)
            report.metadata["usage_known"] = True
            return report
        if kind != "command":
            raise ValueError(f"unsupported backend: {kind}")
        started = time.monotonic()
        request = {"harness": harness.to_dict(), "task": task.to_dict(), "seed": context.seed,
                   "run_id": context.run_id, "repetition": context.repetition,
                   "workspace": str(context.workspace)}
        # No checks, expected answers, other variants or split labels in the request.
        code, out, _err = run_command(backend["command"], context.workspace, request,
                                     timeout=backend.get("timeout", 120), env_allow=backend.get("env_allow", []))
        if code:
            raise RuntimeError(f"executor command exited {code}")
        result = json.loads(out)
        if not isinstance(result, dict) or not isinstance(result.get("output"), str):
            raise ValueError("executor must emit a JSON object with a string output")
        tokens = result.get("cost_tokens", 0)
        score = result.get("behavioral_score", 0.0)
        if type(tokens) is not int or tokens < 0 or isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("invalid executor usage or diagnostic score")
        traces = [TraceStep(**trace) for trace in result.get("traces", [])]
        metadata = dict(result.get("metadata", {}))
        metadata["usage_known"] = "cost_tokens" in result
        return BehavioralReport(harness.id, harness.version, task.id, score, traces=traces,
                                failures=list(result.get("failures", [])), cost_tokens=tokens,
                                latency_ms=int((time.monotonic() - started) * 1000), output=result["output"],
                                metadata=metadata)
    return execute
