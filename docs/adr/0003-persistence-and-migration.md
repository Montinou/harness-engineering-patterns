# ADR 0003: Persistence, lineage and migration

- Status: Accepted
- Date: 2026-10-24
- Spec: FIX-05 (section 7). Audit finding: A7, and the persistence part of A12.

## Context

The audited store reserved version numbers with exclusive file creation and, when two processes branched from the
same base, renumbered the base and remapped descendants. Concurrent runs produced phantom bases, and the shared
index and Markdown files were written without any transaction. Nothing tied a promotion to the candidate that had
actually been evaluated, and a crash during an experiment left no inspectable partial evidence.

## Decision

1. **SQLite is the source of truth.** `library/workbench.sqlite3` (standard library `sqlite3`, WAL, schema version
   checked on open, a newer schema is refused) holds versions, experiments, trials, selections, holdout exposure,
   decisions and promotions. Writes use short `BEGIN IMMEDIATE` transactions; subprocesses and network calls never
   hold a write lock. A busy database raises `StoreBusyError` after a bounded timeout instead of looping.
2. **JSON and Markdown are exported views.** `library/harnesses/<id>/<version>.json`, `library/index.json`, run
   directories, `EXPERIENCE.md` and lessons are written after the commit, each file by write-to-temporary and
   `os.replace`. They are not a transaction over all files. If an export fails after the commit, `ExportError`
   names the committed state and `harness-workbench export-library --root <dir>` regenerates every view from the
   database without creating versions.
3. **Execution hash versus descriptor.** `Harness.execution_config_hash()` covers only the eight executable
   components; lineage, notes and version are administrative and are stored apart. Identity is
   `(harness_id, version)`.
4. **No base renumbering.** `register_base` records the base once. The same identity with the same execution hash is
   reused; the same identity with different content raises `LineageConflict`. Children are allocated by
   `allocate_child` inside a transaction as the next decimal version after the highest existing one, with the
   parent required to be registered and the child's id required to match the parent's. All legitimate children
   reference the one canonical base.
5. **`MigrationRequired`.** A library that has `library/harnesses/**/*.json` versions with no database row is
   refused before anything is written, with the message "run harness-workbench migrate-library --root <root>".
   `migrate-library` is explicit and idempotent: `--dry-run` writes nothing, `<name>.bak-<UTC stamp>` backups of
   `index.json` and the database are made before the first write, rows already present with the same hash are
   skipped, content that differs is reported as a conflict and left alone, malformed or non-finite files are never
   imported, and all inserts go through one transaction. Legacy files are only read, never rewritten or deleted.
6. **Promotion by decision id.** `promote(decision_id, harness)` runs in one transaction and requires an existing
   `eligible` decision whose `candidate_hash` equals the execution hash of the harness being promoted, whose
   baseline id matches, and whose parent `(baseline_id, baseline_version)` is registered with the baseline hash the
   decision was made against. The parent is the decision's `(baseline_id, baseline_version)`, not whatever is
   current. A unique `promotions.decision_id` makes it idempotent: repeating returns the existing version and
   creates no second child. A decision that is missing, not eligible, for another candidate or for an unknown
   parent raises `PromotionConflict`. There is no `promote(arbitrary_harness)`; `persist_result` with a
   `decision_id` promotes exactly one candidate.
7. **Evidence as it happens.** Each trial is written in its own short transaction as it completes, the selection is
   frozen once per experiment, and holdout reservation is one atomic transaction per experiment. An interrupted
   experiment stays `running` with its trials and no decision, and `harness-workbench experiment --root <dir>
   --list-incomplete` lists it. There is no automatic resume.
