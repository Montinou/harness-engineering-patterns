import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from harness_workbench.core.db import (
    DB_SCHEMA_VERSION,
    LineageConflict,
    SchemaMismatch,
    StoreBusyError,
    WorkbenchDB,
)
from harness_workbench.core.harness import Harness, Plugin, ToolSpec


def make_harness(version: str = "1", prompt: str = "You are careful.", *, hid: str = "h") -> Harness:
    return Harness(
        id=hid,
        version=version,
        system_prompt=prompt,
        guardrails=["cite sources"],
        tools=[ToolSpec(name="search", description="web search", schema={"q": "str"}, guidance="g")],
        plugins=[Plugin(name="pl", hook="pre", config={"k": 1})],
        notes="notes",
        domain="research",
    )


class WorkbenchDBTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = WorkbenchDB.open(self.root)
        self.addCleanup(self.db.close)

    def test_open_creates_wal_database_under_library(self):
        path = self.root / "library" / "workbench.sqlite3"
        self.assertTrue(path.is_file())
        self.assertTrue(WorkbenchDB.exists(self.root))
        self.assertFalse(WorkbenchDB.exists(self.root / "elsewhere"))
        probe = sqlite3.connect(path)
        self.addCleanup(probe.close)
        self.assertEqual(probe.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        stored = probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(int(stored), DB_SCHEMA_VERSION)
        self.assertEqual(self.db._conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_reopen_keeps_rows(self):
        self.db.register_base(make_harness())
        self.db.close()
        again = WorkbenchDB.open(self.root)
        self.addCleanup(again.close)
        self.assertEqual(again.harness_ids(), ["h"])
        self.assertEqual(again.get("h", "1").system_prompt, "You are careful.")

    def test_register_base_is_idempotent_and_conflicts_on_different_content(self):
        self.assertEqual(self.db.register_base(make_harness()), "1")
        # Same executable content, different notes: idempotent.
        same = make_harness()
        same.notes = "other notes"
        self.assertEqual(self.db.register_base(same), "1")
        self.assertEqual(len(self.db.versions("h")), 1)
        with self.assertRaises(LineageConflict):
            self.db.register_base(make_harness(prompt="Different prompt"))
        self.assertEqual(self.db.get("h", "1").system_prompt, "You are careful.")

    def test_allocate_child_uses_next_decimal_and_rewrites_parent(self):
        self.db.register_base(make_harness("1"))
        # An existing "2" (e.g. adopted earlier) must not be reused.
        two = make_harness("1").fork(source="manual", version="2")
        self.db.allocate_child("h", "1", two, source="manual")  # becomes "2"
        child = make_harness("1", prompt="child prompt")
        child.version = "provisional-xyz"
        child.lineage.parent_version = "bogus"
        version = self.db.allocate_child("h", "1", child, source="improver")
        self.assertEqual(version, "3")
        stored = self.db.get("h", "3")
        self.assertEqual(stored.version, "3")
        self.assertEqual(stored.lineage.parent_version, "1")
        self.assertEqual(stored.system_prompt, "child prompt")
        # The caller's object is not mutated.
        self.assertEqual(child.version, "provisional-xyz")
        self.assertEqual(child.lineage.parent_version, "bogus")
        row = self.db._conn.execute(
            "SELECT parent_version, source FROM harness_versions WHERE harness_id='h' AND version='3'"
        ).fetchone()
        self.assertEqual(tuple(row), ("1", "improver"))

    def test_allocate_child_requires_registered_parent(self):
        with self.assertRaises(LookupError):
            self.db.allocate_child("h", "1", make_harness("2"), source="improver")
        self.db.register_base(make_harness("1"))
        with self.assertRaises(LookupError):
            self.db.allocate_child("h", "7", make_harness("2"), source="improver")
        self.assertEqual([item.version for item in self.db.versions("h")], ["1"])

    def test_allocate_child_rejects_foreign_harness_id(self):
        self.db.register_base(make_harness("1"))
        with self.assertRaises(ValueError):
            self.db.allocate_child("h", "1", make_harness("2", hid="other"), source="x")

    def test_newer_schema_is_refused(self):
        other = Path(tempfile.mkdtemp(dir=self.root))
        path = other / "library" / "workbench.sqlite3"
        path.parent.mkdir(parents=True)
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
        raw.execute("INSERT INTO schema_meta VALUES('schema_version', '99')")
        raw.commit()
        raw.close()
        with self.assertRaises(SchemaMismatch):
            WorkbenchDB.open(other)
        # Refusal leaves the file untouched: no tables were added.
        raw = sqlite3.connect(path)
        self.addCleanup(raw.close)
        names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {"schema_meta"})

    def test_busy_database_raises_store_busy_error(self):
        path = self.root / "library" / "workbench.sqlite3"
        fast = WorkbenchDB(path, busy_timeout_ms=200)
        self.addCleanup(fast.close)
        holder = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with self.assertRaises(StoreBusyError):
            fast.register_base(make_harness())
        self.assertLess(time.monotonic() - started, 2.0)
        holder.execute("ROLLBACK")
        # The connection recovers once the lock is gone.
        self.assertEqual(fast.register_base(make_harness()), "1")

    def test_transaction_rolls_back_on_exception(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO harness_versions(harness_id, version, execution_config_hash, document)"
                    " VALUES('x','1','h','{}')"
                )
                raise RuntimeError("boom")
        self.assertEqual(self.db.harness_ids(), [])
        self.assertFalse(self.db._conn.in_transaction)

    def test_non_decimal_versions_are_kept_but_not_used_for_allocation(self):
        self.db.register_base(make_harness("1"))
        odd = make_harness("1+inv-verify")
        odd.system_prompt = "odd"
        self.db.register_base(odd)
        self.assertEqual(self.db.register_base(make_harness("2.1", prompt="point one")), "2.1")
        version = self.db.allocate_child("h", "1", make_harness("1", prompt="kid"), source="s")
        self.assertEqual(version, "2")
        self.assertEqual(
            [item.version for item in self.db.versions("h")],
            ["1", "2", "1+inv-verify", "2.1"],
        )

    def test_versions_sorted_numerically_and_find_by_hash(self):
        base = make_harness("1")
        self.db.register_base(base)
        for index in range(9):
            self.db.allocate_child("h", "1", make_harness("1", prompt=f"p{index}"), source="s")
        self.assertEqual([h.version for h in self.db.versions("h")], [str(n) for n in range(1, 11)])
        clone = make_harness("1", prompt="p3")
        found = self.db.find_by_hash("h", clone.execution_config_hash())
        self.assertEqual(found, ["5"])
        self.assertEqual(self.db.find_by_hash("h", "0" * 64), [])
        self.assertIsNone(self.db.get("h", "99"))
        self.assertEqual(self.db.versions("missing"), [])

    def test_non_finite_numbers_are_never_written(self):
        bad = make_harness()
        bad.plugins[0].config = {"k": float("inf")}
        with self.assertRaises(ValueError):
            self.db.register_base(bad)
        self.assertEqual(self.db.harness_ids(), [])

    def test_non_finite_documents_are_rejected_on_read(self):
        good = make_harness()
        self.db.register_base(good)
        for literal in ("1e400", "Infinity", "NaN"):
            document = good.to_json().replace('"k": 1', f'"k": {literal}')
            self.assertIn(literal, document)
            with self.db.transaction() as conn:
                conn.execute("UPDATE harness_versions SET document=?", (document,))
            with self.assertRaises(ValueError, msg=literal):
                self.db.get("h", "1")
            with self.assertRaises(ValueError, msg=literal):
                self.db.versions("h")


if __name__ == "__main__":
    unittest.main()
