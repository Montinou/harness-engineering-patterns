"""Frozen grader bundle, validated workspace snapshot and out-of-workspace evaluation.

The solver's workspace is untrusted data. After the solver has finished, the
supervisor copies it into a validated read-only-by-intent snapshot (regular files
and directories only), materializes the grader bundle next to it in a fresh
directory the solver never saw, and runs the checks from the bundle. Nothing the
solver wrote can change which grader code runs, and nothing the grader reports is
believed unless the supervisor compared it itself (see ``checks.evaluate_checks``).

Import note: ``manifest`` imports this module (``TaskSpec.bundle``) and
``backends`` imports ``manifest``, so this module must not import ``backends`` or
``checks`` at module level; both are used lazily or for annotations only.
"""
from __future__ import annotations

import copy
import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .contracts import EvaluationResult
from .status import EvaluationStatus
from .validation import json_dumps_strict, require_int

if TYPE_CHECKING:  # pragma: no cover - annotations only; see the import note above
    from .backends import ExecutionBackend, ProcessLimits

__all__ = [
    "EvaluationEnvironment", "GraderBundle", "SnapshotError", "SnapshotLimits", "digest_path",
    "evaluate_bundle", "snapshot_workspace",
]

_COPY_CHUNK = 65536


def digest_path(path: Path) -> str:
    """Content fingerprint including names, permissions and symlink targets."""
    h = hashlib.sha256()
    if path.is_file():
        h.update(path.read_bytes())
        return h.hexdigest()
    for item in sorted(path.rglob("*")):
        rel = item.relative_to(path).as_posix()
        h.update(rel.encode("utf-8"))
        if item.is_symlink():
            h.update(b"SYMLINK")
            h.update(os.readlink(item).encode("utf-8"))
        elif item.is_file():
            h.update(item.read_bytes())
            h.update(str(item.stat().st_mode & 0o777).encode("ascii"))
    return h.hexdigest()


# --- grader bundle -----------------------------------------------------------------


