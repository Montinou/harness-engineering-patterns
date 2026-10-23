"""SQLite store for harness versions with short, serialised write transactions.

One database per workbench root (``<root>/library/workbench.sqlite3``). Every
write runs inside a single ``BEGIN IMMEDIATE`` transaction, so version
allocation is race free across processes. No transaction is ever held across a
subprocess or network call: callers do slow work first and write afterwards.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from harness_workbench.core.harness import Harness, harness_from_dict
from harness_workbench.core.store import _version_key
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
)


class StoreBusyError(RuntimeError):
    """Another connection holds the write lock past the busy timeout."""


class LineageConflict(RuntimeError):
    """A registered version already exists with different executable content."""


class SchemaMismatch(RuntimeError):
    """The database was written by a newer schema than this code understands."""


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
