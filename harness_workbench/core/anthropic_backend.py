"""Anthropic Messages API backend.

Sonnet is the frozen solver: it answers the task under the harness.
Haiku is the improver: it proposes harness edits and judges probe contrasts.

No SDK and no extra dependency. Set ``ANTHROPIC_API_KEY``. The mock provider
stays the default so the package still runs with the network off.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from harness_workbench.core.executor import BehavioralReport, Task, TraceStep
from harness_workbench.core.harness import Harness

SOLVER_MODEL = "claude-sonnet-5-5"
IMPROVER_MODEL = "claude-haiku-5-5"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"

Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]

IMPROVER_SYSTEM = (
    "You edit an agent harness. The solver model stays frozen. "
    "Reply with one JSON object only, no markdown and no prose. "
    "The user message is the full instruction, including the edit schema."
)

JUDGE_SYSTEM = (
    "You score one frozen-model answer. You do not solve the task and you do not rewrite the harness. "
    "Reply with JSON only: "
    '{"score": 0.0, "failures": ["short_code"], "notes": "one sentence"}. '
    "score is from 0 to 1. 0 is unusable, 0.5 is partial, 1 followed the harness and is usable. "
    "Judge behavior: guardrails kept, evidence or counterevidence when the task needs it, "
    "recovery when the task mentions noisy tools, no invented sources. "
    "Do not reward length."
)


class AnthropicError(RuntimeError):
    """The Anthropic call could not be completed. The message never includes the API key."""


@dataclass
class Completion:
    """Text plus token counts from one Messages API response."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class AnthropicClient:
    """Small Messages API client. ``transport`` is for tests; production uses urllib."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120,
        transport: Transport | None = None,
    ) -> None:
        key = api_key.strip()
        if not key:
            raise AnthropicError("ANTHROPIC_API_KEY is empty")
        self._api_key = key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport or _urllib_transport

    @classmethod
    def from_env(cls) -> AnthropicClient:
        """Build a client from ``ANTHROPIC_API_KEY`` and optional ``ANTHROPIC_BASE_URL``."""
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key.strip():
            raise AnthropicError("Set ANTHROPIC_API_KEY to use --provider anthropic")
        return cls(key, base_url=os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL))

    def complete(self, *, model: str, user: str, system: str = "", max_tokens: int = 4096) -> Completion:
        """One user turn. Thinking blocks are dropped; only text blocks are returned."""
        if max_tokens < 1:
            raise AnthropicError("max_tokens must be >= 1")
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user}],
        }
        if system:
            payload["system"] = system
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        status, raw = self._transport(f"{self.base_url}/v1/messages", headers, body, self.timeout)
        if status >= 400:
            detail = raw.decode("utf-8", errors="replace")[:400]
            raise AnthropicError(f"Anthropic API {status}: {detail}")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnthropicError("Anthropic API returned non-JSON") from exc
        if not isinstance(data, dict):
            raise AnthropicError("Anthropic API returned a non-object JSON body")
        return parse_message(data)


class AnthropicHarnessExecutor:
    """Probe: Sonnet answers under the harness, Haiku scores that answer.

    There is no live tool runtime. Tool guidance is part of the prompt, and the
    solver is told to admit when it has no source. The judge scores that behavior.
    """

    name = "anthropic"
    # cost_tokens is the solver plus judge API usage sum.
    usage_provenance = "provider_reported"
    # The solver sees the harness flattened into a system prompt: tools, subagents, skills, scripts and
    # plugins never run, so a change to them cannot be tested here.
    supports = frozenset({"system_prompt", "guardrails", "memory_config"})

    def __init__(
        self,
        client: AnthropicClient,
        *,
        solver_model: str = SOLVER_MODEL,
        judge_model: str = IMPROVER_MODEL,
        max_tokens: int = 4096,
    ) -> None:
        self.client = client
        self.solver_model = solver_model
        self.judge_model = judge_model
        self.max_tokens = max_tokens

    def execute(self, harness: Harness, task: Task) -> BehavioralReport:
        system = render_solver_system(harness)
        user = render_solver_user(task)
        started = time.monotonic()
        solved = self.client.complete(
            model=self.solver_model,
            system=system,
            user=user,
            max_tokens=self.max_tokens,
        )
        judge_user = render_judge_user(system, task, solved.text)
        judged = self.client.complete(
            model=self.judge_model,
            system=JUDGE_SYSTEM,
            user=judge_user,
            max_tokens=1024,
        )
        try:
            score, failures, notes = parse_judge(judged.text)
        except ValueError:
            score, failures, notes = 0.0, ["judge_unparseable"], "judge output was not usable JSON"
        elapsed_ms = int((time.monotonic() - started) * 1000)
        preview = " ".join(solved.text.split())
        if len(preview) > 240:
            preview = preview[:239] + "…"
        return BehavioralReport(
            harness_id=harness.id,
            harness_version=harness.version,
            task_id=task.id,
            score=score,
            traces=[
                TraceStep("solver", f"{self.solver_model}: {preview}", True),
                TraceStep("judge", f"{self.judge_model}: {notes}", score >= 0.5 and not failures),
            ],
            failures=failures,
            cost_tokens=solved.tokens + judged.tokens,
            latency_ms=elapsed_ms,
            notes=f"solver={self.solver_model} judge={self.judge_model} stop={solved.stop_reason}",
            outputs={"answer": solved.text},
        )


def render_solver_system(harness: Harness) -> str:
    """Flatten the eight harness components into the solver system prompt."""
    parts: list[str] = [harness.system_prompt.strip() or "You are a careful agent."]
    if harness.guardrails:
        parts.append("Guardrails:\n" + "\n".join(f"- {item}" for item in harness.guardrails))
    window = harness.memory_config.get("window", 2)
    strategy = harness.memory_config.get("strategy", "recent")
    parts.append(f"Memory: keep a window of {window}. Strategy: {strategy}.")
    if harness.tools:
        lines = []
        for tool in harness.tools:
            guidance = f" Guidance: {tool.guidance}" if tool.guidance else ""
            lines.append(f"- {tool.name}: {tool.description}{guidance}")
        parts.append(
            "Tool guidance: this probe has no live tool runtime. "
            "When you would have called a tool, follow the guidance and say if you have no source. "
            "Do not invent tool output.\n" + "\n".join(lines)
        )
    if harness.subagents:
        lines = [f"- {item.name} ({item.role}): {item.system_prompt}".strip() for item in harness.subagents]
        parts.append("Subagents to consult before answering:\n" + "\n".join(lines))
    if harness.skills:
        lines = [f"- {item.name} [{item.kind}] when {item.trigger}: {item.action}" for item in harness.skills]
        parts.append("Skills:\n" + "\n".join(lines))
    if harness.scripts:
        lines = [f"- {item.name} ({item.language}): {item.body}" for item in harness.scripts]
        parts.append("Scripts and checklists:\n" + "\n".join(lines))
    if harness.plugins:
        lines = [f"- {item.name} on {item.hook}" for item in harness.plugins]
        parts.append("Plugins:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def render_solver_user(task: Task) -> str:
    lines = [task.description.strip()]
    if task.constraints:
        lines.append("Constraints:\n" + "\n".join(f"- {item}" for item in task.constraints))
    lines.append(f"Domain: {task.domain}.")
    return "\n\n".join(lines)


def render_judge_user(system: str, task: Task, answer: str) -> str:
    clipped = answer if len(answer) <= 6000 else answer[:5999] + "…"
    return (
        f"TASK\n{task.description}\n\n"
        f"CONSTRAINTS\n{', '.join(task.constraints) or '(none)'}\n\n"
        f"HARNESS\n{system}\n\n"
        f"ANSWER\n{clipped}"
    )


def parse_message(data: dict[str, Any]) -> Completion:
    """Keep text blocks. Adaptive thinking blocks are ignored."""
    texts: list[str] = []
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            texts.append(str(block["text"]))
    text = "\n".join(texts).strip()
    if not text:
        raise AnthropicError(f"model returned no text (stop_reason={data.get('stop_reason')})")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return Completion(
        text=text,
        model=str(data.get("model") or ""),
        input_tokens=_as_int(usage.get("input_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
        stop_reason=str(data.get("stop_reason") or ""),
    )


def parse_judge(raw: str) -> tuple[float, list[str], str]:
    """Parse a judge JSON object. Raises ``ValueError`` when the shape is wrong."""
    data = _json_object(raw)
    if "score" not in data:
        raise ValueError("judge JSON has no score")
    try:
        score = float(data["score"])
    except (TypeError, ValueError) as exc:
        raise ValueError("judge score is not a number") from exc
    score = round(min(1.0, max(0.0, score)), 4)
    failures: list[str] = []
    for item in data.get("failures") or []:
        text = str(item).strip()
        if text:
            failures.append(text[:80])
    notes = str(data.get("notes") or "").strip() or "no notes"
    return score, failures[:8], notes[:300]


def _json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        rest = text.split("\n", 1)
        text = rest[1] if len(rest) == 2 else ""
        if "```" in text:
            text = text.rsplit("```", 1)[0]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("judge JSON did not parse") from exc
    if not isinstance(data, dict):
        raise ValueError("judge JSON must be an object")
    return data


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except urllib.error.URLError as exc:
        raise AnthropicError(f"Anthropic API unreachable: {exc.reason}") from exc
