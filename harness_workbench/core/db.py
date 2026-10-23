"""SQLite store for harness versions with short, serialised write transactions.

One database per workbench root (``<root>/library/workbench.sqlite3``). Every
write runs inside a single ``BEGIN IMMEDIATE`` transaction, so version
allocation is race free across processes. No transaction is ever held across a
subprocess or network call: callers do slow work first and write afterwards.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from harness_workbench.core.harness import Harness, harness_from_dict
from harness_workbench.core.store import _version_key
from harness_workbench.experiments.contracts import ExperimentDecision, PrivateExperimentPlan, TrialRecord
from harness_workbench.experiments.status import DecisionStatus
from harness_workbench.experiments.validation import ValidationError, json_dumps_strict

DB_SCHEMA_VERSION = 1
_DECIMAL_VERSION = re.compile(r"[0-9]+", re.ASCII)

_SCHEMA_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS harness_versions (
        harness_id TEXT,
        version TEXT,
        execution_config_hash TEXT NOT NULL,
        parent_version TEXT,
        source TEXT,
        created_at TEXT,
        document TEXT NOT NULL,
        PRIMARY KEY (harness_id, version)
    )""",
    """CREATE INDEX IF NOT EXISTS harness_versions_hash
        ON harness_versions (harness_id, execution_config_hash)""",
    # Experiment tables. Every statement is idempotent, so a database created before they existed gains
    # them on the next open without a schema version bump.
    """CREATE TABLE IF NOT EXISTS experiments (
        experiment_id TEXT PRIMARY KEY,
        name TEXT,
        family TEXT,
        mode TEXT,
        schema_version INTEGER,
        plan_hash TEXT,
        plan_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running','complete','incomplete','invalid')),
        implementation_hash TEXT,
        created_at TEXT,
        finished_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS trials (
        trial_id TEXT PRIMARY KEY,
        experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
        variant_id TEXT,
        task_id TEXT,
        split TEXT,
        family TEXT,
        repetition INTEGER,
        seed INTEGER,
        attempt INTEGER,
        record_json TEXT NOT NULL,
        created_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS trials_experiment ON trials (experiment_id)",
    """CREATE TABLE IF NOT EXISTS selections (
        experiment_id TEXT PRIMARY KEY REFERENCES experiments(experiment_id),
        finalist_id TEXT,
        finalist_hash TEXT,
        baseline_hash TEXT,
        plan_hash TEXT,
        policy_hash TEXT,
        frozen_at TEXT,
        evidence_json TEXT NOT NULL
    )""",
    # blocked_reason is set on the rows of a refused attempt: the attempt stays visible, but a refused
    # attempt never counts as a reservation, so it cannot lock out the candidate that holds the tasks.
    """CREATE TABLE IF NOT EXISTS holdout_exposure (
        id INTEGER PRIMARY KEY,
        task_fingerprint TEXT NOT NULL,
        candidate_hash TEXT,
        baseline_hash TEXT,
        policy_hash TEXT,
        experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
        reserved_at TEXT,
        decision_status TEXT NULL,
        blocked_reason TEXT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS holdout_exposure_fingerprint ON holdout_exposure (task_fingerprint)",
    """CREATE TABLE IF NOT EXISTS decisions (
        decision_id TEXT PRIMARY KEY,
        experiment_id TEXT NOT NULL UNIQUE REFERENCES experiments(experiment_id),
        status TEXT,
        candidate_hash TEXT,
        baseline_id TEXT,
        baseline_version TEXT,
        plan_hash TEXT,
        decision_json TEXT NOT NULL,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS promotions (
        promotion_id INTEGER PRIMARY KEY,
        decision_id TEXT NOT NULL UNIQUE REFERENCES decisions(decision_id),
        harness_id TEXT,
        version TEXT,
        created_at TEXT
    )""",
)

EXPERIMENT_TERMINAL_STATUSES = ("complete", "incomplete", "invalid")
_FP_PREFIX = 8
_SELECTION_KEYS = ("finalist_id", "finalist_hash", "baseline_hash", "plan_hash", "policy_hash")


class HoldoutReservation(NamedTuple):
    """Outcome of :meth:`WorkbenchDB.reserve_holdout_attempt`."""

    blocked_reason: str | None
    earlier_experiment_ids: tuple[str, ...]


class StoreBusyError(RuntimeError):
    """Another connection holds the write lock past the busy timeout."""


class LineageConflict(RuntimeError):
    """A registered version already exists with different executable content."""


