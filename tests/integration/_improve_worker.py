"""Process entry points for the multiprocess store tests (stdlib plus the package under test).

Run through ``multiprocessing.get_context("spawn")``: every worker is a fresh
interpreter, so no connection, lock or module state is shared with the parent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

BASE_HARNESS = "examples/workbench/base_research_harness.json"
BARRIER_TIMEOUT_SECONDS = 60


class _LockstepExecutor:
    """MockExecutor that makes every worker reach each probe together, so the commits collide."""

    def __init__(self, barrier: Any) -> None:
        from harness_workbench.core.executor import MockExecutor

        self.inner = MockExecutor()
        self.barrier = barrier

    def execute(self, harness: Any, task: Any) -> Any:
        report = self.inner.execute(harness, task)
        self.barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        return report


def _outcome(adopted: str | None, parent: str | None, error: str | None) -> dict[str, Any]:
    return {"adopted": adopted, "parent": parent, "error": error}


def improve_worker(
    repo_root: str, root: str, start_barrier: Any, lockstep_barrier: Any, results: Any
) -> None:
    """Wait at ``start_barrier``, run one ``Improver.improve`` against ``root`` and report the outcome."""
    outcome = _outcome(None, None, "worker did not finish")
    try:
        from harness_workbench.core.harness import load_harness
        from harness_workbench.core.executor import Task
        from harness_workbench.core.improver import Improver

        base = load_harness(Path(repo_root) / BASE_HARNESS)
        task = Task.from_text("Fact-check a claim", domain="research")
        improver = Improver(executor=_LockstepExecutor(lockstep_barrier))
        start_barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        result = improver.improve(base, task, root=Path(root))
        outcome = _outcome(result.adopted.version, result.adopted.lineage.parent_version, None)
    except BaseException as exc:  # reported to the parent, never swallowed
        outcome = _outcome(None, None, f"{type(exc).__name__}: {exc}")
    finally:
        results.put(outcome)


def busy_worker(root: str, busy_timeout_ms: int, results: Any) -> None:
    """Try to write while another connection holds the lock; report how it failed."""
    outcome = _outcome(None, None, "worker did not finish")
    try:
        from harness_workbench.core.db import WorkbenchDB
        from harness_workbench.core.harness import Harness

        db = WorkbenchDB(Path(root) / "library" / "workbench.sqlite3", busy_timeout_ms=busy_timeout_ms)
        try:
            version = db.register_base(Harness(id="busy-probe", version="1", system_prompt="probe"))
        finally:
            db.close()
        outcome = _outcome(version, None, None)
    except BaseException as exc:
        outcome = _outcome(None, None, f"{type(exc).__name__}: {exc}")
    finally:
        results.put(outcome)
