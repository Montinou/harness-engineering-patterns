"""DockerBackend unit tests. None of them needs a Docker daemon.

Where a ``docker`` executable is needed, a fake script written to a temporary
directory stands in for it. The fake only proves how the backend reacts to
exit codes and output; it says nothing about real Docker behavior.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_workbench.experiments.backends import (
    SANDBOX_REQUIRED,
    BackendUnavailable,
    DockerBackend,
    Mounts,
    ProcessLimits,
    build_backend,
    require_backend,
)
from harness_workbench.experiments.manifest import EnvironmentConfig

IMAGE = "python:3.12-slim"
IMAGE_PYTHON = "/usr/local/bin/python3"
LIMITS = ProcessLimits(timeout_seconds=5.0)


def make_fake_docker(directory: Path, *, info_rc: int = 0, run_stdout: str = "HARNESS_SANDBOX_OK\n",
                     run_rc: int = 0, image_stdout: str = "", image_rc: int = 0,
                     run_sleep: float = 0.0, big_output: int = 0,
                     task_rc: int = 0, task_stderr: str = "") -> tuple[Path, Path]:
    """Write a fake ``docker`` that logs argv as JSON lines and answers info/run from fixed values."""
    log = directory / "calls.jsonl"
    script = directory / "docker"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({str(log)!r}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "if cmd == 'info':\n"
        "    print('99.0.0-fake')\n"
        f"    sys.exit({info_rc})\n"
        "if cmd == 'run':\n"
        f"    if {run_sleep!r} and any(a.startswith('hw-run-') for a in sys.argv):\n"
        f"        import time; time.sleep({run_sleep!r})\n"
        f"    if {big_output!r} and any(a.startswith('hw-run-') for a in sys.argv):\n"
        f"        sys.stdout.write('x' * {big_output!r}); sys.stdout.flush()\n"
        f"        import time; time.sleep(30)\n"
        f"    sys.stdout.write({run_stdout!r})\n"
        f"    if any(a.startswith('hw-run-') for a in sys.argv):\n"
        f"        sys.stderr.write({task_stderr!r}); sys.exit({task_rc})\n"
        f"    sys.exit({run_rc})\n"
        "if cmd == 'image':\n"
        f"    sys.stdout.write({image_stdout!r})\n"
        f"    sys.exit({image_rc})\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


def calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.bundle = self.root / "bundle"
        self.work = self.root / "work"
        self.bundle.mkdir()
        self.work.mkdir()
        self.backend = DockerBackend(IMAGE, IMAGE_PYTHON)

    def line(self, argv=None, cwd=None, env=None, mounts=None, name="hw-test") -> list[str]:
        return self.backend.command_line(
            argv if argv is not None else [IMAGE_PYTHON, "-c", "pass"],
            cwd=cwd if cwd is not None else self.work,
            env=env if env is not None else {},
            limits=LIMITS,
            mounts=mounts if mounts is not None else Mounts(read_only=(self.bundle,), writable=(self.work,)),
            name=name,
        )

    def test_class_attributes_and_defaults(self):
        self.assertEqual(DockerBackend.name, "sandboxed")
        self.assertEqual(self.backend.python, IMAGE_PYTHON)
        self.assertEqual(self.backend.docker, "docker")
        self.assertEqual(self.backend.memory, "512m")
        self.assertEqual(self.backend.pids_limit, 256)

    def test_command_line_has_no_network_unprivileged_user_and_no_docker_socket(self):
        line = self.line()
        self.assertEqual(line[:3], ["docker", "run", "--rm"])
        self.assertEqual(line[line.index("--network") + 1], "none")
        self.assertEqual(line[line.index("--user") + 1], "65534:65534")
        self.assertEqual(line[line.index("--cap-drop") + 1], "ALL")
        self.assertEqual(line[line.index("--security-opt") + 1], "no-new-privileges")
        self.assertEqual(line[line.index("--pids-limit") + 1], "256")
        self.assertEqual(line[line.index("--memory") + 1], "512m")
        self.assertEqual(line[line.index("--tmpfs") + 1], "/tmp:rw,size=256m")
        self.assertIn("--init", line)
        self.assertIn("--read-only", line)
        self.assertEqual(line[line.index("--name") + 1], "hw-test")
        self.assertNotIn("/var/run/docker.sock", " ".join(line))
        self.assertNotIn("docker.sock", " ".join(line))

    def test_image_precedes_command_and_command_is_last(self):
        line = self.line(argv=[IMAGE_PYTHON, "-c", "print(1)"])
        self.assertEqual(line[-4:], [IMAGE, IMAGE_PYTHON, "-c", "print(1)"])

    def test_mounts_are_read_only_then_writable_with_stable_mount_points(self):
        line = self.line()
        volumes = [line[i + 1] for i, token in enumerate(line) if token == "-v"]
        self.assertEqual(volumes, [f"{self.bundle}:/sandbox/0:ro", f"{self.work}:/sandbox/1:rw"])

    def test_host_paths_rewritten_in_argv_env_and_cwd(self):
        sub = self.work / "sub"
        line = self.line(
            argv=[IMAGE_PYTHON, str(self.bundle / "grader.py"), f"--out={self.work}/result.json", "keep"],
            cwd=sub,
            env={"HOME": str(self.work / "home"), "BUNDLE": str(self.bundle), "PLAIN": "value"},
        )
        self.assertEqual(line[line.index("-w") + 1], "/sandbox/1/sub")
        self.assertIn("HOME=/sandbox/1/home", [line[i + 1] for i, t in enumerate(line) if t == "-e"])
        envs = [line[i + 1] for i, t in enumerate(line) if t == "-e"]
        self.assertEqual(sorted(envs), ["BUNDLE=/sandbox/0", "HOME=/sandbox/1/home", "PLAIN=value"])
        command = line[line.index(IMAGE) + 1:]
        self.assertEqual(command, [IMAGE_PYTHON, "/sandbox/0/grader.py", "--out=/sandbox/1/result.json", "keep"])
        # No host path leaks into the container-side portion of the line.
        container_side = [t for i, t in enumerate(line) if i > 0 and line[i - 1] != "-v"]
        self.assertNotIn(str(self.root), " ".join(container_side))

    def test_rewrite_respects_path_boundaries(self):
        sibling = Path(str(self.work) + "x") / "f"
        line = self.line(argv=[IMAGE_PYTHON, str(sibling), str(self.work)])
        command = line[line.index(IMAGE) + 1:]
        self.assertEqual(command[1], str(sibling))
        self.assertEqual(command[2], "/sandbox/1")

    def test_most_specific_mount_wins_for_nested_paths(self):
        nested = self.work / "inner"
        nested.mkdir()
        line = self.line(
            argv=[IMAGE_PYTHON, str(nested / "x")], cwd=nested,
            mounts=Mounts(read_only=(), writable=(self.work, nested)),
        )
        self.assertEqual(line[line.index("-w") + 1], "/sandbox/1")
        self.assertEqual(line[line.index(IMAGE) + 2], "/sandbox/1/x")

    def test_cwd_outside_every_mount_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cwd"):
            self.line(cwd=self.root / "elsewhere")

    def test_mount_path_that_would_break_the_volume_syntax_is_rejected(self):
        bad = self.root / "a:b"
        with self.assertRaisesRegex(ValueError, "mount"):
            self.line(mounts=Mounts(read_only=(bad,), writable=(self.work,)))

    def test_custom_limits_and_docker_binary_are_used(self):
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker="/opt/docker", memory="64m", pids_limit=8)
        line = backend.command_line([IMAGE_PYTHON], cwd=self.work, env={}, limits=LIMITS,
                                    mounts=Mounts(writable=(self.work,)), name="n")
        self.assertEqual(line[0], "/opt/docker")
        self.assertEqual(line[line.index("--memory") + 1], "64m")
        self.assertEqual(line[line.index("--pids-limit") + 1], "8")

    def test_host_path_is_never_forwarded_but_other_env_is(self):
        host_path = "/Users/someone/.pyenv/shims:/opt/homebrew/bin:/usr/bin"
        line = self.line(env={"PATH": host_path, "CANARY_PASSTHROUGH": "kept", "HOME": str(self.work / "h"),
                              "TMPDIR": str(self.work / "t")})
        envs = [line[i + 1] for i, t in enumerate(line) if t == "-e"]
        self.assertEqual(sorted(envs), ["CANARY_PASSTHROUGH=kept", "HOME=/sandbox/1/h", "TMPDIR=/sandbox/1/t"])
        self.assertFalse(any(e.startswith("PATH=") for e in envs))
        self.assertNotIn(host_path, " ".join(line))
        self.assertNotIn("/opt/homebrew", " ".join(line))

    def test_generated_container_names_are_unique(self):
        first = self.backend.command_line([IMAGE_PYTHON], cwd=self.work, env={}, limits=LIMITS,
                                          mounts=Mounts(writable=(self.work,)))
        second = self.backend.command_line([IMAGE_PYTHON], cwd=self.work, env={}, limits=LIMITS,
                                           mounts=Mounts(writable=(self.work,)))
        self.assertNotEqual(first[first.index("--name") + 1], second[second.index("--name") + 1])


class RefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def test_sandboxed_mode_without_daemon_raises_backend_unavailable_not_local_fallback(self):
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker="/nonexistent/docker")
        with self.assertRaises(BackendUnavailable) as ctx:
            backend.probe()
        self.assertIn("docker", str(ctx.exception).lower())

    def test_capabilities_before_probe_raises_and_never_reports_no_guarantees(self):
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker="/nonexistent/docker")
        with self.assertRaises(BackendUnavailable):
            backend.capabilities()

    def test_run_before_probe_is_refused(self):
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker="/nonexistent/docker")
        with self.assertRaises(BackendUnavailable):
            backend.run([IMAGE_PYTHON], cwd=self.root, env={}, limits=LIMITS, mounts=Mounts(writable=(self.root,)))

    def test_failed_probe_leaves_backend_unprobed(self):
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker="/nonexistent/docker")
        with self.assertRaises(BackendUnavailable):
            backend.probe()
        with self.assertRaises(BackendUnavailable):
            backend.capabilities()

    def test_require_backend_without_docker_on_path_refuses(self):
        empty = self.root / "empty-bin"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            with self.assertRaises(BackendUnavailable):
                require_backend(EnvironmentConfig(mode="sandboxed"))
            with self.assertRaises(BackendUnavailable):
                build_backend(EnvironmentConfig(mode="sandboxed"))

    def test_daemon_down_info_failure_is_refused(self):
        fake, log = make_fake_docker(self.root, info_rc=1)
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker=str(fake))
        with self.assertRaisesRegex(BackendUnavailable, "daemon"):
            backend.probe()
        self.assertEqual([c[0] for c in calls(log)], ["info"])

    def test_self_test_failure_names_the_failed_properties(self):
        out = "HARNESS_SANDBOX_FAIL network_denied\nHARNESS_SANDBOX_FAIL no_docker_socket\n"
        fake, _ = make_fake_docker(self.root, run_stdout=out, run_rc=1)
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker=str(fake))
        with self.assertRaises(BackendUnavailable) as ctx:
            backend.probe()
        self.assertIn("network_denied", str(ctx.exception))
        self.assertIn("no_docker_socket", str(ctx.exception))
        with self.assertRaises(BackendUnavailable):
            backend.capabilities()

    def test_self_test_container_that_cannot_run_is_refused(self):
        fake, _ = make_fake_docker(self.root, run_stdout="", run_rc=125)
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker=str(fake))
        with self.assertRaisesRegex(BackendUnavailable, "self-test"):
            backend.probe()

    def test_self_test_without_the_ok_marker_is_refused_even_with_exit_zero(self):
        fake, _ = make_fake_docker(self.root, run_stdout="", run_rc=0)
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker=str(fake))
        with self.assertRaisesRegex(BackendUnavailable, "self-test"):
            backend.probe()

    def test_markers_win_over_a_zero_exit_code(self):
        fake, _ = make_fake_docker(self.root, run_stdout="HARNESS_SANDBOX_FAIL read_only_bundle\n", run_rc=0)
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker=str(fake))
        with self.assertRaisesRegex(BackendUnavailable, "read_only_bundle"):
            backend.probe()

    def test_passing_self_test_unlocks_capabilities_and_uses_hardened_flags(self):
        fake, log = make_fake_docker(self.root)
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker=str(fake))
        backend.probe()
        self.assertEqual(backend.capabilities(), SANDBOX_REQUIRED)
        self.assertTrue(backend.capabilities().satisfies(SANDBOX_REQUIRED))
        recorded = calls(log)
        self.assertEqual(recorded[0][:2], ["info", "--format"])
        self.assertEqual(recorded[0][2], "{{.ServerVersion}}")
        run = recorded[1]
        self.assertEqual(run[0], "run")
        for flag in ("--rm", "--init", "--read-only"):
            self.assertIn(flag, run)
        self.assertEqual(run[run.index("--network") + 1], "none")
        self.assertEqual(run[run.index("--user") + 1], "65534:65534")
        self.assertEqual(run[run.index("--cap-drop") + 1], "ALL")
        self.assertTrue(any(v.endswith(":/sandbox/0:ro") for v in run))
        self.assertIn(IMAGE_PYTHON, run)
        self.assertEqual([run[i + 1] for i, t in enumerate(run) if t == "-v"][0].split(":")[-2:],
                         ["/sandbox/0", "ro"])
        self.assertEqual(len([t for t in run if t == "-v"]), 1)
        self.assertEqual(run[run.index("-c") - 1], IMAGE_PYTHON)

    def test_require_backend_returns_a_probed_backend_when_everything_passes(self):
        fake, _ = make_fake_docker(self.root)
        with mock.patch.dict(os.environ, {"PATH": str(self.root)}):
            backend = require_backend(EnvironmentConfig(mode="sandboxed"))
        self.assertIsInstance(backend, DockerBackend)
        self.assertEqual(backend.name, "sandboxed")
        self.assertTrue(backend.capabilities().satisfies(SANDBOX_REQUIRED))
        self.assertIsNotNone(fake)

    def test_require_backend_names_missing_capability_and_never_falls_back(self):
        fake, _ = make_fake_docker(self.root)

        class Weak(DockerBackend):
            def capabilities(self):
                caps = super().capabilities()
                return type(caps)(**{**caps.__dict__, "network_denied": False})

        with mock.patch("harness_workbench.experiments.backends.DockerBackend", Weak):
            with mock.patch.dict(os.environ, {"PATH": str(self.root)}):
                with self.assertRaisesRegex(BackendUnavailable, "network_denied"):
                    require_backend(EnvironmentConfig(mode="sandboxed"))

    def test_require_backend_trusted_local_is_not_sandbox_and_unknown_modes_raise(self):
        from harness_workbench.experiments.backends import LocalBackend
        self.assertIsInstance(require_backend(EnvironmentConfig(mode="trusted_local")), LocalBackend)
        env = EnvironmentConfig(mode="trusted_local")
        object.__setattr__(env, "mode", "other")
        with self.assertRaises(BackendUnavailable):
            require_backend(env)


class RunAndCleanupTests(unittest.TestCase):
    """run() and cleanup() against the fake docker: argv, chmod and the rm -f policy only."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.work = self.root / "work"
        self.bundle = self.root / "bundle"
        self.work.mkdir()
        self.bundle.mkdir()
        self.work.chmod(0o755)

    def probed(self, **fake_kwargs) -> tuple[DockerBackend, Path]:
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake, log = make_fake_docker(bin_dir, **fake_kwargs)
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker=str(fake))
        backend.probe()
        return backend, log

    def run_it(self, backend, limits=LIMITS):
        return backend.run([IMAGE_PYTHON, "-c", "pass"], cwd=self.work, env={"K": "v"}, limits=limits,
                           mounts=Mounts(read_only=(self.bundle,), writable=(self.work,)))

    def test_run_executes_the_command_line_through_the_local_backend(self):
        backend, log = self.probed()
        outcome = self.run_it(backend)
        self.assertEqual(outcome.status.value, "completed")
        recorded = calls(log)
        run_call = next(c for c in recorded[2:] if c[0] == "run" and "-w" in c)
        self.assertEqual(run_call[run_call.index("-w") + 1], "/sandbox/1")
        self.assertNotIn("rm", [c[0] for c in recorded[2:]])

    def test_writable_mounts_are_made_world_writable_before_mounting(self):
        backend, _ = self.probed()
        self.run_it(backend)
        self.assertEqual(self.work.stat().st_mode & 0o777, 0o777)

    def test_timeout_removes_the_container_by_name(self):
        backend, log = self.probed(run_sleep=20.0)
        outcome = self.run_it(backend, ProcessLimits(timeout_seconds=0.5))
        self.assertEqual(outcome.status.value, "timeout")
        recorded = calls(log)
        run_call = next(c for c in recorded[2:] if c[0] == "run")
        name = run_call[run_call.index("--name") + 1]
        self.assertIn(["rm", "-f", name], recorded)

    def test_output_overflow_removes_the_container_by_name(self):
        backend, log = self.probed(big_output=5000)
        outcome = self.run_it(backend, ProcessLimits(timeout_seconds=10.0, max_output_bytes=100))
        self.assertEqual(outcome.status.value, "resource_limit")
        recorded = calls(log)
        run_call = next(c for c in recorded[2:] if c[0] == "run")
        self.assertIn(["rm", "-f", run_call[run_call.index("--name") + 1]], recorded)

    def assert_infra_failure(self, outcome, exit_code):
        self.assertEqual(outcome.status.value, "execution_error")
        # a solver can produce these exit codes and stderr itself, so a retry could be triggered by it
        self.assertFalse(outcome.retryable)
        self.assertEqual(outcome.exit_code, exit_code)
        self.assertIn(f"exit {exit_code}", outcome.error)
        self.assertIn("docker", outcome.error)

    def test_docker_exit_125_is_a_non_retryable_execution_error_not_a_task_failure(self):
        backend, _ = self.probed(task_rc=125, task_stderr="docker: Error response from daemon: Conflict.\n")
        self.assert_infra_failure(self.run_it(backend), 125)

    def test_docker_exit_127_with_docker_prefix_is_a_non_retryable_execution_error(self):
        backend, _ = self.probed(task_rc=127, task_stderr="docker: Error response from daemon: executable not found.\n")
        self.assert_infra_failure(self.run_it(backend), 127)

    def test_docker_exit_126_with_exec_prefix_is_a_non_retryable_execution_error(self):
        backend, _ = self.probed(task_rc=126, task_stderr="exec: permission denied\n")
        self.assert_infra_failure(self.run_it(backend), 126)

    def test_solver_exit_127_without_docker_prefix_stays_a_task_failure(self):
        backend, _ = self.probed(task_rc=127, task_stderr="sh: 1: solver: not found\n")
        outcome = self.run_it(backend)
        self.assertEqual(outcome.status.value, "task_failure")
        self.assertFalse(outcome.retryable)
        self.assertEqual(outcome.exit_code, 127)

    def test_ordinary_solver_exit_code_stays_a_task_failure(self):
        backend, _ = self.probed(task_rc=1, task_stderr="docker: not at the start\n")
        self.assertEqual(self.run_it(backend).status.value, "task_failure")

    def test_cleanup_runs_a_root_chmod_container_over_each_directory(self):
        backend, log = self.probed()
        self.assertTrue(backend.cleanup([self.work]))
        run_call = calls(log)[-1]
        self.assertEqual(run_call[0], "run")
        self.assertEqual(run_call[run_call.index("--user") + 1], "0")
        self.assertEqual(run_call[run_call.index("--network") + 1], "none")
        self.assertIn(f"{self.work}:/x", run_call)
        self.assertEqual(run_call[-5:], [IMAGE, "chmod", "-R", "a+rwX", "/x"])

    def test_cleanup_reports_failure_instead_of_hiding_it(self):
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker="/nonexistent/docker")
        self.assertFalse(backend.cleanup([self.work]))


class ImageDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def test_digest_is_none_when_docker_is_missing(self):
        self.assertIsNone(DockerBackend(IMAGE, IMAGE_PYTHON, docker="/nonexistent/docker").image_digest())

    def test_digest_command_and_parsing(self):
        fake, log = make_fake_docker(self.root, image_stdout="python@sha256:abc\n")
        backend = DockerBackend(IMAGE, IMAGE_PYTHON, docker=str(fake))
        self.assertEqual(backend.image_digest(), "python@sha256:abc")
        self.assertEqual(calls(log)[-1], ["image", "inspect", "--format", "{{index .RepoDigests 0}}", IMAGE])

    def test_digest_is_none_when_inspect_fails_or_prints_nothing(self):
        failing, _ = make_fake_docker(self.root, image_rc=1)
        self.assertIsNone(DockerBackend(IMAGE, IMAGE_PYTHON, docker=str(failing)).image_digest())
        other = self.root / "o"
        other.mkdir()
        empty, _ = make_fake_docker(other, image_stdout="")
        self.assertIsNone(DockerBackend(IMAGE, IMAGE_PYTHON, docker=str(empty)).image_digest())


if __name__ == "__main__":
    unittest.main()
