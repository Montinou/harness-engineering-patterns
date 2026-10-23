"""Opt-in local command executor for real tool-enabled agent harnesses.

The configured command is trusted code, NOT a security sandbox. The supervisor
hands it a public trial request (the eight executable harness components and the
task text, never the harness identity, lineage, notes or the task's split), a
fresh workspace, a fresh HOME and TMPDIR, and an allowlisted environment: the
minimal environment plus any variable the manifest declares in
``solver_env_passthrough``. Nothing else of the parent environment is inherited,
and environment *values* are never written to control files, results or logs.

Latency is measured by the supervisor. Whatever the command reports about its
own latency is kept separately as ``adapter_latency_ms``; token counts are only
``adapter_reported`` evidence. The command must apply the harness it receives;
this adapter does not pretend that passing JSON rewires the target agent.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_workbench.core.executor import BehavioralReport, Task, TraceStep
from harness_workbench.core.harness import ALL_COMPONENTS, Harness
from harness_workbench.experiments.backends import (
    ExecutionBackend, LocalBackend, Mounts, ProcessLimits, ProcessOutcome, minimal_env,
)
from harness_workbench.experiments.contracts import ExecutionResult, Measurement, PublicTrialRequest
from harness_workbench.experiments.core import RunContext
from harness_workbench.experiments.manifest import EnvironmentConfig, ExecutorConfig
from harness_workbench.experiments.status import ExecutionStatus, Provenance, USABLE_EXECUTION
from harness_workbench.experiments.validation import (
    ValidationError, assert_finite_tree, json_dumps_strict, json_loads_strict, require_finite, require_int,
)

__all__ = ["CommandHarnessExecutor"]

# The synthetic example agent. It is a script, not an importable module: importing it would run it.
DEMO_AGENT = Path(__file__).resolve().with_name("demo_agent.py")

TAIL_CHARS = 2000
MAX_REPORTED = 10**12  # upper bound for adapter-reported tokens and latency_ms

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# PYTHON* variables change what the interpreter imports and runs; the minimal
# environment sets the three it needs itself, so a declared name can only be an attack surface.
_FORBIDDEN_PASSTHROUGH = re.compile(r"^PYTHON", re.I)
# minimal_env applies the passthrough after HOME, TMPDIR and XDG_CACHE_HOME, so declaring one of
# them would replace the fresh per-trial directory. (HARNESS_* are set after the passthrough anyway.)
_RESERVED_PASSTHROUGH = re.compile(r"^(HOME|TMPDIR|XDG_CACHE_HOME)$")


class _ProtocolError(Exception):
    """The command's result file violates the adapter protocol."""


def _check_passthrough(names: Any) -> tuple[str, ...]:
    if isinstance(names, str) or not isinstance(names, (list, tuple)):
        raise ValidationError("env_passthrough must be a list of environment variable names")
    checked: list[str] = []
    for name in names:
        if not isinstance(name, str) or not _ENV_NAME.match(name):
            raise ValidationError(f"env_passthrough entry {name!r} is not an environment variable name")
        if _FORBIDDEN_PASSTHROUGH.match(name):
            raise ValidationError(
                f"env_passthrough entry {name!r} is refused: PYTHON* variables alter the interpreter "
                "and are never passed to the solver")
        if _RESERVED_PASSTHROUGH.match(name):
            raise ValidationError(
                f"env_passthrough entry {name!r} is refused: the supervisor sets this variable itself")
        checked.append(name)
    return tuple(checked)


