"""What produced a result: implementation hash, git state, adapter and backend declarations, drift checks.

A confirmation is only worth anything if every trial ran under the same code, the same grader rubric and the
same model, and if the adapter can actually apply what the candidate changes. Nothing here reads a secret:
environment variables are recorded by name only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from harness_workbench.core.harness import ALL_COMPONENTS
from harness_workbench.experiments.contracts import TrialRecord
from harness_workbench.experiments.manifest import ExperimentSpec

SEED_SCOPE = "paired per repetition; honored only if the adapter honors it"
DEPENDENCY_SCOPE = "stdlib only; adapter dependencies not hashed"
_GIT_TIMEOUT_SECONDS = 15.0


def implementation_hash(package_root: Path | None = None) -> str:
    """sha256 over the sorted relative paths and bytes of ``harness_workbench/**/*.py``."""
    package = Path(package_root) if package_root is not None else Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for item in sorted(package.rglob("*.py")):
        digest.update(item.relative_to(package).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str | None:
    """stdout of ``git <args>`` run in ``root``, or ``None`` when git is missing, fails or times out."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    try:
        done = subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True,
                              timeout=_GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def git_state(root: Path) -> dict[str, Any]:
    """``{"commit", "dirty"}`` of the repository whose top level is ``root``; ``None`` values otherwise.

    A directory that merely sits inside some other repository (a site-packages folder in a project
    checkout, say) is not the workbench's repository, so it reports nothing rather than that commit.
    """
    unknown: dict[str, Any] = {"commit": None, "dirty": None}
    root = Path(root)
    if not root.is_dir():
        return unknown
    top = _git(root, "rev-parse", "--show-toplevel")
    if top is None or os.path.realpath(top.strip()) != os.path.realpath(root):
        return unknown
    commit = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain")
    return {"commit": commit.strip() if commit and commit.strip() else None,
            "dirty": None if status is None else bool(status.strip())}


def _backend_record(backend: Any) -> dict[str, Any]:
    capabilities: dict[str, bool] | None = None
    try:
        capabilities = dataclasses.asdict(backend.capabilities())
    except Exception:  # an unprobed sandbox has no declared capabilities yet
        capabilities = None
    digest = None
    image_digest = getattr(backend, "image_digest", None)
    if callable(image_digest):
        try:
            digest = image_digest()
        except Exception:
            digest = None
    image = getattr(backend, "image", None)
    return {"name": str(getattr(backend, "name", type(backend).__name__)), "capabilities": capabilities,
            "image": image if isinstance(image, str) else None, "image_digest": digest}


def adapter_supports(executor: Any) -> frozenset[str]:
    """The components the adapter declares it can apply; an adapter that declares nothing claims all of them."""
    declared = getattr(executor, "supports", None)
    return frozenset(ALL_COMPONENTS) if declared is None else frozenset(declared)


def provenance_record(spec: ExperimentSpec, executor: Any, backend: Any, *, root: Path) -> dict[str, Any]:
    """Everything needed to say which code, adapter and sandbox produced a result. No environment values."""
    argv = getattr(executor, "argv", None)
    environment = spec.environment
    return {
        **git_state(Path(root)),
        "implementation_hash": implementation_hash(),
        "schema_version": spec.schema_version,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "executable": sys.executable,
        "executor": type(executor).__name__,
        "adapter_argv": [str(item) for item in argv] if isinstance(argv, (list, tuple)) else [],
        "adapter_supports": sorted(adapter_supports(executor)),
        "backend": _backend_record(backend),
        "model_requested": str(getattr(executor, "solver_model", "unreported")),
        "environment": {
            "solver_env_passthrough": list(environment.solver_env_passthrough),
            "limits": dataclasses.asdict(environment.limits),
            "mode": environment.mode,
        },
        "seed_scope": SEED_SCOPE,
        "dependency_scope": DEPENDENCY_SCOPE,
    }


def detect_drift(records: list[TrialRecord], rubric_hashes: dict[str, str]) -> list[str]:
    """Problems that make trials of one experiment incomparable: code, rubric or model changed mid-run."""
    problems: list[str] = []
    hashes = sorted({record.implementation_hash for record in records})
    if len(hashes) > 1:
        problems.append(f"implementation drift: trials ran under {len(hashes)} different implementation hashes")
    changed = sorted({record.task_id for record in records
                      if record.task_id in rubric_hashes and record.evaluation.rubric_hash != rubric_hashes[record.task_id]})
    for task_id in changed:
        problems.append(f"rubric drift: task {task_id} was evaluated with a rubric other than the frozen one")
    models = sorted({record.execution.returned_model for record in records
                     if record.execution.returned_model is not None})
    if len(models) > 1:
        problems.append(f"model drift: the adapter reported {len(models)} different models ({', '.join(models)})")
    return problems


def unsupported_components(spec: ExperimentSpec, supports: frozenset[str]) -> list[str]:
    """For each candidate, the components that differ from the baseline's and that the adapter cannot apply."""
    baseline = spec.variants[0].harness.public_components()
    problems: list[str] = []
    for variant in spec.variants[1:]:
        components = variant.harness.public_components()
        for name in ALL_COMPONENTS:
            if components[name] != baseline[name] and name not in supports:
                problems.append(f"candidate {variant.id} changes {name}, which the adapter does not declare "
                                f"support for; the change cannot reach the model, so nothing can be confirmed")
    return problems
