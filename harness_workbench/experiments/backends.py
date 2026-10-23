"""Execution backends, process limits and the minimal child environment.

``LocalBackend`` runs a command as a bounded child process. It is *trusted
local* execution, not a security sandbox: it declares ``NO_GUARANTEES`` so a
caller that needs isolation can refuse it with ``capabilities().missing(...)``.
What it does enforce is resource containment: a wall-clock timeout, a cap on
captured output per stream, and termination of the whole process group (the
child runs in its own session). Only descendants that stay in that process
group are killed. A descendant that escapes it (for example with ``setsid()``)
is not killed and may keep the output pipes open; ``run`` does not wait for it
and returns within the timeout plus a small fixed bound, abandoning the pipes.
"""
from __future__ import annotations

import os
import re
import signal
import select
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Protocol, Sequence

from .manifest import EnvironmentConfig, ProcessLimitsConfig
from .status import ExecutionStatus

__all__ = [
    "BackendCapabilities", "BackendUnavailable", "ExecutionBackend", "GRADER_FORBIDDEN_PASSTHROUGH",
    "LocalBackend", "Mounts", "NO_GUARANTEES", "ProcessLimits", "ProcessOutcome", "SANDBOX_REQUIRED",
    "build_backend", "grader_env", "minimal_env",
]

_READ_CHUNK = 65536
_READER_JOIN_SECONDS = 1.0
_READER_POLL_SECONDS = 0.05
_COPIED_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")

GRADER_FORBIDDEN_PASSTHROUGH = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)


class BackendUnavailable(RuntimeError):
    """The requested execution backend cannot be provided on this host."""


@dataclass(frozen=True)
class ProcessLimits:
    timeout_seconds: float
    max_output_bytes: int = 1_000_000
    max_files: int = 2000
    max_total_bytes: int = 50_000_000

    @classmethod
    def from_config(cls, cfg: ProcessLimitsConfig) -> "ProcessLimits":
        return cls(
            timeout_seconds=cfg.timeout_seconds,
            max_output_bytes=cfg.max_output_bytes,
            max_files=cfg.max_files,
            max_total_bytes=cfg.max_total_bytes,
        )


@dataclass(frozen=True)
class BackendCapabilities:
    isolated_filesystem: bool
    network_denied: bool
    unprivileged_user: bool
    process_containment: bool
    no_docker_socket: bool
    read_only_bundle: bool

    def missing(self, required: "BackendCapabilities") -> list[str]:
        return [f.name for f in fields(self) if getattr(required, f.name) and not getattr(self, f.name)]

    def satisfies(self, required: "BackendCapabilities") -> bool:
        return not self.missing(required)


SANDBOX_REQUIRED = BackendCapabilities(True, True, True, True, True, True)
NO_GUARANTEES = BackendCapabilities(False, False, False, False, False, False)


@dataclass(frozen=True)
class Mounts:
    read_only: tuple[Path, ...] = ()
    writable: tuple[Path, ...] = ()


@dataclass
class ProcessOutcome:
    status: ExecutionStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    truncated: bool
    error: str | None
    retryable: bool = False


class ExecutionBackend(Protocol):
    name: str
    python: str

    def capabilities(self) -> BackendCapabilities: ...

    def run(self, argv: list[str], *, cwd: Path, env: dict[str, str],
            limits: ProcessLimits, mounts: Mounts) -> ProcessOutcome: ...


def minimal_env(*, home: Path, tmp: Path, passthrough: Sequence[str] = ()) -> dict[str, str]:
    """Build the child environment from scratch; nothing is inherited implicitly."""
    env = {name: os.environ[name] for name in _COPIED_ENV if name in os.environ}
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONSAFEPATH"] = "1"
    for name in passthrough:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def grader_env(home: Path, tmp: Path) -> dict[str, str]:
    """Environment for graders: the minimal one, never any passthrough."""
    return minimal_env(home=home, tmp=tmp)


