"""Experiment, selection, per-task exposure, decision and promotion tables of the workbench database."""
from __future__ import annotations

import json
import multiprocessing
import queue
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from harness_workbench.core.db import PromotionConflict, WorkbenchDB
from harness_workbench.experiments.contracts import ExperimentDecision, TrialRecord
from harness_workbench.experiments.status import DecisionStatus
from harness_workbench.experiments.validation import ValidationError
from tests.unit import _holdout_worker
from tests.unit.test_contracts import make_evaluation, make_execution, make_plan, make_record
from tests.unit.test_db import make_harness

RACE_ROUNDS = 20
PROCESS_ROUNDS = 3
RESULT_TIMEOUT_SECONDS = 120
JOIN_TIMEOUT_SECONDS = 30
THREAD_TIMEOUT_SECONDS = 60


def trial(trial_id: str, **overrides: Any) -> TrialRecord:
    record = make_record(make_execution(trial_id=trial_id), make_evaluation())
    record.evaluation.trial_id = trial_id
    record.trial_id = trial_id
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


def make_decision(candidate_hash: Any = "cand-hash", **overrides: Any) -> ExperimentDecision:
    fields: dict[str, Any] = dict(
        status=DecisionStatus.ELIGIBLE,
        reasons=["ok"],
        estimate=0.2,
        lower_bound=0.05,
        n_units=12,
        plan_hash=make_plan().plan_hash,
        candidate_id="cand",
        candidate_hash=candidate_hash,
        baseline_hash="",
        baseline_id="h",
        baseline_version="1",
        evidence_ids=["e1"],
        pending_requirements=[],
        diagnostics={},
    )
    fields.update(overrides)
    return ExperimentDecision(**fields)


class ExperimentDBCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = WorkbenchDB.open(self.root)
        self.addCleanup(self.db.close)

    def start(self, experiment_id: str = "exp1", plan: Any = None) -> None:
        self.db.start_experiment(
            experiment_id,
            name="exp",
            family="fam",
            mode="confirmatory",
            schema_version=1,
            plan=plan or make_plan(),
            implementation_hash="impl",
        )

    def reserve(self, fingerprints: list[str], candidate: str, experiment_id: str) -> Any:
        return self.db.reserve_holdout(
            task_fingerprints=fingerprints,
            candidate_hash=candidate,
            baseline_hash="base",
            policy_hash="policy",
            experiment_id=experiment_id,
        )

    def exposure_rows(self) -> list[sqlite3.Row]:
        return self.db._conn.execute("SELECT * FROM holdout_exposure ORDER BY id").fetchall()


