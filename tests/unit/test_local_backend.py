import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness_workbench.experiments.backends import (
    GRADER_FORBIDDEN_PASSTHROUGH,
    NO_GUARANTEES,
    SANDBOX_REQUIRED,
    BackendCapabilities,
    BackendUnavailable,
    LocalBackend,
    Mounts,
    ProcessLimits,
    ProcessOutcome,
    build_backend,
    grader_env,
    minimal_env,
)
from harness_workbench.experiments.manifest import EnvironmentConfig, ProcessLimitsConfig
from harness_workbench.experiments.status import ExecutionStatus
from tests.support import ENV_CANARY


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class MinimalEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.tmp = Path(self._tmp.name) / "tmp"

    def test_minimal_env_drops_canary_pythonpath_and_sets_fresh_home(self):
        patched = {
            "HARNESS_TEST_CANARY": ENV_CANARY,
            "PYTHONPATH": "/x",
            "PYTHONSTARTUP": "/y",
            "PYTHONHOME": "/z",
        }
        with mock.patch.dict(os.environ, patched):
            env = minimal_env(home=self.home, tmp=self.tmp)
        self.assertNotIn("HARNESS_TEST_CANARY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONSTARTUP", env)
        self.assertNotIn("PYTHONHOME", env)
        self.assertEqual(env["HOME"], str(self.home))
        self.assertEqual(env["TMPDIR"], str(self.tmp))
        self.assertEqual(env["XDG_CACHE_HOME"], str(self.home / ".cache"))
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["PYTHONSAFEPATH"], "1")
        allowed = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "HOME", "TMPDIR", "XDG_CACHE_HOME",
                   "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "PYTHONSAFEPATH"}
        self.assertLessEqual(set(env), allowed)

    def test_basic_variables_are_copied_only_when_set(self):
        with mock.patch.dict(os.environ, {"TZ": "UTC", "LANG": "C.UTF-8"}):
            os.environ.pop("LC_ALL", None)
            env = minimal_env(home=self.home, tmp=self.tmp)
        self.assertEqual(env["TZ"], "UTC")
        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertNotIn("LC_ALL", env)

    def test_passthrough_is_explicit_and_grader_env_never_passes_credentials(self):
        with mock.patch.dict(os.environ, {"HARNESS_TEST_CANARY": ENV_CANARY, "OTHER_TEST_VAR": "other"}):
            plain = minimal_env(home=self.home, tmp=self.tmp)
            passed = minimal_env(home=self.home, tmp=self.tmp, passthrough=["HARNESS_TEST_CANARY", "ABSENT_VAR"])
            grader = grader_env(self.home, self.tmp)
        self.assertNotIn("HARNESS_TEST_CANARY", plain)
        self.assertEqual(passed["HARNESS_TEST_CANARY"], ENV_CANARY)
        self.assertNotIn("OTHER_TEST_VAR", passed)
        self.assertNotIn("ABSENT_VAR", passed)
        self.assertNotIn("HARNESS_TEST_CANARY", grader)
        self.assertEqual(grader, plain)
        for name in ("MY_API_KEY", "GH_TOKEN", "DB_PASSWORD", "CLIENT_SECRET", "AWS_CREDENTIAL"):
            self.assertTrue(GRADER_FORBIDDEN_PASSTHROUGH.search(name), name)
        self.assertIsNone(GRADER_FORBIDDEN_PASSTHROUGH.search("PATH"))

    def test_pythonpath_is_never_copied_from_parent_environment(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/x"}):
            env = minimal_env(home=self.home, tmp=self.tmp)
        self.assertNotIn("PYTHONPATH", env)


class CapabilityTests(unittest.TestCase):
    def test_satisfies_and_missing(self):
        self.assertTrue(SANDBOX_REQUIRED.satisfies(SANDBOX_REQUIRED))
        self.assertTrue(SANDBOX_REQUIRED.satisfies(NO_GUARANTEES))
        self.assertFalse(NO_GUARANTEES.satisfies(SANDBOX_REQUIRED))
        self.assertEqual(
            NO_GUARANTEES.missing(SANDBOX_REQUIRED),
            ["isolated_filesystem", "network_denied", "unprivileged_user",
             "process_containment", "no_docker_socket", "read_only_bundle"],
        )
        partial = BackendCapabilities(True, True, True, True, True, False)
        self.assertEqual(partial.missing(SANDBOX_REQUIRED), ["read_only_bundle"])
        self.assertFalse(partial.satisfies(SANDBOX_REQUIRED))

    def test_local_backend_declares_no_guarantees(self):
        backend = LocalBackend()
        self.assertEqual(backend.name, "trusted_local")
        self.assertEqual(backend.python, sys.executable)
        self.assertEqual(backend.capabilities(), NO_GUARANTEES)

    def test_limits_from_config(self):
        limits = ProcessLimits.from_config(
            ProcessLimitsConfig(timeout_seconds=7.5, max_output_bytes=10, max_files=3, max_total_bytes=99))
        self.assertEqual(limits, ProcessLimits(7.5, 10, 3, 99))
        self.assertEqual(ProcessLimits(1.0).max_output_bytes, 1_000_000)

    def test_build_backend(self):
        self.assertIsInstance(build_backend(EnvironmentConfig(mode="trusted_local")), LocalBackend)
        with self.assertRaisesRegex(BackendUnavailable, "sandboxed backend not implemented"):
            build_backend(EnvironmentConfig(mode="sandboxed"))

    def test_mounts_defaults(self):
        self.assertEqual(Mounts(), Mounts((), ()))


class LocalBackendRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = Path(self._tmp.name)
        self.backend = LocalBackend()

    def _run(self, argv, limits, env=None):
        return self.backend.run(argv, cwd=self.cwd, env=env if env is not None else {}, limits=limits, mounts=Mounts())

    def test_endless_output_is_killed_as_resource_limit(self):
        started = time.perf_counter()
        outcome = self._run(
            [sys.executable, "-c", "while True: print('x'*1000)"],
            ProcessLimits(timeout_seconds=30, max_output_bytes=100_000),
        )
        elapsed = time.perf_counter() - started
        self.assertEqual(outcome.status, ExecutionStatus.RESOURCE_LIMIT)
        self.assertTrue(outcome.truncated)
        self.assertLessEqual(len(outcome.stdout.encode()), 100_000)
        self.assertLess(elapsed, 5)

    def test_timeout_kills_process_group_including_grandchild(self):
        pid_file = self.cwd / "grandchild.pid"
        grandchild = f"import os,time,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
        child = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]); time.sleep(30)"
        )
        started = time.perf_counter()
        outcome = self._run([sys.executable, "-c", child], ProcessLimits(timeout_seconds=1))
        elapsed = time.perf_counter() - started
        self.assertEqual(outcome.status, ExecutionStatus.TIMEOUT)
        self.assertLess(elapsed, 3)
        self.assertTrue(pid_file.exists(), "grandchild never started")
        gpid = int(pid_file.read_text())
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(gpid, 0)

    def test_run_returns_even_when_a_setsid_descendant_holds_the_pipes(self):
        pid_file = self.cwd / "escaped.pid"
        escaped = f"import os,time,pathlib; os.setsid(); pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(10)"
        child = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-c',{escaped!r}]); time.sleep(30)"
        )
        self.addCleanup(self._kill_escaped, pid_file)
        started = time.perf_counter()
        outcome = self._run([sys.executable, "-c", child], ProcessLimits(timeout_seconds=1))
        elapsed = time.perf_counter() - started
        self.assertEqual(outcome.status, ExecutionStatus.TIMEOUT)
        self.assertTrue(pid_file.exists(), "escaped descendant never started")
        self.assertLess(elapsed, 5, "run was held up by a descendant that escaped the process group")

    @staticmethod
    def _kill_escaped(pid_file: Path) -> None:
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def test_residual_children_are_killed_after_main_process_exits(self):
        pid_file = self.cwd / "residual.pid"
        grandchild = f"import os,time,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
        child = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-c',{grandchild!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            f"p=__import__('pathlib').Path({str(pid_file)!r}); "
            "[time.sleep(0.05) for _ in range(100) if not p.exists()]"
        )
        outcome = self._run([sys.executable, "-c", child], ProcessLimits(timeout_seconds=10))
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        gpid = int(pid_file.read_text())
        time.sleep(0.5)
        self.assertFalse(_alive(gpid), "residual child survived the run")

    def test_missing_executable_is_retryable_execution_error_not_task_failure(self):
        outcome = self._run([str(self.cwd / "does-not-exist")], ProcessLimits(timeout_seconds=5))
        self.assertEqual(outcome.status, ExecutionStatus.EXECUTION_ERROR)
        self.assertTrue(outcome.retryable)
        self.assertIsNone(outcome.exit_code)
        self.assertTrue(outcome.error)

    def test_nonzero_exit_is_task_failure_with_streams_and_float_duration(self):
        outcome = self._run(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
            ProcessLimits(timeout_seconds=10),
        )
        self.assertIsInstance(outcome, ProcessOutcome)
        self.assertEqual(outcome.status, ExecutionStatus.TASK_FAILURE)
        self.assertEqual(outcome.exit_code, 3)
        self.assertEqual(outcome.stdout.strip(), "out")
        self.assertEqual(outcome.stderr.strip(), "err")
        self.assertIsInstance(outcome.duration_ms, float)
        self.assertGreaterEqual(outcome.duration_ms, 0.0)
        self.assertFalse(outcome.truncated)
        self.assertFalse(outcome.retryable)

    def test_zero_exit_is_completed_and_uses_given_env_only(self):
        code = "import os; print(os.environ.get('HARNESS_TEST_CANARY','absent'))"
        with mock.patch.dict(os.environ, {"HARNESS_TEST_CANARY": ENV_CANARY}):
            outcome = self._run([sys.executable, "-c", code], ProcessLimits(timeout_seconds=10),
                                env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.stdout.strip(), "absent")

    def test_stdin_is_closed(self):
        outcome = self._run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
                            ProcessLimits(timeout_seconds=10))
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        self.assertEqual(outcome.stdout.strip(), "''")


if __name__ == "__main__":
    unittest.main()
