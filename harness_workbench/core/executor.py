"""Pluggable probe. The mock executor scores behavior without a model call.

Contrastive improvement (Harness²) needs matched probes, not gold labels.
:class:`MockExecutor` is deterministic: the same harness and task always
produce the same report, so tests and offline runs stay reproducible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from harness_workbench.core.harness import ALL_COMPONENTS, Harness

# Failure codes shared with the rule-based proposer.
MISSING_VERIFICATION = "missing_verification"
MISSING_PLAN = "missing_plan"
THIN_MEMORY = "thin_memory"
MISSING_DOMAIN_TOOL = "missing_domain_tool"
TOOL_NOISE_UNHANDLED = "tool_noise_unhandled"
NO_REVIEWER = "no_reviewer"

_MEMORY_FLOOR = 4

# Behavioral fitness weights. These are not task grades. They exist so a probe
# can tell two harnesses apart under matched conditions.
_BASE_SCORE = 0.28
_VERIFY_BONUS = 0.18
_PLAN_BONUS = 0.08
_MEMORY_BONUS = 0.08
_TOOL_BONUS = 0.16
_REFLEX_BONUS = 0.10
_REVIEW_BONUS = 0.06
_DOMAIN_SKILL_BONUS = 0.04
_SCRIPT_BONUS = 0.03


@dataclass
class Task:
    """A single task the harness is probed on.

    ``domain`` selects domain-specific edits and the tool the mock probe
    expects. ``constraints`` are extra requirements (for example "noisy tools").
    """

    id: str
    description: str
    domain: str = "general"
    constraints: list[str] = field(default_factory=list)

    @classmethod
    def from_text(
        cls,
        description: str,
        *,
        domain: str = "general",
        task_id: str | None = None,
        constraints: list[str] | None = None,
    ) -> Task:
        text = description.strip()
        if not text:
            raise ValueError("task description is empty")
        return cls(
            id=task_id or slug(text),
            description=text,
            domain=(domain or "general").strip().lower(),
            constraints=list(constraints or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraceStep:
    """One observable step in a behavioral trace."""

    kind: str
    detail: str
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BehavioralReport:
    """What a probe returns. ``score`` is behavioral fitness in ``[0, 1]``."""

    harness_id: str
    harness_version: str
    task_id: str
    score: float
    traces: list[TraceStep] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    cost_tokens: int = 0
    latency_ms: int = 0
    notes: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)
    # The model ID the provider says answered, when the executor can tell; None when it cannot.
    returned_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


class Executor(Protocol):
    """Something that can run a harness on a task and return a report.

    Real adapters (a hosted bot, an MCP runner) implement this.
    They must probe candidates under matched conditions.
    """

    def execute(self, harness: Harness, task: Task) -> BehavioralReport:
        """Run ``harness`` on ``task`` and return a behavioral report."""
        ...


def slug(text: str) -> str:
    """Short id derived from free text."""
    chars: list[str] = []
    for char in text.lower():
        if char.isalnum():
            chars.append(char)
        elif chars and chars[-1] != "-":
            chars.append("-")
    value = "".join(chars).strip("-")[:48]
    return value or "task"


def task_blob(task: Task) -> str:
    return f"{task.description} {' '.join(task.constraints)}".lower()


def needs_reflex(task: Task) -> bool:
    """True when the task mentions noisy tools or mid-run user corrections."""
    blob = task_blob(task)
    markers = ("noise", "noisy", "malformed", "timeout", "user correction", "user said")
    return any(marker in blob for marker in markers)


def needs_reviewer(task: Task) -> bool:
    blob = task_blob(task)
    return any(marker in blob for marker in ("review", "audit", "second agent"))


def has_plan(harness: Harness) -> bool:
    text = harness.system_prompt.lower()
    return "step-by-step" in text or "plan step" in text or "plan before" in text


def has_verification(harness: Harness) -> bool:
    blob = f"{harness.system_prompt} {' '.join(harness.guardrails)}".lower()
    return any(marker in blob for marker in ("verify", "verification", "cite", "counterevidence"))


def has_reflection(harness: Harness) -> bool:
    if any(skill.kind == "reflection" for skill in harness.skills):
        return True
    return any(plugin.hook == "reflection" for plugin in harness.plugins)


def has_reviewer(harness: Harness) -> bool:
    return any("review" in sub.role.lower() or "review" in sub.name.lower() for sub in harness.subagents)


def domain_tool(task: Task) -> tuple[str, str]:
    """Tool name and description the probe expects for ``task.domain``."""
    table = {
        "research": ("search", "Search for primary sources and counterevidence."),
        "evidence": ("search", "Search for primary sources and counterevidence."),
        "code": ("repo_search", "Search the repository before editing."),
        "qa": ("test_runner", "Run the relevant checks and report failures."),
        "general": ("search", "Look up supporting evidence before answering."),
    }
    return table.get(task.domain, table["general"])


def diagnose(harness: Harness, task: Task) -> list[str]:
    """Stable failure codes for a harness on a task.

    The mock executor and the rule-based proposer share this vocabulary so a
    candidate edit is aimed at a failure the probe will actually re-measure.
    """
    failures: list[str] = []
    if not has_verification(harness):
        failures.append(MISSING_VERIFICATION)
    if not has_plan(harness):
        failures.append(MISSING_PLAN)
    window = int(harness.memory_config.get("window") or 0)
    if window < _MEMORY_FLOOR:
        failures.append(THIN_MEMORY)
    tool_name, _ = domain_tool(task)
    if tool_name not in harness.tool_names():
        failures.append(f"{MISSING_DOMAIN_TOOL}:{tool_name}")
    if needs_reflex(task) and not has_reflection(harness):
        failures.append(TOOL_NOISE_UNHANDLED)
    if needs_reviewer(task) and not has_reviewer(harness):
        failures.append(NO_REVIEWER)
    return failures


class MockExecutor:
    """Offline executor.

    Score starts at a low base and rises when components close the failures
    from :func:`diagnose`. A reflection skill only scores when the task
    actually needs recovery. Cost is a rough token estimate from the text the
    harness would put in context, not a billed invoice.
    """

    name = "mock"
    # cost_tokens is a text-length estimate, never a billed count.
    usage_provenance = "estimated"
    supports = frozenset(ALL_COMPONENTS)

    def execute(self, harness: Harness, task: Task) -> BehavioralReport:
        failures = diagnose(harness, task)
        score = _BASE_SCORE
        if MISSING_VERIFICATION not in failures:
            score += _VERIFY_BONUS
        if MISSING_PLAN not in failures:
            score += _PLAN_BONUS
        if THIN_MEMORY not in failures:
            score += _MEMORY_BONUS
        if not any(item.startswith(MISSING_DOMAIN_TOOL) for item in failures):
            score += _TOOL_BONUS
        if needs_reflex(task) and has_reflection(harness):
            score += _REFLEX_BONUS
        if needs_reviewer(task) and has_reviewer(harness):
            score += _REVIEW_BONUS
        if any(skill.kind == "domain" for skill in harness.skills):
            score += _DOMAIN_SKILL_BONUS
        if harness.scripts:
            score += _SCRIPT_BONUS
        score = round(min(1.0, score), 4)

        window = int(harness.memory_config.get("window") or 0)
        tool_list = ", ".join(sorted(harness.tool_names())) or "(none)"
        traces = [
            TraceStep("system", _clip(harness.system_prompt), bool(harness.system_prompt.strip())),
            TraceStep("guardrail", _guardrail_detail(harness), MISSING_VERIFICATION not in failures),
            TraceStep("memory", f"window={window} strategy={harness.memory_config.get('strategy', 'recent')}", THIN_MEMORY not in failures),
            TraceStep("tool", tool_list, not any(item.startswith(MISSING_DOMAIN_TOOL) for item in failures)),
        ]
        if needs_reflex(task):
            traces.append(
                TraceStep(
                    "reflex",
                    "reflection skill armed" if has_reflection(harness) else "noisy tool feedback unhandled",
                    has_reflection(harness),
                )
            )
        if needs_reviewer(task):
            traces.append(
                TraceStep(
                    "subagent",
                    "reviewer present" if has_reviewer(harness) else "no reviewer",
                    has_reviewer(harness),
                )
            )

        return BehavioralReport(
            harness_id=harness.id,
            harness_version=harness.version,
            task_id=task.id,
            score=score,
            traces=traces,
            failures=failures,
            cost_tokens=_estimate_tokens(harness),
            latency_ms=15 + 4 * len(harness.tools) + 2 * len(harness.skills),
            notes="mock executor: behavioral fitness, not a gold label",
        )


def _clip(text: str, limit: int = 160) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact or "(empty system prompt)"
    return compact[: limit - 1] + "…"


def _guardrail_detail(harness: Harness) -> str:
    if harness.guardrails:
        return "; ".join(harness.guardrails)
    return "(no guardrails)"


def _estimate_tokens(harness: Harness) -> int:
    chars = len(harness.system_prompt)
    chars += sum(len(item) for item in harness.guardrails)
    chars += sum(len(tool.description) + len(tool.guidance) for tool in harness.tools)
    chars += sum(len(skill.action) for skill in harness.skills)
    return 64 + int(chars * 0.25) + 30 * len(harness.tools)