class TrialTests(ExperimentDBCase):
    def test_trials_are_persisted_individually_and_round_trip(self) -> None:
        self.start()
        first, second = trial("tr-1"), trial("tr-2", repetition=1, seed=9)
        self.db.record_trial("exp1", first)
        # Each record is committed on its own: visible to a second connection before the next one.
        other = WorkbenchDB.open(self.root)
        self.addCleanup(other.close)
        self.assertEqual([r.trial_id for r in other.trials("exp1")], ["tr-1"])
        self.db.record_trial("exp1", second)
        loaded = self.db.trials("exp1")
        self.assertEqual(loaded, [first, second])
        self.assertEqual(self.db.trials("unknown"), [])
        row = self.db._conn.execute("SELECT * FROM trials WHERE trial_id='tr-2'").fetchone()
        self.assertEqual((row["experiment_id"], row["variant_id"], row["task_id"]), ("exp1", "v1", "task-1"))
        self.assertEqual((row["split"], row["family"], row["repetition"], row["seed"], row["attempt"]),
                         ("dev", "fam", 1, 9, 1))

    def test_trial_for_unknown_experiment_is_refused(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.record_trial("missing", trial("tr-1"))

    def test_stored_plan_round_trips_and_keeps_null_budget_values_as_null(self) -> None:
        plan = make_plan(budget={"max_cost_ratio": None, "max_tokens": 1000})
        self.start(plan=plan)
        row = self.db._conn.execute("SELECT * FROM experiments WHERE experiment_id='exp1'").fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["plan_hash"], plan.plan_hash)
        self.assertEqual(row["implementation_hash"], "impl")
        self.assertIsNone(row["finished_at"])
        self.assertNotIn("Infinity", row["plan_json"])
        self.assertIsNone(json.loads(row["plan_json"])["budget"]["max_cost_ratio"])

    def test_reads_reject_non_finite_stored_documents(self) -> None:
        self.start()
        self.db.record_trial("exp1", trial("tr-1"))
        stored = self.db._conn.execute("SELECT record_json FROM trials").fetchone()["record_json"]
        poisoned = stored.replace('"score": 1.0', '"score": 1e400')
        self.assertNotEqual(stored, poisoned)
        self.db._conn.execute("UPDATE trials SET record_json=?", (poisoned,))
        with self.assertRaises(ValidationError):
            self.db.trials("exp1")

    def test_writes_refuse_non_finite_documents_and_leave_nothing_behind(self) -> None:
        self.start()
        with self.assertRaises(ValidationError):
            self.db.record_selection("exp1", self.selection(evidence={"max_cost_ratio": float("inf")}))
        self.assertIsNone(self.db.selection("exp1"))

    def selection(self, **overrides: Any) -> dict:
        data: dict[str, Any] = dict(
            finalist_id="cand",
            finalist_hash="cand-hash",
            baseline_hash="base",
            plan_hash=make_plan().plan_hash,
            policy_hash="policy",
            evidence={"max_cost_ratio": None, "scores": [0.5, 0.75]},
        )
        data.update(overrides)
        return data

    def test_selection_is_frozen_once_and_round_trips_with_null(self) -> None:
        self.start()
        self.db.record_selection("exp1", self.selection())
        stored = self.db.selection("exp1")
        self.assertEqual(stored["finalist_id"], "cand")
        self.assertEqual(stored["evidence"], {"max_cost_ratio": None, "scores": [0.5, 0.75]})
        self.assertIsNotNone(stored["frozen_at"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.record_selection("exp1", self.selection(finalist_id="other"))
        self.assertEqual(self.db.selection("exp1")["finalist_id"], "cand")

    def test_selection_for_another_plan_is_refused(self) -> None:
        self.start()
        with self.assertRaises(ValueError):
            self.db.record_selection("exp1", self.selection(plan_hash="x" * 64))
        self.assertIsNone(self.db.selection("exp1"))

    def test_status_check_constraint_rejects_unknown_status(self) -> None:
        self.start()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db._conn.execute("UPDATE experiments SET status='bogus'")
        with self.assertRaises(ValueError):
            self.db.finish_experiment("exp1", "running")
        with self.assertRaises(ValueError):
            self.db.finish_experiment("exp1", "bogus")


class HoldoutReservationTests(ExperimentDBCase):
    def setUp(self) -> None:
        super().setUp()
        self.start("exp-a")
        self.start("exp-b")

    def test_reserve_holdout_blocks_after_completed_decision_even_for_the_same_candidate(self) -> None:
        self.assertIsNone(self.reserve(["fp1", "fp2"], "cand-a", "exp-a"))
        self.db.record_decision("exp-a", make_decision("cand-a"))
        for candidate, experiment in (("cand-a", "exp-b"), ("cand-b", "exp-b")):
            reason = self.reserve(["fp1"], candidate, experiment)
            self.assertEqual(
                reason,
                "holdout task fp1 already consumed by a completed decision (eligible) in experiment exp-a",
            )
        self.assertEqual(
            [(r["experiment_id"], r["decision_status"]) for r in self.exposure_rows()],
            [("exp-a", "eligible")] * 2 + [("exp-b", None)] * 2,
        )

    def test_reserve_holdout_blocks_other_candidate_while_reserved_and_allows_same_candidate_rerun(self) -> None:
        self.assertIsNone(self.reserve(["fp1"], "cand-a-long-hash", "exp-a"))
        # An interrupted run of the same candidate may resume.
        self.assertIsNone(self.reserve(["fp1"], "cand-a-long-hash", "exp-a"))
        reason = self.reserve(["fp1"], "cand-b", "exp-b")
        self.assertEqual(
            reason, "holdout task fp1 reserved by another candidate cand-a-l (experiment exp-a)"
        )
        # The blocked attempt is visible but never locks out the holder of the reservation.
        self.assertEqual(len(self.exposure_rows()), 3)
        self.assertEqual(self.exposure_rows()[-1]["experiment_id"], "exp-b")
        self.assertIsNone(self.reserve(["fp1"], "cand-a-long-hash", "exp-a"))

    def attempt(self, experiment_id: str, *, baseline: str = "base", policy: str = "policy",
                candidate: str = "cand-a") -> Any:
        return self.db.reserve_holdout_attempt(
            task_fingerprints=["f1", "f2"], candidate_hash=candidate, baseline_hash=baseline,
            policy_hash=policy, experiment_id=experiment_id,
        )

    def test_rerun_with_a_different_policy_is_blocked(self) -> None:
        self.start("exp-c")
        self.assertIsNone(self.attempt("exp-a").blocked_reason)
        outcome = self.attempt("exp-c", policy="lax")
        self.assertIn("policy", outcome.blocked_reason)
        self.assertIn("exp-a", outcome.blocked_reason)
        self.assertEqual(outcome.earlier_experiment_ids, ())

    def test_rerun_with_a_different_baseline_is_blocked(self) -> None:
        self.start("exp-c")
        self.assertIsNone(self.attempt("exp-a").blocked_reason)
        outcome = self.attempt("exp-c", baseline="other-base")
        self.assertIn("baseline", outcome.blocked_reason)
        self.assertEqual(outcome.earlier_experiment_ids, ())

    def test_rerun_with_a_different_plan_hash_is_blocked(self) -> None:
        other_plan = make_plan(seed=12)
        self.assertNotEqual(other_plan.plan_hash, make_plan().plan_hash)
        self.start("exp-c", plan=other_plan)
        self.assertIsNone(self.attempt("exp-a").blocked_reason)
        outcome = self.attempt("exp-c")
        self.assertIn("plan", outcome.blocked_reason)
        self.assertEqual(outcome.earlier_experiment_ids, ())
        # The refused attempt stays visible but never counts as a reservation.
        self.assertEqual(sum(r["blocked_reason"] is not None for r in self.exposure_rows()), 2)
        self.assertIsNone(self.attempt("exp-a").blocked_reason)

    def test_identical_rerun_after_an_interrupted_attempt_is_allowed_and_surfaces_earlier_experiments(self) -> None:
        self.start("exp-c")
        self.start("exp-d", plan=make_plan())
        first = self.attempt("exp-a")
        self.assertEqual((first.blocked_reason, first.earlier_experiment_ids), (None, ()))
        second = self.attempt("exp-b")
        self.assertEqual((second.blocked_reason, second.earlier_experiment_ids), (None, ("exp-a",)))
        third = self.attempt("exp-c")
        self.assertEqual((third.blocked_reason, third.earlier_experiment_ids), (None, ("exp-a", "exp-b")))
        # The same experiment id is not its own earlier attempt, and a refused attempt is not listed.
        again = self.attempt("exp-c")
        self.assertEqual(again.earlier_experiment_ids, ("exp-a", "exp-b"))
        self.assertIsNotNone(self.attempt("exp-d", candidate="cand-z").blocked_reason)
        self.assertEqual(self.attempt("exp-d").earlier_experiment_ids, ("exp-a", "exp-b", "exp-c"))

    def test_reserve_holdout_keeps_returning_the_reason_or_none(self) -> None:
        self.assertIsNone(self.reserve(["f1", "f2"], "cand-a", "exp-a"))
        self.assertIsNone(self.reserve(["f1", "f2"], "cand-a", "exp-b"))
        self.start("exp-c")
        self.assertIsInstance(self.db.reserve_holdout(
            task_fingerprints=["f1"], candidate_hash="cand-a", baseline_hash="x", policy_hash="policy",
            experiment_id="exp-c"), str)

    def test_long_fingerprints_are_shortened_to_eight_characters_in_the_reason(self) -> None:
        self.assertIsNone(self.reserve(["0123456789abcdef"], "fedcba9876543210", "exp-a"))
        self.assertEqual(
            self.reserve(["0123456789abcdef"], "other", "exp-b"),
            "holdout task 01234567 reserved by another candidate fedcba98 (experiment exp-a)",
        )

    def test_partial_overlap_of_task_content_is_detected(self) -> None:
        self.assertIsNone(self.reserve(["a1", "a2", "a3", "a4", "a5"], "cand-a", "exp-a"))
        reason = self.reserve(["b1", "b2", "a3", "b4", "b5"], "cand-b", "exp-b")
        self.assertIn("holdout task a3 reserved by another candidate", reason)
        self.assertEqual(len(self.exposure_rows()), 10)

    def test_reservation_records_every_fingerprint_once_and_empty_input_is_free(self) -> None:
        self.assertIsNone(self.reserve(["x", "y", "x"], "cand-a", "exp-a"))
        self.assertEqual([r["task_fingerprint"] for r in self.exposure_rows()], ["x", "y"])
        row = self.exposure_rows()[0]
        self.assertEqual((row["candidate_hash"], row["baseline_hash"], row["policy_hash"]),
                         ("cand-a", "base", "policy"))
        self.assertIsNotNone(row["reserved_at"])
        self.assertIsNone(self.reserve([], "cand-b", "exp-b"))

    def test_decision_marks_only_this_experiments_reservations(self) -> None:
        self.assertIsNone(self.reserve(["fp1"], "cand-a", "exp-a"))
        self.assertIsNotNone(self.reserve(["fp1"], "cand-b", "exp-b"))
        self.db.record_decision("exp-b", make_decision("cand-b", status=DecisionStatus.INVALID_EXPERIMENT))
        statuses = {r["experiment_id"]: r["decision_status"] for r in self.exposure_rows()}
        # exp-b never ran the holdout (blocked), so its decision must not consume the task for exp-a.
        self.assertEqual(statuses, {"exp-a": None, "exp-b": None})
        self.assertIsNone(self.reserve(["fp1"], "cand-a", "exp-a"))

    def test_reserve_holdout_is_atomic_under_two_connections(self) -> None:
        paths = self.root / "library" / "workbench.sqlite3"
        for round_index in range(RACE_ROUNDS):
            fingerprints = [f"r{round_index}-{n}" for n in range(5)]
            barrier = threading.Barrier(2, timeout=THREAD_TIMEOUT_SECONDS)
            outcomes: dict[str, Any] = {}

            def contend(candidate: str, experiment: str) -> None:
                db = None
                try:
                    db = WorkbenchDB(paths)
                    barrier.wait()
                    outcomes[candidate] = (db.reserve_holdout(
                        task_fingerprints=fingerprints, candidate_hash=candidate, baseline_hash="base",
                        policy_hash="policy", experiment_id=experiment), None)
                except BaseException as exc:
                    outcomes[candidate] = (None, f"{type(exc).__name__}: {exc}")
                finally:
                    if db is not None:
                        db.close()

            threads = [threading.Thread(target=contend, args=(c, e)) for c, e in
                       (("cand-a", "exp-a"), ("cand-b", "exp-b"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(THREAD_TIMEOUT_SECONDS)
                self.assertFalse(thread.is_alive())
            self.assertEqual([err for _, err in outcomes.values()], [None, None], outcomes)
            reasons = [reason for reason, _ in outcomes.values()]
            self.assertEqual(sum(reason is None for reason in reasons), 1, outcomes)
            self.assertIn("reserved by another candidate", next(r for r in reasons if r))
            rows = self.db._conn.execute(
                "SELECT candidate_hash FROM holdout_exposure WHERE task_fingerprint=? ORDER BY id",
                (fingerprints[0],),
            ).fetchall()
            self.assertEqual(sorted(r["candidate_hash"] for r in rows), ["cand-a", "cand-b"])

    def test_reserve_holdout_is_atomic_across_spawned_processes(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        db_path = str(self.root / "library" / "workbench.sqlite3")
        for round_index in range(PROCESS_ROUNDS):
            fingerprints = [f"p{round_index}-{n}" for n in range(5)]
            self.start(f"exp-cand-a{round_index}")
            self.start(f"exp-cand-b{round_index}")
            barrier = ctx.Barrier(2)
            results = ctx.Queue()
            processes = [
                ctx.Process(target=_holdout_worker.reserve_worker,
                            args=(db_path, fingerprints, f"cand-{name}{round_index}", barrier, results))
                for name in ("a", "b")
            ]
            for process in processes:
                process.start()
            rows: list[dict] = []
            try:
                for _ in processes:
                    try:
                        rows.append(results.get(timeout=RESULT_TIMEOUT_SECONDS))
                    except queue.Empty:
                        break
            finally:
                for process in processes:
                    process.join(JOIN_TIMEOUT_SECONDS)
                    if process.is_alive():
                        process.terminate()
                        process.join(JOIN_TIMEOUT_SECONDS)
            self.assertEqual(len(rows), 2, rows)
            self.assertEqual([row["error"] for row in rows], [None, None], rows)
            self.assertEqual(sum(row["reason"] is None for row in rows), 1, rows)


class DecisionInvariantTests(unittest.TestCase):
    def test_eligible_decision_requires_a_candidate_hash(self) -> None:
        for missing in (None, ""):
            with self.assertRaises(ValidationError):
                make_decision(candidate_hash=missing)

    def test_eligible_decision_requires_evidence(self) -> None:
        with self.assertRaises(ValidationError):
            make_decision(evidence_ids=[])

    def test_from_dict_cannot_smuggle_an_unsupported_eligible_decision(self) -> None:
        data = make_decision().to_dict()
        for key, value in (("candidate_hash", None), ("evidence_ids", [])):
            with self.assertRaises(ValidationError):
                ExperimentDecision.from_dict({**data, key: value})

    def test_other_statuses_may_lack_candidate_and_evidence(self) -> None:
        for status in DecisionStatus:
            if status is DecisionStatus.ELIGIBLE:
                continue
            decision = make_decision(candidate_hash=None, evidence_ids=[], status=status)
            self.assertEqual(decision.status, status)


class DecisionAndPromotionTests(ExperimentDBCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = make_harness("1")
        self.db.register_base(self.base)
        self.candidate = make_harness("1", prompt="candidate prompt")
        self.start()

    def decide(self, **overrides: Any) -> str:
        fields: dict[str, Any] = dict(
            candidate_hash=self.candidate.execution_config_hash(),
            baseline_hash=self.base.execution_config_hash(),
        )
        fields.update(overrides)
        return self.db.record_decision("exp1", make_decision(**fields))

    def test_decision_round_trips_and_is_unique_per_experiment(self) -> None:
        decision_id = self.decide()
        self.assertEqual(len(decision_id), 32)
        loaded = self.db.decision(decision_id)
        self.assertEqual(loaded.status, DecisionStatus.ELIGIBLE)
        self.assertEqual(loaded.candidate_hash, self.candidate.execution_config_hash())
        self.assertIsNone(self.db.decision("0" * 32))
        with self.assertRaises(sqlite3.IntegrityError):
            self.decide()
        self.assertEqual(self.db._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)

    def test_decision_for_another_plan_or_with_non_finite_content_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.decide(plan_hash="x" * 64)
        with self.assertRaises(ValidationError):
            self.decide(diagnostics={"ratio": float("inf")})
        self.assertEqual(self.db._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_decision_revalidates_invariants_of_a_mutated_object(self) -> None:
        decision = make_decision(candidate_hash=self.candidate.execution_config_hash())
        decision.evidence_ids = []
        with self.assertRaises(ValidationError):
            self.db.record_decision("exp1", decision)

    def test_stored_decision_with_non_finite_number_is_rejected_on_read(self) -> None:
        decision_id = self.decide()
        stored = self.db._conn.execute("SELECT decision_json FROM decisions").fetchone()["decision_json"]
        poisoned = stored.replace('"estimate": 0.2', '"estimate": 1e400')
        self.assertNotEqual(stored, poisoned)
        self.db._conn.execute("UPDATE decisions SET decision_json=?", (poisoned,))
        with self.assertRaises(ValidationError):
            self.db.decision(decision_id)
        with self.assertRaises(PromotionConflict):
            self.db.promote(decision_id, self.candidate, source="experiment")

    def test_promote_is_idempotent_for_the_same_decision(self) -> None:
        decision_id = self.decide()
        version, created = self.db.promote(decision_id, self.candidate, source="experiment")
        self.assertEqual((version, created), ("2", True))
        self.assertEqual(self.db.promote(decision_id, self.candidate, source="experiment"), ("2", False))
        self.assertEqual([h.version for h in self.db.versions("h")], ["1", "2"])
        child = self.db.get("h", "2")
        self.assertEqual(child.lineage.parent_version, "1")
        self.assertEqual(child.execution_config_hash(), self.candidate.execution_config_hash())
        rows = self.db._conn.execute("SELECT * FROM promotions").fetchall()
        self.assertEqual([(r["decision_id"], r["harness_id"], r["version"]) for r in rows],
                         [(decision_id, "h", "2")])

    def test_promote_is_idempotent_across_connections(self) -> None:
        decision_id = self.decide()
        other = WorkbenchDB.open(self.root)
        self.addCleanup(other.close)
        self.assertEqual(self.db.promote(decision_id, self.candidate, source="experiment"), ("2", True))
        self.assertEqual(other.promote(decision_id, self.candidate, source="experiment"), ("2", False))

    def test_promote_rejects_substituted_candidate_non_eligible_decision_and_unknown_parent(self) -> None:
        decision_id = self.decide()
        substituted = make_harness("1", prompt="a different prompt")
        with self.assertRaises(PromotionConflict):
            self.db.promote(decision_id, substituted, source="experiment")
        with self.assertRaises(PromotionConflict):
            self.db.promote("0" * 32, self.candidate, source="experiment")
        other_id = make_harness("1", prompt="candidate prompt", hid="other")
        with self.assertRaises(PromotionConflict):
            self.db.promote(decision_id, other_id, source="experiment")
        self.assertEqual([h.version for h in self.db.versions("h")], ["1"])

        # Not eligible: the same candidate content is refused.
        self.start("exp2")
        rejected = self.db.record_decision("exp2", make_decision(
            status=DecisionStatus.REJECTED, candidate_hash=self.candidate.execution_config_hash(),
            evidence_ids=["e"], baseline_hash=self.base.execution_config_hash(),
            plan_hash=make_plan().plan_hash))
        with self.assertRaises(PromotionConflict):
            self.db.promote(rejected, self.candidate, source="experiment")

        # Unknown parent version, and a baseline whose content changed.
        self.start("exp3")
        orphan = self.db.record_decision("exp3", make_decision(
            candidate_hash=self.candidate.execution_config_hash(),
            baseline_hash=self.base.execution_config_hash(), baseline_version="99"))
        with self.assertRaises(PromotionConflict):
            self.db.promote(orphan, self.candidate, source="experiment")
        self.start("exp4")
        mismatched = self.db.record_decision("exp4", make_decision(
            candidate_hash=self.candidate.execution_config_hash(), baseline_hash="f" * 64))
        with self.assertRaises(PromotionConflict):
            self.db.promote(mismatched, self.candidate, source="experiment")
        self.assertEqual([h.version for h in self.db.versions("h")], ["1"])
        self.assertEqual(self.db._conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0], 0)

    def test_stored_eligible_decision_without_evidence_is_not_promotable(self) -> None:
        decision_id = self.decide()
        stored = json.loads(self.db._conn.execute("SELECT decision_json FROM decisions").fetchone()[0])
        stored["evidence_ids"] = []
        self.db._conn.execute("UPDATE decisions SET decision_json=?", (json.dumps(stored),))
        with self.assertRaises(PromotionConflict):
            self.db.promote(decision_id, self.candidate, source="experiment")
        self.assertEqual([h.version for h in self.db.versions("h")], ["1"])


class FinishAndIncompleteTests(ExperimentDBCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = iter(f"2026-10-23T10:00:{n:02d}.000000+00:00" for n in range(1, 60))
        self.db._now = lambda: next(self.clock)

    def test_finish_states_and_incomplete_listing(self) -> None:
        self.start("no-trials")
        self.start("interrupted")
        self.db.record_trial("interrupted", trial("tr-1"))
        self.start("done")
        self.db.record_trial("done", trial("tr-2"))
        self.db.finish_experiment("done", "complete")
        self.start("bad")
        self.db.finish_experiment("bad", "invalid")
        self.start("cut")
        self.db.finish_experiment("cut", "incomplete")
        self.assertEqual(self.db.incomplete_experiments(), ["no-trials", "interrupted"])
        statuses = {r["experiment_id"]: (r["status"], r["finished_at"] is not None)
                    for r in self.db._conn.execute("SELECT * FROM experiments")}
        self.assertEqual(statuses["done"], ("complete", True))
        self.assertEqual(statuses["bad"], ("invalid", True))
        self.assertEqual(statuses["cut"], ("incomplete", True))
        self.assertEqual(statuses["interrupted"], ("running", False))
        with self.assertRaises(LookupError):
            self.db.finish_experiment("missing", "complete")

    def test_running_experiment_with_trials_no_newer_than_its_start_is_not_listed(self) -> None:
        self.start("fresh")
        self.db._conn.execute("UPDATE experiments SET created_at='2999-01-01T00:00:00.000000+00:00'")
        self.db.record_trial("fresh", trial("tr-1"))
        self.assertEqual(self.db.incomplete_experiments(), [])


class OldDatabaseTests(unittest.TestCase):
    def test_t12_era_database_gains_the_experiment_tables_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "library" / "workbench.sqlite3"
            path.parent.mkdir(parents=True)
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO schema_meta VALUES('schema_version','1');
                CREATE TABLE harness_versions (
                    harness_id TEXT, version TEXT, execution_config_hash TEXT NOT NULL,
                    parent_version TEXT, source TEXT, created_at TEXT, document TEXT NOT NULL,
                    PRIMARY KEY (harness_id, version));
                """
            )
            conn.commit()
            conn.close()
            db = WorkbenchDB(path)
            try:
                tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"experiments", "trials", "selections", "holdout_exposure", "decisions",
                                 "promotions"} <= tables)
                index = db._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND tbl_name='holdout_exposure'"
                    " AND sql LIKE '%task_fingerprint%'").fetchone()
                self.assertIsNotNone(index)
                db.register_base(make_harness())
            finally:
                db.close()
            again = WorkbenchDB(path)  # reopening is idempotent
            again.close()


if __name__ == "__main__":
    unittest.main()