@dataclass(frozen=True)
class GraderBundle:
    task_id: str
    checks: tuple[dict, ...]
    files: tuple[tuple[str, bytes], ...]
    version: str
    hash: str

    @classmethod
    def build(cls, task_id: str, checks: Any, files_dir: Path | None, version: str = "1") -> "GraderBundle":
        """Freeze the checks and the bytes of every grader file at this moment."""
        frozen_checks = tuple(copy.deepcopy(list(checks)))
        files: list[tuple[str, bytes]] = []
        if files_dir is not None:
            for item in sorted(p for p in files_dir.rglob("*") if p.is_file()):
                files.append((item.relative_to(files_dir).as_posix(), item.read_bytes()))
        files.sort(key=lambda entry: entry[0])
        h = hashlib.sha256()
        h.update(json_dumps_strict(list(frozen_checks), sort_keys=True, separators=(",", ":")).encode("utf-8"))
        for name, data in files:
            encoded = name.encode("utf-8")
            h.update(b"\0F" + str(len(encoded)).encode("ascii") + b":" + encoded)
            h.update(str(len(data)).encode("ascii") + b":" + data)
        return cls(task_id, frozen_checks, tuple(files), version, h.hexdigest())

    def materialize(self, into: Path) -> None:
        """Write the files under ``into``, then make files 0o444 and directories 0o555."""
        into = Path(into)
        into.mkdir(parents=True, exist_ok=True)
        root = into.resolve()
        for name, data in self.files:
            target = (root / name).resolve()
            if os.path.isabs(name) or not target.is_relative_to(root) or target == root:
                raise ValueError(f"grader file path escapes the bundle: {name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
            for filename in filenames:
                os.chmod(os.path.join(dirpath, filename), 0o444)
            os.chmod(dirpath, 0o555)


# --- workspace snapshot --------------------------------------------------------------


class SnapshotError(RuntimeError):
    """The solver workspace cannot be snapshotted safely."""


@dataclass(frozen=True)
class SnapshotLimits:
    max_files: int
    max_total_bytes: int

    def __post_init__(self) -> None:
        require_int("max_files", self.max_files, minimum=1)
        require_int("max_total_bytes", self.max_total_bytes, minimum=1)


def _copy_regular_file(source: Path, target: Path, listed: os.stat_result, budget: int) -> int:
    """Copy one regular file through an fd that was verified after opening; return bytes copied."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(source, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (listed.st_dev, listed.st_ino):
            raise SnapshotError(f"{source.name}: changed while it was being snapshotted")
        if opened.st_nlink > 1:
            raise SnapshotError(f"{source.name}: hard-linked files are not allowed")
        copied = 0
        out_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            while True:
                chunk = os.read(fd, _COPY_CHUNK)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > budget:
                    raise SnapshotError("workspace exceeds the snapshot byte limit")
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out_fd, view):]
        finally:
            os.close(out_fd)
        os.chmod(target, stat.S_IMODE(opened.st_mode))
        return copied
    finally:
        os.close(fd)


def snapshot_workspace(workspace: Path, destination: Path, limits: SnapshotLimits) -> str:
    """Copy regular files and directories of ``workspace`` into ``destination``; return its digest.

    Raises ``SnapshotError`` on symlinks, hard-linked files, sockets, FIFOs, devices,
    any path that does not resolve under ``workspace``, and on exceeding the limits.
    """
    workspace = Path(workspace)
    destination = Path(destination)
    try:
        if workspace.is_symlink() or not workspace.is_dir():
            raise SnapshotError("workspace must be a real directory")
        root = workspace.resolve()
        dest = destination.resolve()
        if dest == root or dest.is_relative_to(root) or root.is_relative_to(dest):
            raise SnapshotError("snapshot destination overlaps the workspace")
        if destination.exists() and any(destination.iterdir()):
            raise SnapshotError("snapshot destination must be empty")
        destination.mkdir(parents=True, exist_ok=True)
        entries = 0
        total = 0
        pending: list[tuple[Path, Path]] = [(workspace, destination)]
        while pending:
            source_dir, target_dir = pending.pop()
            for name in sorted(os.listdir(source_dir)):
                source = source_dir / name
                listed = os.lstat(source)
                entries += 1
                if entries > limits.max_files:
                    raise SnapshotError(f"workspace has more than {limits.max_files} entries")
                if stat.S_ISLNK(listed.st_mode):
                    raise SnapshotError(f"{source.relative_to(workspace).as_posix()}: symlinks are not allowed")
                if not source.resolve().is_relative_to(root):
                    raise SnapshotError(f"{name}: path is outside the workspace")
                target = target_dir / name
                if stat.S_ISDIR(listed.st_mode):
                    target.mkdir()
                    pending.append((source, target))
                elif stat.S_ISREG(listed.st_mode):
                    if listed.st_nlink > 1:
                        raise SnapshotError(f"{source.relative_to(workspace).as_posix()}: hard-linked files are not allowed")
                    total += _copy_regular_file(source, target, listed, limits.max_total_bytes - total)
                else:
                    raise SnapshotError(
                        f"{source.relative_to(workspace).as_posix()}: only regular files and directories are allowed")
        return digest_path(destination)
    except SnapshotError:
        raise
    except OSError as exc:
        raise SnapshotError(f"{type(exc).__name__}: {exc}") from exc


# --- evaluation ----------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationEnvironment:
    """Directories and backend for one evaluation; ``out`` is the only writable result directory."""
    artifact: Path
    bundle: Path
    out: Path
    home: Path
    tmp: Path
    backend: "ExecutionBackend"
    limits: "ProcessLimits"


def _remove_tree(path: Path) -> None:
    for dirpath, dirnames, _filenames in os.walk(path):
        for directory in [dirpath, *(os.path.join(dirpath, d) for d in dirnames)]:
            try:
                if not os.path.islink(directory):
                    os.chmod(directory, 0o700)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _error_result(trial_id: str, bundle: GraderBundle, artifact_hash: str | None, checks: list[dict],
                  message: str) -> EvaluationResult:
    return EvaluationResult(trial_id, EvaluationStatus.EVALUATION_ERROR, bundle.hash, artifact_hash,
                            checks, None, message)


def evaluate_bundle(
    bundle: GraderBundle,
    outputs: dict,
    workspace: Path | None,
    *,
    backend: "ExecutionBackend",
    limits: "ProcessLimits",
    snapshot_limits: SnapshotLimits,
    trial_id: str,
) -> EvaluationResult:
    """Evaluate ``outputs`` and a snapshot of ``workspace`` with the frozen ``bundle``.

    Call this only after the solver has finished: the bundle is materialized into a
    fresh directory that did not exist while the solver ran.
    """
    from .checks import evaluate_checks  # lazy: checks -> backends -> manifest -> grader

    temp = Path(tempfile.mkdtemp(prefix="harness-eval-"))
    artifact_hash: str | None = None
    try:
        artifact = temp / "artifact"
        bundle_dir = temp / "bundle"
        out = temp / "out"
        home = temp / "home"
        tmp = temp / "tmp"
        for directory in (out, home, tmp):
            directory.mkdir()
        if workspace is not None:
            try:
                artifact_hash = snapshot_workspace(workspace, artifact, snapshot_limits)
            except SnapshotError as exc:
                return _error_result(trial_id, bundle, None, [], f"snapshot failed: {exc}")
        try:
            bundle.materialize(bundle_dir)
        except (OSError, ValueError) as exc:
            return _error_result(trial_id, bundle, artifact_hash, [],
                                 f"grader bundle could not be materialized: {type(exc).__name__}: {exc}")
        if not bundle.checks:
            return _error_result(trial_id, bundle, artifact_hash, [], "grader bundle has no checks")
        env = EvaluationEnvironment(artifact, bundle_dir, out, home, tmp, backend, limits)
        try:
            results = evaluate_checks(bundle.checks, outputs, env)
        except Exception as exc:  # evaluator failure, never a task failure
            return _error_result(trial_id, bundle, artifact_hash, [],
                                 f"evaluation raised {type(exc).__name__}: {exc}")
        errored = [r for r in results if r["status"] == "error"]
        if errored:
            message = "; ".join(f"{r['id']}: {r['detail']}" for r in errored)
            return _error_result(trial_id, bundle, artifact_hash, results, message)
        total = sum(float(r["weight"]) for r in results)
        earned = sum(float(r["weight"]) for r in results if r["passed"])
        status = EvaluationStatus.PASSED if all(r["passed"] for r in results) else EvaluationStatus.FAILED
        score = earned / total if total else 0.0
        return EvaluationResult(trial_id, status, bundle.hash, artifact_hash, results, score, None)
    finally:
        _remove_tree(temp)
