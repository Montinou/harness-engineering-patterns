"""Split assignment and the pilot manifest for the LiveCodeBench experiment.

Offline. Assignment tests use a synthetic ``pool.json`` (the assignment never reads a problem's text or
tests); the manifest tests import a few small synthetic problems with the real importer so that fixtures,
grader bundles and expected hashes exist on disk, and load the written manifest with the real loader.
"""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from benchmarks.livecodebench import make_controls, make_manifests
from harness_workbench.core.harness import harness_from_dict, load_harness
from harness_workbench.experiments.claude_code_agent import STOP_MARGIN_SECONDS, SUPPORTED
from harness_workbench.experiments.manifest import ExperimentSpec, Split
from tests.lcb import test_lcb_import as importer_tests

SEED = 20261101
CLAUDE = "/Users/agustinmontoya/.local/share/claude/versions/2.1.316"
ENVIRONMENT = {
    "mode": "os_sandboxed",
    "srt": {"node": "/opt/homebrew/bin/node",
            "cli": "/Users/agustinmontoya/.local/share/harness-tools/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js",
            "version": "0.0.81"},
    "egress_allow": ["ai-gateway.vercel.sh:443"],
    "solver_read_paths": [CLAUDE],
    "parallelism": 6,
}

# platform -> difficulty -> count; large enough to fill every default quota
BIG_POOL = {
    "atcoder": {"easy": 70, "medium": 70, "hard": 42},
    "leetcode": {"easy": 30, "medium": 30, "hard": 18},
}


def synthetic_pool(counts):
    pool = []
    for platform, by_difficulty in sorted(counts.items()):
        for difficulty, number in sorted(by_difficulty.items()):
            for index in range(number):
                pool.append({"id": "%s_%s_%03d" % (platform[:2], difficulty[:1], index),
                             "platform": platform, "difficulty": difficulty, "mode": "stdin",
                             "expected_hashes": ["0" * 64]})
    return pool


def write_controls_report(pool_dir, kept):
    """The part of make_controls' controls_report.json that the manifest tool reads."""
    kept = sorted(kept)
    (Path(pool_dir) / "controls_report.json").write_text(
        json.dumps({"kept": kept, "kept_count": len(kept), "excluded": []}), encoding="utf-8")


class PoolTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lcb-manifests-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.pool_dir = self.root / "pool"
        self.pool_dir.mkdir()

    def write_pool(self, entries, near_duplicates=(), kept=None):
        """``kept``: ids the controls report keeps (default all); ``False`` writes no controls report."""
        (self.pool_dir / "pool.json").write_text(json.dumps(entries), encoding="utf-8")
        (self.pool_dir / "import_report.json").write_text(
            json.dumps({"revision": "a" * 40, "near_duplicates": list(near_duplicates)}), encoding="utf-8")
        if kept is not False:
            write_controls_report(self.pool_dir, [entry["id"] for entry in entries] if kept is None else kept)

    def splits(self):
        return json.loads((self.pool_dir / "splits.json").read_text(encoding="utf-8"))


