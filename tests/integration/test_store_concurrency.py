"""Multiprocess lineage races against the workbench database.

Each writer is a separate spawned interpreter that waits at a ``Barrier(n)``
and then calls ``Improver.improve(base, task, root=root)``, so the version
allocations really collide. A failure here is a store bug, not a test to relax.
"""
import json
import multiprocessing
import queue
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from harness_workbench.core.db import WorkbenchDB
from harness_workbench.core.harness import load_harness
from harness_workbench.core.store import load_versions
from tests.integration import _improve_worker

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_ID = "research-agent"
RESULT_TIMEOUT_SECONDS = 120
JOIN_TIMEOUT_SECONDS = 30
BUSY_TIMEOUT_MS = 50
BUSY_DEADLINE_SECONDS = 20  # includes interpreter spawn; the wait itself is ~50 ms


def _collect(ctx: Any, processes: list[Any], results: Any, count: int) -> list[dict]:
    """Read ``count`` outcomes, then reap every process; stragglers are terminated, never leaked."""
    rows: list[dict] = []
    try:
        for _ in range(count):
            try:
                rows.append(results.get(timeout=RESULT_TIMEOUT_SECONDS))
            except queue.Empty:
                rows.append({"adopted": None, "parent": None, "error": "no result before the deadline"})
                break
    finally:
        for process in processes:
            process.join(JOIN_TIMEOUT_SECONDS)
            if process.is_alive():
                process.terminate()
                process.join(JOIN_TIMEOUT_SECONDS)
    return rows


def run_improvers(n: int, root: Path) -> list[dict]:
    """Run ``n`` improvers in spawned processes released together by a ``Barrier(n)``."""
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Barrier(n)
    lockstep = ctx.Barrier(n)  # equalises probe counts so the commits happen together
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_improve_worker.improve_worker,
            args=(str(REPO_ROOT), str(root), start, lockstep, results),
        )
        for _ in range(n)
    ]
    for process in processes:
        process.start()
    return _collect(ctx, processes, results, n)


class StoreConcurrencyCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def assert_race_outcome(self, writers: int) -> None:
        rows = run_improvers(writers, self.root)
        self.assertEqual([r["error"] for r in rows], [None] * writers)
        self.assertEqual({r["parent"] for r in rows}, {"1"})
        self.assertEqual(len({r["adopted"] for r in rows}), writers)

        directory = self.root / "library" / "harnesses" / HARNESS_ID
        stems = sorted((p.stem for p in directory.glob("*.json")), key=int)
        self.assertEqual(stems, [str(v) for v in range(1, writers + 2)])
        documents = {stem: json.loads((directory / f"{stem}.json").read_text()) for stem in stems}
        roots = [stem for stem, doc in documents.items() if doc["lineage"]["parent_version"] is None]
        self.assertEqual(roots, ["1"], "no file other than 1.json may be a lineage root")
        hashes = {stem: load_harness(directory / f"{stem}.json").execution_config_hash() for stem in stems}
        clones = [
            stem for stem, doc in documents.items()
            if stem != "1" and hashes[stem] == hashes["1"] and doc["lineage"]["parent_version"] is None
        ]
        self.assertEqual(clones, [], "no phantom copy of the base under a new version number")

        # The database agrees with the files.
        db = WorkbenchDB.open(self.root)
        try:
            stored = db.versions(HARNESS_ID)
        finally:
            db.close()
        self.assertEqual(sorted((h.version for h in stored), key=int), stems)
        self.assertEqual([h.version for h in stored if h.lineage.parent_version is None], ["1"])
        self.assertEqual(sorted((h.version for h in load_versions(self.root, HARNESS_ID)), key=int), stems)

    def test_six_processes_keep_one_base_and_distinct_children(self) -> None:
        self.assert_race_outcome(6)

    def test_twelve_processes(self) -> None:
        self.assert_race_outcome(12)

    def test_busy_errors_are_explicit_not_infinite(self) -> None:
        library = self.root / "library"
        library.mkdir(parents=True)
        holder_db = WorkbenchDB.open(self.root)  # creates the schema; closed again before the lock is taken
        holder_db.close()
        holder = sqlite3.connect(library / "workbench.sqlite3", isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")

        ctx = multiprocessing.get_context("spawn")
        results = ctx.Queue()
        process = ctx.Process(target=_improve_worker.busy_worker, args=(str(self.root), BUSY_TIMEOUT_MS, results))
        started = time.monotonic()
        process.start()
        rows = _collect(ctx, [process], results, 1)
        elapsed = time.monotonic() - started
        holder.execute("ROLLBACK")

        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["error"])
        self.assertTrue(rows[0]["error"].startswith("StoreBusyError"), rows[0]["error"])
        self.assertIsNone(rows[0]["adopted"])
        self.assertLess(elapsed, BUSY_DEADLINE_SECONDS)
        self.assertEqual(process.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
