"""Versioned agent harness.

A harness is the editable runtime around a frozen model. The eight components
follow Harness² (system prompt, guardrails, memory, tools, subagents, skills,
scripts, plugins). Each version keeps a parent pointer plus the metrics and
lessons that justified the change.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    """UTC timestamp without microseconds, suitable for filenames and lessons."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _take(cls: type, data: dict[str, Any]) -> Any:
    if not isinstance(data, dict):
        raise ValueError(f"expected an object for {cls.__name__}, got {type(data).__name__}")
    allowed = {item.name for item in fields(cls)}
    return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass
class ToolSpec:
    """One tool the harness may call, including the guidance the model sees."""

    name: str
    description: str
    schema: dict[str, Any] = field(default_factory=dict)
    guidance: str = ""


@dataclass
class SubagentSpec:
    """A routed specialist. ``tools`` lists tool names it is allowed to use."""

    name: str
    role: str
    system_prompt: str = ""
    tools: list[str] = field(default_factory=list)


@dataclass
class Skill:
    """Reusable procedure.

    ``kind`` is ``invariant`` (cross-domain), ``domain`` (task-specific), or
    ``reflection`` (Agent-Reflex style recovery from noisy tool feedback).
    """

    name: str
    trigger: str
    action: str
    kind: str = "domain"


@dataclass
class Script:
    """Executable or checklist the harness can run outside the model."""

    name: str
    language: str
    body: str


@dataclass
class Plugin:
    """Middleware hook. ``hook`` is typically pre, post, tool, or reflection."""

    name: str
    hook: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class Lineage:
    """Where this version came from, and the evidence attached to it."""

    parent_id: str | None = None
    parent_version: str | None = None
    created_at: str = ""
    source: str = "seed"
    metrics: dict[str, Any] = field(default_factory=dict)
    lessons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = utc_now()


@dataclass
class Harness:
    """Structured, versionable harness.

    ``id`` is stable across versions. ``version`` changes when a composed
    harness is adopted. Candidate edits may use a temporary version such as
    ``1+inv-verify``; persisted versions are decimal strings (``1``, ``2``).
    """

    id: str
    version: str
    system_prompt: str
    guardrails: list[str] = field(default_factory=list)
    memory_config: dict[str, Any] = field(default_factory=dict)
    tools: list[ToolSpec] = field(default_factory=list)
    subagents: list[SubagentSpec] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    scripts: list[Script] = field(default_factory=list)
    plugins: list[Plugin] = field(default_factory=list)
    lineage: Lineage = field(default_factory=Lineage)
    name: str = ""
    domain: str = "general"
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.id
        if not self.memory_config:
            self.memory_config = {"window": 2, "strategy": "recent"}

    def copy(self) -> Harness:
        """Deep copy via the JSON shape so nested dataclasses stay independent."""
        return harness_from_dict(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False) + "\n"

    def tool_names(self) -> set[str]:
        return {tool.name for tool in self.tools}

    def fork(self, *, source: str, version: str | None = None) -> Harness:
        """Copy that records this harness as its parent.

        Components are unchanged. Callers apply edits on the returned object.
        """
        child = self.copy()
        child.version = version if version is not None else next_version(self.version)
        child.lineage = Lineage(
            parent_id=self.id,
            parent_version=self.version,
            source=source,
        )
        return child


def next_version(version: str) -> str:
    """Increment a decimal version. Non-numeric versions gain a ``.1`` suffix."""
    try:
        return str(int(version) + 1)
    except ValueError:
        return f"{version}.1"


def _load_list(items: list[dict[str, Any]] | None, cls: type) -> list[Any]:
    return [_take(cls, item) for item in (items or [])]


def harness_from_dict(data: dict[str, Any]) -> Harness:
    """Build a harness from the JSON object written by :meth:`Harness.to_dict`."""
    if "id" not in data:
        raise ValueError("harness is missing id")
    lineage_raw = data.get("lineage") or {}
    return Harness(
        id=str(data["id"]),
        version=str(data.get("version", "1")),
        system_prompt=str(data.get("system_prompt") or ""),
        guardrails=[str(item) for item in (data.get("guardrails") or [])],
        memory_config=dict(data.get("memory_config") or {}),
        tools=_load_list(data.get("tools"), ToolSpec),
        subagents=_load_list(data.get("subagents"), SubagentSpec),
        skills=_load_list(data.get("skills"), Skill),
        scripts=_load_list(data.get("scripts"), Script),
        plugins=_load_list(data.get("plugins"), Plugin),
        lineage=_take(Lineage, lineage_raw) if lineage_raw else Lineage(source="seed"),
        name=str(data.get("name") or data["id"]),
        domain=str(data.get("domain") or "general"),
        notes=str(data.get("notes") or ""),
    )


def load_harness(path: str | Path) -> Harness:
    """Load a harness JSON file."""
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{source} must contain a JSON object")
    return harness_from_dict(raw)


def save_harness(harness: Harness, path: str | Path) -> Path:
    """Write ``harness`` as JSON, creating parent directories."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(harness.to_json(), encoding="utf-8")
    return dest


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    return value


def component_diff(before: Harness, after: Harness) -> str:
    """Textual diff of editable components. Identity and lineage are ignored."""
    names = (
        "name",
        "domain",
        "system_prompt",
        "guardrails",
        "memory_config",
        "tools",
        "subagents",
        "skills",
        "scripts",
        "plugins",
        "notes",
    )
    chunks: list[str] = []
    for name in names:
        old = _plain(getattr(before, name))
        new = _plain(getattr(after, name))
        if old == new:
            continue
        old_text = old if isinstance(old, str) else json.dumps(old, ensure_ascii=False, indent=2, sort_keys=True)
        new_text = new if isinstance(new, str) else json.dumps(new, ensure_ascii=False, indent=2, sort_keys=True)
        body = [f"--- {name}", f"+++ {name}"]
        for line in old_text.splitlines() or [""]:
            body.append(f"-{line}")
        for line in new_text.splitlines() or [""]:
            body.append(f"+{line}")
        chunks.append("\n".join(body))
    return "\n".join(chunks) if chunks else "(no change)"
