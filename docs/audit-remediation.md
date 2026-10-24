# Audit remediation

Map of the audit findings A1-A12 (spec `docs/SPEC_fixes_experimental-evaluation-layer.md`, sections 3 to 9) to the
change that addresses each, the tests that fail if it is broken, the local result, and what remains open. It also
records every pre-existing test whose expectation new semantics changed, what was not reproduced, and what was not
run locally.

How to read it:

- **Module paths** are under `harness_workbench/` (`experiments/...` is `harness_workbench/experiments/...`).
- **Test ids** are `path::Class::test_name` and run with `python -m unittest` (dotted form,
  for example `python -m unittest tests.unit.test_grader.EvaluateBundle`).
- **Local result** is a focused run of the named tests on one interpreter, listed under "Local commands". The sets
  overlap, so counts are per row and are not additive. A local run is not CI.
- **CI** is a placeholder until T33. Nothing has been pushed, so no GitHub Actions run exists for this work, and no
  run id is claimed anywhere in these documents. The historical run ids in `docs/verification.md` belong to an
  older commit.
- **Synthetic data.** Every simulation, benchmark and demo result cited here is synthetic. The auditor's figures
  (34/400, 12/400 and 47/300, that is 8.5 %, 3.0 % and 15.7 %) are the auditor's, not results of this work; see
  `docs/statistical-validation.md` section 8.
- **No real-model validation.** Nothing in this work calls a paid model or measures a language model.

## A1-A12 map

