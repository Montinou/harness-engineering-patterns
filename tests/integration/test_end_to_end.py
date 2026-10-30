"""End-to-end flows of the experiment runner and closure checks for the reproduction suite.

The runs use :class:`tests.support.ScriptedExecutor` (an in-process adapter that reports its tokens) on
``LocalBackend`` with the published ``binary_v1`` profile floors for the validation and regression units, so
they need no Docker, no model and no subprocess per trial. The agent is a script and the tasks are synthetic
template variants: a result here says the pipeline works, not that any harness improves a model.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Iterator

from harness_workbench.experiments import ExperimentSpec, run_experiment
from harness_workbench.experiments.backends import LocalBackend
from harness_workbench.experiments.profiles import PROFILES
from harness_workbench.experiments.status import DecisionStatus
from tests.support import ScriptedExecutor, exact_task, write_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPRODUCTIONS = REPO_ROOT / "tests" / "adversarial" / "test_audit_reproductions.py"
PROFILE = PROFILES["binary_v1"]
CONTROLS = {"positive": {"outputs": {"answer": "CORRECT"}}, "negatives": [{"outputs": {"answer": "INCORRECT"}}]}
REPETITIONS = 5
DEVELOPMENT_TASKS = 6
SMALL_VALIDATION = 3
SMALL_REGRESSION = 1

# Closure floor for ``test_suite_counts``. The 36 tests that existed before the experimental-evaluation work
# (``tests/test_experiments.py``, ``tests/test_workbench.py``, ``tests/test_anthropic.py``) must all still be
# discovered. The suite held 619 tests at the CI workflow commit (T29), so 583 were added by this work before
# this module; this module adds the five below. The floor only ever moves up: a deleted test fails it.
PRE_EXISTING_TESTS = 36
ADDED_BEFORE_THIS_MODULE = 619 - PRE_EXISTING_TESTS
TESTS_IN_THIS_MODULE = 5
PRE_EXISTING_MODULES = ("test_experiments", "test_workbench", "test_anthropic")


def _holdout(task_id: str, split: str, content: str) -> dict[str, Any]:
    """A holdout task; the fingerprint follows ``content``, not the id."""
    return exact_task(task_id, f"holdout case number {content}", split=split,
                      runs_candidate_code=False, controls=CONTROLS)


def _tasks(validation: int, regression: int, *, prefix: str = "", content: str = "") -> list[dict[str, Any]]:
    tasks = [exact_task(f"d{i}", split="development", runs_candidate_code=False)
             for i in range(1, DEVELOPMENT_TASKS + 1)]
    tasks += [_holdout(f"{prefix}v{i}", "validation", f"{content}v{i}") for i in range(1, validation + 1)]
    tasks += [_holdout(f"{prefix}r{i}", "regression", f"{content}r{i}") for i in range(1, regression + 1)]
    return tasks


def _confirmatory(root: Path, candidates: tuple[str, ...], tasks: list[dict[str, Any]]) -> ExperimentSpec:
    path = write_manifest(
        root, tasks=tasks, variants=[{"id": name, "system_prompt": name} for name in candidates],
        schema_version=2, mode="confirmatory", family="end-to-end", dataset={"id": "end-to-end", "version": "1"},
        repetitions=REPETITIONS, environment={"mode": "trusted_local", "trusted_solver_acknowledged": True})
    return ExperimentSpec.load(path)


def _always(_task_id: str) -> bool:
    return True


def _never(_task_id: str) -> bool:
    return False


class EndToEndCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.library = self.root / "library-root"

    def run_spec(self, spec: ExperimentSpec, answers: dict[str, Callable[[str], bool]]) -> dict[str, Any]:
        executor = ScriptedExecutor({"base": _never, **answers})
        self.assertEqual(executor.usage_provenance, "adapter_reported")
        return run_experiment(spec, executor, self.library, backend=LocalBackend())

    def query(self, sql: str, *args: Any) -> list[tuple]:
        conn = sqlite3.connect(str(self.library / "library" / "workbench.sqlite3"))
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


class LegacyAndConfirmatoryFlows(EndToEndCase):
    def test_legacy_v1_manifest_still_runs_and_is_exploratory_only(self) -> None:
        manifest = write_manifest(
            self.root / "legacy", tasks=[exact_task("t1"), exact_task("t2")],
            variants=[{"id": "winner", "system_prompt": "winner"}, {"id": "loser", "system_prompt": "loser"}],
            repetitions=2)
        spec = ExperimentSpec.load(manifest)
        self.assertEqual(spec.mode, "exploratory")
        payload = self.run_spec(spec, {"winner": _always, "loser": _never})
        # Every variant ran on every task, so nothing was held out, and no verdict can promote it.
        self.assertEqual({run["variant_id"] for run in payload["runs"]}, {"baseline", "winner", "loser"})
        self.assertEqual(len(payload["runs"]), 3 * 2 * 2)
        self.assertEqual(payload["mode"], "exploratory")
        self.assertEqual(payload["manifest_schema_version"], 1)
        self.assertEqual(payload["decision"]["status"], DecisionStatus.EXPLORATORY_ONLY.value)
        self.assertEqual(payload["summary"]["recommended"], [])
        self.assertIsNone(payload["selection"])
        self.assertEqual(payload["exposure"], {"fingerprints": [], "block_reason": None})
        self.assertEqual(self.query("SELECT status FROM experiments WHERE experiment_id=?",
                                    payload["experiment_id"]), [("complete",)])
        self.assertEqual(self.query("SELECT status FROM decisions WHERE experiment_id=?",
                                    payload["experiment_id"]), [(DecisionStatus.EXPLORATORY_ONLY.value,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM selections"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM holdout_exposure"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM promotions"), [(0,)])

    def test_confirmatory_run_with_scripted_executor_is_eligible_and_persisted(self) -> None:
        tasks = _tasks(PROFILE.min_independent_units, PROFILE.min_regression_units)
        spec = _confirmatory(self.root / "m", ("candidate",), tasks)
        self.assertEqual(spec.validate_confirmatory(), [])
        # The baseline fails every validation task and holds every regression task; the candidate passes two of
        # three validation tasks (a degenerate zero-variance effect would be refused) and holds the regression.
        payload = self.run_spec(spec, {
            "base": lambda task_id: task_id.startswith("r"),
            "candidate": lambda task_id: task_id.startswith("r") or int(task_id[1:]) % 3 != 0})
        decision = payload["decision"]
        self.assertEqual(decision["status"], DecisionStatus.ELIGIBLE.value, decision["reasons"])
        self.assertEqual(decision["n_units"], PROFILE.min_independent_units)
        self.assertEqual(decision["diagnostics"]["n_regression_units"], PROFILE.min_regression_units)
        self.assertEqual(decision["candidate_id"], "candidate")
        self.assertEqual(payload["summary"]["recommended"], ["candidate"])
        experiment_id = payload["experiment_id"]
        holdout = PROFILE.min_independent_units + PROFILE.min_regression_units
        # Experiment complete, one frozen selection, one exposure row per holdout task, one eligible decision.
        self.assertEqual(self.query("SELECT status, mode FROM experiments WHERE experiment_id=?", experiment_id),
                         [("complete", "confirmatory")])
        self.assertEqual(self.query("SELECT finalist_id FROM selections WHERE experiment_id=?", experiment_id),
                         [("candidate",)])
        self.assertEqual(self.query(
            "SELECT decision_status, blocked_reason, COUNT(*) FROM holdout_exposure WHERE experiment_id=?"
            " GROUP BY decision_status, blocked_reason", experiment_id),
            [(DecisionStatus.ELIGIBLE.value, None, holdout)])
        self.assertEqual(self.query("SELECT status, decision_id FROM decisions WHERE experiment_id=?", experiment_id),
                         [(DecisionStatus.ELIGIBLE.value, payload["decision_id"])])
        # The finalist and the baseline both ran every holdout task on every repetition. (A single candidate is
        # the finalist by construction, so there are no development trials to select from.)
        self.assertEqual(
            self.query("SELECT split, variant_id, COUNT(*) FROM trials WHERE experiment_id=?"
                       " GROUP BY split, variant_id ORDER BY split, variant_id", experiment_id),
            [("regression", "baseline", PROFILE.min_regression_units * REPETITIONS),
             ("regression", "candidate", PROFILE.min_regression_units * REPETITIONS),
             ("validation", "baseline", PROFILE.min_independent_units * REPETITIONS),
             ("validation", "candidate", PROFILE.min_independent_units * REPETITIONS)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM promotions"), [(0,)], "promotion is a separate step")
        self.assertTrue(Path(payload["results_path"]).is_file())


class HoldoutConsumption(EndToEndCase):
    def exposure_rows(self, experiment_id: str) -> list[tuple]:
        return self.query("SELECT decision_status, blocked_reason IS NOT NULL, COUNT(*) FROM holdout_exposure"
                          " WHERE experiment_id=? GROUP BY decision_status, blocked_reason IS NOT NULL",
                          experiment_id)

    def test_second_candidate_on_consumed_holdout_is_blocked_and_genuinely_new_holdout_content_is_allowed(
            self) -> None:
        holdout = SMALL_VALIDATION + SMALL_REGRESSION
        first = self.run_spec(
            _confirmatory(self.root / "first", ("a",), _tasks(SMALL_VALIDATION, SMALL_REGRESSION)), {"a": _always})
        self.assertIsNone(first["exposure"]["block_reason"], "precondition: the first candidate reserves the holdout")
        self.assertEqual(first["decision"]["status"], DecisionStatus.INSUFFICIENT_EVIDENCE.value)
        self.assertEqual(self.exposure_rows(first["experiment_id"]),
                         [(DecisionStatus.INSUFFICIENT_EVIDENCE.value, 0, holdout)])

        # Another candidate on the same holdout content under new task ids: the registry follows the content.
        renamed = _tasks(SMALL_VALIDATION, SMALL_REGRESSION, prefix="renamed-")
        renamed_spec = _confirmatory(self.root / "second", ("b",), renamed)
        self.assertTrue({"v1", "r1"}.isdisjoint({task.id for task in renamed_spec.tasks}))
        second = self.run_spec(renamed_spec, {"b": _always})
        self.assertEqual(second["decision"]["status"], DecisionStatus.EXPLORATORY_ONLY.value)
        reason = second["exposure"]["block_reason"]
        self.assertIsNotNone(reason)
        self.assertIn(reason, second["decision"]["reasons"])
        self.assertEqual(sorted(second["exposure"]["fingerprints"]), sorted(first["exposure"]["fingerprints"]))
        # A refused attempt stays visible with its reason but never takes a decision status or a reservation.
        self.assertEqual(self.exposure_rows(second["experiment_id"]), [(None, 1, holdout)])

        # Genuinely new holdout content has new fingerprints, so a third candidate is not blocked by the others.
        fresh_tasks = _tasks(SMALL_VALIDATION, SMALL_REGRESSION, prefix="fresh-", content="fresh-")
        fresh_spec = _confirmatory(self.root / "third", ("c",), fresh_tasks)
        third = self.run_spec(fresh_spec, {"c": _always})
        self.assertIsNone(third["exposure"]["block_reason"])
        self.assertNotEqual(third["decision"]["status"], DecisionStatus.EXPLORATORY_ONLY.value)
        self.assertTrue(set(third["exposure"]["fingerprints"]).isdisjoint(first["exposure"]["fingerprints"]))
        self.assertEqual(self.exposure_rows(third["experiment_id"]),
                         [(DecisionStatus.INSUFFICIENT_EVIDENCE.value, 0, holdout)])
        # The first candidate's reservation was not disturbed by either later attempt.
        self.assertEqual(self.exposure_rows(first["experiment_id"]),
                         [(DecisionStatus.INSUFFICIENT_EVIDENCE.value, 0, holdout)])


def _flatten(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _discover() -> list[unittest.TestCase]:
    """The tests ``python -m unittest discover -s tests`` finds, by loader discovery only (nothing is run)."""
    saved = list(sys.path)
    try:
        suite = unittest.TestLoader().discover(str(REPO_ROOT / "tests"))
    finally:
        sys.path[:] = saved
    return list(_flatten(suite))


class ReproductionClosure(unittest.TestCase):
    def test_no_repro_decorators_remain(self) -> None:
        source = REPRODUCTIONS.read_text(encoding="utf-8")
        self.assertEqual(source.count("@repro"), 0,
                         "a reproduction is still an expected failure: fix the defect or report it, do not hide it")

    def test_suite_counts(self) -> None:
        discovered = _discover()
        failed = [test.id() for test in discovered if type(test).__name__ == "_FailedTest"]
        self.assertEqual(failed, [], "test modules that failed to import")
        pre_existing = [test for test in discovered
                        if type(test).__module__.rsplit(".", 1)[-1] in PRE_EXISTING_MODULES]
        self.assertGreaterEqual(len(pre_existing), PRE_EXISTING_TESTS,
                                f"the {PRE_EXISTING_TESTS} tests that existed before this work were deleted")
        floor = PRE_EXISTING_TESTS + ADDED_BEFORE_THIS_MODULE + TESTS_IN_THIS_MODULE
        self.assertGreaterEqual(
            len(discovered), floor,
            f"discovery found {len(discovered)} tests, expected at least {floor} "
            f"({PRE_EXISTING_TESTS} pre-existing + {ADDED_BEFORE_THIS_MODULE} added by T1-T31 "
            f"+ {TESTS_IN_THIS_MODULE} here); a test was deleted or its module no longer imports")


if __name__ == "__main__":
    unittest.main()