class AssignmentTests(PoolTestCase):
    def test_assignment_counts_and_strata(self):
        entries = synthetic_pool(BIG_POOL)
        by_id = {entry["id"]: entry for entry in entries}
        self.write_pool(entries)
        make_manifests.assign(self.pool_dir, seed=SEED)
        splits = self.splits()
        self.assertEqual(splits["seed"], SEED)
        ids = splits["splits"]
        self.assertEqual({name: len(items) for name, items in ids.items()},
                         {"development": 25, "validation": 50, "regression": 80})
        everything = [qid for items in ids.values() for qid in items]
        self.assertEqual(len(everything), len(set(everything)), "a problem is in two splits")

        def count(split, **wanted):
            return sum(1 for qid in ids[split]
                       if all(by_id[qid][key] in values for key, values in wanted.items()))

        self.assertEqual(count("development", difficulty={"medium", "hard"}), 15)
        self.assertEqual(count("development", difficulty={"easy"}), 10)
        self.assertEqual(count("validation", difficulty={"medium", "hard"}), 50)
        self.assertEqual(count("regression", difficulty={"easy", "medium"}), 80)
        # stratified by platform: each split mirrors the 70/30 platform mix of the pool
        for split, total in (("development", 25), ("validation", 50), ("regression", 80)):
            leetcode = count(split, platform={"leetcode"})
            self.assertAlmostEqual(leetcode / total, 0.3, delta=0.1, msg=split)
            self.assertGreater(count(split, platform={"atcoder"}), 0)
        # every problem is its own family, and the file records it
        for qid in everything:
            self.assertEqual(splits["assignments"][qid]["family"], qid)
        self.assertEqual({qid for qid in splits["assignments"]}, set(everything))

    def test_assignment_is_deterministic(self):
        self.write_pool(synthetic_pool(BIG_POOL))
        make_manifests.assign(self.pool_dir, seed=SEED)
        first = (self.pool_dir / "splits.json").read_bytes()
        os.remove(self.pool_dir / "splits.json")
        make_manifests.assign(self.pool_dir, seed=SEED)
        self.assertEqual((self.pool_dir / "splits.json").read_bytes(), first)
        # the order of pool.json does not matter, and another seed gives another assignment
        self.write_pool(list(reversed(synthetic_pool(BIG_POOL))))
        make_manifests.assign(self.pool_dir, seed=SEED)
        self.assertEqual((self.pool_dir / "splits.json").read_bytes(), first)
        make_manifests.assign(self.pool_dir, seed=SEED + 1)
        self.assertNotEqual((self.pool_dir / "splits.json").read_bytes(), first)

    def test_shortfall_stops_with_counts(self):
        small = {"atcoder": {"easy": 4, "medium": 30, "hard": 30}, "leetcode": {"easy": 1, "medium": 5, "hard": 5}}
        self.write_pool(synthetic_pool(small))
        with self.assertRaises(make_manifests.ShortfallError) as caught:
            make_manifests.assign(self.pool_dir, seed=SEED)
        rows = {(row["split"], row["difficulties"]): row for row in caught.exception.shortfalls}
        development_easy = rows[("development", "easy")]
        self.assertEqual((development_easy["need"], development_easy["have"]), (10, 5))
        self.assertEqual(development_easy["by_platform"], {"atcoder": 4, "leetcode": 1})
        message = str(caught.exception)
        self.assertIn("development", message)
        self.assertIn("need 10", message)
        self.assertIn("have 5", message)
        self.assertFalse((self.pool_dir / "splits.json").exists(), "a short pool must not leave splits.json")

    def test_shortfall_exit_code_and_message_on_the_command_line(self):
        self.write_pool(synthetic_pool({"atcoder": {"easy": 3, "medium": 3, "hard": 3}}))
        done = subprocess.run(
            [sys.executable, make_manifests.__file__, "assign", "--pool", str(self.pool_dir), "--seed", str(SEED)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 2)
        self.assertIn("need", done.stderr)
        self.assertFalse((self.pool_dir / "splits.json").exists())

    def test_near_duplicate_pairs_stay_out_of_two_splits(self):
        entries = synthetic_pool(BIG_POOL)
        pairs = [
            {"a": "at_e_000", "b": "at_h_000", "jaccard": 0.9},   # easy and hard version of one problem
            {"a": "at_h_000", "b": "at_m_001", "jaccard": 0.85},  # chains into one group of three
            {"a": "le_m_000", "b": "le_m_001", "jaccard": 0.95},
        ]
        self.write_pool(entries, near_duplicates=pairs)
        make_manifests.assign(self.pool_dir, seed=SEED)
        splits = self.splits()
        placed = {qid: item["split"] for qid, item in splits["assignments"].items()}
        for group in (["at_e_000", "at_h_000", "at_m_001"], ["le_m_000", "le_m_001"]):
            in_play = [qid for qid in group if qid in placed]
            self.assertLessEqual(len(in_play), 1, "near-duplicates are not independent units: keep one")
        # the easiest of a group is the one kept, and every dropped one says what it duplicates
        dropped = {row["id"]: row for row in splits["dropped_near_duplicates"]}
        self.assertEqual(set(dropped), {"at_h_000", "at_m_001", "le_m_001"})
        for qid in ("at_h_000", "at_m_001"):
            self.assertNotIn(qid, placed)
            self.assertEqual(dropped[qid]["duplicate_of"], "at_e_000")
        self.assertNotIn("le_m_001", placed)
        self.assertEqual(dropped["le_m_001"]["duplicate_of"], "le_m_000")

    def test_assign_excludes_problems_without_verified_controls(self):
        entries = synthetic_pool(BIG_POOL)
        failed = {entry["id"] for entry in entries[:40]} | {"at_e_001", "le_m_002"}
        self.write_pool(entries, kept=[entry["id"] for entry in entries if entry["id"] not in failed])
        make_manifests.assign(self.pool_dir, seed=SEED)
        placed = set(self.splits()["assignments"])
        self.assertTrue(placed, "nothing was assigned")
        self.assertEqual(placed & failed, set(), "a problem that failed its controls was assigned")

    def test_assign_refuses_without_a_controls_report(self):
        self.write_pool(synthetic_pool(BIG_POOL), kept=False)
        with self.assertRaises(make_manifests.ManifestToolError) as caught:
            make_manifests.assign(self.pool_dir, seed=SEED)
        self.assertIn("controls_report.json", str(caught.exception))
        self.assertFalse((self.pool_dir / "splits.json").exists())
        done = subprocess.run(
            [sys.executable, make_manifests.__file__, "assign", "--pool", str(self.pool_dir), "--seed", str(SEED)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 2)
        self.assertIn("controls_report.json", done.stderr)

    def test_assign_refuses_a_malformed_controls_report(self):
        self.write_pool(synthetic_pool(BIG_POOL), kept=False)
        (self.pool_dir / "controls_report.json").write_text(json.dumps({"kept": "all"}), encoding="utf-8")
        with self.assertRaises(make_manifests.ManifestToolError):
            make_manifests.assign(self.pool_dir, seed=SEED)

    def test_shortfall_counts_only_the_kept_pool(self):
        entries = synthetic_pool(BIG_POOL)
        # 70 atcoder easy and 30 leetcode easy exist; the controls keep 6 and 3 of them
        easy_kept = {"at_e_%03d" % index for index in range(6)} | {"le_e_%03d" % index for index in range(3)}
        kept = [entry["id"] for entry in entries
                if entry["difficulty"] != "easy" or entry["id"] in easy_kept]
        self.write_pool(entries, kept=kept)
        with self.assertRaises(make_manifests.ShortfallError) as caught:
            make_manifests.assign(self.pool_dir, seed=SEED)
        rows = {(row["split"], row["difficulties"]): row for row in caught.exception.shortfalls}
        row = rows[("development", "easy")]
        self.assertEqual((row["need"], row["have"]), (10, 9))
        self.assertEqual(row["by_platform"], {"atcoder": 6, "leetcode": 3})
        self.assertFalse((self.pool_dir / "splits.json").exists())

    def test_unsafe_ids_are_refused(self):
        entries = synthetic_pool({"atcoder": {"easy": 2}})
        entries[0]["id"] = "../escape"
        self.write_pool(entries)
        with self.assertRaises(make_manifests.ManifestToolError):
            make_manifests.assign(self.pool_dir, seed=SEED)


class HarnessFileTests(unittest.TestCase):
    def test_base_harness_file_equals_the_builtin_base(self):
        from_file = load_harness(make_manifests.BASE_HARNESS_PATH)
        builtin = harness_from_dict(copy.deepcopy(make_controls.BASE_HARNESS))
        self.assertEqual(from_file.execution_config_hash(), builtin.execution_config_hash())
        self.assertEqual(from_file.id, "lcb-base")
        self.assertEqual([tool.name for tool in from_file.tools], ["Bash", "Read", "Write", "Edit"])
        for component in ("guardrails", "subagents", "skills", "scripts", "plugins"):
            self.assertEqual(getattr(from_file, component), [], component)
        # the controls builder picks the file up by default, so both must be the same harness
        loaded, source = make_controls.load_base_harness()
        self.assertEqual(source, os.path.abspath(make_manifests.BASE_HARNESS_PATH))
        self.assertEqual(loaded.execution_config_hash(), builtin.execution_config_hash())

    def test_pilot_candidate_is_the_base_plus_one_guardrail(self):
        base = load_harness(make_manifests.BASE_HARNESS_PATH)
        candidate = load_harness(make_manifests.PILOT_CANDIDATE_PATH)
        self.assertNotEqual(candidate.id, base.id)
        self.assertNotEqual(candidate.execution_config_hash(), base.execution_config_hash())
        self.assertEqual(len(candidate.guardrails), 1)
        self.assertIn("run solution.py on the examples in problem.md before finishing", candidate.guardrails[0].lower())
        same = {name: value for name, value in candidate.public_components().items() if name != "guardrails"}
        expected = {name: value for name, value in base.public_components().items() if name != "guardrails"}
        self.assertEqual(same, expected)
        self.assertEqual(candidate.plugins, [])


class PilotManifestTests(importer_tests.ImportTestCase):
    def setUp(self):
        super().setUp()
        self.manifest_dir = self.root / "manifests"
        self.manifest_dir.mkdir()
        self.manifest = self.manifest_dir / "pilot.json"

    def import_three(self):
        rows = [
            importer_tests.stdin_row("abc900_a", title="Sum Two"),
            importer_tests.functional_row("3001", title="Double Them", func_name="doubleAll"),
            importer_tests.stdin_row("abc900_c", title="Sum Again", content="Print the sum of two integers.",
                                     difficulty="hard"),
            importer_tests.stdin_row("abc900_d", title="Held Out", content="Print the difference of two integers.",
                                     difficulty="medium"),
        ]
        self.run_import(rows)

    def write_controls(self, kept=None):
        write_controls_report(self.out, [entry["id"] for entry in self.pool()] if kept is None else kept)

    def write_splits(self, development, **others):
        if not (self.out / "controls_report.json").exists():
            self.write_controls()
        splits = {"seed": SEED, "splits": {"development": development, **others},
                  "assignments": {qid: {"split": name, "family": qid}
                                  for name, items in {"development": development, **others}.items()
                                  for qid in items},
                  "dropped_near_duplicates": []}
        (self.out / "splits.json").write_text(json.dumps(splits), encoding="utf-8")

    def build(self, **overrides):
        options = dict(environment=copy.deepcopy(ENVIRONMENT), claude=CLAUDE)
        options.update(overrides)
        return make_manifests.build_pilot(self.out, self.manifest, **options)

    def test_pilot_manifest_loads_and_has_development_only(self):
        self.import_three()
        self.write_splits(["abc900_a", "3001", "abc900_c"], validation=["abc900_d"])
        self.build()
        spec = ExperimentSpec.load(self.manifest)
        self.assertEqual(spec.mode, "exploratory")
        self.assertEqual(spec.repetitions, 3)
        self.assertEqual(spec.seed, SEED)
        self.assertEqual(sorted(task.id for task in spec.tasks), ["3001", "abc900_a", "abc900_c"])
        self.assertTrue(all(task.split == Split.DEVELOPMENT for task in spec.tasks))
        self.assertEqual(len(spec.tasks_in("validation")), 0)
        self.assertEqual(len(spec.tasks_in("regression")), 0)
        for task in spec.tasks:
            self.assertEqual(task.family, task.id)
            self.assertIs(task.runs_candidate_code, True)
            self.assertEqual(task.task.domain, "competitive-programming")
        self.assertEqual([variant.id for variant in spec.variants], ["baseline", "pilot"])
        self.assertEqual(spec.variants[0].config_hash, load_harness(make_manifests.BASE_HARNESS_PATH).execution_config_hash())
        self.assertEqual(spec.variants[1].config_hash, load_harness(make_manifests.PILOT_CANDIDATE_PATH).execution_config_hash())
        self.assertTrue(spec.statistics.timeout_is_task_failure)
        self.assertEqual(spec.dataset_id, "livecodebench/code_generation_lite")
        self.assertEqual(spec.dataset_version, importer_tests.REVISION)
        self.assertEqual(spec.environment.mode, "os_sandboxed")
        self.assertEqual(spec.environment.srt.version, "0.0.81")
        self.assertEqual(spec.environment.egress_allow, ("ai-gateway.vercel.sh:443",))
        # the solver can read the Gateway credentials, and nothing else of the host's environment
        self.assertEqual(set(spec.environment.solver_env_passthrough), {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"})
        # the executor of spec section 6
        executor = spec.executor
        self.assertEqual(executor.type, "command")
        self.assertEqual(executor.timeout_seconds, 900)
        self.assertGreater(executor.timeout_seconds, STOP_MARGIN_SECONDS)
        self.assertEqual(executor.supports, frozenset(SUPPORTED))
        self.assertNotIn("plugins", executor.supports)
        self.assertEqual(executor.model, "anthropic/claude-haiku-5.5")
        argv = list(executor.argv)
        self.assertEqual(argv[:3], ["{python}", "-I", "{claude_code_agent}"])
        self.assertEqual(argv, make_controls.default_adapter_argv(
            "anthropic/claude-haiku-5.5", CLAUDE, "2.1.316", "0.50"))

    def test_task_checks_are_oracle_json_equal_with_expected_hashes(self):
        self.import_three()
        self.write_splits(["abc900_a", "3001"], validation=["abc900_c"])
        self.build()
        spec = ExperimentSpec.load(self.manifest)
        pool = {entry["id"]: entry for entry in self.pool()}
        for task in spec.tasks:
            self.assertEqual(len(task.checks), 1, task.id)
            check = task.checks[0]
            self.assertEqual(check["type"], "oracle")
            self.assertEqual(check["compare"], "json_equal")
            self.assertEqual(check["observed"], "observed.json")
            self.assertTrue(check["critical"])
            self.assertEqual(check["expected"], {"hashes": pool[task.id]["expected_hashes"]})
            # identical to the check the controls were verified with
            self.assertEqual(check, make_controls.oracle_check(pool[task.id]["expected_hashes"]))
            self.assertEqual(task.grader_files, (self.out / "graders" / task.id).resolve())
            self.assertEqual(task.fixture, (self.out / "fixtures" / task.id).resolve())
            # the solver's workspace is the fixture; the expected hashes are not in it or in the graders
            for directory in (task.fixture, task.grader_files):
                for path in directory.rglob("*"):
                    if path.is_file():
                        text = path.read_text(encoding="utf-8")
                        for digest in pool[task.id]["expected_hashes"]:
                            self.assertNotIn(digest, text, str(path))

    def test_manifest_paths_are_relative_to_the_manifest(self):
        self.import_three()
        self.write_splits(["abc900_a"])
        self.build()
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        for value in (data["baseline"], data["variants"][0]["harness"], data["tasks"][0]["fixture"],
                      data["tasks"][0]["grader_files"]):
            self.assertFalse(os.path.isabs(value), value)
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["mode"], "exploratory")

    def test_pilot_refuses_what_it_cannot_run(self):
        self.import_three()
        self.write_splits(["abc900_a"])
        with self.assertRaises(make_manifests.ManifestToolError):  # not a solver the adapter can isolate
            self.build(environment={"mode": "trusted_local"})
        with self.assertRaises(make_manifests.ManifestToolError):  # the adapter would be stopped after the executor
            self.build(timeout_seconds=STOP_MARGIN_SECONDS)
        with self.assertRaises(make_manifests.ManifestToolError):
            self.build(claude="")
        self.write_splits([])
        with self.assertRaises(make_manifests.ManifestToolError):
            self.build()
        self.write_splits(["not_in_the_pool"])
        with self.assertRaises(make_manifests.ManifestToolError):
            self.build()

    def test_pilot_refuses_a_development_problem_without_verified_controls(self):
        self.import_three()
        self.write_controls(kept=["abc900_a", "abc900_c"])
        self.write_splits(["abc900_a", "3001"])
        with self.assertRaises(make_manifests.ManifestToolError) as caught:
            self.build()
        self.assertIn("3001", str(caught.exception))
        self.assertFalse(self.manifest.exists())
        self.write_splits(["abc900_a", "abc900_c"])
        self.build()
        self.assertEqual(sorted(task.id for task in ExperimentSpec.load(self.manifest).tasks),
                         ["abc900_a", "abc900_c"])

    def test_pilot_refuses_without_a_controls_report(self):
        self.import_three()
        self.write_splits(["abc900_a"])
        os.remove(self.out / "controls_report.json")
        with self.assertRaises(make_manifests.ManifestToolError) as caught:
            self.build()
        self.assertIn("controls_report.json", str(caught.exception))
        self.assertFalse(self.manifest.exists())

    def test_executor_validation(self):
        make_manifests.validate_executor(900, SUPPORTED)
        with self.assertRaises(make_manifests.ManifestToolError):
            make_manifests.validate_executor(STOP_MARGIN_SECONDS + 0.5, SUPPORTED)
        with self.assertRaises(make_manifests.ManifestToolError):
            make_manifests.validate_executor(900, (*SUPPORTED, "plugins"))
        with self.assertRaises(make_manifests.ManifestToolError):
            make_manifests.validate_executor(900, SUPPORTED[:-1])

    def test_command_line_assign_then_pilot(self):
        self.import_three()
        env_file = self.root / "environment.json"
        env_file.write_text(json.dumps(ENVIRONMENT), encoding="utf-8")
        script = make_manifests.__file__
        # the tiny pool cannot fill the default quotas: the command line says so and writes nothing
        self.write_controls()
        done = subprocess.run([sys.executable, script, "assign", "--pool", str(self.out), "--seed", str(SEED)],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.write_splits(["abc900_a", "3001"])
        done = subprocess.run(
            [sys.executable, script, "pilot", "--pool", str(self.out), "--environment", str(env_file),
             "--out", str(self.manifest), "--claude", CLAUDE],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(task.id for task in ExperimentSpec.load(self.manifest).tasks), ["3001", "abc900_a"])

    def test_loader_rejects_a_pilot_with_a_plugin_candidate(self):
        # guard on the harness side: the adapter does not support plugins, so the pilot may not use them
        self.import_three()
        self.write_splits(["abc900_a"])
        with self.assertRaises(make_manifests.ManifestToolError):
            make_manifests.build_pilot(self.out, self.manifest, environment=copy.deepcopy(ENVIRONMENT),
                                       claude=CLAUDE, candidate=self._plugin_candidate())

    def _plugin_candidate(self):
        data = json.loads(Path(make_manifests.PILOT_CANDIDATE_PATH).read_text(encoding="utf-8"))
        data["plugins"] = [{"name": "p", "hook": "pre", "config": {}}]
        path = self.root / "plugin_candidate.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)


if __name__ == "__main__":
    unittest.main()
