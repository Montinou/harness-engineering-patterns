"""Persistence for runs and the improvement library.

``runs/`` is the evidence for one improve call. ``library/`` is the continual
record: versioned harnesses plus lessons, in the role of Harness²'s
improvement library (``EXPERIENCE.md``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness_workbench.core.harness import Harness, load_harness, save_harness, utc_now


def persist_result(root: Path, result: Any) -> Path:
    """Write a run directory and update the library. Returns the run path."""
    root.mkdir(parents=True, exist_ok=True)
    _persist_unique_versions(root, result)
    stamp = utc_now().replace("-", "").replace(":", "")
    run_id = f"{stamp}-{result.base.id}-v{result.adopted.version}"
    run_dir = root / "runs" / run_id
    suffix = 2
    while run_dir.exists():
        run_dir = root / "runs" / f"{run_id}-{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)

    _write_json(run_dir / "task.json", result.task.to_dict())
    _write_json(run_dir / "summary.json", result.to_dict() | {"run_dir": _rel(run_dir, root)})
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
    # Immutable version files were allocated before writing the run evidence.
    _append_text(library / "EXPERIENCE.md", lesson_text)
    domain = result.task.domain or "general"
    _append_text(library / "lessons" / f"{domain}.md", lesson_text)
    _update_index(library / "index.json", result.versions)
    return run_dir



def _persist_unique_versions(root: Path, result: Any) -> None:
    """Allocate immutable version files, preserving parent pointers.

    Concurrent creators cannot overwrite a version because 'x' is exclusive.
    The Markdown summary/index are best-effort single-writer metadata; a
    database/transactional store is needed for multi-host coordination.
    """
    root_dir = root / "library" / "harnesses" / result.base.id
    root_dir.mkdir(parents=True, exist_ok=True)
    version_map: dict[str, str] = {}
    for index, harness in enumerate(result.versions):
        original = harness.version
        if harness.lineage.parent_version in version_map:
            harness.lineage.parent_version = version_map[harness.lineage.parent_version]
        candidate = original
        if index == 0:
            existing = root_dir / f"{candidate}.json"
            if existing.exists():
                # An already-known parent is never replaced.
                continue
        while True:
            filename = root_dir / f"{candidate}.json"
            if filename.exists():
                candidate = _next_available_version(root_dir)
            harness.version = candidate
            try:
                with filename.open("x", encoding="utf-8") as handle:
                    handle.write(harness.to_json())
                break
            except FileExistsError:
                candidate = _next_available_version(root_dir)
        version_map[original] = candidate


def _next_available_version(directory: Path) -> str:
    numbers = [int(p.stem) for p in directory.glob("*.json") if p.stem.isdecimal()]
    return str(max(numbers, default=0) + 1)


def load_versions(root: Path, harness_id: str) -> list[Harness]:
    """Load every saved version of ``harness_id``, oldest version key first."""
    harness_dir = root / "library" / "harnesses" / harness_id
    if not harness_dir.is_dir():
        return []
    files = sorted(harness_dir.glob("*.json"), key=lambda path: _version_key(path.stem))
    return [load_harness(path) for path in files]


def known_harness_ids(root: Path) -> list[str]:
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


def _update_index(path: Path, versions: list[Harness]) -> None:
    index: dict[str, Any] = {"harnesses": {}}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("harnesses"), dict):
            index = loaded
    harnesses = index.setdefault("harnesses", {})
    for harness in versions:
        entry = harnesses.setdefault(
            harness.id,
            {"name": harness.name, "domain": harness.domain, "versions": []},
        )
        entry["name"] = harness.name
        entry["domain"] = harness.domain
        listed = entry.setdefault("versions", [])
        score = harness.lineage.metrics.get("score")
        record = {
            "version": harness.version,
            "parent": harness.lineage.parent_version,
            "source": harness.lineage.source,
            "score": score,
            "created_at": harness.lineage.created_at,
        }
        replaced = False
        for existing in listed:
            if existing.get("version") == harness.version:
                existing.update(record)
                replaced = True
                break
        if not replaced:
            listed.append(record)
        listed.sort(key=lambda item: _version_key(str(item.get("version", ""))))
    _write_json(path, index)


def _version_key(version: str) -> tuple:
    try:
        return (0, int(version))
    except ValueError:
        return (1, version)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
