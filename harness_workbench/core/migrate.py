"""Import legacy ``library/harnesses/<id>/<version>.json`` files into the workbench database.

The migration is additive and idempotent: JSON files are only read (never
rewritten or deleted), rows already present with the same
``execution_config_hash`` are skipped, and rows present with a different hash
are reported as conflicts and left alone. Files that cannot be trusted
(malformed, non-finite numbers, id or version disagreeing with the path) are
reported as conflicts and never imported. All rows go in through one short
``BEGIN IMMEDIATE`` transaction, so a failure leaves nothing half-imported.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_workbench.core.db import WorkbenchDB, _loads_finite
from harness_workbench.core.harness import Harness, harness_from_dict
from harness_workbench.core.store import _version_key


@dataclass
class MigrationReport:
    imported: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    backups: list[Path] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "imported": [list(pair) for pair in self.imported],
            "skipped": [list(pair) for pair in self.skipped],
            "conflicts": list(self.conflicts),
            "backups": [str(path) for path in self.backups],
            "dry_run": self.dry_run,
        }


def migrate_library(root: Path, *, dry_run: bool = False) -> MigrationReport:
    root = Path(root)
    report = MigrationReport(dry_run=dry_run)
    candidates, file_conflicts = _read_candidates(root)
    report.conflicts.extend(file_conflicts)
    existing = _existing_hashes(root)
    pending = _classify(candidates, existing, report)
    if dry_run or not pending:
        return report

    # Back up before the first write; a failed backup aborts with nothing written.
    report.backups = _make_backups(root)

    db = WorkbenchDB.open(root)
    try:
        with db.transaction() as conn:
            # Classify again under the write lock: another process may have registered rows meanwhile.
            live = {
                (row["harness_id"], row["version"]): row["execution_config_hash"]
                for row in conn.execute(
                    "SELECT harness_id, version, execution_config_hash FROM harness_versions"
                )
            }
            final = MigrationReport(dry_run=False)
            final.conflicts = list(file_conflicts)
            to_insert = _classify(candidates, live, final)
            for harness, digest in to_insert:
                db._insert(conn, harness, digest, source=harness.lineage.source)
    finally:
        db.close()
    final.backups = report.backups
    return final


def _read_candidates(root: Path) -> tuple[list[tuple[Harness, str]], list[str]]:
    """Parse every legacy file. Untrustworthy files are returned as conflict messages and dropped."""
    harness_root = root / "library" / "harnesses"
    conflicts: list[str] = []
    if not harness_root.is_dir():
        return [], conflicts
    candidates: list[tuple[Harness, str]] = []
    for path in sorted(harness_root.glob("*/*.json")):
        if not path.is_file():
            continue
        harness_id, stem = path.parent.name, path.stem
        label = f"{harness_id}/{stem}"
        try:
            raw = _loads_finite(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("document is not a JSON object")
            harness = harness_from_dict(raw)
            digest = harness.execution_config_hash()
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            conflicts.append(f"{label}: unreadable document, not imported ({exc})")
            continue
        if harness.id != harness_id or harness.version != stem:
            conflicts.append(
                f"{label}: document says {harness.id}/{harness.version}, "
                "which disagrees with its path; not imported"
            )
            continue
        candidates.append((harness, digest))
    candidates.sort(key=lambda item: (item[0].id, _version_key(item[0].version)))
    return candidates, conflicts


def _classify(
    candidates: list[tuple[Harness, str]],
    existing: dict[tuple[str, str], str],
    report: MigrationReport,
) -> list[tuple[Harness, str]]:
    pending: list[tuple[Harness, str]] = []
    for harness, digest in candidates:
        key = (harness.id, harness.version)
        known = existing.get(key)
        if known is None:
            report.imported.append(key)
            pending.append((harness, digest))
        elif known == digest:
            report.skipped.append(key)
        else:
            report.conflicts.append(
                f"{harness.id}/{harness.version}: the database holds different executable content "
                f"({known[:12]} vs file {digest[:12]}); left untouched"
            )
    return pending


def _existing_hashes(root: Path) -> dict[tuple[str, str], str]:
    """Read ``(id, version) -> hash`` without creating or modifying the database."""
    path = root / "library" / "workbench.sqlite3"
    if not path.is_file():
        return {}
    conn = _connect_readonly(path)
    try:
        try:
            rows = conn.execute(
                "SELECT harness_id, version, execution_config_hash FROM harness_versions"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return {}
            raise
    finally:
        conn.close()
    return {(harness_id, version): digest for harness_id, version, digest in rows}


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Open without side effects. A WAL database opened ``mode=ro`` leaves ``-wal``/``-shm`` files
    behind; with no live ``-wal`` file all content is in the main file, so ``immutable=1`` reads it
    without touching the directory."""
    uri = path.resolve().as_uri()
    if path.with_name(path.name + "-wal").exists():
        return sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=5)
    return sqlite3.connect(f"{uri}?immutable=1", uri=True, timeout=5)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _reserve_backup(source: Path, stamp: str) -> Path:
    """Create an empty ``<name>.bak-<stamp>[-n]`` exclusively, so two backups never share a name."""
    attempt = 1
    while True:
        suffix = stamp if attempt == 1 else f"{stamp}-{attempt}"
        target = source.with_name(f"{source.name}.bak-{suffix}")
        try:
            os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
        except FileExistsError:
            attempt += 1
            continue
        return target


def _make_backups(root: Path) -> list[Path]:
    library = root / "library"
    stamp = _utc_stamp()
    made: list[Path] = []
    try:
        index = library / "index.json"
        if index.is_file():
            target = _reserve_backup(index, stamp)
            made.append(target)
            shutil.copyfile(index, target)
        database = library / "workbench.sqlite3"
        if database.is_file():
            target = _reserve_backup(database, stamp)
            made.append(target)
            # The backup API copies a consistent snapshot, including content still in the WAL.
            source = _connect_readonly(database)
            try:
                destination = sqlite3.connect(str(target))
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()
    except BaseException:
        for path in made:
            path.unlink(missing_ok=True)
        raise
    return made
