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
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Protocol, Sequence

from .manifest import EnvironmentConfig, ProcessLimitsConfig
from .status import ExecutionStatus

__all__ = [
    "BackendCapabilities", "BackendUnavailable", "DockerBackend", "ExecutionBackend",
    "GRADER_FORBIDDEN_PASSTHROUGH", "LocalBackend", "Mounts", "NO_GUARANTEES", "ProcessLimits",
    "ProcessOutcome", "SANDBOX_REQUIRED", "build_backend", "grader_env", "minimal_env", "require_backend",
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


_DOCKER_CLIENT_ENV = (
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_RUNTIME_DIR",
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
)
_INFO_TIMEOUT_SECONDS = 15.0
_SELF_TEST_TIMEOUT_SECONDS = 120.0  # the first run may have to pull the image
_CONTROL_TIMEOUT_SECONDS = 60.0
_CONTROL_OUTPUT_BYTES = 65536
_SANDBOX_USER = "65534:65534"
_SELF_TEST_OK = "HARNESS_SANDBOX_OK"
_SELF_TEST_FAIL = "HARNESS_SANDBOX_FAIL"
_MOUNT_ROOT = "/sandbox"
_DOCKER_RUN_ERROR = 125  # docker run itself failed (daemon, name conflict, mount rejected, lost mid-run)
_DOCKER_EXEC_ERRORS = (126, 127)  # docker could not exec the command; solver exits look the same
_DOCKER_STDERR_PREFIXES = ("docker:", "exec:")

# Runs inside the container. Each failed property is printed by name; the OK marker is
# required, so a container that never ran the script cannot pass the self-test.
_SELF_TEST_SCRIPT = f"""
import errno, os, sys, urllib.request
failed = []
if os.getuid() != 65534:
    failed.append("unprivileged_user")
try:
    urllib.request.urlopen("http://example.com", timeout=3)
    failed.append("network_denied")
except Exception:
    pass
if os.path.exists("/var/run/docker.sock") or os.path.exists("/run/docker.sock"):
    failed.append("no_docker_socket")
try:
    open("{_MOUNT_ROOT}/0/.sandbox_probe", "w").close()
    failed.append("read_only_bundle")
except OSError as exc:
    if exc.errno != errno.EROFS:
        failed.append("read_only_bundle")
try:
    open("/.sandbox_probe", "w").close()
    failed.append("isolated_filesystem")
except OSError as exc:
    if exc.errno != errno.EROFS:
        failed.append("isolated_filesystem")
if os.getpid() == 1:
    failed.append("process_containment")
for name in failed:
    print("{_SELF_TEST_FAIL} " + name)
if not failed:
    print("{_SELF_TEST_OK}")
sys.exit(1 if failed else 0)
"""


def _open_tree(root: Path) -> None:
    """``chmod -R a+rwX`` for a tree the host owns, so the container user can use all of it.

    Symlinks are neither followed nor changed: a link planted in a fixture must not reach a file
    outside the tree. Raises ``OSError`` when a path cannot be changed.
    """
    def fail(error: OSError) -> None:
        raise error

    def widen(path: str | Path, add: int) -> None:
        mode = os.lstat(path).st_mode
        if not stat.S_ISLNK(mode):
            os.chmod(path, stat.S_IMODE(mode) | add)

    widen(root, 0o777)
    for dirpath, dirnames, filenames in os.walk(root, onerror=fail):
        for directory in dirnames:
            widen(os.path.join(dirpath, directory), 0o777)
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            mode = os.lstat(path).st_mode
            if stat.S_ISREG(mode):
                os.chmod(path, stat.S_IMODE(mode) | 0o666 | (0o111 if mode & 0o111 else 0))


class DockerBackend:
    """Candidate code in a locked-down container, or an explicit refusal.

    ``probe`` must succeed before ``capabilities`` or ``run`` are usable; there is no
    degraded mode and nothing here ever falls back to ``LocalBackend`` for the task itself.
    """

    name = "sandboxed"

    def __init__(self, image: str, python: str, docker: str = "docker", memory: str = "512m",
                 pids_limit: int = 256) -> None:
        self.image = image
        self.python = python
        self.docker = docker
        self.memory = memory
        self.pids_limit = pids_limit
        self._probed = False

    # -- docker client plumbing -------------------------------------------------

    @staticmethod
    def _client_env() -> dict[str, str]:
        return {name: os.environ[name] for name in _DOCKER_CLIENT_ENV if name in os.environ}

    def _control(self, argv: list[str], timeout: float) -> ProcessOutcome:
        return LocalBackend().run(
            argv, cwd=Path(tempfile.gettempdir()), env=self._client_env(),
            limits=ProcessLimits(timeout_seconds=timeout, max_output_bytes=_CONTROL_OUTPUT_BYTES),
            mounts=Mounts())

    @staticmethod
    def _detail(outcome: ProcessOutcome) -> str:
        text = (outcome.error or outcome.stderr or outcome.stdout or "").strip()
        return " ".join(text.split())[:300] or f"status {outcome.status.value}"

    def _remove_container(self, name: str) -> bool:
        """``docker rm -f``; True when the container is gone (``-f`` also succeeds for a missing one)."""
        return self._control(
            [self.docker, "rm", "-f", name], _CONTROL_TIMEOUT_SECONDS).status is ExecutionStatus.COMPLETED

    # -- command line -----------------------------------------------------------

    def command_line(self, argv: list[str], cwd: Path, env: dict[str, str], limits: ProcessLimits,
                     mounts: Mounts, name: str | None = None) -> list[str]:
        """The full ``docker run`` argv. Pure: no Docker, no filesystem writes."""
        entries: list[tuple[Path, str, str]] = []
        for host in mounts.read_only:
            entries.append((Path(host), f"{_MOUNT_ROOT}/{len(entries)}", "ro"))
        for host in mounts.writable:
            entries.append((Path(host), f"{_MOUNT_ROOT}/{len(entries)}", "rw"))
        forms: dict[str, str] = {}
        volumes: list[str] = []
        for host, point, mode in entries:
            resolved = str(host.resolve())
            for form in (os.path.abspath(host), resolved):
                if any(ch in form for ch in ":\n"):
                    raise ValueError(f"mount path {form!r} cannot be passed to docker -v")
                forms[form] = point
            volumes += ["-v", f"{resolved}:{point}:{mode}"]
        if forms:
            alternatives = "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True))
            pattern = re.compile(rf"(?<![\w.\-/])(?:{alternatives})(?![\w.\-])")
        else:
            pattern = None

        def rewrite(text: str) -> str:
            return pattern.sub(lambda match: forms[match.group(0)], text) if pattern else text

        workdir = rewrite(os.path.abspath(cwd))
        if not workdir.startswith(f"{_MOUNT_ROOT}/"):
            raise ValueError(f"cwd {str(cwd)!r} is not under any mount")
        line = [
            self.docker, "run", "--rm", "--init", "--name", name or f"hw-run-{uuid.uuid4().hex}",
            "--network", "none", "--user", _SANDBOX_USER, "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", str(self.pids_limit),
            "--memory", self.memory, "--read-only", "--tmpfs", "/tmp:rw,size=256m",
            *volumes, "-w", workdir,
        ]
        for key, value in env.items():
            if key == "PATH":
                # minimal_env copies the host PATH; forwarding it would override the image's own
                # PATH, make solver behavior depend on the host and leak the host layout. Dropping
                # it lets the image default apply (the self-test already runs that way).
                continue
            line += ["-e", f"{key}={rewrite(value)}"]
        line.append(self.image)
        line += [rewrite(arg) for arg in argv]
        return line

    # -- capability self-test ---------------------------------------------------

    def probe(self) -> None:
        """Verify the daemon and the isolation properties; raise ``BackendUnavailable`` on any failure."""
        self._probed = False
        info = self._control([self.docker, "info", "--format", "{{.ServerVersion}}"], _INFO_TIMEOUT_SECONDS)
        if info.status is not ExecutionStatus.COMPLETED or not info.stdout.strip():
            raise BackendUnavailable(f"docker daemon unavailable: {self._detail(info)}")
        with tempfile.TemporaryDirectory(prefix="hw-selftest-") as tmp:
            probe_dir = Path(tmp)
            probe_dir.chmod(0o777)  # only the read-only mount, not file modes, may stop the write
            name = f"hw-selftest-{uuid.uuid4().hex}"
            line = self.command_line(
                [self.python, "-c", _SELF_TEST_SCRIPT], cwd=probe_dir, env={},
                limits=ProcessLimits(timeout_seconds=_SELF_TEST_TIMEOUT_SECONDS),
                mounts=Mounts(read_only=(probe_dir,)), name=name)
            outcome = self._control(line, _SELF_TEST_TIMEOUT_SECONDS)
            if outcome.status in (ExecutionStatus.TIMEOUT, ExecutionStatus.RESOURCE_LIMIT):
                self._remove_container(name)
        failed = [row.split(None, 1)[1].strip() for row in outcome.stdout.splitlines()
                  if row.startswith(_SELF_TEST_FAIL + " ")]
        if failed:
            raise BackendUnavailable(f"sandbox self-test failed: {', '.join(failed)}")
        if outcome.status is not ExecutionStatus.COMPLETED or _SELF_TEST_OK not in outcome.stdout:
            raise BackendUnavailable(
                f"sandbox self-test did not complete (exit {outcome.exit_code}): {self._detail(outcome)}")
        self._probed = True

    def capabilities(self) -> BackendCapabilities:
        if not self._probed:
            raise BackendUnavailable("sandbox capabilities are unknown until probe() succeeds")
        return SANDBOX_REQUIRED

    def image_digest(self) -> str | None:
        outcome = self._control(
            [self.docker, "image", "inspect", "--format", "{{index .RepoDigests 0}}", self.image],
            _CONTROL_TIMEOUT_SECONDS)
        digest = outcome.stdout.strip()
        return digest if outcome.status is ExecutionStatus.COMPLETED and digest else None

    # -- execution --------------------------------------------------------------

    def run(self, argv: list[str], *, cwd: Path, env: dict[str, str],
            limits: ProcessLimits, mounts: Mounts) -> ProcessOutcome:
        if not self._probed:
            raise BackendUnavailable("sandbox backend used before a successful probe()")
        try:
            for path in mounts.writable:
                _open_tree(Path(path))  # uid 65534 must be able to use everything the host created there
        except OSError as exc:
            return ProcessOutcome(
                status=ExecutionStatus.EXECUTION_ERROR, exit_code=None, stdout="", stderr="",
                duration_ms=0.0, truncated=False,
                error=f"could not prepare writable mount: {type(exc).__name__}: {exc}")
        name = f"hw-run-{uuid.uuid4().hex}"
        line = self.command_line(argv, cwd, env, limits, mounts, name=name)
        outcome = LocalBackend().run(line, cwd=cwd, env=self._client_env(), limits=limits, mounts=Mounts())
        problems: list[str] = []
        if outcome.status in (ExecutionStatus.TIMEOUT, ExecutionStatus.RESOURCE_LIMIT):
            # A container that survives could keep changing the tree that is about to be snapshotted.
            if not self._remove_container(name):
                problems.append(f"container {name} could not be removed after {outcome.status.value}")
        outcome = self._classify_docker_failure(outcome)
        # Whatever the container wrote belongs to uid 65534: hand it back before anyone on the host
        # snapshots, reads or deletes it. A tree that cannot be reclaimed is not a solver result.
        if not self.cleanup(mounts.writable):
            problems.append("writable mounts could not be reclaimed for the host user")
        if problems:
            return replace(
                outcome, status=ExecutionStatus.EXECUTION_ERROR, retryable=True,
                error=f"docker infrastructure failure, not a solver result: {'; '.join(problems)}")
        return outcome

    @staticmethod
    def _classify_docker_failure(outcome: ProcessOutcome) -> ProcessOutcome:
        """Docker's own failures are infrastructure errors, never a scoreable solver failure.

        These codes are also what a solver can exit with, and the stderr prefix is solver-controlled, so
        the outcome is never ``retryable``: a retry the solver could trigger would be best-of-N against
        the baseline. It stays an ``EXECUTION_ERROR`` (fail closed) and the attempt remains the evidence.
        """
        if outcome.status is not ExecutionStatus.TASK_FAILURE:
            return outcome
        code = outcome.exit_code
        starts_with_docker = outcome.stderr.lstrip().startswith(_DOCKER_STDERR_PREFIXES)
        if code == _DOCKER_RUN_ERROR or (code in _DOCKER_EXEC_ERRORS and starts_with_docker):
            detail = " ".join(outcome.stderr.split())[:300] or "no stderr"
            return replace(
                outcome, status=ExecutionStatus.EXECUTION_ERROR, retryable=False,
                error=(f"docker infrastructure failure (exit {code}), not a solver result: {detail}"))
        return outcome

    def cleanup(self, paths: Sequence[Path]) -> bool:
        """Make files created by uid 65534 usable by the host user. True when every path succeeded."""
        ok = True
        for path in paths:
            name = f"hw-clean-{uuid.uuid4().hex}"
            outcome = self._control(
                [self.docker, "run", "--rm", "--name", name, "--network", "none", "--user", "0",
                 "-v", f"{Path(path).resolve()}:/x", self.image, "chmod", "-R", "a+rwX", "/x"],
                _CONTROL_TIMEOUT_SECONDS)
            if outcome.status is not ExecutionStatus.COMPLETED:
                self._remove_container(name)
            ok = ok and outcome.status is ExecutionStatus.COMPLETED
        return ok


def require_backend(environment: EnvironmentConfig) -> ExecutionBackend:
    """The backend the manifest asks for, or ``BackendUnavailable``. Never degrades to local."""
    if environment.mode == "trusted_local":
        return LocalBackend()
    if environment.mode == "sandboxed":
        backend = DockerBackend(environment.image, environment.image_python)
        backend.probe()
        missing = backend.capabilities().missing(SANDBOX_REQUIRED)
        if missing:
            raise BackendUnavailable(f"sandboxed backend lacks required capabilities: {', '.join(missing)}")
        return backend
    raise BackendUnavailable(f"unknown environment mode {environment.mode!r}")


def build_backend(environment: EnvironmentConfig) -> ExecutionBackend:
    return require_backend(environment)
