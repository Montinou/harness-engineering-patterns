"""Harness-blind acceptance checks, evaluated by the supervisor.

Checks receive only outputs and an ``EvaluationEnvironment``: a snapshot of the
solver artifact, the frozen grader bundle (the working directory of ``command``
and ``oracle`` checks), a result directory ``out`` and scratch ``home``/``tmp``.
Each result has a ``status``: ``passed`` and ``failed`` are verdicts about the
solver; ``error`` means the evaluator could not decide (never counted as failed).
What the solver controls can only produce ``failed``: a missing output or a
malformed output is the solver's fault, an unreadable grader is not.

``command`` and ``oracle`` run through ``env.backend``. The oracle program only
*observes*: it writes a value under ``out`` and the supervisor reads that file and
compares it with ``expected`` itself, so the grader process never reports a verdict.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

from .backends import GRADER_FORBIDDEN_PASSTHROUGH, Mounts, ProcessOutcome, grader_env
from .grader import EvaluationEnvironment
from .status import ExecutionStatus
from .validation import json_loads_strict

_USABLE_RUN = (ExecutionStatus.COMPLETED, ExecutionStatus.TASK_FAILURE)
_ALLOWED_PYTHON_ENV = frozenset({"PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "PYTHONSAFEPATH"})
_DETAIL_CHARS = 160


class _Failed(Exception):
    """A solver-attributable failed verdict raised from inside a check."""


class _OracleViolation(Exception):
    """The oracle protocol was not honoured; this is an evaluator error."""


def _target(outputs: dict[str, Any], path: str) -> Any:
    value: Any = outputs
    for key in path.split("."):
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            raise _Failed(f"missing output {path}")
    return value


def _require_env(env: EvaluationEnvironment | None, what: str) -> EvaluationEnvironment:
    if env is None:
        raise ValueError(f"{what} requires an evaluation environment")
    return env


def _inside(root: Path, path: str) -> Path:
    base = root.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(base):
        raise ValueError("check path escapes the artifact snapshot")
    return target


def _artifact_target(env: EvaluationEnvironment | None, path: str) -> Path:
    env = _require_env(env, "a file check")
    if not env.artifact.is_dir():
        raise ValueError("a file check requires a fixture workspace")
    return _inside(env.artifact, path)


def _argv(check: dict[str, Any], env: EvaluationEnvironment) -> list[str]:
    argv = check.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("check requires nonempty argv string array")
    replacements = {
        "{python}": env.backend.python,
        "{artifact}": str(env.artifact),
        "{bundle}": str(env.bundle),
        "{out}": str(env.out),
    }
    result = []
    for item in argv:
        for token, value in replacements.items():
            item = item.replace(token, value)
        result.append(item)
    return result


def _environment(env: EvaluationEnvironment) -> dict[str, str]:
    variables = dict(grader_env(env.home, env.tmp))
    variables["HARNESS_ARTIFACT"] = str(env.artifact)
    variables["HARNESS_BUNDLE"] = str(env.bundle)
    variables["HARNESS_OUT"] = str(env.out)
    for name in variables:
        if GRADER_FORBIDDEN_PASSTHROUGH.search(name) or (name.startswith("PYTHON") and name not in _ALLOWED_PYTHON_ENV):
            raise ValueError(f"grader environment must not carry {name}")
    return variables


def _limits(check: dict[str, Any], env: EvaluationEnvironment):
    if "timeout_seconds" in check:
        requested = float(check["timeout_seconds"])
        if not 0 < requested <= 300:
            raise ValueError("command timeout must be in (0, 300] seconds")
        return dataclasses.replace(env.limits, timeout_seconds=min(requested, env.limits.timeout_seconds))
    return env.limits


def _run(check: dict[str, Any], env: EvaluationEnvironment) -> ProcessOutcome:
    argv = _argv(check, env)
    return env.backend.run(
        argv, cwd=env.bundle, env=_environment(env), limits=_limits(check, env),
        mounts=Mounts(read_only=(env.bundle, env.artifact), writable=(env.out, env.home, env.tmp)),
    )


def _outcome_detail(outcome: ProcessOutcome) -> str:
    return (f"{outcome.status.value}; exit={outcome.exit_code}; stdout={outcome.stdout[:_DETAIL_CHARS]!r}; "
            f"stderr={outcome.stderr[:_DETAIL_CHARS]!r}")


def _command(check: dict[str, Any], env: EvaluationEnvironment | None) -> tuple[str, str]:
    env = _require_env(env, "a command check")
    outcome = _run(check, env)
    if outcome.status not in _USABLE_RUN:
        return "error", f"grader process {_outcome_detail(outcome)}; {outcome.error}"
    expected_exit = int(check.get("exit_code", 0))
    passed = outcome.exit_code == expected_exit
    if check.get("stdout_regex"):
        passed = passed and re.search(str(check["stdout_regex"]), outcome.stdout) is not None
    return ("passed" if passed else "failed"), f"expected_exit={expected_exit}; {_outcome_detail(outcome)}"


def _read_observed(check: dict[str, Any], env: EvaluationEnvironment) -> str:
    observed = str(check["observed"])
    out = env.out.resolve()
    path = env.out / observed
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(out):
            raise _OracleViolation(f"observed path {observed!r} is not a regular file inside out")
        if not path.is_file():
            raise _OracleViolation(f"observed file {observed!r} was not written")
        if path.stat().st_size > env.limits.max_output_bytes:
            raise _OracleViolation(f"observed file {observed!r} exceeds {env.limits.max_output_bytes} bytes")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _OracleViolation(f"observed file {observed!r} is unreadable: {type(exc).__name__}") from exc


def _clear_stale_observed(observed: str, env: EvaluationEnvironment) -> None:
    """Delete a value left by an earlier check, only after proving the path stays inside out."""
    relative = Path(observed)
    if not observed or relative.is_absolute() or ".." in relative.parts:
        raise _OracleViolation(f"observed path {observed!r} must be a relative path without '..'")
    current = env.out
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise _OracleViolation(f"observed path {observed!r} passes through a symlink")
    stale = env.out / relative
    if not stale.parent.resolve().is_relative_to(env.out.resolve()):
        raise _OracleViolation(f"observed path {observed!r} is outside out")
    try:
        if stale.is_symlink() or stale.is_file():
            stale.unlink()  # a value left by an earlier check must not satisfy this one
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise _OracleViolation(f"stale observed file {observed!r} could not be removed: {type(exc).__name__}") from exc


def _oracle(check: dict[str, Any], env: EvaluationEnvironment | None) -> tuple[str, str]:
    env = _require_env(env, "an oracle check")
    observed = str(check["observed"])
    try:
        _clear_stale_observed(observed, env)
    except _OracleViolation as exc:
        return "error", f"oracle protocol violation: {exc}"
    outcome = _run(check, env)
    try:
        if outcome.status not in _USABLE_RUN:
            raise _OracleViolation(f"grader process {_outcome_detail(outcome)}; {outcome.error}")
        text = _read_observed(check, env)
        compare = check.get("compare", "exact")
        if compare == "json_equal":
            try:
                value: Any = json_loads_strict(text)
            except ValueError as exc:
                raise _OracleViolation(f"observed file {observed!r} is not valid JSON: {exc}") from exc
        else:
            value = text
    except _OracleViolation as exc:
        return "error", f"oracle protocol violation: {exc}"
    verdict = "passed" if value == check["expected"] else "failed"
    return verdict, f"supervisor compared observed {observed!r} ({compare}); {_outcome_detail(outcome)}"


def _check(check: dict[str, Any], outputs: dict[str, Any], env: EvaluationEnvironment | None) -> tuple[str, str]:
    kind = str(check["type"])
    if kind in {"exact", "contains", "not_contains", "regex", "json_equal"}:
        actual = _target(outputs, str(check.get("target", "answer")))
        expected = check.get("value")
        if kind == "exact":
            return _verdict(actual == expected), "exact comparison"
        if kind == "json_equal":
            try:
                actual_json = json_loads_strict(actual) if isinstance(actual, str) else actual
            except ValueError as exc:
                raise _Failed(f"output is not valid JSON: {exc}") from exc
            return _verdict(actual_json == expected), "JSON structural comparison"
        if kind == "contains":
            return _verdict(str(expected) in str(actual)), "substring present"
        if kind == "not_contains":
            return _verdict(str(expected) not in str(actual)), "substring absent"
        return _verdict(re.search(str(expected), str(actual)) is not None), "regex search"
    if kind == "file_exists":
        return _verdict(_artifact_target(env, str(check["path"])).is_file()), "file exists"
    if kind == "file_equals":
        path = _artifact_target(env, str(check["path"]))
        if not path.is_file():
            return "failed", "file does not exist"
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "failed", "file is not valid UTF-8"
        return _verdict(content == str(check["value"])), "file exact content"
    if kind == "command":
        return _command(check, env)
    if kind == "oracle":
        return _oracle(check, env)
    raise ValueError(f"unsupported acceptance check type: {kind}")


def _verdict(passed: bool) -> str:
    return "passed" if passed else "failed"


def evaluate_checks(
    checks: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    outputs: dict[str, Any],
    env: EvaluationEnvironment | None,
) -> list[dict[str, Any]]:
    if env is not None and not isinstance(env, EvaluationEnvironment):
        raise TypeError(f"env must be an EvaluationEnvironment or None, got {type(env).__name__}")
    results: list[dict[str, Any]] = []
    for index, check in enumerate(checks):
        identity = str(check.get("id") or f"check-{index + 1}")
        try:
            status, detail = _check(check, outputs, env)
        except _Failed as exc:
            status, detail = "failed", str(exc)
        except Exception as exc:  # the evaluator could not decide: an error, never a failed verdict
            status, detail = "error", f"{type(exc).__name__}: {exc}"
        try:
            weight = float(check.get("weight", 1.0))
        except (TypeError, ValueError):
            weight, status, detail = 1.0, "error", "invalid weight"
        results.append({
            "id": identity, "type": str(check.get("type", "")), "weight": weight,
            "status": status, "passed": status == "passed", "detail": detail,
        })
    return results
