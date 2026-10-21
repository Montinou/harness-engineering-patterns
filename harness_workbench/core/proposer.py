"""Candidate harness edits.

Two proposers share one edit language:

* :class:`RuleBasedProposer` — offline, failure-driven, invariant plus domain.
* :class:`LLMProposer` — calls a user-supplied completer and parses JSON edits.

Edits are structured operations. Each candidate also carries a textual diff of
the components that changed, in the spirit of the SEARCH/REPLACE patches used
by Harness Learning.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol

from harness_workbench.core.executor import (
    MISSING_DOMAIN_TOOL,
    MISSING_PLAN,
    MISSING_VERIFICATION,
    NO_REVIEWER,
    THIN_MEMORY,
    TOOL_NOISE_UNHANDLED,
    BehavioralReport,
    Task,
    diagnose,
    domain_tool,
)
from harness_workbench.core.harness import (
    Harness,
    Plugin,
    Script,
    Skill,
    SubagentSpec,
    ToolSpec,
    component_diff,
)

OPS = frozenset(
    {
        "append_prompt",
        "add_guardrail",
        "set_memory",
        "add_tool",
        "add_subagent",
        "add_skill",
        "add_script",
        "add_plugin",
        "bundle",
    }
)

_OP_COMPONENT = {
    "append_prompt": "system_prompt",
    "add_guardrail": "guardrails",
    "set_memory": "memory",
    "add_tool": "tools",
    "add_subagent": "subagents",
    "add_skill": "skills",
    "add_script": "scripts",
    "add_plugin": "plugins",
    "bundle": "bundle",
}

VERIFY_GUARD = "Verify claims against a second source before answering."
PLAN_SENTENCE = "Plan step-by-step, then verify intermediate results."


class ProposerError(ValueError):
    """The proposer output could not be turned into edits."""


@dataclass
class HarnessEdit:
    """One structured change, or a bundle of changes probed as a unit."""

    id: str
    kind: str
    component: str
    op: str
    payload: dict[str, Any]
    rationale: str
    diff: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    """An edit plus the harness that results from applying it to the parent."""

    edit: HarnessEdit
    harness: Harness
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "edit": self.edit.to_dict(),
            "harness": self.harness.to_dict(),
        }


class Proposer(Protocol):
    """Emit up to ``k`` invariant and ``k`` domain candidates."""

    def propose(
        self,
        task: Task,
        harness: Harness,
        report: BehavioralReport | None = None,
        *,
        k: int = 1,
    ) -> list[Candidate]:
        """Return candidate edits for ``harness`` on ``task``."""
        ...


Completer = Callable[[str], str]


def edit_from_mapping(data: dict[str, Any], *, fallback_kind: str = "domain") -> HarnessEdit:
    """Parse one edit object. Unknown ops and kinds raise :class:`ProposerError`."""
    if not isinstance(data, dict):
        raise ProposerError("each edit must be a JSON object")
    op = data.get("op")
    if op not in OPS:
        raise ProposerError(f"unsupported op: {op!r}")
    kind = str(data.get("kind") or fallback_kind)
    if kind not in {"invariant", "domain"}:
        raise ProposerError(f"unsupported kind: {kind!r}")
    payload = data.get("payload") or {}
    if not isinstance(payload, dict):
        raise ProposerError("payload must be an object")
    component = str(data.get("component") or _OP_COMPONENT[op])
    return HarnessEdit(
        id=str(data.get("id") or f"{kind}-{op}"),
        kind=kind,
        component=component,
        op=str(op),
        payload=payload,
        rationale=str(data.get("rationale") or ""),
        diff=str(data.get("diff") or ""),
    )


def parse_edit_payload(raw: str) -> list[HarnessEdit]:
    """Parse a completer response. Markdown fences around the JSON are allowed."""
    text = raw.strip()
    if not text:
        raise ProposerError("completer returned an empty response")
    if text.startswith("```"):
        stripped = text.split("\n", 1)
        text = stripped[1] if len(stripped) == 2 else ""
        if "```" in text:
            text = text.rsplit("```", 1)[0]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ProposerError("completer response did not contain a JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProposerError(f"completer JSON did not parse: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("edits"), list):
        raise ProposerError("completer JSON must be an object with an edits list")
    return [edit_from_mapping(item) for item in data["edits"]]


def apply_edit(harness: Harness, edit: HarnessEdit) -> Harness:
    """Return a new harness with ``edit`` applied. The input is not mutated.

    Adds are idempotent on name (or exact guardrail text). A bundle applies
    its children in order on successive copies.
    """
    if edit.op == "bundle":
        current = harness
        for raw in edit.payload.get("edits") or []:
            current = apply_edit(current, edit_from_mapping(raw, fallback_kind=edit.kind))
        return current

    out = harness.copy()
    payload = edit.payload
    if edit.op == "append_prompt":
        text = _required_text(payload, "append_prompt")
        if text not in out.system_prompt:
            spacer = "" if not out.system_prompt or out.system_prompt.endswith((" ", "\n")) else " "
            out.system_prompt = f"{out.system_prompt}{spacer}{text}".strip()
    elif edit.op == "add_guardrail":
        text = _required_text(payload, "add_guardrail")
        if text not in out.guardrails:
            out.guardrails.append(text)
    elif edit.op == "set_memory":
        if not payload:
            raise ProposerError("set_memory payload is empty")
        out.memory_config.update(payload)
    elif edit.op == "add_tool":
        name = _required(payload, "name", "add_tool")
        if name not in out.tool_names():
            out.tools.append(
                ToolSpec(
                    name=name,
                    description=str(payload.get("description") or ""),
                    schema=dict(payload.get("schema") or {}),
                    guidance=str(payload.get("guidance") or ""),
                )
            )
    elif edit.op == "add_subagent":
        name = _required(payload, "name", "add_subagent")
        if name not in {item.name for item in out.subagents}:
            tools = payload.get("tools") or []
            out.subagents.append(
                SubagentSpec(
                    name=name,
                    role=str(payload.get("role") or ""),
                    system_prompt=str(payload.get("system_prompt") or ""),
                    tools=[str(item) for item in tools],
                )
            )
    elif edit.op == "add_skill":
        name = _required(payload, "name", "add_skill")
        if name not in {item.name for item in out.skills}:
            out.skills.append(
                Skill(
                    name=name,
                    trigger=str(payload.get("trigger") or ""),
                    action=str(payload.get("action") or ""),
                    kind=str(payload.get("kind") or "domain"),
                )
            )
    elif edit.op == "add_script":
        name = _required(payload, "name", "add_script")
        if name not in {item.name for item in out.scripts}:
            out.scripts.append(
                Script(
                    name=name,
                    language=str(payload.get("language") or "text"),
                    body=str(payload.get("body") or ""),
                )
            )
    elif edit.op == "add_plugin":
        name = _required(payload, "name", "add_plugin")
        if name not in {item.name for item in out.plugins}:
            out.plugins.append(
                Plugin(
                    name=name,
                    hook=str(payload.get("hook") or "post"),
                    config=dict(payload.get("config") or {}),
                )
            )
    else:
        raise ProposerError(f"unsupported op: {edit.op}")
    return out


def make_candidate(parent: Harness, edit: HarnessEdit) -> Candidate:
    """Apply ``edit`` and attach the component diff. Version is provisional."""
    child = apply_edit(parent, edit)
    child.version = f"{parent.version}+{edit.id}"
    child.lineage.parent_id = parent.id
    child.lineage.parent_version = parent.version
    child.lineage.source = "propose"
    child.lineage.metrics = {}
    child.lineage.lessons = []
    edit.diff = component_diff(parent, child)
    label = f"{edit.kind}:{edit.component}:{edit.op}"
    return Candidate(edit=edit, harness=child, label=label)


def limit_by_kind(edits: list[HarnessEdit], k: int) -> list[HarnessEdit]:
    """Keep the first ``k`` invariant edits and the first ``k`` domain edits."""
    if k < 1:
        raise ValueError("k must be >= 1")
    invariant = [edit for edit in edits if edit.kind == "invariant"][:k]
    domain = [edit for edit in edits if edit.kind == "domain"][:k]
    return invariant + domain


class RuleBasedProposer:
    """Offline proposer.

    Invariant edits cover planning, verification, memory and reflection.
    Domain edits cover the tool, skill or script the task domain is missing.
    ``k`` is how many of each kind to emit (Harness²'s ``k``).
    """

    name = "rule"

    def propose(
        self,
        task: Task,
        harness: Harness,
        report: BehavioralReport | None = None,
        *,
        k: int = 1,
    ) -> list[Candidate]:
        if k < 1:
            raise ValueError("k must be >= 1")
        failures = list(report.failures) if report is not None else diagnose(harness, task)
        edits = _invariant_edits(harness, failures) + _domain_edits(harness, task, failures)
        chosen = limit_by_kind(edits, k)
        candidates = [make_candidate(harness, edit) for edit in chosen]
        return [candidate for candidate in candidates if candidate.edit.diff != "(no change)"]


class LLMProposer:
    """Proposer that delegates the edit decision to a completer.

    ``completer`` takes the prompt and returns text. This stays free of any
    SDK so a caller can wrap Grok, a local model, or a recorded fixture.
    The completer must return JSON ``{"edits": [...]}``.
    """

    name = "llm"

    def __init__(self, completer: Completer) -> None:
        self.completer = completer

    def propose(
        self,
        task: Task,
        harness: Harness,
        report: BehavioralReport | None = None,
        *,
        k: int = 1,
    ) -> list[Candidate]:
        prompt = render_llm_prompt(task, harness, report, k=k)
        raw = self.completer(prompt)
        edits = limit_by_kind(parse_edit_payload(raw), k)
        candidates = [make_candidate(harness, edit) for edit in edits]
        return [candidate for candidate in candidates if candidate.edit.diff != "(no change)"]


class FileProposer:
    """Apply edits a model already wrote to a JSON file. Fully offline."""

    name = "file"

    def __init__(self, raw: str) -> None:
        self._edits = parse_edit_payload(raw)

    def propose(
        self,
        task: Task,
        harness: Harness,
        report: BehavioralReport | None = None,
        *,
        k: int = 1,
    ) -> list[Candidate]:
        del task, report
        edits = limit_by_kind(self._edits, k)
        candidates = [make_candidate(harness, edit) for edit in edits]
        return [candidate for candidate in candidates if candidate.edit.diff != "(no change)"]


def render_llm_prompt(
    task: Task,
    harness: Harness,
    report: BehavioralReport | None,
    *,
    k: int,
) -> str:
    """Instruction a completer sees. The contract is the JSON edit language."""
    report_payload: dict[str, Any] | None
    if report is None:
        report_payload = None
    else:
        report_payload = report.to_dict()
    body = {
        "task": task.to_dict(),
        "harness": harness.to_dict(),
        "report": report_payload,
        "k": k,
    }
    return (
        "You revise an agent harness. Model weights stay frozen.\n"
        f"Return at most {k} invariant edits and {k} domain edits.\n"
        "Invariant edits are cross-domain (planning, verification, memory, reflection).\n"
        "Domain edits are specific to this task.\n"
        "Reply with JSON only, no prose:\n"
        '{"edits":[{"id":"string","kind":"invariant|domain","component":"string",'
        '"op":"append_prompt|add_guardrail|set_memory|add_tool|add_subagent|add_skill|'
        'add_script|add_plugin|bundle","payload":{},"rationale":"string"}]}\n'
        "A bundle payload is {\"edits\": [ ...same objects without kind... ]}.\n"
        "Do not wrap the JSON in markdown.\n\n"
        + json.dumps(body, ensure_ascii=False, indent=2)
    )


def _required(payload: dict[str, Any], key: str, op: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProposerError(f"{op} payload is missing {key}")
    return value.strip()


def _required_text(payload: dict[str, Any], op: str) -> str:
    return _required(payload, "text", op)


def _bundle(edit_id: str, kind: str, component: str, rationale: str, edits: list[dict[str, Any]]) -> HarnessEdit:
    if len(edits) == 1:
        only = edits[0]
        return HarnessEdit(
            id=edit_id,
            kind=kind,
            component=str(only.get("component") or component),
            op=str(only["op"]),
            payload=dict(only.get("payload") or {}),
            rationale=rationale,
        )
    return HarnessEdit(
        id=edit_id,
        kind=kind,
        component=component,
        op="bundle",
        payload={"edits": edits},
        rationale=rationale,
    )


def _invariant_edits(harness: Harness, failures: list[str]) -> list[HarnessEdit]:
    edits: list[HarnessEdit] = []
    structural: list[dict[str, Any]] = []
    if MISSING_VERIFICATION in failures:
        structural.append(
            {
                "op": "add_guardrail",
                "component": "guardrails",
                "payload": {"text": VERIFY_GUARD},
            }
        )
    if MISSING_PLAN in failures:
        structural.append(
            {
                "op": "append_prompt",
                "component": "system_prompt",
                "payload": {"text": PLAN_SENTENCE},
            }
        )
    if structural:
        edits.append(
            _bundle(
                "inv-verify-plan",
                "invariant",
                "guardrails",
                "Invariant planning and verification. These do not depend on the task domain.",
                structural,
            )
        )
    if THIN_MEMORY in failures:
        window = int(harness.memory_config.get("window") or 0)
        edits.append(
            HarnessEdit(
                id="inv-memory",
                kind="invariant",
                component="memory",
                op="set_memory",
                payload={
                    "window": max(window, 6),
                    "strategy": harness.memory_config.get("strategy") or "recent",
                },
                rationale="Invariant context window. The probe reported thin memory.",
            )
        )
    if TOOL_NOISE_UNHANDLED in failures:
        edits.append(
            _bundle(
                "inv-reflex",
                "invariant",
                "skills",
                "Agent-Reflex recovery: do not accept the first noisy tool result.",
                [
                    {
                        "op": "add_skill",
                        "component": "skills",
                        "payload": {
                            "name": "recover_from_noise",
                            "trigger": "tool returned unexpected format, timeout, or contradiction",
                            "action": (
                                "Do not accept the first tool result. Re-call with clearer "
                                "arguments or cross-check a second source before continuing."
                            ),
                            "kind": "reflection",
                        },
                    },
                    {
                        "op": "add_plugin",
                        "component": "plugins",
                        "payload": {
                            "name": "reflection",
                            "hook": "reflection",
                            "config": {"on": ["tool_error", "user_correction"]},
                        },
                    },
                ],
            )
        )
    if NO_REVIEWER in failures:
        edits.append(
            HarnessEdit(
                id="inv-reviewer",
                kind="invariant",
                component="subagents",
                op="add_subagent",
                payload={
                    "name": "reviewer",
                    "role": "review",
                    "system_prompt": "Check the draft for unsupported claims and missing counterevidence.",
                    "tools": [],
                },
                rationale="Invariant review pass. The task asked for review or audit.",
            )
        )
    return edits


def _domain_edits(harness: Harness, task: Task, failures: list[str]) -> list[HarnessEdit]:
    edits: list[HarnessEdit] = []
    tool_failures = [item for item in failures if item.startswith(f"{MISSING_DOMAIN_TOOL}:")]
    if tool_failures:
        name, description = domain_tool(task)
        guidance = "Prefer primary sources. Record counterevidence explicitly. Zero new findings is valid."
        if task.domain in {"code", "qa"}:
            guidance = "Read the failing check before changing the harness or the code under test."
        edits.append(
            HarnessEdit(
                id=f"dom-tool-{name}",
                kind="domain",
                component="tools",
                op="add_tool",
                payload={"name": name, "description": description, "guidance": guidance},
                rationale=f"Domain tool '{name}' is missing for domain '{task.domain}'.",
            )
        )
    if not any(skill.kind == "domain" for skill in harness.skills):
        edits.append(_domain_skill(task))
    if not harness.scripts:
        edits.append(_domain_script(task))
    return edits


def _domain_skill(task: Task) -> HarnessEdit:
    if task.domain in {"code", "qa"}:
        payload = {
            "name": "reproduce_failure",
            "trigger": "a check failed or the report is incomplete",
            "action": "Reproduce the failure, name the invariant, then change one thing.",
            "kind": "domain",
        }
    else:
        payload = {
            "name": "cite_primary",
            "trigger": "a factual claim is about to be stated",
            "action": "Attach a primary source or an explicit counterevidence note. Do not upgrade a lead into a finding.",
            "kind": "domain",
        }
    return HarnessEdit(
        id=f"dom-skill-{payload['name']}",
        kind="domain",
        component="skills",
        op="add_skill",
        payload=payload,
        rationale=f"Domain skill for '{task.domain}' tasks.",
    )


def _domain_script(task: Task) -> HarnessEdit:
    if task.domain in {"code", "qa"}:
        payload = {
            "name": "run_checks",
            "language": "python",
            "body": "python -m unittest discover -s tests",
        }
    else:
        payload = {
            "name": "evidence_check",
            "language": "markdown",
            "body": "List claims, sources, and counterevidence. Zero new findings is a valid result.",
        }
    return HarnessEdit(
        id=f"dom-script-{payload['name']}",
        kind="domain",
        component="scripts",
        op="add_script",
        payload=payload,
        rationale=f"Domain checklist for '{task.domain}'.",
    )
