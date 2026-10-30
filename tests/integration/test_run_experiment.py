"""A confirmatory experiment killed with SIGKILL keeps the trials it finished and records no decision.

The worker is a separate interpreter started in its own session (``start_new_session=True``), so its
process group is exactly the worker: the test kills that group and nothing else.
"""
from __future__ import annotations

import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness_workbench.core.db import WorkbenchDB
from tests.support import exact_task, write_manifest

WORKER = Path(__file__).resolve().with_name("_slow_experiment_worker.py")
CONTROLS = {"positive": {"outputs": {"answer": "CORRECT"}}, "negatives": [{"outputs": {"answer": "INCORRECT"}}]}
HOLD_TRIAL = 3
WAIT_SECONDS = 60


def _wait_for(path: Path, process: subprocess.Popen | None = None) -> bool:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            return True
        if process is not None and process.poll() is not None:
            return False
        time.sleep(0.05)
    return False


class SigkillTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_sigkill_during_confirmation_keeps_completed_trials_and_no_decision(self) -> None:
        tasks = [exact_task(task_id, f"slow holdout case {task_id}", split=split, runs_candidate_code=False,
                            controls=CONTROLS) for task_id, split in (("v1", "validation"), ("r1", "regression"))]
        manifest = write_manifest(
            self.root / "m", tasks=tasks, variants=[{"id": "candidate", "system_prompt": "cand"}],
            schema_version=2, mode="confirmatory", family="sigkill", dataset={"id": "sigkill", "version": "1"},
            repetitions=5, environment={"mode": "trusted_local", "trusted_solver_acknowledged": True})
        library, markers = self.root / "library-root", self.root / "markers"
        markers.mkdir()
        log = (self.root / "worker.log").open("wb")
        self.addCleanup(log.close)
        worker = subprocess.Popen(
            [sys.executable, "-I", str(WORKER), str(library), str(manifest), str(markers), str(HOLD_TRIAL)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        held = markers / f"marker-{HOLD_TRIAL}"
        try:
            reached = _wait_for(held, worker)
        finally:
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGKILL)  # the worker's own group only
            worker.wait(timeout=WAIT_SECONDS)
            # Release the held solver (its own session, so the kill did not reach it) and reap its files.
            (markers / "go").write_text("go", encoding="utf-8")
            if _wait_for(markers / f"solver-{HOLD_TRIAL}-done") and held.exists():
                left_behind = Path(held.read_text(encoding="utf-8"))
                if left_behind.name.startswith("harness-experiment-"):  # the killed trial's own temp dir only
                    shutil.rmtree(left_behind, ignore_errors=True)
        self.assertTrue(reached, (self.root / "worker.log").read_text(encoding="utf-8", errors="replace")[-2000:])
        self.assertEqual(worker.returncode, -signal.SIGKILL)

        db = WorkbenchDB.open(library)
        self.addCleanup(db.close)
        (experiment_id,) = db.incomplete_experiments()
        trials = db.trials(experiment_id)
        self.assertEqual(len(trials), 2)
        self.assertEqual({trial.split for trial in trials}, {"validation"})
        conn = sqlite3.connect(str(library / "library" / "workbench.sqlite3"))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decisions").fetchone(), (0,))
            self.assertEqual(conn.execute("SELECT status FROM experiments").fetchall(), [("running",)])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
