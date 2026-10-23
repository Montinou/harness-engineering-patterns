"""persist_result allocates through the database; the JSON files are exported views."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from harness_workbench.cli import main
from harness_workbench.core import store
from harness_workbench.core.db import LineageConflict, WorkbenchDB
from harness_workbench.core.executor import MockExecutor, Task
from harness_workbench.core.harness import Harness, save_harness
from harness_workbench.core.improver import Improver
from harness_workbench.core.proposer import RuleBasedProposer
from harness_workbench.core.store import (
    ExportError,
    MigrationRequired,
    export_library,
    export_views,
    load_versions,
    persist_result,
)
from harness_workbench.experiments import ExperimentSpec, run_experiment
from harness_workbench.experiments.validation import ValidationError
from tests.support import ScriptedExecutor, exact_task, write_manifest
from tests.unit.test_improver_gate import record_decision


def base_harness(prompt: str = "Answer") -> Harness:
    return Harness(id="sample", version="1", system_prompt=prompt)


def research_task() -> Task:
    return Task.from_text("research question", domain="research")


def improve(root: Path, harness: Harness | None = None):
    return Improver(RuleBasedProposer(), MockExecutor()).improve(
        harness or base_harness(), research_task(), root=root)


class StoreViewTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.harness_dir = self.root / "library" / "harnesses" / "sample"

    def db_versions(self):
        db = WorkbenchDB.open(self.root)
        try:
            return db.versions("sample")
        finally:
            db.close()

    def test_versions_are_allocated_in_db_and_exported_as_files(self) -> None:
        result = improve(self.root)
        self.assertEqual(result.adopted.version, "2")
        self.assertEqual([v.version for v in self.db_versions()], ["1", "2"])
        self.assertEqual(sorted(p.name for p in self.harness_dir.glob("*.json")), ["1.json", "2.json"])
        exported = json.loads((self.harness_dir / "2.json").read_text(encoding="utf-8"))
        self.assertEqual(exported["lineage"]["parent_version"], "1")
        # Reports describe the allocated versions.
        for step in result.steps:
            self.assertEqual(step.adopted_report.harness_version, step.adopted.version)
            self.assertEqual(step.base_report.harness_version, step.base.version)
        self.assertTrue((result.run_dir / "summary.json").is_file())
        self.assertFalse(list(self.harness_dir.glob("*.tmp-*")))

    def test_decision_id_is_recorded_in_the_summary(self) -> None:
        result = Improver(RuleBasedProposer(), MockExecutor()).improve(base_harness(), research_task())
        decision_id = record_decision(self.root, result.base, result.adopted)
        run_dir = persist_result(self.root, result, decision_id=decision_id)
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["decision_id"], decision_id)

    def test_base_is_never_renumbered_and_index_is_rebuilt_from_db(self) -> None:
        improve(self.root)
        improve(self.root)
        self.assertEqual(sorted(p.name for p in self.harness_dir.glob("*.json")),
                         ["1.json", "2.json", "3.json"])
        parents = {v.version: v.lineage.parent_version for v in self.db_versions()}
        self.assertEqual(parents, {"1": None, "2": "1", "3": "1"})
        index = json.loads((self.root / "library" / "index.json").read_text(encoding="utf-8"))
        listed = {item["version"]: item["parent"] for item in index["harnesses"]["sample"]["versions"]}
        self.assertEqual(listed, {"1": None, "2": "1", "3": "1"})
        # A stale or damaged index is rebuilt from the database, never merged.
        (self.root / "library" / "index.json").write_text('{"harnesses": {"ghost": {}}}', encoding="utf-8")
        improve(self.root)
        index = json.loads((self.root / "library" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(list(index["harnesses"]), ["sample"])
        self.assertEqual(len(index["harnesses"]["sample"]["versions"]), 4)

    def test_failed_export_keeps_db_state_and_export_library_regenerates(self) -> None:
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_replace(src, dst)

        with mock.patch.object(store.os, "replace", flaky):
            with self.assertRaises(ExportError):
                improve(self.root)
        self.assertEqual([v.version for v in self.db_versions()], ["1", "2"])
        self.assertFalse((self.harness_dir / "2.json").exists())
        self.assertFalse(list(self.harness_dir.glob("*.tmp-*")), "failed export leaves no temp file")
        written = export_library(self.root)
        self.assertIn(self.harness_dir / "2.json", written)
        self.assertTrue((self.harness_dir / "2.json").is_file())
        self.assertTrue((self.root / "library" / "index.json").is_file())
        # Idempotent: a second regeneration changes no harness file.
        before = (self.harness_dir / "2.json").read_bytes()
        export_library(self.root)
        self.assertEqual((self.harness_dir / "2.json").read_bytes(), before)

    def test_export_views_without_result_only_writes_library_views(self) -> None:
        improve(self.root)
        (self.harness_dir / "1.json").unlink()
        db = WorkbenchDB.open(self.root)
        self.addCleanup(db.close)
        self.assertIsNone(export_views(db, self.root))
        self.assertTrue((self.harness_dir / "1.json").is_file())

    def test_lineage_conflict_on_same_identity_different_content(self) -> None:
        improve(self.root)
        runs_before = sorted(p.name for p in (self.root / "runs").iterdir())
        with self.assertRaises(LineageConflict):
            improve(self.root, base_harness("A different prompt"))
        self.assertEqual([v.version for v in self.db_versions()], ["1", "2"])
        self.assertEqual(sorted(p.name for p in (self.root / "runs").iterdir()), runs_before)

    def test_unmigrated_json_library_is_refused_before_writing(self) -> None:
        save_harness(base_harness(), self.harness_dir / "1.json")
        two = base_harness("Answer carefully")
        two.version = "2"
        save_harness(two, self.harness_dir / "2.json")
        with self.assertRaises(MigrationRequired) as caught:
            improve(self.root)
        self.assertIn("harness-workbench migrate-library --root", str(caught.exception))
        self.assertFalse(WorkbenchDB.exists(self.root))
        self.assertFalse((self.root / "runs").exists())
        self.assertEqual(sorted(p.name for p in self.harness_dir.glob("*.json")), ["1.json", "2.json"])

    def test_json_file_without_db_row_is_refused_even_when_db_exists(self) -> None:
        improve(self.root)
        stray = base_harness("stray")
        stray.version = "9"
        save_harness(stray, self.harness_dir / "9.json")
        with self.assertRaises(MigrationRequired):
            improve(self.root)
        self.assertEqual([v.version for v in self.db_versions()], ["1", "2"])

    def test_load_versions_reads_legacy_files_without_db(self) -> None:
        save_harness(base_harness(), self.harness_dir / "1.json")
        two = base_harness("Answer carefully")
        two.version = "2"
        save_harness(two, self.harness_dir / "2.json")
        self.assertEqual([v.version for v in load_versions(self.root, "sample")], ["1", "2"])
        self.assertEqual(store.known_harness_ids(self.root), ["sample"])
        self.assertFalse(WorkbenchDB.exists(self.root))

    def test_load_versions_reads_the_database_when_present(self) -> None:
        improve(self.root)
        (self.harness_dir / "2.json").unlink()
        self.assertEqual([v.version for v in load_versions(self.root, "sample")], ["1", "2"])
        self.assertEqual(store.known_harness_ids(self.root), ["sample"])

    def test_legacy_file_with_overflowing_number_is_rejected(self) -> None:
        self.harness_dir.mkdir(parents=True)
        document = json.dumps(base_harness().to_dict())
        (self.harness_dir / "1.json").write_text(
            document.replace('"window": 2', '"window": 1e400'), encoding="utf-8")
        with self.assertRaises(ValidationError):
            load_versions(self.root, "sample")

    def test_exported_views_never_contain_non_finite_literals(self) -> None:
        improve(self.root)
        for path in (self.root / "library").rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("Infinity", text, path)
            self.assertNotIn("NaN", text, path)

    def test_results_json_serializes_an_unlimited_budget_without_infinity(self) -> None:
        manifest = write_manifest(
            self.root / "m", tasks=[exact_task(f"t{i}") for i in (1, 2, 3)],
            variants=[{"id": "candidate", "system_prompt": "cand"}],
            max_cost_ratio=None, max_latency_ratio=None)
        executor = ScriptedExecutor({"base": lambda t: False, "cand": lambda t: True})
        result = run_experiment(ExperimentSpec.load(manifest), executor, self.root / "out")
        text = Path(result["results_path"]).read_text(encoding="utf-8")
        self.assertNotIn("Infinity", text)
        settings = json.loads(text)["settings"]
        self.assertIsNone(settings["max_cost_ratio"])
        self.assertIsNone(settings["max_latency_ratio"])

    def test_export_library_cli_prints_regenerated_paths(self) -> None:
        improve(self.root)
        (self.harness_dir / "2.json").unlink()
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["export-library", "--root", str(self.root), "--json"]), 0)
        paths = json.loads(out.getvalue())
        self.assertIn(str(self.harness_dir / "2.json"), paths)
        self.assertTrue((self.harness_dir / "2.json").is_file())

    def test_export_library_cli_refuses_a_root_without_a_database(self) -> None:
        err = StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(main(["export-library", "--root", str(self.root)]), 2)
        self.assertFalse(WorkbenchDB.exists(self.root))

    def test_concurrent_first_open_of_a_new_database_does_not_fail(self) -> None:
        errors: list[str] = []
        for round_number in range(40):
            root = self.root / f"round{round_number}"
            gate = threading.Barrier(6)

            def open_and_close() -> None:
                gate.wait(timeout=30)
                try:
                    WorkbenchDB.open(root).close()
                except Exception as exc:  # reported below
                    errors.append(repr(exc))

            threads = [threading.Thread(target=open_and_close) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