class SchemaMismatch(RuntimeError):
    """The database was written by a newer schema than this code understands."""


class PromotionConflict(RuntimeError):
    """A decision cannot be promoted: it is not eligible, names another candidate or has no valid parent."""


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _busy(exc: sqlite3.OperationalError) -> StoreBusyError:
    return StoreBusyError(f"workbench database is busy: {exc}")


def _reject_constant(token: str) -> Any:
    raise ValidationError(f"non-finite JSON constant {token} is not allowed")


def _parse_finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValidationError(f"non-finite JSON number {text} is not allowed")
    return value


def _loads_finite(text: str) -> Any:
    """Parse a stored document, rejecting NaN/Infinity and overflowing literals such as ``1e400``."""
    return json.loads(text, parse_constant=_reject_constant, parse_float=_parse_finite_float)


class WorkbenchDB:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._depth = 0
        self._busy_timeout_s = busy_timeout_ms / 1000
        # isolation_level=None: no implicit transactions; transaction() issues BEGIN IMMEDIATE.
        self._conn = sqlite3.connect(
            str(self.path), timeout=busy_timeout_ms / 1000, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._initialize()
        except BaseException as exc:
            self._conn.close()
            if isinstance(exc, sqlite3.OperationalError) and _is_busy(exc):
                raise _busy(exc) from exc
            raise

    @classmethod
    def open(cls, root: Path) -> WorkbenchDB:
        return cls(Path(root) / "library" / "workbench.sqlite3")

    @staticmethod
    def exists(root: Path) -> bool:
        return (Path(root) / "library" / "workbench.sqlite3").is_file()

    def close(self) -> None:
        self._conn.close()

    # -- schema ---------------------------------------------------------

    def _initialize(self) -> None:
        self._enable_wal()
        # Check before creating anything so a refused database stays untouched.
        has_meta = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        if has_meta:
            self._check_schema_version()
        with self.transaction() as conn:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(DB_SCHEMA_VERSION),),
            )
            self._check_schema_version()

    def _enable_wal(self) -> None:
        """Switch to WAL. The switch fails at once with "locked" when several connections open
        a new database together (the busy handler is not consulted), so retry until the
        busy timeout elapses; an already-WAL database needs no switch at all."""
        deadline = time.monotonic() + self._busy_timeout_s
        while True:
            try:
                if str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                    return
                self._conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)

    def _check_schema_version(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            return
        try:
            found = int(row[0])
        except (TypeError, ValueError):
            raise SchemaMismatch(f"unreadable schema_version {row[0]!r}") from None
        if found > DB_SCHEMA_VERSION:
            raise SchemaMismatch(
                f"workbench database schema {found} is newer than supported {DB_SCHEMA_VERSION}"
            )

    # -- transactions ---------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE``; commit on exit, roll back on exception. Re-entrant: an inner use joins the outer."""
        if self._depth:
            yield self._conn
            return
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise _busy(exc) from exc
            raise
        self._depth = 1
        try:
            try:
                yield self._conn
            except BaseException as exc:
                self._rollback()
                if isinstance(exc, sqlite3.OperationalError) and _is_busy(exc):
                    raise _busy(exc) from exc
                raise
            try:
                self._conn.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                self._rollback()
                if _is_busy(exc):
                    raise _busy(exc) from exc
                raise
        finally:
            self._depth = 0

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def _rollback(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")

    # -- writes ---------------------------------------------------------

    def register_base(self, harness: Harness) -> str:
        digest = harness.execution_config_hash()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT execution_config_hash FROM harness_versions WHERE harness_id=? AND version=?",
                (harness.id, harness.version),
            ).fetchone()
            if row is not None:
                if row["execution_config_hash"] == digest:
                    return harness.version
                raise LineageConflict(
                    f"{harness.id} v{harness.version} is already registered with different content"
                )
            self._insert(conn, harness, digest, source=harness.lineage.source)
        return harness.version

    def allocate_child(
        self, parent_id: str, parent_version: str, child: Harness, *, source: str
    ) -> str:
        if child.id != parent_id:
            raise ValueError(f"child harness id {child.id!r} does not match parent {parent_id!r}")
        with self.transaction() as conn:
            parent = conn.execute(
                "SELECT 1 FROM harness_versions WHERE harness_id=? AND version=?",
                (parent_id, parent_version),
            ).fetchone()
            if parent is None:
                raise LookupError(f"parent {parent_id} v{parent_version} is not registered")
            decimals = [
                int(row["version"])
                for row in conn.execute(
                    "SELECT version FROM harness_versions WHERE harness_id=?", (parent_id,)
                )
                if _DECIMAL_VERSION.fullmatch(row["version"])
            ]
            version = str(max(decimals, default=0) + 1)
            stored = child.copy()
            stored.version = version
            stored.lineage.parent_version = parent_version
            self._insert(conn, stored, stored.execution_config_hash(), source=source)
        return version

    @staticmethod
    def _insert(conn: sqlite3.Connection, harness: Harness, digest: str, *, source: str) -> None:
        document = json_dumps_strict(harness.to_dict(), sort_keys=True, ensure_ascii=False)
        conn.execute(
            "INSERT INTO harness_versions"
            "(harness_id, version, execution_config_hash, parent_version, source, created_at, document)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                harness.id,
                harness.version,
                digest,
                harness.lineage.parent_version,
                source,
                harness.lineage.created_at,
                document,
            ),
        )

    # -- reads ----------------------------------------------------------

    def versions(self, harness_id: str) -> list[Harness]:
        rows = self._conn.execute(
            "SELECT version, document FROM harness_versions WHERE harness_id=?", (harness_id,)
        ).fetchall()
        rows.sort(key=lambda row: _version_key(row["version"]))
        return [harness_from_dict(_loads_finite(row["document"])) for row in rows]

    def version_keys(self) -> set[tuple[str, str]]:
        """Every registered ``(harness_id, version)`` pair, without parsing documents."""
        rows = self._conn.execute("SELECT harness_id, version FROM harness_versions").fetchall()
        return {(row["harness_id"], row["version"]) for row in rows}

    def harness_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT harness_id FROM harness_versions ORDER BY harness_id"
        ).fetchall()
        return [row["harness_id"] for row in rows]

    def get(self, harness_id: str, version: str) -> Harness | None:
        row = self._conn.execute(
            "SELECT document FROM harness_versions WHERE harness_id=? AND version=?",
            (harness_id, version),
        ).fetchone()
        if row is None:
            return None
        return harness_from_dict(_loads_finite(row["document"]))

    def find_by_hash(self, harness_id: str, execution_config_hash: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT version FROM harness_versions WHERE harness_id=? AND execution_config_hash=?",
            (harness_id, execution_config_hash),
        ).fetchall()
        return sorted((row["version"] for row in rows), key=lambda v: _version_key(v))

    # -- experiments ----------------------------------------------------

    def start_experiment(
        self,
        experiment_id: str,
        *,
        name: str,
        family: str,
        mode: str,
        schema_version: int,
        plan: PrivateExperimentPlan,
        implementation_hash: str,
    ) -> None:
        plan_json = json_dumps_strict(plan.to_dict(), sort_keys=True, ensure_ascii=False)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO experiments(experiment_id, name, family, mode, schema_version, plan_hash,"
                " plan_json, status, implementation_hash, created_at, finished_at)"
                " VALUES(?,?,?,?,?,?,?,'running',?,?,NULL)",
                (experiment_id, name, family, mode, schema_version, plan.plan_hash, plan_json,
                 implementation_hash, self._now()),
            )

    def record_trial(self, experiment_id: str, record: TrialRecord) -> None:
        """Persist one trial in its own short transaction, so an interrupted run keeps what it finished."""
        record_json = json_dumps_strict(record.to_dict(), sort_keys=True, ensure_ascii=False)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO trials(trial_id, experiment_id, variant_id, task_id, split, family,"
                " repetition, seed, attempt, record_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (record.trial_id, experiment_id, record.variant_id, record.task_id, record.split,
                 record.family, record.repetition, record.seed, record.attempt, record_json, self._now()),
            )

    def trials(self, experiment_id: str) -> list[TrialRecord]:
        rows = self._conn.execute(
            "SELECT record_json FROM trials WHERE experiment_id=? ORDER BY rowid", (experiment_id,)
        ).fetchall()
        return [TrialRecord.from_dict(_loads_finite(row["record_json"])) for row in rows]

    def record_selection(self, experiment_id: str, selection: dict) -> None:
        """Freeze the finalist of an experiment. One selection per experiment, never rewritten."""
        missing = [key for key in _SELECTION_KEYS if key not in selection]
        if missing:
            raise ValueError(f"selection is missing {', '.join(missing)}")
        evidence_json = json_dumps_strict(selection.get("evidence", {}), sort_keys=True, ensure_ascii=False)
        with self.transaction() as conn:
            plan_hash = self._experiment_plan_hash(conn, experiment_id)
            if selection["plan_hash"] != plan_hash:
                raise ValueError("selection plan_hash does not match the experiment plan")
            conn.execute(
                "INSERT INTO selections(experiment_id, finalist_id, finalist_hash, baseline_hash, plan_hash,"
                " policy_hash, frozen_at, evidence_json) VALUES(?,?,?,?,?,?,?,?)",
                (experiment_id, selection["finalist_id"], selection["finalist_hash"],
                 selection["baseline_hash"], selection["plan_hash"], selection["policy_hash"],
                 selection.get("frozen_at") or self._now(), evidence_json),
            )

    def selection(self, experiment_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM selections WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
        if row is None:
            return None
        data = {key: row[key] for key in (*_SELECTION_KEYS, "frozen_at")}
        data["experiment_id"] = experiment_id
        data["evidence"] = _loads_finite(row["evidence_json"])
        return data

    @staticmethod
    def _experiment_plan_hash(conn: sqlite3.Connection, experiment_id: str) -> str:
        row = conn.execute(
            "SELECT plan_hash FROM experiments WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"experiment {experiment_id} is not registered")
        return row["plan_hash"]

    def finish_experiment(self, experiment_id: str, status: str) -> None:
        if status not in EXPERIMENT_TERMINAL_STATUSES:
            raise ValueError(f"experiment status must be one of {EXPERIMENT_TERMINAL_STATUSES}, got {status!r}")
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE experiments SET status=?, finished_at=? WHERE experiment_id=?",
                (status, self._now(), experiment_id),
            )
            if cursor.rowcount == 0:
                raise LookupError(f"experiment {experiment_id} is not registered")

    def incomplete_experiments(self) -> list[str]:
        """Experiments still ``running`` that have no trials or recorded trials after they started."""
        rows = self._conn.execute(
            "SELECT e.experiment_id FROM experiments e WHERE e.status='running' AND ("
            " NOT EXISTS (SELECT 1 FROM trials t WHERE t.experiment_id=e.experiment_id)"
            " OR e.created_at < (SELECT MAX(t.created_at) FROM trials t WHERE t.experiment_id=e.experiment_id)"
            ") ORDER BY e.rowid"
        ).fetchall()
        return [row["experiment_id"] for row in rows]

    # -- holdout exposure -----------------------------------------------

    def reserve_holdout(
        self,
        *,
        task_fingerprints: Iterable[str],
        candidate_hash: str,
        baseline_hash: str,
        policy_hash: str,
        experiment_id: str,
    ) -> str | None:
        """Evaluate the exposure rule and record the attempt; return the block reason or ``None``.

        Thin wrapper over :meth:`reserve_holdout_attempt` for callers that only need the verdict. Callers
        that must link earlier interrupted attempts to the final decision use that method instead.
        """
        return self.reserve_holdout_attempt(
            task_fingerprints=task_fingerprints,
            candidate_hash=candidate_hash,
            baseline_hash=baseline_hash,
            policy_hash=policy_hash,
            experiment_id=experiment_id,
        ).blocked_reason

    def reserve_holdout_attempt(
        self,
        *,
        task_fingerprints: Iterable[str],
        candidate_hash: str,
        baseline_hash: str,
        policy_hash: str,
        experiment_id: str,
    ) -> HoldoutReservation:
        """Evaluate the exposure rule and record the attempt in ONE ``BEGIN IMMEDIATE`` transaction.

        ``experiment_id`` must be registered (its ``plan_hash`` is part of the rule). A row per fingerprint
        is inserted either way, so a refused attempt stays visible. The result carries:

        * ``blocked_reason``: why the holdout is blocked, or ``None`` when this candidate may use it. A
          holdout task already consumed by a completed decision, or reserved by another candidate, is
          blocked. An unfinished reservation of the SAME candidate may be rerun only when its baseline hash,
          policy hash and experiment ``plan_hash`` are all identical to this attempt's; any difference blocks.
        * ``earlier_experiment_ids``: the other experiments (never ``experiment_id`` itself) that hold an
          unfinished, unrefused reservation of these fingerprints under the identical candidate, baseline,
          policy and plan, in reservation order. Empty when the attempt is blocked. A rerun's decision should
          link these so no earlier attempt is dropped.
        """
        fingerprints = list(dict.fromkeys(task_fingerprints))
        with self.transaction() as conn:
            plan_hash = self._experiment_plan_hash(conn, experiment_id)
            existing = {
                fingerprint: conn.execute(
                    "SELECT h.experiment_id, h.candidate_hash, h.baseline_hash, h.policy_hash,"
                    " h.decision_status, e.plan_hash FROM holdout_exposure h"
                    " JOIN experiments e ON e.experiment_id=h.experiment_id"
                    " WHERE h.task_fingerprint=? AND h.blocked_reason IS NULL ORDER BY h.id",
                    (fingerprint,),
                ).fetchall()
                for fingerprint in fingerprints
            }
            reason = self._exposure_block(existing, candidate_hash, baseline_hash, policy_hash, plan_hash)
            earlier: list[str] = []
            if reason is None:
                for rows in existing.values():
                    for row in rows:
                        if row["experiment_id"] != experiment_id and row["experiment_id"] not in earlier:
                            earlier.append(row["experiment_id"])
            now = self._now()
            conn.executemany(
                "INSERT INTO holdout_exposure(task_fingerprint, candidate_hash, baseline_hash, policy_hash,"
                " experiment_id, reserved_at, decision_status, blocked_reason) VALUES(?,?,?,?,?,?,NULL,?)",
                [(fp, candidate_hash, baseline_hash, policy_hash, experiment_id, now, reason)
                 for fp in fingerprints],
            )
        return HoldoutReservation(reason, tuple(earlier))

    def record_holdout_exposure(self, *, task_fingerprints: Iterable[str], experiment_id: str) -> None:
        """Mark holdout content as executed by an experiment that selects nothing (an exploratory run).

        The rows carry no candidate: once the experiment records its decision they are consumed, so a later
        confirmation of the same content is blocked, whatever ids the tasks carry. Until then they count
        as a reservation by another candidate. Call before the first holdout trial.
        """
        fingerprints = list(dict.fromkeys(task_fingerprints))
        if not fingerprints:
            return
        with self.transaction() as conn:
            self._experiment_plan_hash(conn, experiment_id)  # the experiment must be registered
            now = self._now()
            conn.executemany(
                "INSERT INTO holdout_exposure(task_fingerprint, candidate_hash, baseline_hash, policy_hash,"
                " experiment_id, reserved_at, decision_status, blocked_reason) VALUES(?,NULL,NULL,NULL,?,?,NULL,NULL)",
                [(fingerprint, experiment_id, now) for fingerprint in fingerprints],
            )

    @staticmethod
    def _exposure_block(
        existing: dict[str, list[sqlite3.Row]],
        candidate_hash: str,
        baseline_hash: str,
        policy_hash: str,
        plan_hash: str,
    ) -> str | None:
        for fingerprint, rows in existing.items():
            for row in rows:
                if row["decision_status"] is not None:
                    return (
                        f"holdout task {fingerprint[:_FP_PREFIX]} already consumed by a completed decision"
                        f" ({row['decision_status']}) in experiment {row['experiment_id']}"
                    )
        for fingerprint, rows in existing.items():
            for row in rows:
                if row["candidate_hash"] != candidate_hash:
                    return (
                        f"holdout task {fingerprint[:_FP_PREFIX]} reserved by another candidate"
                        f" {str(row['candidate_hash'])[:_FP_PREFIX]} (experiment {row['experiment_id']})"
                    )
        # Same candidate: an interrupted confirmation resumes only under identical fingerprints.
        for fingerprint, rows in existing.items():
            for row in rows:
                changed = [
                    name for name, stored, current in (
                        ("baseline", row["baseline_hash"], baseline_hash),
                        ("policy", row["policy_hash"], policy_hash),
                        ("plan", row["plan_hash"], plan_hash),
                    ) if stored != current
                ]
                if changed:
                    return (
                        f"holdout task {fingerprint[:_FP_PREFIX]} was reserved by an interrupted confirmation"
                        f" of this candidate (experiment {row['experiment_id']}) under a different"
                        f" {' and '.join(changed)}; a rerun needs the identical candidate, baseline, policy"
                        " and plan"
                    )
        return None

    # -- decisions and promotion ----------------------------------------

    def record_decision(self, experiment_id: str, decision: ExperimentDecision) -> str:
        """Store the decision (one per experiment) and mark this experiment's reservations as consumed.

        An eligible decision stands only while no other experiment has already recorded a decision on any
        holdout task this experiment reserved: overlapping confirmations of one candidate must not each be
        promotable (best of N). The check shares the transaction with the insert, so exactly one of two
        concurrent decisions keeps ``eligible``; the later one is stored as ``exploratory_only``. Read the
        stored decision back with :meth:`decision` to see what was recorded.
        """
        # Rebuild through the validating constructor: a decision mutated after construction must not
        # reach the table without its invariants.
        checked = ExperimentDecision.from_dict(decision.to_dict())
        decision_json = json_dumps_strict(checked.to_dict(), sort_keys=True, ensure_ascii=False)
        decision_id = uuid.uuid4().hex
        with self.transaction() as conn:
            if checked.plan_hash != self._experiment_plan_hash(conn, experiment_id):
                raise ValueError("decision plan_hash does not match the experiment plan")
            if checked.status is DecisionStatus.ELIGIBLE:
                earlier = conn.execute(
                    "SELECT DISTINCT other.experiment_id FROM holdout_exposure mine"
                    " JOIN holdout_exposure other ON other.task_fingerprint=mine.task_fingerprint"
                    " WHERE mine.experiment_id=? AND mine.blocked_reason IS NULL"
                    " AND other.experiment_id<>mine.experiment_id AND other.blocked_reason IS NULL"
                    " AND other.decision_status IS NOT NULL ORDER BY other.id",
                    (experiment_id,),
                ).fetchall()
                if earlier:
                    ids = [row["experiment_id"] for row in earlier]
                    checked = dataclasses.replace(
                        checked, status=DecisionStatus.EXPLORATORY_ONLY,
                        reasons=[*checked.reasons,
                                 "holdout already consumed by a decision in experiment "
                                 f"{', '.join(ids)}; recorded as exploratory only"])
                    decision_json = json_dumps_strict(checked.to_dict(), sort_keys=True, ensure_ascii=False)
            conn.execute(
                "INSERT INTO decisions(decision_id, experiment_id, status, candidate_hash, baseline_id,"
                " baseline_version, plan_hash, decision_json, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (decision_id, experiment_id, checked.status.value, checked.candidate_hash,
                 checked.baseline_id, checked.baseline_version, checked.plan_hash, decision_json,
                 self._now()),
            )
            conn.execute(
                "UPDATE holdout_exposure SET decision_status=? WHERE experiment_id=? AND blocked_reason IS NULL",
                (checked.status.value, experiment_id),
            )
        return decision_id

    def decision(self, decision_id: str) -> ExperimentDecision | None:
        row = self._conn.execute(
            "SELECT decision_json FROM decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is None:
            return None
        return ExperimentDecision.from_dict(_loads_finite(row["decision_json"]))

    def promote(self, decision_id: str, harness: Harness, *, source: str) -> tuple[str, bool]:
        """Register ``harness`` as the next child of the decision's baseline, once per decision.

        Returns ``(version, created)``; repeating the call returns the existing version with ``False``.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT decision_json FROM decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
            if row is None:
                raise PromotionConflict(f"decision {decision_id} does not exist")
            try:
                decision = ExperimentDecision.from_dict(_loads_finite(row["decision_json"]))
            except ValueError as exc:
                raise PromotionConflict(f"decision {decision_id} is unreadable: {exc}") from exc
            if decision.status is not DecisionStatus.ELIGIBLE:
                raise PromotionConflict(f"decision {decision_id} is {decision.status.value}, not eligible")
            if decision.candidate_hash != harness.execution_config_hash():
                raise PromotionConflict(f"harness is not the candidate approved by decision {decision_id}")
            if harness.id != decision.baseline_id:
                raise PromotionConflict(
                    f"harness id {harness.id!r} is not the baseline harness {decision.baseline_id!r}"
                )
            existing = conn.execute(
                "SELECT version FROM promotions WHERE decision_id=?", (decision_id,)
            ).fetchone()
            if existing is not None:
                return existing["version"], False
            parent = conn.execute(
                "SELECT execution_config_hash FROM harness_versions WHERE harness_id=? AND version=?",
                (decision.baseline_id, decision.baseline_version),
            ).fetchone()
            if parent is None:
                raise PromotionConflict(
                    f"parent {decision.baseline_id} v{decision.baseline_version} is not registered"
                )
            if parent["execution_config_hash"] != decision.baseline_hash:
                raise PromotionConflict(
                    f"parent {decision.baseline_id} v{decision.baseline_version} no longer matches the"
                    " baseline the decision was made against"
                )
            version = self.allocate_child(
                decision.baseline_id, decision.baseline_version, harness, source=source
            )
            conn.execute(
                "INSERT INTO promotions(decision_id, harness_id, version, created_at) VALUES(?,?,?,?)",
                (decision_id, harness.id, version, self._now()),
            )
        return version, True
