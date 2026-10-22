"""Harness-blind acceptance checks.

Checks receive only outputs and an isolated workspace, never harness prompts,
candidate names, or behavioral-judge scores. Only trusted manifest authors
should enable command checks (they execute arbitrary programs locally).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any


def _target(outputs: dict[str, Any], path: str) -> Any:
    value: Any = outputs
    for key in path.split("."):
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            raise KeyError(f"missing output {path}")
    return value


def _inside(workspace: Path | None, path: str) -> Path:
    if workspace is None:
        raise ValueError("a file check requires a fixture workspace")
    target = (workspace / path).resolve()
    if not target.is_relative_to(workspace.resolve()):
        raise ValueError("check path escapes the isolated workspace")
    return target


def _check(check: dict[str, Any], outputs: dict[str, Any], workspace: Path | None) -> tuple[bool, str]:
    kind = str(check["type"])
    if kind in {"exact", "contains", "not_contains", "regex", "json_equal"}:
        actual = _target(outputs, str(check.get("target", "answer")))
        expected = check.get("value")
        if kind == "exact":
            return actual == expected, "exact comparison"
        if kind == "json_equal":
            actual_json = json.loads(actual) if isinstance(actual, str) else actual
            return actual_json == expected, "JSON structural comparison"
        if kind == "contains":
            return str(expected) in str(actual), "substring present"
        if kind == "not_contains":
            return str(expected) not in str(actual), "substring absent"
        if kind == "regex":
            return re.search(str(expected), str(actual)) is not None, "regex search"
    if kind == "file_exists":
        return _inside(workspace, str(check["path"])).is_file(), "file exists"
    if kind == "file_equals":
        path = _inside(workspace, str(check["path"]))
        return path.is_file() and path.read_text(encoding="utf-8") == str(check["value"]), "file exact content"
    if kind == "command":
        if workspace is None:
            raise ValueError("command checks require a fixture workspace")
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("command check requires nonempty argv string array")
        timeout = float(check.get("timeout_seconds", 30))
        if not (0 < timeout <= 300):
            raise ValueError("command timeout must be in (0, 300] seconds")
        completed = subprocess.run(
            argv, cwd=workspace, shell=False, check=False,
            capture_output=True, text=True, timeout=timeout,
        )
        expected_exit = int(check.get("exit_code", 0))
        passed = completed.returncode == expected_exit
        if check.get("stdout_regex"):
            passed = passed and bool(re.search(str(check["stdout_regex"]), completed.stdout))
        return passed, f"exit={completed.returncode} expected={expected_exit}; stdout={completed.stdout[:160]!r}; stderr={completed.stderr[:160]!r}"
    raise ValueError(f"unsupported acceptance check type: {kind}")


def evaluate_checks(
    checks: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    outputs: dict[str, Any],
    workspace: Path | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, check in enumerate(checks):
        identity = str(check.get("id") or f"check-{index + 1}")
        try:
            passed, detail = _check(check, outputs, workspace)
        except Exception as exc:
            passed, detail = False, f"{type(exc).__name__}: {exc}"
        results.append({
            "id": identity, "type": str(check.get("type", "")),
            "weight": float(check.get("weight", 1.0)),
            "passed": passed, "detail": detail,
        })
    return results
