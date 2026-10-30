"""The demo manifest runs from any directory with no ``python`` alias on PATH (A11).

The foreign-cwd test always runs. The wheel and editable-install script is opt-in
(``HARNESS_PORTABILITY_TESTS=1``): it builds a wheel and two virtualenvs, so it is slow.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEMO_MANIFEST = REPO / "examples" / "experiments" / "local-command.json"
SCRIPT = Path(__file__).resolve().with_name("_portability_check.sh")
NOT_RUN_EXIT = 3
SCRIPT_TIMEOUT_SECONDS = 900
RUN_TIMEOUT_SECONDS = 300
OPT_IN = "HARNESS_PORTABILITY_TESTS"


def _path_without_python() -> str:
    """The directories of the current PATH that hold no ``python`` executable."""
    kept = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory and not os.access(os.path.join(directory, "python"), os.X_OK):
            kept.append(directory)
    return os.pathsep.join(kept)


class DemoManifestPortabilityTests(unittest.TestCase):
    def test_demo_manifest_runs_from_foreign_cwd_without_python_alias(self) -> None:
        path = _path_without_python()
        for directory in path.split(os.pathsep):
            self.assertFalse(os.access(os.path.join(directory, "python"), os.X_OK))
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "elsewhere"
            cwd.mkdir()
            env = {"PATH": path, "PYTHONPATH": str(REPO)}
            proc = subprocess.run(
                [sys.executable, "-m", "harness_workbench", "experiment", "--provider", "command",
                 "--spec", str(DEMO_MANIFEST), "--root", str(Path(tmp) / "root"), "--json"],
                cwd=cwd, env=env, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["decision"]["status"], "exploratory_only")

    def test_portability_script(self) -> None:
        if os.environ.get(OPT_IN) != "1":
            self.skipTest(f"set {OPT_IN}=1")
        with tempfile.TemporaryDirectory() as work:
            env = dict(os.environ, PYTHON=sys.executable, REPO=str(REPO), WORK=work)
            proc = subprocess.run(
                ["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=SCRIPT_TIMEOUT_SECONDS)
        if proc.returncode == NOT_RUN_EXIT:
            self.skipTest((proc.stdout.strip() or proc.stderr.strip() or "NOT RUN").splitlines()[0])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
