"""migrate-library imports legacy JSON versions into the database: dry run, backups, idempotence."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from harness_workbench.cli import main
from harness_workbench.core.db import WorkbenchDB
from harness_workbench.core.harness import Harness, save_harness
from harness_workbench.core.migrate import MigrationReport, migrate_library
from harness_workbench.core.store import MigrationRequired, persist_result


def legacy_harness(version: str, prompt: str | None = None, harness_id: str = "sample") -> Harness:
    return Harness(id=harness_id, version=version, system_prompt=prompt or f"prompt {version}")


class MigrateLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.library = self.root / "library"
        self.harness_dir = self.library / "harnesses" / "sample"

    def write_legacy(self, *versions: str) -> None:
        for version in versions:
            save_harness(legacy_harness(version), self.harness_dir / f"{version}.json")

    def write_index(self) -> Path:
        index = self.library / "index.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text('{"harnesses": {}}\n', encoding="utf-8")
        return index

    def snapshot_files(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in sorted(self.library.rglob("*.json"))
        }

    def db_keys(self) -> set[tuple[str, str]]:
        db = WorkbenchDB.open(self.root)
        try:
            return db.version_keys()
        finally:
            db.close()

    def test_dry_run_reports_without_writing(self) -> None:
        self.write_legacy("1", "2")
        self.write_index()
        before = self.snapshot_files()

        report = migrate_library(self.root, dry_run=True)

        self.assertIsInstance(report, MigrationReport)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.imported, [("sample", "1"), ("sample", "2")])
        self.assertEqual(report.backups, [])
        self.assertFalse(WorkbenchDB.exists(self.root))
        self.assertEqual(sorted(p.name for p in self.library.iterdir()), ["harnesses", "index.json"])
        self.assertEqual(self.snapshot_files(), before)
        # The persist path still refuses until a real migration ran.
        with self.assertRaises(MigrationRequired):
            persist_result(self.root, mock.Mock())

    def test_dry_run_against_existing_database_writes_nothing(self) -> None:
        db = WorkbenchDB.open(self.root)
        db.register_base(legacy_harness("1"))
        db.close()
        self.write_legacy("1", "2")
        listing = sorted(p.name for p in self.library.iterdir())

        report = migrate_library(self.root, dry_run=True)

        self.assertEqual(report.imported, [("sample", "2")])
        self.assertEqual(report.skipped, [("sample", "1")])
        self.assertEqual(report.backups, [])
        self.assertEqual(sorted(p.name for p in self.library.iterdir()), listing)
        self.assertEqual(self.db_keys(), {("sample", "1")})

    def test_migration_is_idempotent_and_backs_up_index_and_db(self) -> None:
        other = legacy_harness("1", harness_id="other")
        db = WorkbenchDB.open(self.root)
        db.register_base(other)
        db.close()
        self.write_legacy("1", "2")
        index = self.write_index()
        before = self.snapshot_files()

        first = migrate_library(self.root)

        self.assertFalse(first.dry_run)
        self.assertEqual(first.imported, [("sample", "1"), ("sample", "2")])
        self.assertEqual(first.conflicts, [])
        names = sorted(path.name.split(".bak-")[0] for path in first.backups)
        self.assertEqual(names, ["index.json", "workbench.sqlite3"])
        for path in first.backups:
            self.assertTrue(path.is_file())
            self.assertRegex(path.name, r"\.bak-\d{8}T\d{6}Z")
        db_backup = next(p for p in first.backups if p.name.startswith("workbench.sqlite3"))
        index_backup = next(p for p in first.backups if p.name.startswith("index.json"))
        self.assertEqual(index_backup.read_bytes(), index.read_bytes())
        copy = sqlite3.connect(str(db_backup))
        try:
            rows = copy.execute("SELECT harness_id, version FROM harness_versions").fetchall()
        finally:
            copy.close()
        self.assertEqual(rows, [("other", "1")])  # backup predates the import
        self.assertEqual(self.db_keys(), {("other", "1"), ("sample", "1"), ("sample", "2")})
        after = self.snapshot_files()
        for key, content in before.items():
            self.assertEqual(after[key], content)  # JSON files are never rewritten
        self.assertEqual({key for key in after if ".bak-" not in key}, set(before))

        second = migrate_library(self.root)

        self.assertEqual(second.imported, [])
        self.assertEqual(second.skipped, [("sample", "1"), ("sample", "2")])
        self.assertEqual(second.backups, [])
        self.assertEqual(second.conflicts, [])
        self.assertEqual(self.db_keys(), {("other", "1"), ("sample", "1"), ("sample", "2")})

    def test_first_migration_without_database_backs_up_only_the_index(self) -> None:
        self.write_legacy("1")
        self.write_index()

        report = migrate_library(self.root)

        self.assertEqual([p.name.split(".bak-")[0] for p in report.backups], ["index.json"])
        self.assertTrue(WorkbenchDB.exists(self.root))

    def test_backup_names_are_collision_safe_within_one_second(self) -> None:
        self.write_index()
        with mock.patch("harness_workbench.core.migrate._utc_stamp", return_value="20261023T120000Z"):
            self.write_legacy("1")
            first = migrate_library(self.root)
            # A second migration in the same second (new file to import) must not overwrite the first backup.
            self.write_legacy("2")
            second = migrate_library(self.root)
        self.assertEqual(len(first.backups), 1)
        self.assertEqual(len(second.backups), 2)
        all_backups = [*first.backups, *second.backups]
        self.assertEqual(len({str(p) for p in all_backups}), len(all_backups))
        self.assertTrue(all(p.is_file() for p in all_backups))

    def test_non_decimal_version_file_is_imported_and_next_child_is_max_decimal_plus_one(self) -> None:
        self.write_legacy("1", "1.1", "2")

        report = migrate_library(self.root)

        self.assertEqual(report.imported, [("sample", "1"), ("sample", "2"), ("sample", "1.1")])
        db = WorkbenchDB.open(self.root)
        try:
            self.assertEqual({v.version for v in db.versions("sample")}, {"1", "1.1", "2"})
            child = legacy_harness("x", "child prompt")
            self.assertEqual(db.allocate_child("sample", "2", child, source="improve"), "3")
        finally:
            db.close()

    def test_conflicting_content_is_reported_not_overwritten(self) -> None:
        db = WorkbenchDB.open(self.root)
        db.register_base(legacy_harness("1", "database prompt"))
        db.close()
        save_harness(legacy_harness("1", "file prompt"), self.harness_dir / "1.json")
        self.write_legacy("2")
        before = self.snapshot_files()

        report = migrate_library(self.root)

        self.assertEqual(report.imported, [("sample", "2")])
        self.assertEqual(len(report.conflicts), 1)
        self.assertIn("sample/1", report.conflicts[0])
        db = WorkbenchDB.open(self.root)
        try:
            self.assertEqual(db.get("sample", "1").system_prompt, "database prompt")
        finally:
            db.close()
        self.assertEqual(self.snapshot_files(), before)

    def test_non_finite_and_malformed_documents_are_conflicts_never_imported(self) -> None:
        self.write_legacy("1")
        nan_doc = json.dumps(legacy_harness("2").to_dict())
        (self.harness_dir / "2.json").write_text(
            nan_doc.replace('"memory_config"', '"bad": 1e400, "memory_config"'), encoding="utf-8")
        (self.harness_dir / "3.json").write_text('{"id": "sample", "version": "3", "x": NaN}', encoding="utf-8")
        (self.harness_dir / "4.json").write_text("{not json", encoding="utf-8")
        (self.harness_dir / "5.json").write_text("[1, 2]", encoding="utf-8")
        save_harness(legacy_harness("7"), self.harness_dir / "6.json")  # version field disagrees with file name

        report = migrate_library(self.root)

        self.assertEqual(report.imported, [("sample", "1")])
        self.assertEqual(len(report.conflicts), 5)
        self.assertEqual(self.db_keys(), {("sample", "1")})

    def test_failure_mid_import_leaves_no_half_imported_state(self) -> None:
        self.write_legacy("1", "2", "3")
        real_insert = WorkbenchDB._insert
        calls = {"n": 0}

        def flaky(conn, harness, digest, *, source):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("disk full")
            return real_insert(conn, harness, digest, source=source)

        with mock.patch.object(WorkbenchDB, "_insert", staticmethod(flaky)):
            with self.assertRaises(RuntimeError):
                migrate_library(self.root)

        self.assertEqual(self.db_keys(), set())
        report = migrate_library(self.root)
        self.assertEqual(report.imported, [("sample", "1"), ("sample", "2"), ("sample", "3")])

    def test_unrelated_files_and_directories_are_ignored(self) -> None:
        self.write_legacy("1")
        (self.harness_dir / "notes.txt").write_text("x", encoding="utf-8")
        (self.library / "harnesses" / "stray.json").write_text("{}", encoding="utf-8")

        report = migrate_library(self.root)

        self.assertEqual(report.imported, [("sample", "1")])
        self.assertEqual(report.conflicts, [])

    def test_empty_root_is_a_no_op(self) -> None:
        report = migrate_library(self.root)
        self.assertEqual((report.imported, report.skipped, report.conflicts, report.backups), ([], [], [], []))
        self.assertFalse(WorkbenchDB.exists(self.root))

    def test_to_dict_is_json_serialisable(self) -> None:
        self.write_legacy("1")
        self.write_index()
        data = migrate_library(self.root).to_dict()
        text = json.dumps(data)
        loaded = json.loads(text)
        self.assertEqual(loaded["imported"], [["sample", "1"]])
        self.assertEqual(loaded["dry_run"], False)
        self.assertEqual(len(loaded["backups"]), 1)
        self.assertIsInstance(loaded["backups"][0], str)


class MigrateLibraryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        save_harness(legacy_harness("1"), self.root / "library" / "harnesses" / "sample" / "1.json")

    def run_cli(self, *args: str) -> tuple[int, str]:
        out = StringIO()
        with redirect_stdout(out):
            code = main(["migrate-library", "--root", str(self.root), *args])
        return code, out.getvalue()

    def test_dry_run_json_then_real_run(self) -> None:
        code, text = self.run_cli("--dry-run", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["imported"], [["sample", "1"]])
        self.assertTrue(json.loads(text)["dry_run"])
        self.assertFalse(WorkbenchDB.exists(self.root))

        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("sample", text)
        self.assertTrue(WorkbenchDB.exists(self.root))

    def test_conflicts_make_the_exit_code_nonzero(self) -> None:
        db = WorkbenchDB.open(self.root)
        db.register_base(legacy_harness("1", "other content"))
        db.close()
        code, text = self.run_cli("--json")
        self.assertEqual(code, 1)
        self.assertEqual(len(json.loads(text)["conflicts"]), 1)


if __name__ == "__main__":
    unittest.main()
