"""Claude Code adapter: apply the seven supported harness components to headless Claude Code.

This is a standalone script, not an importable module of the package: the command executor mounts it
read-only into the solver and runs it as ``{python} -I {claude_code_agent} ...``. It imports nothing
from ``harness_workbench`` because the solver process must not need the package on its path.

Outcome mapping (spec section 6). Any result Claude Code reports, success or an error subtype
(``error_max_turns``, ``error_max_budget_usd``, ``error_during_execution``), is written to the result
file and the adapter exits 0: the grader then scores whatever ``solution.py`` exists. The adapter stops
Claude Code itself a margin before the executor timeout and writes a result the same way. Failures that
are not the candidate's (an unknown tool or malformed component, a missing or wrong-version CLI,
authentication or payment errors, no parsable result) write no result file and exit 3.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SUPPORTED = ("system_prompt", "guardrails", "memory_config", "tools", "subagents", "skills", "scripts")
ALLOWED_TOOLS = ("Bash", "Read", "Write", "Edit", "Glob", "Grep")

EXIT_OK = 0
EXIT_NOT_THE_CANDIDATES_FAULT = 3

DEFAULT_BASE_URL = "https://ai-gateway.vercel.sh"
# The executor timeout is announced in HARNESS_TIMEOUT_SECONDS; Claude Code is stopped this long before it.
STOP_MARGIN_SECONDS = 30.0
MIN_RUN_SECONDS = 1.0
VERSION_CHECK_SECONDS = 30.0
MAX_REPORTED = 10**12  # the command executor's bound for adapter-reported tokens
STDERR_TAIL_CHARS = 1000
PLUGIN_NAME = "harness"
PLUGIN_VERSION = "1.0.0"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SCRIPT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Text Claude Code puts in `result` when the API rejects the key or the account cannot pay. Applied only to
# an `is_error` result with subtype `success`, which is how an API failure surfaces; never to an answer.
_AUTH_TEXT = re.compile(
    r"invalid api key|authentication[_ ]error|unauthorized|payment required|credit balance|insufficient (?:credit|fund)",
    re.I)
_AUTH_STATUSES = (401, 402, 403)
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


class AdapterError(Exception):
    """A failure that is not the candidate's: the run must fail loudly instead of being scored."""


# ---------------------------------------------------------------------------------------------------------
# Component validation and mapping
# ---------------------------------------------------------------------------------------------------------

def _as_list(harness: dict[str, Any], key: str) -> list[Any]:
    value = harness.get(key, [])
    if not isinstance(value, list):
        raise AdapterError(f"harness {key} must be a list, got {type(value).__name__}")
    return value


