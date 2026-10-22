"""Frozen, provider-independent experimental inputs. Only standard-library types."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from harness_workbench.core.executor import Task
from harness_workbench.core.harness import Harness, harness_from_dict
from harness_workbench.core.proposer import apply_edit, edit_from_mapping

SCHEMA_VERSION = 1
MAX_FIXTURE_BYTES = 8 * 1024 * 1024


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}", value):
        raise ValueError(f"invalid identifier: {value!r}")
    return value


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or "\\" in value or ":" in value or path.is_absolute() or any(
        part in {"..", ".git", ".runtime"} for part in path.parts
    ) or str(path) == ".":
        raise ValueError(f"unsafe fixture/artifact path: {value!r}")
    return str(path)


def unknown(data: dict, allowed: set[str], label: str) -> None:
    extra = set(data) - allowed
    if extra:
        raise ValueError(f"unknown {label} fields: {sorted(extra)}")


def positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


@dataclass
class Policy:
    repetitions: int = 5
    min_validation_tasks: int = 5
    margin: float = 0.05
    alpha: float = 0.05
    regression_tolerance: float = 0.0
    max_tokens_ratio: float | None = None
    max_latency_ratio: float | None = None
    max_total_tokens: int | None = None
    max_workers: int = 1
    bootstrap_samples: int = 4000
    seed: int = 1729

    def validate(self) -> None:
        for key in ("repetitions", "min_validation_tasks", "max_workers", "bootstrap_samples"):
            positive_int(getattr(self, key), key)
        if self.max_workers > 32 or self.bootstrap_samples > 100000:
            raise ValueError("max_workers <= 32 and bootstrap_samples <= 100000 required")
        if type(self.seed) is not int:
            raise ValueError("seed must be an integer")
        for key in ("margin", "regression_tolerance"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{key} must be finite and in [0, 1]")
        if isinstance(self.alpha, bool) or not isinstance(self.alpha, (int, float)) or not 0 < self.alpha < 0.5:
            raise ValueError("alpha must be in (0, 0.5)")
        for key in ("max_tokens_ratio", "max_latency_ratio"):
            value = getattr(self, key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{key} must be finite and positive")
        if self.max_total_tokens is not None:
            positive_int(self.max_total_tokens, "max_total_tokens")


@dataclass
class Case:
    task: Task
    split: str
    checks: list[dict[str, Any]]
    fixtures: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        identifier(self.task.id)
        if not isinstance(self.task.description, str) or not self.task.description.strip() or self.split not in {"development", "validation", "regression"}:
            raise ValueError("each case needs a description and a valid split")
        if not self.checks:
            raise ValueError(f"{self.task.id}: objective checks are required")
        if not isinstance(self.task.constraints, list) or any(not isinstance(c, str) for c in self.task.constraints):
            raise ValueError("task constraints must be a list of strings")
        size, normalized = 0, set()
        for name, text in self.fixtures.items():
            clean = relative_path(name)
            if clean != name or clean in normalized:
                raise ValueError("fixture paths must be canonical and unique")
            normalized.add(clean)
            if not isinstance(text, str):
                raise ValueError("fixtures must contain UTF-8 text")
            size += len(text.encode("utf-8"))
        for name in normalized:
            if any(str(parent) in normalized for parent in PurePosixPath(name).parents):
                raise ValueError("a fixture file cannot also be a parent directory")
        if size > MAX_FIXTURE_BYTES:
            raise ValueError("fixture snapshot exceeds 8 MiB")
        for check in self.checks:
            unknown(check, {"kind", "expected", "target", "weight", "command", "files", "timeout"}, "check")
            kind = check.get("kind")
            if kind not in {"exact", "contains", "json_equals", "file_equals", "command"}:
                raise ValueError(f"unsupported check kind: {kind!r}")
            weight = check.get("weight", 1.0)
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight <= 0:
                raise ValueError("check weights must be finite and positive")
            if kind != "command" and "expected" not in check:
                raise ValueError("a check requires expected")
            if kind in {"exact", "contains", "file_equals"} and not isinstance(check["expected"], str):
                raise ValueError("text check expected must be a string")
            if kind == "file_equals":
                relative_path(check.get("target", ""))
            if kind == "command":
                validate_command(check.get("command"))
                timeout = check.get("timeout", 30)
                if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError("command timeout must be finite and positive")
                for name, text in check.get("files", {}).items():
                    relative_path(name)
                    if not isinstance(text, str):
                        raise ValueError("grader files must be text")
        if not math.isfinite(sum(check.get("weight", 1.0) for check in self.checks)):
            raise ValueError("total check weight must be finite")
        canonical(self.checks)

    def to_dict(self) -> dict:
        return {"task": self.task.to_dict(), "split": self.split, "checks": self.checks, "fixtures": self.fixtures}


@dataclass
class Variant:
    label: str
    harness: Harness

    @property
    def id(self) -> str:
        return "var-" + digest({"label": self.label, "harness": self.harness.to_dict()})[:24]

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "harness": self.harness.to_dict()}


@dataclass
class ExperimentSpec:
    name: str
    baseline: Harness
    variants: list[Variant]
    cases: list[Case]
    policy: Policy = field(default_factory=Policy)
    backend: dict[str, Any] = field(default_factory=lambda: {"kind": "mock"})
    provenance: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        identifier(self.name)
        self.policy.validate()
        if not self.variants or not self.cases:
            raise ValueError("at least one candidate and one case are required")
        labels = [identifier(v.label) for v in self.variants]
        if "baseline" in labels or len(set(labels)) != len(labels):
            raise ValueError("variant labels must be unique and not 'baseline'")
        ids, fingerprints, splits = set(), set(), set()
        for case in self.cases:
            case.validate()
            if case.task.id in ids:
                raise ValueError("task IDs must be globally unique")
            # Even an exact duplicate under a different ID can leak held-out cases.
            fingerprint = digest({"description": " ".join(case.task.description.lower().split()),
                                  "constraints": case.task.constraints, "fixtures": case.fixtures})
            if fingerprint in fingerprints:
                raise ValueError("duplicate case content; do not reuse tasks across splits")
            ids.add(case.task.id)
            fingerprints.add(fingerprint)
            splits.add(case.split)
        if splits != {"development", "validation", "regression"}:
            raise ValueError("development, validation and regression cases are all required")
        if len(self.cases) * (len(self.variants) + 1) * self.policy.repetitions > 100000:
            raise ValueError("experiment exceeds 100000 planned trials")
        unknown(self.backend, {"kind", "solver_model", "judge_model", "max_tokens", "command", "timeout", "env_allow", "files"}, "backend")
        kind = self.backend.get("kind", "mock")
        if kind not in {"mock", "anthropic", "command", "custom"}:
            raise ValueError(f"unsupported backend: {kind}")
        if kind == "anthropic":
            for key in ("solver_model", "judge_model"):
                if not isinstance(self.backend.get(key), str) or not self.backend[key].strip():
                    raise ValueError(f"freeze {key} explicitly for experiments")
            positive_int(self.backend.get("max_tokens", 4096), "max_tokens")
        if kind == "command":
            validate_command(self.backend.get("command"))
            timeout = self.backend.get("timeout", 120)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("backend timeout must be finite and positive")
            env_allow = self.backend.get("env_allow", [])
            if not isinstance(env_allow, list) or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", x) for x in env_allow):
                raise ValueError("env_allow must contain environment variable names, never values")
        canonical(self.to_dict())

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "name": self.name,
                "baseline": self.baseline.to_dict(), "variants": [v.to_dict() for v in self.variants],
                "cases": [c.to_dict() for c in self.cases], "policy": asdict(self.policy),
                "backend": self.backend, "provenance": self.provenance}


def validate_command(command: Any) -> None:
    if not isinstance(command, list) or not command or any(not isinstance(s, str) or not s or "\0" in s for s in command):
        raise ValueError("command must be a nonempty argv list; shell strings are not supported")


def snapshot(directory: Path) -> dict[str, str]:
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("fixture directory must be a real directory")
    result, total = {}, 0
    for path in sorted(directory.rglob("*")):
        name = path.relative_to(directory).as_posix()
        if name.split("/", 1)[0] == ".runtime":
            continue
        relative_path(name)
        if path.is_symlink():
            raise ValueError("symlinks are not allowed in fixtures or artifacts")
        if path.is_file():
            total += path.stat().st_size
            if total > MAX_FIXTURE_BYTES:
                raise ValueError("workspace snapshot exceeds 8 MiB")
            result[name] = path.read_text(encoding="utf-8")
    return result


def load_spec(path: Path | str) -> ExperimentSpec:
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    unknown(data, {"name", "baseline", "variants", "cases", "policy", "backend", "provenance"}, "experiment")
    home = path.parent

    def harness(value: Any) -> Harness:
        if isinstance(value, str):
            value = json.loads((home / value).read_text(encoding="utf-8"))
        return harness_from_dict(value)

    baseline = harness(data["baseline"])
    variants = []
    for item in data["variants"]:
        unknown(item, {"label", "harness", "edits"}, "variant")
        if ("harness" in item) == ("edits" in item):
            raise ValueError("each variant needs exactly one of harness or edits")
        variant = harness(item["harness"]) if "harness" in item else baseline.copy()
        for edit in item.get("edits", []):
            variant = apply_edit(variant, edit_from_mapping(edit))
        variants.append(Variant(item["label"], variant))
    cases = []
    for item in data["cases"]:
        unknown(item, {"task", "split", "checks", "fixtures", "fixture_dir"}, "case")
        if "fixtures" in item and "fixture_dir" in item:
            raise ValueError("use fixtures or fixture_dir, not both")
        task = Task(**item["task"])
        fixtures = snapshot(home / item["fixture_dir"]) if "fixture_dir" in item else item.get("fixtures", {})
        cases.append(Case(task, item["split"], item["checks"], fixtures))
    backend = dict(data.get("backend", {"kind": "mock"}))
    # Declared external code is fingerprinted and checked before every trial.
    file_hashes = {}
    for name in backend.get("files", []):
        absolute = (home / name).resolve()
        file_hashes[str(absolute)] = hashlib.sha256(absolute.read_bytes()).hexdigest()
    backend["files"] = file_hashes
    if "command" in backend:
        backend["command"] = [s.replace("{config_dir}", str(home)) for s in backend["command"]]
    spec = ExperimentSpec(data["name"], baseline, variants, cases,
                          Policy(**data.get("policy", {})), backend, data.get("provenance", {}))
    spec.validate()
    return spec