def _expand(template: str, values: dict[str, str]) -> str:
    """Single-pass placeholder expansion: a substituted value is never expanded again."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def _read_result(path: Path, max_bytes: int) -> Any:
    """Parse ``result.json`` strictly; ``None`` when absent. Raises ``_ProtocolError``."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:  # includes ELOOP: the file is a symlink
        raise _ProtocolError(f"result.json cannot be opened as a regular file ({type(exc).__name__})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _ProtocolError("result.json is not a regular file")
        if info.st_size > max_bytes:
            raise _ProtocolError(f"result.json is larger than {max_bytes} bytes")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise _ProtocolError(f"result.json is larger than {max_bytes} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _ProtocolError("result.json is not valid UTF-8") from None
    try:
        data = json_loads_strict(text)
        # json_loads_strict rejects the NaN/Infinity constants but lets an overflow literal such as
        # 1e400 parse to inf, so every parse is followed by a finite-tree walk.
        assert_finite_tree(data)
    except (ValueError, RecursionError) as exc:
        raise _ProtocolError(f"result.json is not strict JSON: {exc}") from None
    if not isinstance(data, dict):
        raise _ProtocolError("result.json must contain a JSON object")
    if "outputs" in data and not isinstance(data["outputs"], dict):
        raise _ProtocolError("result.json outputs must be a JSON object")
    for name in ("tokens", "latency_ms"):
        if name in data:
            try:
                require_int(name, data[name], minimum=0, maximum=MAX_REPORTED)
            except ValidationError as exc:
                raise _ProtocolError(str(exc)) from None
    return data


def _fresh_dir(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


@dataclass
class CommandHarnessExecutor:
    argv: list[str]
    timeout_seconds: float = 300.0
    solver_model: str = "external-command"
    env_passthrough: tuple[str, ...] = ()
    supports: frozenset[str] = frozenset(ALL_COMPONENTS)
    # Process limits for execute_case, which has no limits parameter; execute_trial takes them explicitly.
    limits: ProcessLimits | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.argv, list) or not self.argv or not all(isinstance(x, str) and x for x in self.argv):
            raise ValueError("command executor requires executor.argv (nonempty string array)")
        self.env_passthrough = _check_passthrough(self.env_passthrough)
        self.supports = frozenset(self.supports)

    @classmethod
    def from_config(cls, config: ExecutorConfig, environment: EnvironmentConfig) -> "CommandHarnessExecutor":
        if not config.argv:
            raise ValueError("command executor requires executor.argv (nonempty string array)")
        env_limits = ProcessLimits.from_config(environment.limits)
        limits = ProcessLimits(
            timeout_seconds=min(config.timeout_seconds, env_limits.timeout_seconds),
            max_output_bytes=env_limits.max_output_bytes,
            max_files=env_limits.max_files,
            max_total_bytes=env_limits.max_total_bytes,
        )
        return cls(
            list(config.argv), config.timeout_seconds, config.model,
            tuple(environment.solver_env_passthrough), frozenset(config.supports), limits,
        )

    def execute_trial(self, request: PublicTrialRequest, *, workspace: Path, backend: ExecutionBackend,
                      limits: ProcessLimits) -> ExecutionResult:
        return self._run(request, workspace, backend, limits)[0]

    def _run(self, request: PublicTrialRequest, workspace: Path, backend: ExecutionBackend,
             limits: ProcessLimits) -> tuple[ExecutionResult, dict[str, Any]]:
        workspace = Path(workspace)
        parent = workspace.parent
        control, home, tmp = parent / "control", parent / "home", parent / "tmp"
        workspace.mkdir(parents=True, exist_ok=True)
        control.mkdir(parents=True, exist_ok=True)
        # HOME and TMPDIR belong to this trial alone; nothing a previous trial left survives.
        _fresh_dir(home)
        _fresh_dir(tmp)
        harness_file, task_file = control / "harness.json", control / "task.json"
        prompt_file, result_file = control / "prompt.txt", control / "result.json"
        if result_file.is_symlink() or result_file.exists():
            result_file.unlink()  # a result left by an earlier trial is never read
        harness_file.write_text(json_dumps_strict(request.harness_public, ensure_ascii=False), encoding="utf-8")
        task_file.write_text(json_dumps_strict({
            "description": request.task_description, "domain": request.domain,
            "constraints": list(request.constraints), "trial_id": request.trial_id,
        }, ensure_ascii=False), encoding="utf-8")
        public = request.harness_public
        prompt = (
            "Follow the complete harness specification in the attached harness JSON.\n"
            "Harness system prompt: " + str(public.get("system_prompt", "")) +
            "\nGuardrails:\n" + "\n".join(str(item) for item in public.get("guardrails", [])) +
            "\nTask: " + request.task_description +
            "\nConstraints: " + "; ".join(request.constraints)
        )
        prompt_file.write_text(prompt, encoding="utf-8")
        values = {
            "workspace": str(workspace), "harness_json": str(harness_file), "task_json": str(task_file),
            "prompt_file": str(prompt_file), "result_json": str(result_file), "task": request.task_description,
            "prompt": prompt, "seed": str(request.seed),
            "python": backend.python, "demo_agent": str(DEMO_AGENT), "trial_id": request.trial_id,
        }
        args = [_expand(argument, values) for argument in self.argv]
        env = minimal_env(home=home, tmp=tmp, passthrough=self.env_passthrough)
        env.update({
            "HARNESS_WORKSPACE": str(workspace), "HARNESS_SPEC_FILE": str(harness_file),
            "HARNESS_TASK_FILE": str(task_file), "HARNESS_RESULT_FILE": str(result_file),
            "HARNESS_SEED": str(request.seed), "HARNESS_TRIAL_ID": request.trial_id,
        })
        mounts = Mounts(read_only=(DEMO_AGENT,), writable=(workspace, control, home, tmp))
        outcome = backend.run(args, cwd=workspace, env=env, limits=limits, mounts=mounts)
        return self._result(request, outcome, result_file, limits)

    @staticmethod
    def _result(request: PublicTrialRequest, outcome: ProcessOutcome, result_file: Path,
                limits: ProcessLimits) -> tuple[ExecutionResult, dict[str, Any]]:
        common: dict[str, Any] = {
            "trial_id": request.trial_id,
            "exit_code": outcome.exit_code,
            "stdout_tail": outcome.stdout[-TAIL_CHARS:],
            "stderr_tail": outcome.stderr[-TAIL_CHARS:],
            "latency_ms": Measurement.measured(outcome.duration_ms),
            "artifact_hash": None,
        }
        no_usage = {"tokens": Measurement.unknown(), "adapter_latency_ms": Measurement.unknown(),
                    "returned_model": None}
        if outcome.status not in USABLE_EXECUTION:
            # Timeout, resource limit and spawn failure keep their status, error and retryable flag;
            # a result file written by a killed process is not trusted.
            return ExecutionResult(status=outcome.status, outputs={}, error=outcome.error,
                                   retryable=outcome.retryable, **common, **no_usage), {}
        try:
            data = _read_result(result_file, limits.max_output_bytes)
        except _ProtocolError as exc:
            return ExecutionResult(
                status=ExecutionStatus.EXECUTION_ERROR, outputs={}, error=f"adapter protocol error: {exc}",
                retryable=False, **common, **no_usage), {}
        data = data or {}
        outputs = data["outputs"] if "outputs" in data else {"answer": outcome.stdout}
        model = data.get("model")
        return ExecutionResult(
            status=outcome.status, outputs=outputs, error=outcome.error, retryable=False,
            tokens=(Measurement.reported(data["tokens"], Provenance.ADAPTER_REPORTED)
                    if "tokens" in data else Measurement.unknown()),
            adapter_latency_ms=(Measurement.reported(data["latency_ms"], Provenance.ADAPTER_REPORTED)
                                if "latency_ms" in data else Measurement.unknown()),
            returned_model=model if isinstance(model, str) else None,
            **common), data

    def execute_case(self, harness: Harness, task: Task, context: RunContext) -> BehavioralReport:
        """Legacy entry point (until T11): one trial through LocalBackend, converted to a BehavioralReport."""
        if context.workspace is None:
            raise RuntimeError("command executor needs a workspace path")
        request = PublicTrialRequest(
            trial_id=context.run_id, task_description=task.description, domain=task.domain,
            constraints=tuple(task.constraints), harness_public=harness.public_components(),
            fixture_hash=None, seed=context.seed, budget={},
        )
        limits = self.limits or ProcessLimits(timeout_seconds=self.timeout_seconds)
        result, data = self._run(request, context.workspace, LocalBackend(), limits)
        if result.status in (ExecutionStatus.TIMEOUT, ExecutionStatus.EXECUTION_ERROR):
            raise RuntimeError(result.error or result.status.value)
        failures: list[str] = []
        if result.status is not ExecutionStatus.COMPLETED:
            failures.append(result.status.value)
        score = require_finite("behavioral_score", data.get("behavioral_score", 0), minimum=0.0, maximum=1.0)
        return BehavioralReport(
            harness_id=harness.id, harness_version=harness.version, task_id=task.id, score=score,
            traces=[TraceStep("command", f"exit {result.exit_code}; status={result.status.value}",
                              result.status is ExecutionStatus.COMPLETED)],
            failures=failures,
            cost_tokens=int(result.tokens.value or 0), latency_ms=int(result.latency_ms.value or 0),
            notes="tool-enabled command adapter; behavioral score is not an acceptance verdict"
                  + (f"; {result.error}" if result.error else ""),
            outputs=result.outputs,
        )