def _entry(kind: str, item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise AdapterError(f"harness {kind} entry must be an object, got {type(item).__name__}")
    return item


def _text(kind: str, item: dict[str, Any], key: str, *, required: bool) -> str:
    value = item.get(key, "")
    if not isinstance(value, str):
        raise AdapterError(f"harness {kind} {key} must be a string")
    if required and not value.strip():
        raise AdapterError(f"harness {kind} {item.get('name', '?')!r}: {key} must not be empty")
    return value


def _name(kind: str, item: dict[str, Any], pattern: re.Pattern[str], seen: set[str]) -> str:
    name = item.get("name")
    if not isinstance(name, str) or not pattern.match(name):
        raise AdapterError(f"harness {kind} name {name!r} is not a safe name")
    if name in seen:
        raise AdapterError(f"harness {kind} name {name!r} appears twice")
    seen.add(name)
    return name


def _check_tool_names(where: str, names: list[str]) -> None:
    for name in names:
        if name not in ALLOWED_TOOLS:
            raise AdapterError(f"unknown tool {name!r} in {where}; allowed: {', '.join(ALLOWED_TOOLS)}")


def _validate(harness: Any, task: Any) -> dict[str, Any]:
    """Check every supported component and return it in the shape the builders use."""
    if not isinstance(harness, dict):
        raise AdapterError("harness file must contain a JSON object")
    if not isinstance(task, dict):
        raise AdapterError("task file must contain a JSON object")
    description = task.get("description")
    if not isinstance(description, str) or not description.strip():
        raise AdapterError("task description must be a nonempty string")
    constraints = task.get("constraints", [])
    if not isinstance(constraints, list) or not all(isinstance(c, str) for c in constraints):
        raise AdapterError("task constraints must be a list of strings")

    system_prompt = harness.get("system_prompt", "")
    if not isinstance(system_prompt, str):
        raise AdapterError("harness system_prompt must be a string")
    guardrails = _as_list(harness, "guardrails")
    if not all(isinstance(g, str) for g in guardrails):
        raise AdapterError("harness guardrails must be a list of strings")
    memory = harness.get("memory_config", {})
    if not isinstance(memory, dict):
        raise AdapterError("harness memory_config must be an object")

    tools: list[tuple[str, str]] = []
    for raw in _as_list(harness, "tools"):
        item = _entry("tool", raw)
        name = item.get("name")
        if not isinstance(name, str):
            raise AdapterError("harness tool name must be a string")
        _check_tool_names("tools", [name])
        guidance = _text("tool", item, "guidance", required=False)
        if all(name != existing for existing, _ in tools):
            tools.append((name, guidance))

    subagents: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for raw in _as_list(harness, "subagents"):
        item = _entry("subagent", raw)
        name = _name("subagent", item, _NAME, seen)
        sub_tools = item.get("tools", [])
        if not isinstance(sub_tools, list) or not all(isinstance(t, str) for t in sub_tools):
            raise AdapterError(f"subagent {name!r} tools must be a list of strings")
        _check_tool_names(f"subagent {name!r}", sub_tools)
        definition: dict[str, Any] = {
            "description": _text("subagent", item, "role", required=True),
            "prompt": _text("subagent", item, "system_prompt", required=False),
        }
        if sub_tools:
            definition["tools"] = list(sub_tools)
        subagents[name] = definition

    skills: list[tuple[str, str, str]] = []
    seen = set()
    for raw in _as_list(harness, "skills"):
        item = _entry("skill", raw)
        name = _name("skill", item, _NAME, seen)
        skills.append((name, _text("skill", item, "trigger", required=True),
                       _text("skill", item, "action", required=True)))

    scripts: list[tuple[str, str, str]] = []
    seen = set()
    for raw in _as_list(harness, "scripts"):
        item = _entry("script", raw)
        name = _name("script", item, _SCRIPT_NAME, seen)
        scripts.append((name, _text("script", item, "language", required=False) or "text",
                        _text("script", item, "body", required=False)))

    return {"system_prompt": system_prompt, "guardrails": guardrails, "memory": memory, "tools": tools,
            "subagents": subagents, "skills": skills, "scripts": scripts, "description": description,
            "constraints": constraints}


def _write_plugin(plugin: Path, skills: list[tuple[str, str, str]], scripts: list[tuple[str, str, str]]) -> None:
    (plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": PLUGIN_NAME, "version": PLUGIN_VERSION}), encoding="utf-8")
    for name, trigger, action in skills:
        directory = plugin / "skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        # JSON strings are valid YAML double-quoted scalars, so no trigger text can break the frontmatter.
        (directory / "SKILL.md").write_text(
            f"---\nname: {json.dumps(name)}\ndescription: {json.dumps(trigger)}\n---\n\n{action}\n",
            encoding="utf-8")
    if scripts:
        (plugin / "scripts").mkdir(parents=True, exist_ok=True)
    for name, _language, body in scripts:
        path = plugin / "scripts" / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)


def _system_prompt_text(parts: dict[str, Any], plugin: Path) -> str:
    sections: list[str] = []
    if parts["system_prompt"].strip():
        sections.append("## System prompt\n" + parts["system_prompt"].strip())
    if parts["guardrails"]:
        sections.append("## Guardrails\n" + "\n".join(f"- {g}" for g in parts["guardrails"]))
    if parts["memory"]:
        sections.append("## Memory\nMemory configuration: "
                        + json.dumps(parts["memory"], sort_keys=True, ensure_ascii=False))
    guided = [(name, guidance) for name, guidance in parts["tools"] if guidance.strip()]
    if guided:
        sections.append("## Tool guidance\n" + "\n".join(f"- {name}: {guidance.strip()}" for name, guidance in guided))
    if parts["scripts"]:
        lines = [f"- {name} ({language}): {plugin / 'scripts' / name}" for name, language, _ in parts["scripts"]]
        sections.append("## Scripts\nThese scripts are available; run them with the Bash tool.\n" + "\n".join(lines))
    return "\n\n".join(sections)


def _task_prompt(parts: dict[str, Any]) -> str:
    prompt = parts["description"].strip()
    if parts["constraints"]:
        prompt += "\n\nConstraints:\n" + "\n".join(f"- {c}" for c in parts["constraints"])
    return prompt


def build_invocation(harness: dict[str, Any], task: dict[str, Any], workspace: Path, home: Path,
                     args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    """Return ``(argv, env, plugin_dir)``; writes the generated plugin directory under ``home``.

    ``home`` is the fresh HOME of the Claude Code process. The task prompt is not part of argv: ``-p`` reads
    it from stdin (see ``task_prompt``), so a variadic flag before it cannot swallow it.
    """
    parts = _validate(harness, task)
    home = Path(home)
    plugin = home / "harness-plugin"
    _write_plugin(plugin, parts["skills"], parts["scripts"])
    claude_tmp = home / "tmp"
    claude_tmp.mkdir(parents=True, exist_ok=True)

    tool_names = [name for name, _ in parts["tools"]]
    if parts["skills"]:
        tool_names.append("Skill")
    if parts["subagents"]:
        tool_names.append("Agent")

    argv = [
        str(args.claude), "-p", "--output-format", "json", "--model", args.model,
        "--setting-sources", "", "--strict-mcp-config",
        "--permission-mode", "bypassPermissions", "--max-budget-usd", str(args.max_budget_usd),
        "--tools", ",".join(tool_names),
    ]
    prompt = _system_prompt_text(parts, plugin)
    if prompt:
        argv += ["--append-system-prompt", prompt]
    if parts["subagents"]:
        argv += ["--agents", json.dumps(parts["subagents"], ensure_ascii=False)]
    if parts["skills"]:
        argv += ["--plugin-dir", str(plugin)]

    path_dirs = [os.path.dirname(sys.executable)]
    path_dirs += [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p not in path_dirs]
    env = {
        "PATH": os.pathsep.join(path_dirs),
        "HOME": str(home),
        "TMPDIR": str(claude_tmp),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CLAUDE_CODE_TMPDIR": str(claude_tmp),
        "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": args.model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": args.model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": args.model,
        "CLAUDE_CODE_SUBAGENT_MODEL": args.model,
    }
    for name in ("ANTHROPIC_API_KEY", "HTTPS_PROXY"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return argv, env, plugin


def task_prompt(harness: dict[str, Any], task: dict[str, Any]) -> str:
    """The text handed to ``claude -p`` on stdin."""
    return _task_prompt(_validate(harness, task))


# ---------------------------------------------------------------------------------------------------------
# Result mapping
# ---------------------------------------------------------------------------------------------------------

def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return 0
    return int(value)


def _finite(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def map_result(cli_json: dict[str, Any], requested_model: str) -> dict[str, Any]:
    """The result file for one Claude Code result object.

    ``model`` is the requested string when every key of ``modelUsage`` equals it, otherwise the sorted keys
    joined with ``+``, so a trial where another model answered shows up as model drift.
    """
    answer = cli_json.get("result")
    subtype = cli_json.get("subtype")
    turns = cli_json.get("num_turns")
    details = {
        "subtype": subtype if isinstance(subtype, str) else "unknown",
        "num_turns": turns if isinstance(turns, int) and not isinstance(turns, bool) else None,
        "total_cost_usd": _finite(cli_json.get("total_cost_usd")),
        "is_error": bool(cli_json.get("is_error")),
    }
    result: dict[str, Any] = {"outputs": {"answer": answer if isinstance(answer, str) else "", "details": details}}
    usage = cli_json.get("usage")
    if isinstance(usage, dict) and any(key in usage for key in USAGE_KEYS):
        result["tokens"] = min(sum(_count(usage.get(key)) for key in USAGE_KEYS), MAX_REPORTED)
    model_usage = cli_json.get("modelUsage")
    keys = sorted(str(key) for key in model_usage) if isinstance(model_usage, dict) else []
    result["model"] = requested_model if all(key == requested_model for key in keys) else "+".join(keys)
    return result


def _timeout_result(requested_model: str) -> dict[str, Any]:
    return {"outputs": {"answer": "", "details": {
        "subtype": "adapter_timeout", "num_turns": None, "total_cost_usd": None, "is_error": True}},
        "model": requested_model}


def _auth_failure(cli_json: dict[str, Any]) -> str | None:
    status = cli_json.get("api_error_status")
    if isinstance(status, int) and not isinstance(status, bool) and status in _AUTH_STATUSES:
        return f"Claude Code reported API status {status}"
    result = cli_json.get("result")
    if cli_json.get("is_error") and cli_json.get("subtype") == "success" and isinstance(result, str) \
            and _AUTH_TEXT.search(result):
        return "Claude Code reported an authentication or payment error"
    return None


def _parse_cli_output(stdout: str) -> dict[str, Any]:
    try:
        data = json.loads(stdout)
    except ValueError:
        raise AdapterError("Claude Code printed no JSON result") from None
    if isinstance(data, list):
        results = [e for e in data if isinstance(e, dict) and e.get("type") == "result"]
        data = results[-1] if results else None
    if not isinstance(data, dict) or not isinstance(data.get("subtype"), str):
        raise AdapterError("Claude Code printed no result object")
    return data


# ---------------------------------------------------------------------------------------------------------
# Process handling
# ---------------------------------------------------------------------------------------------------------

def _redact(text: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    text = text.replace(key, "[redacted]") if key else text
    return text[-STDERR_TAIL_CHARS:]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_version(claude: Path, expected: str, env: dict[str, str]) -> None:
    if not claude.is_file() or not os.access(claude, os.X_OK):
        raise AdapterError(f"Claude Code CLI not found or not executable at {claude}")
    try:
        proc = subprocess.run([str(claude), "--version"], env=env, capture_output=True, text=True,
                              timeout=VERSION_CHECK_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"Claude Code CLI did not start: {type(exc).__name__}") from None
    words = proc.stdout.split()
    found = words[0] if words else ""
    if proc.returncode != 0 or found != expected:
        raise AdapterError(f"Claude Code version {found!r} is not the pinned {expected!r}")


def _run_claude(argv: list[str], env: dict[str, str], cwd: Path, prompt: str,
                deadline: float | None) -> tuple[str, str, bool]:
    """Run the CLI; returns ``(stdout, stderr, timed_out)``. A timed-out run is killed with its process group."""
    try:
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as exc:
        raise AdapterError(f"Claude Code CLI did not start: {type(exc).__name__}") from None
    timeout = None if deadline is None else max(deadline - time.monotonic(), 0.0)
    try:
        stdout, stderr = proc.communicate(prompt, timeout=timeout)
        return stdout, stderr, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return stdout or "", stderr or "", True


def _executor_deadline(start: float, margin: float) -> float | None:
    raw = os.environ.get("HARNESS_TIMEOUT_SECONDS")
    if not raw:
        return None
    try:
        total = float(raw)
    except ValueError:
        raise AdapterError("HARNESS_TIMEOUT_SECONDS is not a number") from None
    if not math.isfinite(total) or total <= 0:
        raise AdapterError("HARNESS_TIMEOUT_SECONDS must be a positive number")
    return start + max(total - margin, MIN_RUN_SECONDS)


def _read_json(path: str, what: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdapterError(f"cannot read {what} file: {type(exc).__name__}") from None


def _write_result(path: Path, result: dict[str, Any]) -> None:
    text = json.dumps(result, allow_nan=False, ensure_ascii=False)
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(text, encoding="utf-8")
    os.replace(staging, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply a harness to headless Claude Code for one trial.")
    parser.add_argument("--harness", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-budget-usd", required=True)
    parser.add_argument("--claude", required=True)
    parser.add_argument("--claude-version", required=True)
    parser.add_argument("--stop-margin-seconds", type=float, default=STOP_MARGIN_SECONDS)
    return parser


def _fresh_home(workspace: Path) -> Path:
    base = Path(os.environ["HOME"]) if os.environ.get("HOME") else workspace.parent
    if not base.is_dir():
        base = workspace.parent
    return Path(tempfile.mkdtemp(prefix="claude-home-", dir=base))


def _run(args: argparse.Namespace) -> int:
    start = time.monotonic()
    workspace, result_path, claude = Path(args.workspace), Path(args.result), Path(args.claude)
    harness, task = _read_json(args.harness, "harness"), _read_json(args.task, "task")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise AdapterError("ANTHROPIC_API_KEY is not set")
    try:
        budget = float(args.max_budget_usd)
    except ValueError:
        budget = float("nan")
    if not math.isfinite(budget) or budget <= 0:
        raise AdapterError("--max-budget-usd must be a positive number")
    deadline = _executor_deadline(start, args.stop_margin_seconds)

    home = _fresh_home(workspace)
    argv, env, _plugin = build_invocation(harness, task, workspace, home, args)
    prompt = task_prompt(harness, task)
    _check_version(claude, args.claude_version, env)
    pin = {"claude_version": args.claude_version, "claude_path": str(claude), "claude_sha256": _sha256(claude)}

    stdout, stderr, timed_out = _run_claude(argv, env, workspace, prompt, deadline)
    if timed_out:
        result = _timeout_result(args.model)
    else:
        try:
            cli_json = _parse_cli_output(stdout)
        except AdapterError as exc:
            raise AdapterError(f"{exc}; stderr tail: {_redact(stderr)!r}") from None
        failure = _auth_failure(cli_json)
        if failure:
            raise AdapterError(failure)
        result = map_result(cli_json, args.model)
    result["outputs"]["details"].update(pin)
    _write_result(result_path, result)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except AdapterError as exc:
        print(f"claude_code_agent: {_redact(str(exc))}", file=sys.stderr)
        return EXIT_NOT_THE_CANDIDATES_FAULT


if __name__ == "__main__":
    sys.exit(main())
