"""Process entry point for the holdout reservation race (run through the ``spawn`` start method).

Every worker is a fresh interpreter with its own connection, so nothing is shared with the parent.
"""
from __future__ import annotations

from typing import Any

BARRIER_TIMEOUT_SECONDS = 60


def reserve_worker(
    db_path: str, fingerprints: list[str], candidate: str, start_barrier: Any, results: Any
) -> None:
    """Open the database, wait at ``start_barrier`` and report the outcome of one ``reserve_holdout``."""
    outcome: dict[str, Any] = {"candidate": candidate, "reason": None, "error": "worker did not finish"}
    try:
        from pathlib import Path

        from harness_workbench.core.db import WorkbenchDB

        db = WorkbenchDB(Path(db_path))
        try:
            start_barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
            reason = db.reserve_holdout(
                task_fingerprints=fingerprints,
                candidate_hash=candidate,
                baseline_hash="base",
                policy_hash="policy",
                experiment_id=f"exp-{candidate}",
            )
            outcome = {"candidate": candidate, "reason": reason, "error": None}
        finally:
            db.close()
    except BaseException as exc:  # reported to the parent, never swallowed
        outcome = {"candidate": candidate, "reason": None, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        results.put(outcome)