8. **Sequential Improve under a gate.** In `sequential` mode the intermediate adoptions stay in the result's steps
   as evidence. When a promotion gate is configured (`--validation-spec`), the gate runs once on the final composed
   harness; if it approves, only the base and the final harness are persisted, the final as a direct child of the
   base; if it rejects, raises an ordinary exception or returns something that is not a verdict, only the base is
   persisted (the gate's `VALIDATION REJECT` or `VALIDATION ERROR` lesson is recorded). An infrastructure failure
   (`OSError`, `StoreBusyError`, `BackendUnavailable`) is not a rejection of the candidate: it is re-raised,
   `persist_result` never runs and nothing is persisted, not even the base, so the caller reports the failure.
   Without a gate every adopted step is still persisted, as before.

## Consequences

- Six and twelve concurrent processes starting from the same v1 yield a single canonical v1 and distinct children
  of it, with no renumbered bases.
- Retrying a promotion never duplicates a child or an approval event.
- A stored decision cannot be turned into a promotion of a different candidate, and a base whose content changed
  since the decision blocks promotion.
- Old libraries need one explicit `migrate-library` run before the first write.

## Limits

- SQLite coordinates processes on one host. It is not a distributed store, and a network filesystem may not honor
  its locking.
- Hashes detect accidental inconsistency when they are checked. They do not protect against an administrator who
  can alter both the database and the hashes.
- The exported files are replaced one by one, so a crash between two exports leaves views that disagree until
  `export-library` runs. The database is authoritative.
- The exposure registry only knows evaluations recorded in this library.
- Experiment results (`results.json`, HTML) still describe the run; they are views of the stored trials, not an
  independent record.

## Enforced by

- `tests/unit/test_db.py::WorkbenchDBTests::test_register_base_is_idempotent_and_conflicts_on_different_content`
- `tests/unit/test_db.py::WorkbenchDBTests::test_allocate_child_uses_next_decimal_and_rewrites_parent`
- `tests/unit/test_db.py::WorkbenchDBTests::test_allocate_child_requires_registered_parent`
- `tests/unit/test_db.py::WorkbenchDBTests::test_busy_database_raises_store_busy_error`
- `tests/unit/test_db.py::WorkbenchDBTests::test_newer_schema_is_refused`
- `tests/unit/test_harness_hash.py::HarnessHashTests::test_hash_ignores_lineage_notes_and_version`
- `tests/unit/test_store_views.py::StoreViewTests::test_base_is_never_renumbered_and_index_is_rebuilt_from_db`
- `tests/unit/test_store_views.py::StoreViewTests::test_lineage_conflict_on_same_identity_different_content`
- `tests/unit/test_store_views.py::StoreViewTests::test_failed_export_keeps_db_state_and_export_library_regenerates`
- `tests/unit/test_store_views.py::StoreViewTests::test_unmigrated_json_library_is_refused_before_writing`
- `tests/unit/test_store_views.py::StoreViewTests::test_json_file_without_db_row_is_refused_even_when_db_exists`
- `tests/unit/test_migrate_library.py::MigrateLibraryTests::test_dry_run_reports_without_writing`
- `tests/unit/test_migrate_library.py::MigrateLibraryTests::test_migration_is_idempotent_and_backs_up_index_and_db`
- `tests/unit/test_migrate_library.py::MigrateLibraryTests::test_conflicting_content_is_reported_not_overwritten`
- `tests/unit/test_migrate_library.py::MigrateLibraryTests::test_failure_mid_import_leaves_no_half_imported_state`
- `tests/integration/test_store_concurrency.py::StoreConcurrencyCase::test_six_processes_keep_one_base_and_distinct_children`
- `tests/integration/test_store_concurrency.py::StoreConcurrencyCase::test_twelve_processes`
- `tests/integration/test_store_concurrency.py::StoreConcurrencyCase::test_busy_errors_are_explicit_not_infinite`
- `tests/adversarial/test_audit_reproductions.py::A07ConcurrentLineage::test_six_concurrent_improvers_keep_a_single_base`
- `tests/unit/test_db_experiments.py::DecisionAndPromotionTests::test_promote_is_idempotent_for_the_same_decision`
- `tests/unit/test_db_experiments.py::DecisionAndPromotionTests::test_promote_is_idempotent_across_connections`
- `tests/unit/test_db_experiments.py::DecisionAndPromotionTests::test_promote_rejects_substituted_candidate_non_eligible_decision_and_unknown_parent`
- `tests/unit/test_db_experiments.py::HoldoutReservationTests::test_reserve_holdout_is_atomic_across_spawned_processes`
- `tests/unit/test_db_experiments.py::TrialTests::test_trials_are_persisted_individually_and_round_trip`
- `tests/unit/test_db_experiments.py::FinishAndIncompleteTests::test_finish_states_and_incomplete_listing`
- `tests/unit/test_cli_experiments.py::ListIncompleteTests::test_list_incomplete`
- `tests/integration/test_run_experiment.py::SigkillTests::test_sigkill_during_confirmation_keeps_completed_trials_and_no_decision`
- `tests/unit/test_improver_gate.py::ApprovedChainTests::test_approved_sequential_result_persists_base_and_final_only_with_direct_parent`
- `tests/unit/test_phase4_review_fixes.py::ImproverGateErrorLessonTests::test_infrastructure_exceptions_propagate_out_of_the_improver`
- `tests/unit/test_phase4_review_fixes.py::ImproveGateInfrastructureTests::test_infrastructure_failure_inside_the_gate_exits_two_without_a_reject_lesson`
- `tests/unit/test_improver_gate.py::ApprovedChainTests::test_without_a_gate_every_adopted_step_is_still_persisted`
- `tests/unit/test_improver_gate.py::GateCallTests::test_rejected_chain_persists_only_the_base`
- `tests/unit/test_improver_gate.py::PersistByDecisionTests::test_persist_with_decision_id_is_idempotent_promotion`
- `tests/unit/test_improver_gate.py::PersistByDecisionTests::test_persist_with_decision_for_other_candidate_conflicts`
- `tests/unit/test_improver_gate.py::PersistByDecisionTests::test_a_decision_promotes_exactly_one_candidate`
