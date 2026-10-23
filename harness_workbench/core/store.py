"""Persistence for runs and the improvement library.

``runs/`` is the evidence for one improve call. ``library/`` is the continual
record: versioned harnesses plus lessons, in the role of Harness²'s
improvement library (``EXPERIENCE.md``).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness_workbench.core.harness import Harness, harness_from_dict, save_harness, utc_now
from harness_workbench.experiments.validation import json_dumps_strict

if TYPE_CHECKING:  # db imports _version_key from this module, so it is imported lazily below
    from harness_workbench.core.db import WorkbenchDB


class MigrationRequired(RuntimeError):
    """The library holds JSON versions the database has never seen."""


class ExportError(RuntimeError):
    """The database transaction committed but a JSON view could not be written."""


def persist_result(root: Path, result: Any, *, decision_id: str | None = None) -> Path:
    """Allocate versions in the database, then export the JSON views. Returns the run path.

    The database is the source of truth: versions are allocated in one
    transaction and committed before any file is written. A failure while
    exporting raises :class:`ExportError`; ``export_library`` regenerates the
    views from the committed state.

    With ``decision_id`` the adopted version is promoted through
    ``WorkbenchDB.promote`` instead of allocated: the decision must be an
    eligible one for exactly that candidate (otherwise ``PromotionConflict``),
    and promoting the same decision again returns the version it created.
    """
    from harness_workbench.core.db import WorkbenchDB

    root = Path(root)
    promoting = decision_id is not None and len(result.versions) > 1
    if promoting and len(result.versions) != 2:
        raise ValueError(
            f"a decision promotes exactly one candidate, but the result has {len(result.versions) - 1}"
        )
    _refuse_unmigrated(root)
    root.mkdir(parents=True, exist_ok=True)
    db = WorkbenchDB.open(root)
    try:
        _refuse_unmigrated(root, db)
        with db.transaction():
            db.register_base(result.base)
            if promoting:
                final = result.versions[1]
                final.version, _created = db.promote(decision_id, final, source=final.lineage.source)
                stored = db.get(final.id, final.version)
                assert stored is not None
                final.lineage.parent_version = stored.lineage.parent_version  # the decision's baseline
            else:
                for previous, harness in zip(result.versions, result.versions[1:]):
                    harness.version = db.allocate_child(
                        previous.id, previous.version, harness, source=harness.lineage.source
                    )
                    harness.lineage.parent_version = previous.version
        # Report metadata must describe the committed version IDs, not the provisional ones.
        for step in result.steps:
            step.base_report.harness_version = step.base.version
            step.adopted_report.harness_version = step.adopted.version
        try:
            run_dir = export_views(db, root, result, decision_id=decision_id)
        except Exception as exc:
            raise ExportError(
                f"version(s) committed to the database but exporting views failed: {exc}; "
                f"run harness-workbench export-library --root {root}"
            ) from exc
        assert run_dir is not None
        return run_dir
    finally:
        db.close()


def export_views(
    db: WorkbenchDB, root: Path, result: Any | None = None, *, decision_id: str | None = None
) -> Path | None:
    """Write the JSON views derived from the database; with ``result`` also its run directory and lessons.

    Returns the run directory when ``result`` is given. Harness files are immutable, so
    only versions without a file are written; ``index.json`` is always regenerated.
    """
    run_dir: Path | None = None
    if result is not None:
        run_dir = _write_run(root, result, decision_id=decision_id)
    _export_library_files(db, root)
    return run_dir


def export_library(root: Path) -> list[Path]:
    """Regenerate every JSON view from the database. Returns the paths written."""
    from harness_workbench.core.db import WorkbenchDB

    root = Path(root)
    if not WorkbenchDB.exists(root):
        _refuse_unmigrated(root)
        raise FileNotFoundError(f"no workbench database under {root / 'library'}")
    db = WorkbenchDB.open(root)
    try:
        return _export_library_files(db, root)
    finally:
        db.close()


def _legacy_keys(root: Path) -> set[tuple[str, str]]:
    harness_root = root / "library" / "harnesses"
    if not harness_root.is_dir():
        return set()
    return {
        (path.parent.name, path.stem) for path in harness_root.glob("*/*.json") if path.is_file()
    }


def _refuse_unmigrated(root: Path, db: WorkbenchDB | None = None) -> None:
    """Raise MigrationRequired when a JSON version has no database row. Writes nothing."""
    from harness_workbench.core.db import WorkbenchDB

    if db is None and WorkbenchDB.exists(root):
        return  # re-checked once the database is open
    known = db.version_keys() if db is not None else set()
    if _legacy_keys(root) - known:
        raise MigrationRequired(f"run harness-workbench migrate-library --root {root}")


def _export_library_files(db: WorkbenchDB, root: Path) -> list[Path]:
    """Harness files for versions that lack one, then ``index.json``, under one write lock.

    Holding the database write lock serialises concurrent exporters, so the last
    index written reflects the newest committed state.
    """
    written: list[Path] = []
    library = root / "library"
    with db.transaction():
        index: dict[str, Any] = {"harnesses": {}}
        for harness_id in db.harness_ids():
            versions = db.versions(harness_id)
            for harness in versions:
                target = library / "harnesses" / harness_id / f"{harness.version}.json"
                if not target.exists():
                    _atomic_write(target, harness.to_json())
                    written.append(target)
            index["harnesses"][harness_id] = _index_entry(versions)
        index_path = library / "index.json"
        _atomic_write(index_path, json_dumps_strict(index, indent=2, ensure_ascii=False) + "\n")
        written.append(index_path)
    return written


def _index_entry(versions: list[Harness]) -> dict[str, Any]:
    latest = versions[-1]
    return {
        "name": latest.name,
        "domain": latest.domain,
        "versions": [
            {
                "version": harness.version,
                "parent": harness.lineage.parent_version,
                "source": harness.lineage.source,
                "score": harness.lineage.metrics.get("score"),
                "created_at": harness.lineage.created_at,
            }
            for harness in versions
        ],
    }


def _write_run(root: Path, result: Any, *, decision_id: str | None) -> Path:
    stamp = utc_now().replace("-", "").replace(":", "")
    run_id = f"{stamp}-{result.base.id}-v{result.adopted.version}"
    run_dir = root / "runs" / run_id
    suffix = 2
    while run_dir.exists():
        run_dir = root / "runs" / f"{run_id}-{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)

    summary = result.to_dict() | {"run_dir": _rel(run_dir, root)}
    if decision_id is not None:
        summary["decision_id"] = decision_id
    _write_json(run_dir / "task.json", result.task.to_dict())
    _write_json(run_dir / "summary.json", summary)
    save_harness(result.base, run_dir / "base.json")
    save_harness(result.adopted, run_dir / "adopted.json")
    _write_json(
        run_dir / "candidates.json",
        [
            {
                "step": index,
                "base_version": step.base.version,
                "candidates": [candidate.to_dict() for candidate in step.candidates],
            }
            for index, step in enumerate(result.steps)
        ],
    )
    _write_json(
        run_dir / "reports.json",
        [
            {
                "step": index,
                "base": step.base_report.to_dict(),
                "adopted": step.adopted_report.to_dict(),
                "candidates": [
                    {"label": candidate.label, "edit_id": candidate.edit.id, "report": report.to_dict()}
                    for candidate, report in step.candidate_reports
                ],
            }
            for index, step in enumerate(result.steps)
        ],
    )
    lesson_text = format_experience(result, run_dir=run_dir, root=root)
    (run_dir / "lessons.md").write_text(lesson_text, encoding="utf-8")

    library = root / "library"
    _append_text(library / "EXPERIENCE.md", lesson_text)
    domain = result.task.domain or "general"
    _append_text(library / "lessons" / f"{domain}.md", lesson_text)
    return run_dir


def _atomic_write(path: Path, text: str) -> None:
    """Write ``path`` through a sibling temp file and ``os.replace``; never leaves a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def load_versions(root: Path, harness_id: str) -> list[Harness]:
    """Load every saved version of ``harness_id``, oldest version key first.

    Reads the database when the root has one; otherwise reads the JSON files (legacy, read-only).
    """
    from harness_workbench.core.db import WorkbenchDB, _loads_finite

    root = Path(root)
    if WorkbenchDB.exists(root):
        db = WorkbenchDB.open(root)
        try:
            return db.versions(harness_id)
        finally:
            db.close()
    harness_dir = root / "library" / "harnesses" / harness_id
    if not harness_dir.is_dir():
        return []
    files = sorted(harness_dir.glob("*.json"), key=lambda path: _version_key(path.stem))
    versions = []
    for path in files:
        try:
            raw = _loads_finite(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must contain a JSON object")
        versions.append(harness_from_dict(raw))
    return versions


def known_harness_ids(root: Path) -> list[str]:
    from harness_workbench.core.db import WorkbenchDB

    root = Path(root)
    if WorkbenchDB.exists(root):
        db = WorkbenchDB.open(root)
        try:
            return db.harness_ids()
        finally:
            db.close()
    harness_root = root / "library" / "harnesses"
    if not harness_root.is_dir():
        return []
    return sorted(path.name for path in harness_root.iterdir() if path.is_dir())


def lineage_chain(versions: list[Harness]) -> list[Harness]:
    """Follow parent_version from the newest saved harness back to the seed."""
    if not versions:
        return []
    by_version = {item.version: item for item in versions}
    current: Harness | None = max(versions, key=lambda item: _version_key(item.version))
    chain: list[Harness] = []
    seen: set[str] = set()
    while current is not None and current.version not in seen:
        chain.append(current)
        seen.add(current.version)
        parent = current.lineage.parent_version
        current = by_version.get(parent) if parent else None
    chain.reverse()
    return chain


def format_experience(result: Any, *, run_dir: Path, root: Path) -> str:
    """Markdown lesson block. One block is appended per improve call."""
    base_score = result.base_report.score
    adopted_score = result.adopted_report.score
    delta = adopted_score - base_score
    lines = [
        f"## {utc_now()} — {result.base.id} v{result.base.version} → v{result.adopted.version}",
        "",
        f"Task ({result.task.domain}): {result.task.description}",
        f"Mode: {result.mode}  k={result.k}  margin={result.margin:.3f}",
        f"Score: {base_score:.3f} → {adopted_score:.3f} (delta {delta:+.3f})",
        "",
        "Lessons:",
    ]
    for lesson in result.lessons:
        lines.append(f"- {lesson}")
    lines.extend(["", f"Run: {_rel(run_dir, root)}", ""])
    return "\n".join(lines) + "\n"


def _version_key(version: str) -> tuple:
    try:
        return (0, int(version))
    except ValueError:
        return (1, version)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps_strict(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
