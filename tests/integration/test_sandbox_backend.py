"""Docker sandbox integration tests.

Skipped unless HARNESS_SANDBOX_TESTS=1 and a Docker daemon passes the backend's
own capability self-test. A skipped test is NOT RUN, not passed.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness_workbench.experiments.backends import (
    BackendUnavailable,
    DockerBackend,
    Mounts,
    ProcessLimits,
)
from harness_workbench.experiments.manifest import EnvironmentConfig
from harness_workbench.experiments.status import ExecutionStatus

SKIP_REASON = "Docker daemon unavailable"
_DEFAULTS = EnvironmentConfig(mode="sandboxed")
LIMITS = ProcessLimits(timeout_seconds=60.0)


def _probe() -> DockerBackend | None:
    if os.environ.get("HARNESS_SANDBOX_TESTS") != "1":
        return None
    backend = DockerBackend(_DEFAULTS.image, _DEFAULTS.image_python)
    try:
        backend.probe()
    except BackendUnavailable:
        return None
    return backend


_BACKEND = _probe()


@unittest.skipIf(_BACKEND is None, SKIP_REASON)
class SandboxBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = _BACKEND
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.bundle = self.root / "bundle"
        self.work = self.root / "work"
        self.bundle.mkdir()
        self.work.mkdir()
        (self.bundle / "marker.txt").write_text("bundle", encoding="utf-8")

    def _cleanup(self) -> None:
        self.backend.cleanup([self.work])
        self._tmp.cleanup()

    def run_py(self, code: str, limits: ProcessLimits = LIMITS):
        return self.backend.run(
            [self.backend.python, "-c", code], cwd=self.work, env={"HOME": str(self.work)},
            limits=limits, mounts=Mounts(read_only=(self.bundle,), writable=(self.work,)))

    def test_probe_self_test_passes(self):
        self.backend.probe()
        self.assertTrue(self.backend.capabilities().network_denied)

    def test_network_is_denied(self):
        outcome = self.run_py("import urllib.request; urllib.request.urlopen('http://example.com', timeout=3)")
        self.assertEqual(outcome.status, ExecutionStatus.TASK_FAILURE)
        self.assertIn("URLError", outcome.stderr)

    def test_user_is_unprivileged_and_socket_absent(self):
        outcome = self.run_py(
            "import os; print(os.getuid()); print(os.path.exists('/var/run/docker.sock'))")
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        self.assertEqual(outcome.stdout.split(), ["65534", "False"])

    def test_bundle_mount_is_read_only(self):
        outcome = self.run_py(f"open('{self.bundle}/new.txt', 'w')")
        self.assertEqual(outcome.status, ExecutionStatus.TASK_FAILURE)
        self.assertFalse((self.bundle / "new.txt").exists())

    def test_residual_processes_do_not_outlive_the_run(self):
        name = "hw-run-residual-test"
        line = self.backend.command_line(
            ["sh", "-c", "sleep 60 & echo started"], cwd=self.work, env={}, limits=LIMITS,
            mounts=Mounts(writable=(self.work,)), name=name)
        done = subprocess.run(line, capture_output=True, text=True, timeout=60)
        self.assertIn("started", done.stdout)
        listing = subprocess.run([self.backend.docker, "ps", "-q", "--filter", f"name={name}"],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(listing.stdout.strip(), "")

    def test_timeout_removes_container_and_cleanup_makes_dirs_removable(self):
        code = ("import os, time; os.makedirs('sub', exist_ok=True); open('sub/f','w').write('x'); "
                "os.chmod('sub', 0o700); time.sleep(120)")
        outcome = self.run_py(code, ProcessLimits(timeout_seconds=4.0))
        self.assertEqual(outcome.status, ExecutionStatus.TIMEOUT)
        listing = subprocess.run([self.backend.docker, "ps", "-q", "--filter", "name=hw-run-"],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(listing.stdout.strip(), "")
        self.assertTrue(self.backend.cleanup([self.work]))
        import shutil
        shutil.rmtree(self.work)
        self.assertFalse(self.work.exists())
        self.work.mkdir()


if __name__ == "__main__":
    unittest.main()