| ID | Finding | Change (modules) | Tests | Local result (Python 3.12.12) | CI | Remaining limitations |
|---|---|---|---|---|---|---|
| A1 | The grader ran inside the solver's workspace: the solver could rewrite `check.py`, shadow `unittest.py`/`json.py`, or print `PASS`. | Frozen `GraderBundle`, validated snapshot, supervisor-side evaluation, `oracle` checks, `trusted_local` versus `sandboxed` (`experiments/grader.py`, `experiments/checks.py`, `experiments/backends.py`, `experiments/manifest.py`). ADR 0001. | `tests/adversarial/test_audit_reproductions.py::A01GraderIntegrity::test_rewritten_check_script_still_fails`<br>`::A01GraderIntegrity::test_shadowed_unittest_module_cannot_pass`<br>`tests/adversarial/test_grader_shadowing.py::ShadowedModules`, `::RunnerTampering`<br>`tests/unit/test_grader.py::EvaluateBundle`, `::RunControls`<br>`tests/integration/test_sandbox_backend.py` (not run locally) | 51 tests OK | pending (T33) | **An `oracle` verdict can still be forged** by candidate code that runs in the grader process and writes `$HARNESS_OUT/observed.json` late (for example from an `atexit` hook), because `out` is writable by the grader and `sandboxed` mounts it read-write too (ADR 0001, Limits). A `command` check is defeated by candidate code in the grader process (`os._exit(0)`). `python -I` is not a trust boundary. `trusted_local` is not a sandbox and lets the solver read host files. The sandbox has only been exercised where Docker runs. The bundle hash covers checks and grader files, not every module an interpreter imports. |
| A2 | The best of several candidates was chosen on the same tasks that decided promotion, with no development/holdout split. | Splits and families, one frozen finalist, holdout reserved before the first holdout trial, proposer never sees holdout data (`experiments/protocol.py`, `experiments/core.py`, `experiments/manifest.py`, `core/improver.py`, `cli.py`). ADR 0002. | `tests/adversarial/test_audit_reproductions.py::A02SingleFinalist::test_only_one_variant_reaches_validation_tasks`<br>`tests/adversarial/test_a09_proposer_isolation.py::ProposerIsolationTests::test_proposer_never_receives_holdout_tasks_or_results`<br>`tests/unit/test_protocol.py::SelectionTests`, `::FrozenSelectionTests`<br>`tests/unit/test_run_experiment_phases.py::FinalistTests::test_only_finalist_and_baseline_run_on_holdout`<br>`tests/unit/test_manifest.py::ConfirmatoryTests::test_finalists_must_be_one` | 18 tests OK | pending (T33) | One finalist and one primary metric; several confirmatory finalists are rejected, not corrected for multiplicity. Hashes do not prove semantic independence of splits. The calibration recreation of A2 is synthetic (0/2000 promotions, seed 303). |
| A3 | One task repeated many times counted as enough evidence (pseudo-replication). | The independent unit is the family; repetitions, checks, steps and retries never raise the unit count; profile floors of 50 validation and 80 regression units (`experiments/stats.py`, `experiments/profiles.py`, `experiments/manifest.py`). ADR 0002. | `tests/adversarial/test_audit_reproductions.py::A03MinimumUnits::test_one_task_many_repetitions_is_not_eligible`<br>`tests/unit/test_decide.py::EvidenceTests::test_one_task_many_repetitions_is_insufficient`<br>`tests/unit/test_manifest.py::ConfirmatoryTests::test_confirmatory_rejects_repetitions_below_profile` | 7 tests OK | pending (T33) | Independence of families is an assumption the manifest author makes and the registry only checks locally. The confirmatory demo's units are template variants of one sentence pattern, not semantically independent. |
| A4 | Cost and latency gates failed open (zero or unknown baseline, agent-reported latency). | `Measurement` with provenance; supervisor-measured latency; fail-closed budgets in `decide` (`experiments/contracts.py`, `experiments/command.py`, `experiments/stats.py`, `experiments/manifest.py` `BudgetPolicy`). | `tests/adversarial/test_audit_reproductions.py::A04UsageGates::test_unknown_baseline_tokens_block`, `::test_adapter_latency_is_not_authoritative`<br>`tests/unit/test_decide.py::BudgetTests`<br>`tests/unit/test_command_adapter.py::MeasurementTests::test_supervisor_latency_wins_over_adapter_latency` | 48 tests OK | pending (T33) | The token gate compares tokens, not money; a monetary limit would need a versioned price model. Adapter-reported tokens are accepted by default and are only as honest as the adapter. Usage outside the trial executor (for example an external proposer) is not recorded. |
| A5 | A grader infrastructure error counted as a quality failure and the run was still "verified". | Explicit `evaluation_error` with no score, which invalidates confirmation (`experiments/grader.py`, `experiments/status.py`, `experiments/stats.py`). | `tests/adversarial/test_audit_reproductions.py::A05GraderErrors::test_missing_grader_is_not_verified`<br>`tests/unit/test_grader.py::EvaluateBundle::test_missing_grader_executable_is_evaluation_error_without_score`<br>`tests/unit/test_decide.py::IntegrityTests` | 8 tests OK | pending (T33) | There is no automatic retry of an evaluation error; the experiment is invalid. The verdict-forging limit of A1 applies. |
| A6 | `NaN`, `Infinity`, booleans and out-of-range values were accepted or coerced, disabling gates. | Strict finite-number validation at manifest load, on API-built specs and on adapter result files (`experiments/validation.py`, `experiments/manifest.py`, `experiments/command.py`). | `tests/adversarial/test_audit_reproductions.py::A06StrictValues`<br>`tests/unit/test_validation.py`<br>`tests/unit/test_manifest.py::ApiValidationTests` | 21 tests OK | pending (T33) | None recorded beyond the free-form text outputs of a solver, which checks compare as text or JSON and never read as numbers. |
| A7 | Concurrent processes renumbered the shared base: phantom bases and wrong lineage. | SQLite source of truth, short transactions, no base renumbering, promotion by decision id, exported JSON views, explicit migration (`core/db.py`, `core/store.py`, `core/migrate.py`, `core/improver.py`, `cli.py`). ADR 0003. | `tests/adversarial/test_audit_reproductions.py::A07ConcurrentLineage::test_six_concurrent_improvers_keep_a_single_base`<br>`tests/integration/test_store_concurrency.py::StoreConcurrencyCase` (6 and 12 processes, with barriers)<br>`tests/unit/test_db.py`, `tests/unit/test_store_views.py`, `tests/unit/test_migrate_library.py` | 47 tests OK | pending (T33) | SQLite coordinates processes on one host; it is not a distributed store and a network filesystem may not honor its locking. The exported files are replaced one by one, so a crash between exports leaves disagreeing views until `export-library`. Hashes detect accidents, not an administrator who edits database and hashes. `experiment --list-incomplete` creates an empty `library/workbench.sqlite3` on a root that has none. An old library needs one explicit `migrate-library`: `improve`, `experiment` and `experiment --list-incomplete` refuse an unmigrated root (exit 2) before creating the database, using the holdout or running a model, and `improve` also refuses a base whose id and version are registered with different content (final review round 1). A disk error between the gate's decision and the promotion can still leave an eligible decision unpromoted (rare, accepted); a rerun of `improve` on the same library then ends `exploratory_only`. |
| A8 | Low statistical power: the audited promotion rule recommended true improvements rarely (the auditor's 47/300 = 15.7 % for 5 tasks and 0.5 to 0.7). | `cluster_percentile_ci_v1` bound over independent units, non-inferiority with Bonferroni, published profile `binary_v1`, Monte Carlo simulator over the real functions, pre-registered calibration (`experiments/stats.py`, `experiments/profiles.py`, `experiments/simulate.py`, `docs/statistical-validation.md`). ADR 0002. | `tests/statistical/test_stats.py::BoundTests::test_percentile_bound_matches_reference_within_tolerance`<br>`tests/statistical/test_simulate.py::ProfileTests`, `::MatrixTests::test_recreations_are_a2_a3_a8`, `::ProtocolTests::test_null_smoke_rate_is_small_and_power_smoke_rate_is_large`<br>`tests/unit/test_decide.py` | 47 tests OK (`tests.statistical`) | pending (T33) | Synthetic: worst null `upper_95` 0.0553 and target power `lower_95` 0.8359 hold for the simulator's generator at 50 validation and 80 regression units, not for language models. The profile needs those units; a 5-task plan returns `insufficient_evidence` (0/2000 promotions in the recreation). The regression tolerance is 0.10. The selection followed a dated deviation from the pre-registered 1000-simulation run (ADR 0002). Several regression groups, and plans above the floors, were not calibrated. Calibration ran on Python 3.14, not on the CI interpreters. |
| A9 | The validation set was reused at every step; renaming task ids hid the reuse. | Holdout exposure registry keyed by the content fingerprint of each task, reserved in one transaction before the first holdout trial; a rerun is allowed only for the identical interrupted confirmation (`core/db.py`, `experiments/protocol.py`, `experiments/core.py`). ADR 0002 and ADR 0003. | `tests/adversarial/test_audit_reproductions.py::A09HoldoutReuse::test_renamed_holdout_second_candidate_is_not_eligible`<br>`tests/unit/test_run_experiment_phases.py::ExposureTests`<br>`tests/unit/test_db_experiments.py::HoldoutReservationTests`<br>`tests/adversarial/test_a09_proposer_isolation.py` | 42 tests OK | pending (T33) | The registry is local to one library: it cannot see evaluations run in another library or by another team, nor exposure through model pretraining. Content fingerprints catch renamed tasks, not paraphrases. A rerun that already has holdout trials of the same candidate is `exploratory_only` and its trials are not pooled. |
| A10 | The solver and the grader inherited the parent's environment, including secrets. | Allowlisted minimal environment, fresh `HOME` and temporary directory per trial, no `PYTHON*` inheritance, grader without credentials, bounded streams (`experiments/backends.py`, `experiments/checks.py`, `experiments/command.py`). The grandchild timeout hang is under "Not reproduced". | `tests/adversarial/test_audit_reproductions.py::A10EnvironmentSecrets::test_solver_does_not_inherit_parent_canary`, `::test_grader_does_not_inherit_parent_canary`<br>`tests/unit/test_local_backend.py::MinimalEnvTests`<br>`tests/unit/test_command_adapter.py::EnvironmentTests` | 26 tests OK | pending (T33) | Variables the manifest declares in `solver_env_passthrough` reach the solver, and in `sandboxed` mode their values are on the `docker run` command line (`-e KEY=VALUE`), visible to the host's process list. `trusted_local` is the same user with the same files. Process groups do not contain a descendant that starts a new session (for example with `setsid()`). |
| A11 | The demo manifest used a bare `python` and a package that had to be importable, so the improve-gate test failed outside CI. | `{python}` placeholder resolved by the controller, `-I` rule for `command` and `oracle` checks, example manifests rewritten, foreign-cwd and no-alias portability tests (`experiments/manifest.py`, `experiments/backends.py`, `experiments/command.py`, `examples/experiments/*.json`). | `tests/integration/test_portability.py::DemoManifestPortabilityTests::test_demo_manifest_runs_from_foreign_cwd_without_python_alias`<br>`::test_portability_script` (opt-in, not run locally)<br>`tests/unit/test_manifest.py::LegacyAndValidationTests::test_bare_python_and_missing_grader_file_are_rejected`<br>`tests/test_experiments.py::ComposeAndStoreTests::test_improve_cli_can_require_experiment_validation` | 4 tests OK, skipped=1 (the portability script is opt-in and was not run) | pending (T33) | The wheel and editable install checks run only with `HARNESS_PORTABILITY_TESTS=1` and a Python that has `pip` and `setuptools`; they have not run on this machine. Linux CI is the intended evidence and does not exist yet. macOS and Windows are unverified. |
| A12 | No traceability: unbounded output, results tied to no implementation, nothing persisted until the end of the suite. | Output caps with `resource_limit`, provenance record with implementation hash and drift detection, per-trial persistence, adapter capability declaration, returned-model status (`experiments/provenance.py`, `experiments/backends.py`, `experiments/core.py`, `core/db.py`). | `tests/adversarial/test_audit_reproductions.py::A12Traceability` (3 tests)<br>`tests/unit/test_provenance.py`<br>`tests/integration/test_run_experiment.py::SigkillTests::test_sigkill_during_confirmation_keeps_completed_trials_and_no_decision` | 26 tests OK | pending (T33) | The implementation hash covers `harness_workbench/**/*.py` only; interpreter, adapter dependencies and image contents are not hashed (an image digest is recorded when Docker provides one). `returned_model_status` is `unknown` when a provider names no model. A seed is shared, not honored by every provider. `adapter_argv` is stored verbatim, so a manifest must hold no secrets. A command manifest without `executor.supports` declares every component, so `unsupported_components` cannot fire for it. `commit` and `dirty` describe the workbench checkout, and `dirty` is true whenever the checkout has untracked files, including the run's own `library/` and `experiments/` when `--root` is the checkout. `results.json` and the HTML are written at the end; the trials before that are in the database. There is no automatic resume. |

### Local commands

Every command ran from the repository root on Python 3.12.12 (`uv run --python 3.12 --no-project python -m
unittest ...`), which is inside the CI matrix (3.10, 3.12, 3.13). Counts are `Ran N tests`; all rows ended `OK`.

| ID | Command arguments after `python -m unittest` | Result |
|---|---|---|
| A1 | `tests.adversarial.test_audit_reproductions.A01GraderIntegrity tests.adversarial.test_grader_shadowing tests.unit.test_grader` | 51 OK |
| A2 | `tests.adversarial.test_audit_reproductions.A02SingleFinalist tests.adversarial.test_a09_proposer_isolation tests.unit.test_protocol tests.unit.test_run_experiment_phases.FinalistTests` | 18 OK |
| A3 | `tests.adversarial.test_audit_reproductions.A03MinimumUnits tests.unit.test_decide.EvidenceTests tests.unit.test_manifest.ConfirmatoryTests.test_confirmatory_rejects_repetitions_below_profile` | 7 OK |
| A4 | `tests.adversarial.test_audit_reproductions.A04UsageGates tests.unit.test_decide.BudgetTests tests.unit.test_command_adapter` | 48 OK |
| A5 | `tests.adversarial.test_audit_reproductions.A05GraderErrors tests.unit.test_grader.EvaluateBundle.test_missing_grader_executable_is_evaluation_error_without_score tests.unit.test_decide.IntegrityTests` | 8 OK |
| A6 | `tests.adversarial.test_audit_reproductions.A06StrictValues tests.unit.test_validation tests.unit.test_manifest.ApiValidationTests` | 21 OK |
| A7 | `tests.adversarial.test_audit_reproductions.A07ConcurrentLineage tests.integration.test_store_concurrency tests.unit.test_db tests.unit.test_store_views tests.unit.test_migrate_library` | 47 OK |
| A8 | `tests.statistical.test_stats tests.statistical.test_simulate` | 47 OK |
| A9 | `tests.adversarial.test_audit_reproductions.A09HoldoutReuse tests.unit.test_db_experiments tests.unit.test_run_experiment_phases.ExposureTests` | 42 OK |
| A10 | `tests.adversarial.test_audit_reproductions.A10EnvironmentSecrets tests.unit.test_local_backend tests.unit.test_command_adapter.EnvironmentTests` | 26 OK |
| A11 | `tests.integration.test_portability tests.unit.test_manifest.LegacyAndValidationTests.test_bare_python_and_missing_grader_file_are_rejected tests.test_experiments.ComposeAndStoreTests.test_improve_cli_can_require_experiment_validation` | 4 OK, skipped=1 |
| A12 | `tests.adversarial.test_audit_reproductions.A12Traceability tests.unit.test_provenance tests.integration.test_run_experiment` | 26 OK |

Also run on 3.12.12: `tests.integration.test_confirmatory_demo` (5 tests OK, 216 s) and
`tests.integration.test_sandbox_backend tests.unit.test_docker_backend_argv tests.unit.test_sandbox_reclaim`
(60 tests OK, skipped=7: the Docker integration tests, see "Not run locally").

Full suite, recorded by T29 at the CI workflow commit, on three interpreters through `uv run --python <v> --no-project
python -m unittest discover -s tests`: Python 3.10.19, 3.12.12 and 3.13.14 each `OK`, 619 tests, skipped=8 (seven Docker
sandbox tests and the opt-in portability script). The 36 tests that existed before this work (`tests/test_experiments.py`,
`tests/test_workbench.py`, `tests/test_anthropic.py`) were run again for this document on `python3` 3.15.0
(Homebrew, outside the CI matrix): 36 tests OK. The synthetic confirmatory demo was run on that same 3.15.0
(`harness-workbench experiment --provider command --spec examples/experiments/confirmatory-demo.json`):
decision `eligible`, estimate +1.0000, lower bound +1.0000, 50 validation units, 80 regression units, labeled
"Synthetic data". That number comes from a deterministic synthetic agent; it says the pipeline works, not that any
harness improves a model.

## Expectation changes in pre-existing tests

The 36 tests that existed at the start of this work all still exist and pass; six of them in
`tests/test_experiments.py` changed an assertion (T7, T18, T20). The table also lists tests added earlier in this
plan whose expectation a later task changed, so no changed expectation is hidden. Nothing was deleted to get green.

| Test | Change | Reason |
|---|---|---|
| `tests/unit/test_run_experiment_phases.py::LegacyTests::test_legacy_manifest_runs_all_variants_and_is_exploratory_only` | `payload["schema_version"]` is now `2` (was `1`) and `payload["manifest_schema_version"]` is asserted to be `1`. | T27: the top-level `schema_version` of `results.json` names the layout of the results file (2: decision statuses, per-trial execution/evaluation statuses, provenance). It no longer mirrors the manifest's version for a legacy v1 manifest, which moves to the new key `manifest_schema_version`. Existing keys are kept; one key was added. See "The `schema_version` keys" below. |
| `tests/test_experiments.py::ExperimentTests::test_checks_cannot_read_outside_workspace` | Builds an `EvaluationEnvironment` and asserts `passed` is false **and** `status == "error"`. | T7: checks run through the supervisor's evaluation environment. A path that escapes the artifact snapshot is an evaluator error, not a failed solver verdict. |
| `tests/test_experiments.py::ExperimentTests::test_command_verifier_runs_in_fixture` | The command is `{python} -I -c <code> {artifact}` and reads `seed.txt` under `argv[1]`, not under its working directory. | T7: the working directory of a `command` check is the frozen grader bundle; the solver's artifact is a read-only snapshot passed as `{artifact}`. |
| `tests/test_experiments.py::ExperimentTests::test_isolated_paired_repetitions_and_reports` | The two eligibility assertions became `result["decision"]["status"] == "exploratory_only"` and `summary["recommended"] == []` (were `contrasts[...]["eligible"]` true and `recommended == ["candidate"]`); the "judge score is ignored" assertion now checks the diagnostic `mean_delta == 0.5`. | T18: the manifest has no `schema_version`, so it is legacy and exploratory. Only a confirmatory `eligible` decision recommends anything; per-variant contrasts are diagnostics. The negative case (a high judge score cannot promote) is stronger elsewhere (`tests/unit/test_decide.py`, `tests/adversarial/`); here it holds because nothing exploratory can be eligible. |
| `tests/test_experiments.py::ExperimentTests::test_detect_task_specific_regression` | The per-task regression is still asserted on the diagnostic summary; `eligible` false became `decision["status"] != "eligible"`. | T18: the same reason; the verdict lives in the recorded decision. |
| `tests/test_experiments.py::ExperimentTests::test_behavioral_judge_cannot_promote_identical_outputs` | `contrasts[...]["eligible"]` false became `decision["status"] != "eligible"`; `mean_delta == 0` is unchanged. | T18: the same reason. |
| `tests/test_experiments.py::ComposeAndStoreTests::test_improve_cli_can_require_experiment_validation` | Was exit 0 with a `results.json` and `2.json` written; now exit 2 and no `experiments/` directory. This is the A11 test. | T20: `examples/experiments/local-command.json` is `mode: exploratory`, and the improve gate accepts only a confirmatory validation spec; it refuses before running anything (exit 2). The old "version 2 is persisted through the gate" assertion lives in `tests/integration/test_confirmatory_demo.py::EndToEndTests::test_improve_with_confirmatory_demo_persists_version_2_via_promotion`, against the confirmatory demo. The example now uses `{python}` instead of a bare `python`. |
| `tests/unit/test_trial_pipeline.py::BackendSelectionTests::test_the_backend_comes_from_the_environment_mode` | Patches `core.require_backend` instead of `core.build_backend`. | T18: the phased runner selects its backend through `require_backend`, which refuses to degrade to the host and raises `BackendUnavailable`; `build_backend` is its target. |
| `tests/adversarial/test_audit_reproductions.py::A12Traceability::test_trial_evidence_persisted_before_suite_end` | The crash is a `BaseException` (`_ProcessCrash`) raised through a `_Crashing` wrapper; the test also opens the database and asserts one incomplete experiment with 2 trials. | T18: an exception raised by an adapter (`SimulatedCrash`) is now, by design, one failed trial (`execution_error`) and does not end the run. A crash of the whole process is the case the finding is about, and is modeled as a `BaseException`. |
| `tests/adversarial/test_audit_reproductions.py::A02SingleFinalist::test_only_one_variant_reaches_validation_tasks` and `::A09HoldoutReuse::test_renamed_holdout_second_candidate_is_not_eligible` | Manifests became schema v2 confirmatory manifests with controls; A09 asserts `exploratory_only` plus the exposure block reason (was a contrast `eligible` flag). | T18: the behavior these reproductions describe only exists on a confirmatory manifest (splits, finalist, exposure). The finding is unchanged; the assertion moved from a diagnostic flag to the recorded decision. |
| `tests/unit/test_store_views.py::StoreViewTests::test_decision_id_is_recorded_in_the_summary` | Records a real eligible decision with `record_decision` and persists with that id (was an invented `"dec-1"`). | T19: `persist_result(..., decision_id=...)` promotes by decision id and requires an existing `eligible` decision for the candidate's execution hash; an unknown id raises `PromotionConflict` (ADR 0003). |
| `tests/unit/test_run_experiment_phases.py::FinalistTests::test_only_finalist_and_baseline_run_on_holdout` | The reason assertion was `["statistics not yet wired"]`; it is now the real `independent units 3 is below min_independent_units ...` reason, `n_units == 3` and one regression unit. | T22: `decide` is wired to the statistics. (T24 later moved the floor in this message from 30 to 50, below.) |
| `tests/unit/test_run_experiment_phases.py::ExposureTests::test_same_candidate_rerun_after_interruption_is_allowed_and_old_run_stays_incomplete` | The rerun's status is `exploratory_only` with a reason naming the interrupted experiment and "2 holdout trial" (was `insufficient_evidence`). A new test covers the rerun after an interruption before any holdout trial, which stays not exposed. | T22, from the Phase 4 review (spec section 7): a rerun that already has holdout trials of the same candidate is not pooled and cannot be eligible; otherwise kill-and-rerun would draw fresh samples until one passes. |
| `tests/unit/test_decide.py::EligibleTests::test_the_t_method_decides_too` | Builds its spec inside `mock.patch.dict(PROFILES, ...)` with the `cluster_t_v1` method. | T23: a confirmatory manifest's `statistics` must match the published profile's method, so a test that decides with the t method has to switch the profile table for the construction. The decision rule under test is unchanged. |
| T24 defaults: `StatisticsPolicy` in `experiments/manifest.py` and `binary_v1` in `experiments/profiles.py` | `regression_margin` 0.05 to 0.10, `min_independent_units` 30 to 50, `min_regression_units` 30 to 80. | T24: the 30/30/0.05 values were provisional plan placeholders; the pre-registered calibration selected 50/80/0.10 (ADR 0002 and `docs/statistical-validation.md`, including the dated deviation). Code, not a test, but it changes what every confirmatory fixture below must contain. |
| `tests/unit/test_decide.py` (T24 fixtures: `BudgetTests`, `EvidenceTests::test_one_task_many_repetitions_is_insufficient`, `RegressionGroupTests`, `EligibleTests::test_eligible_path`) | Fixture sizes: 51 validation and 80 regression tasks (were 30 and 30). Messages and counts: `min_independent_units 30` to `50`, `min_regression_units 30` to `80`; unknown-usage trial counts 300 to 655 and 600 to 1310; evidence ids `2*2*30*5` to `2*(51+80)*5`. The "wide uncertainty" regression scores went from ±0.5 to ±1.0 so the data stay undecided at 80 units; the Bonferroni groups went from 15 and 15 families to 40 and 40; the uncovered family from `r29` to `r79`. | T24: the same reason. Each change follows the new floors; the behaviors asserted (insufficient, rejected, undecided, `group_alpha == 0.025`) are unchanged. |
| `tests/unit/test_run_experiment_phases.py` (T24: `FinalistTests` floor message; `EligibleEndToEndTests::test_a_run_with_enough_independent_units_is_eligible_and_recorded`) | `min_independent_units 30` to `50`; the end-to-end fixture went from 30+30 tasks to 51+80, `n_units` 30 to 51, evidence ids to `2*131*REPETITIONS`. | T24: the same reason. |
| `tests/adversarial/test_audit_reproductions.py::A03MinimumUnits::test_one_task_many_repetitions_is_not_eligible` | The expected reason text is `min_independent_units 50` (was 30). | T24: the same reason. |
| `tests/statistical/test_simulate.py::ProfileTests::test_binary_v1_is_published_with_the_selected_values` (renamed from `..._with_the_brief_values`), `::ProfileEnforcementTests::test_profile_mismatch_is_rejected_at_load`, `::ProfileEnforcementTests::test_floors_are_read_from_the_profile_table` | The published tuple is now `(..., 0.10, 50, 80, ...)`; the mismatching `regression_margin` probe is 0.05 (was 0.10, now the profile's own value); the raised-floor probe is 60 (was 50, now the profile's own value). | T24: the same reason. The probes must differ from the published values to test the mismatch rule. |
| `tests/unit/test_local_backend.py::CapabilityTests::test_build_backend` | The sandboxed case expects `docker daemon unavailable` with an empty `PATH` (was `sandboxed backend not implemented`). | T10: the Docker backend exists; `sandboxed` mode raises `BackendUnavailable` when Docker or a capability is missing, and never falls back to the host. |
| `tests/unit/test_docker_backend_argv.py::RunAndCleanupTests::test_run_executes_the_command_line_through_the_local_backend`, `tests/integration/test_sandbox_backend.py::SandboxBackendTests::test_timeout_removes_container_and_cleanup_makes_dirs_removable` | The first test picks the `docker run ... -w` call instead of the last call and checks no `rm` follows; the second no longer calls `cleanup` explicitly (a new integration test covers host reads after a run). | Phase 2 fix round: `run` now ends with a reclaim container (`chmod -R a+rwX`) after the solver, so the solver call is no longer last and the run already hands the tree back. See "Plan wording that the code does not follow". |
| `tests/unit/test_docker_backend_argv.py::RunAndCleanupTests::test_docker_exit_125_..._execution_error_not_a_task_failure`, `::test_docker_exit_127_with_docker_prefix_...`, `::test_docker_exit_126_with_exec_prefix_...` (renamed from `..._is_a_retryable_...` to `..._is_a_non_retryable_...`) | `retryable` is asserted false (was true). | Phase 4 fix round: a solver can produce exit 125, 126, 127 and the stderr prefix itself, so a retry it could trigger would be best-of-N against the baseline. The outcome stays an `execution_error` (fail closed) and the attempt is kept as evidence. |
| Rule P5: duplicate check ids in one task are rejected at load (`experiments/manifest.py`, `_reject_duplicate_check_ids`) | No existing assertion changed. A manifest that repeats a check id, including an explicit id equal to the implicit `check-N` of an id-less check, no longer loads (`ManifestError: duplicate check id`). `decide` also vetoes on any failing result that shares an id. | Phase 5 review fix: check ids name the critical invariants, and a duplicate id could hide a failing critical check behind a passing one. New tests: `tests/unit/test_manifest.py::LegacyAndValidationTests::test_duplicate_check_ids_in_one_task_are_rejected`, `tests/unit/test_decide.py::IntegrityTests::test_a_failing_critical_result_is_not_hidden_by_a_same_id_passing_result`. The same id in two different tasks is fine. |
| P6 provenance field `returned_model_status` | No existing assertion changed; `results.json` and the report gained `provenance.returned_model_status` and `provenance.returned_models`. | Phase 6 review fix: the model ID the provider returns is carried from the Anthropic executor to the trial. `verified` means every completed trial reported the same model, `mixed` means more than one (a confirmatory run is invalidated as model drift), `partial` means some completed trials named a model and some did not (a limitation, not drift), `unknown` means none did, so reproducibility across trials cannot be claimed. New tests: `tests/unit/test_provenance.py::ReturnedModelTests`. |

### The `schema_version` keys

Three keys carry a version number and they name different things.

| Where | Value | What it names |
|---|---|---|
| Manifest top-level `schema_version` | `2` (or absent or `1` for a legacy manifest) | The layout of the experiment manifest. A manifest without it, or with `1`, is legacy: exploratory only, with a trusted-local environment and a warning; `harness-workbench migrate-manifest` converts it. |
| `results.json` top-level `schema_version` | `2` | The layout of the results file: decision statuses, per-trial execution and evaluation statuses, provenance. It no longer mirrors the manifest's version. |
| `results.json` top-level `manifest_schema_version` | the manifest's own version (`1` or `2`) | Which manifest layout the run was loaded from. |
| `results.json` `provenance.schema_version` | the manifest's version, like `manifest_schema_version` | The provenance record repeats the manifest layout; it is not the results layout. |

A reader that wants the results layout reads the top-level `schema_version`; a reader that wants to know whether a
run came from a legacy manifest reads `manifest_schema_version`. For a schema v2 manifest all three read 2.

## Plan wording that the code does not follow

The phase reviewer ruled that two passages of the registered plan are insufficient or contradictory. The plan was
not edited mid-run; the code follows the tests, and this section states what the code does.

- **T10, "writable mounts are `chmod 0o777` before mounting".** Changing the mode of the mount roots alone is not
  enough: the container runs as user 65534 with all capabilities dropped, so everything inside a root that the host
  user owns (a private fixture file, a `0700` subdirectory) stays unusable, and files that the container creates
  belong to 65534 and the host cannot read, snapshot or delete them. What `DockerBackend` does (`_open_tree`,
  `run`, `cleanup` in `experiments/backends.py`):
  - before the container starts, every writable mount tree is opened recursively on the host: each directory gets
    its mode OR `0o777`, each regular file its mode OR `0o666` (execute bits are added only where a file already
    has one). Symlinks are neither followed nor changed (`lstat`), so a link planted in a fixture cannot reach a
    file outside the tree. A path that cannot be changed makes `run` return an `execution_error`
    ("could not prepare writable mount").
  - after the container (and after removing it on a timeout or output overflow), `cleanup` runs a short
    root container, `chmod -R a+rwX` on each writable mount, so the host can snapshot and delete the tree. If the
    container cannot be removed or the reclaim fails, the outcome is an `execution_error` with
    `retryable=True` ("docker infrastructure failure, not a solver result"): the tree is never scored.
  - tests: `tests/unit/test_sandbox_reclaim.py::OpenAndReclaimTests` (whole tree opened, symlinks untouched,
    reclaim after the solver, failed reclaim overrides a timeout) and
    `tests/unit/test_docker_backend_argv.py::RunAndCleanupTests::test_writable_mounts_are_made_world_writable_before_mounting`.
    The Docker end of this (`tests/integration/test_sandbox_backend.py`) is not run locally.
- **T7, symlinks.** The plan has the grader snapshot reject symlinks, and a few tasks later has the trial copy the
  fixture with `shutil.copytree(..., symlinks=False)`. With `symlinks=False`, `copytree` follows a link and copies
  the target's content into the workspace, which contradicts the plan's own test name
  (`test_fixture_is_copied_without_following_symlinks`) and would let a fixture link read a host file. What the code
  does: the trial copies with `symlinks=True`, which keeps a link as a link and never reads through it
  (`experiments/core.py`); `snapshot_workspace` (`experiments/grader.py`) rejects symlinks, hard-linked files,
  sockets, FIFOs, devices, a symlinked workspace, paths that do not resolve under the workspace and trees over the
  limits, so a symlinked fixture makes the trial an `evaluation_error` and never a score. The fixture fingerprint
  (`digest_path`) includes names, permissions and symlink targets. Tests:
  `tests/unit/test_trial_pipeline.py::FixtureTests::test_fixture_is_copied_without_following_symlinks`,
  `tests/unit/test_grader.py` (`test_rejects_symlink`, `test_rejects_symlink_to_outside_file`,
  `test_rejects_hardlink`, `test_snapshot_rejects_symlink_hardlink_fifo_and_limits`).

## Not reproduced

**The grandchild timeout hang.** The finding (a grandchild that outlives its parent and holds the output pipes keeps
the audited `subprocess.run(..., timeout=...)` call from returning when it should) was not reproduced by the auditor,
and it was not reproduced by this work either: `tests/adversarial/test_audit_reproductions.py` holds no test for it,
because no reproduction against the audited commit was written. What exists is the new backend's own bound.
`LocalBackend` (`experiments/backends.py`) starts the child in its own session, kills the whole process group on a
timeout and after the main process exits, and stops its reader threads after a fixed bound
(`_READER_JOIN_SECONDS`, 1 second per join) instead of waiting for a descendant that may keep the pipes open.
`tests/unit/test_local_backend.py::LocalBackendRunTests::test_run_returns_even_when_a_setsid_descendant_holds_the_pipes`
and `::test_residual_children_are_killed_after_main_process_exits` show that `run` returns and that same-session
residual children die. While the backend was being written, the first test failed once on its first implementation
(10.1 s against a 5 s bound) and was fixed before the commit; that was a defect in the new code, not evidence about
the audited code. **No hang is claimed as fixed.** A descendant that starts a new session survives the group kill;
that is a remaining limitation, not a fix.

## Not run locally

A skipped test is "not run", never "passed". These did not run on this machine:

- **The Docker sandbox integration tests** (`tests/integration/test_sandbox_backend.py`, 7 tests). They need
  `HARNESS_SANDBOX_TESTS=1` and a Docker daemon whose capability self-test passes. No Docker daemon is running
  here, so every run reports `skipped=7`. The sandboxed demo (`examples/experiments/sandboxed-demo.json`) was not
  run for the same reason. What does run without Docker is the argv and reclaim logic against a fake docker
  (`tests/unit/test_docker_backend_argv.py`, `tests/unit/test_sandbox_reclaim.py`). The flag combination, the
  in-image self-test, container reaping and the permission reclaim against a real daemon are unverified.
- **The portability script** (`tests/integration/test_portability.py::DemoManifestPortabilityTests::test_portability_script`,
  1 skipped test). It builds a wheel and an editable install and needs `HARNESS_PORTABILITY_TESTS=1` and a Python with
  `pip` and `setuptools`. The local interpreters are the Homebrew 3.15.0 `python3` (no `setuptools`) and `uv`-managed
  interpreters without `pip`, so the script reports `NOT RUN`. The always-on test in the same module (foreign cwd, no
  `python` alias on `PATH`) passes.
- **The CI jobs of `.github/workflows/harness-tests.yml`** as GitHub runs them: `core` (matrix 3.10, 3.12, 3.13),
  `no-install`, `isolation`, `portability` and `smoke`. The smoke commands were executed locally with a console-script shim; the
  rest, including whether `ubuntu-latest` ships `/usr/bin/python` (the portability job fails by design if it does),
  is unverified until T33.
- **Real-model runs.** No test or demo calls a model. The Anthropic backend is exercised only with a fake client.

## Open limitation: oracle verdict forgery

Found by the T30 review of ADR 0001 and not fixed in code. An `oracle` check's verdict can be forged by candidate
code that runs in the grader process and writes `$HARNESS_OUT/observed.json` after the grader's own write (for
example from an `atexit` hook that writes the expected value while the code returns a wrong answer), because the
result directory `out` is writable by the grader (`experiments/checks.py`, `_run`) and `sandboxed` mounts every
writable path read-write (`experiments/backends.py`, `command_line`). The oracle defeats an early `os._exit(0)`
(no observation, so `evaluation_error`), not a late write. Until the evaluator observes in a separate process that
never imports the candidate, a confirmatory result for a `runs_candidate_code` task whose grader imports the
candidate must not be read as proof against a hostile candidate.

## Not part of this work

Real-model validation, a distributed store, automatic resume, an authenticated or tamper-evident registry, support
for untrusted code under `trusted_local`, and any claim about platforms other than the local macOS machine
(Python 3.12 and 3.15) and the Linux CI that has not run yet.