def _killpg(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class _Capture:
    """Reads one pipe fd, keeping at most ``cap`` bytes; kills the group on overflow.

    The reader polls with ``select`` and honours ``stop`` so the owner can end it
    and only then close the stream; closing a pipe another thread is blocked
    reading would hang behind that thread.
    """

    def __init__(self, stream, cap: int, on_overflow) -> None:
        self.stream = stream
        self.cap = cap
        self.on_overflow = on_overflow
        self.data = bytearray()
        self.overflowed = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)

    def _read(self) -> None:
        try:
            fd = self.stream.fileno()
            while not self.stop.is_set():
                if not select.select([fd], [], [], _READER_POLL_SECONDS)[0]:
                    continue
                chunk = os.read(fd, _READ_CHUNK)
                if not chunk:
                    return
                room = self.cap - len(self.data)
                if len(chunk) > room:
                    if room > 0:
                        self.data += chunk[:room]
                    if not self.overflowed:
                        self.overflowed = True
                        self.on_overflow()
                    # keep draining so the writer is never blocked on a full pipe
                else:
                    self.data += chunk
        except (OSError, ValueError):
            return

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


class LocalBackend:
    """Bounded child process on the host. Trusted code only; no isolation."""

    name = "trusted_local"
    python = sys.executable

    def capabilities(self) -> BackendCapabilities:
        return NO_GUARANTEES

    def run(self, argv: list[str], *, cwd: Path, env: dict[str, str],
            limits: ProcessLimits, mounts: Mounts) -> ProcessOutcome:
        started = time.perf_counter()

        def elapsed_ms() -> float:
            return float((time.perf_counter() - started) * 1000.0)

        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, bufsize=0,
            )
        except OSError as exc:
            return ProcessOutcome(
                status=ExecutionStatus.EXECUTION_ERROR, exit_code=None, stdout="", stderr="",
                duration_ms=elapsed_ms(), truncated=False,
                error=f"could not start process: {type(exc).__name__}: {exc}", retryable=True,
            )

        pgid = proc.pid  # start_new_session makes the child its own group leader
        limit_hit = threading.Event()

        def overflow() -> None:
            limit_hit.set()
            _killpg(pgid)

        out = _Capture(proc.stdout, limits.max_output_bytes, overflow)
        err = _Capture(proc.stderr, limits.max_output_bytes, overflow)
        out.thread.start()
        err.thread.start()
        timed_out = False
        try:
            try:
                proc.wait(timeout=limits.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _killpg(pgid)
                proc.wait()
        finally:
            # Residual same-session children die before the caller snapshots anything.
            _killpg(pgid)
            # Readers normally reach EOF at once. A descendant that escaped the group
            # (setsid) may still hold the pipes: stop the readers instead of waiting for it.
            deadline = time.monotonic() + _READER_JOIN_SECONDS
            for capture in (out, err):
                capture.thread.join(timeout=max(0.0, deadline - time.monotonic()))
            for capture in (out, err):
                capture.stop.set()
                capture.thread.join(timeout=_READER_JOIN_SECONDS)
                if not capture.thread.is_alive():
                    try:
                        capture.stream.close()
                    except (OSError, ValueError):
                        pass

        truncated = out.overflowed or err.overflowed
        status: ExecutionStatus
        error: str | None = None
        if limit_hit.is_set():
            status = ExecutionStatus.RESOURCE_LIMIT
            error = f"output exceeded {limits.max_output_bytes} bytes on a stream"
        elif timed_out:
            status = ExecutionStatus.TIMEOUT
            error = f"process exceeded {limits.timeout_seconds} seconds"
        elif proc.returncode == 0:
            status = ExecutionStatus.COMPLETED
        else:
            status = ExecutionStatus.TASK_FAILURE
        return ProcessOutcome(
            status=status, exit_code=proc.returncode, stdout=out.text(), stderr=err.text(),
            duration_ms=elapsed_ms(), truncated=truncated, error=error,
        )


def build_backend(environment: EnvironmentConfig) -> ExecutionBackend:
    if environment.mode == "trusted_local":
        return LocalBackend()
    if environment.mode == "sandboxed":
        raise BackendUnavailable("sandboxed backend not implemented")
    raise BackendUnavailable(f"unknown environment mode {environment.mode!r}")
