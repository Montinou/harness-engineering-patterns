# ADR 0001: Evaluator trust boundary

- Status: Accepted
- Date: 2026-10-24
- Spec: FIX-01 (section 3), FIX-06 (section 8). Audit findings: A1, A5, A10.

## Context

The audited version of the experiment runner executed the grader inside the solver's workspace. A solver that
could write files there could rewrite `check.py`, plant `unittest.py`, `json.py`, `sitecustomize.py` or `conftest.py`,
or print `PASS`, and the evaluator treated the outcome as a verified pass. A missing grader executable was also
recorded as verified. The experiment layer therefore had no boundary between "what the solver controls" and "what
decides the score".

## Decision

1. **Frozen bundle outside the workspace.** Each task has a `GraderBundle` (checks, grader files, hash) built at
   load time from the manifest directory. After the solver has finished, the supervisor copies the workspace into a
   validated snapshot (regular files and directories only; symlinks, hardlinks, FIFOs, traversal and over-budget
   trees are rejected), materializes the bundle read-only in a fresh directory the solver never saw, and runs the
   checks from there. The bundle hash does not change when the solver rewrites its own copy of a script.
2. **Verdicts come from the supervisor.** Output checks read the recorded outputs and the snapshot, never files the
   solver could forge. An `oracle` check runs a grader program that only *observes*: it writes a value under the
   result directory, and the supervisor reads and compares that value with `expected` itself. A grader that exits 0
   without writing the observation is an `evaluation_error` (oracle protocol violation), never a pass. This
   defeats a grader process that exits early; it does not make the observation unforgeable by candidate code that
   runs inside that process (see Limits).
3. **Explicit statuses.** A grader that cannot start, an unreadable snapshot, an empty bundle or a protocol
   violation is `evaluation_error` with no score, which blocks confirmation. It is not a failed solver and not a
   zero.
4. **Two environment modes, no default.** A schema v2 manifest must state `environment.mode`.
   - `trusted_local` is opt-in. It runs the solver and grader as local child processes (`LocalBackend`), declares
     `NO_GUARANTEES` as its capabilities, and is not a security sandbox. A confirmatory manifest that uses it must
     also set `trusted_solver_acknowledged: true`; the loader's message is "trusted_local lets the solver read host
     files including grader files; acknowledge or use sandboxed". A schema v1 manifest loads as `trusted_local`
     with a warning and is exploratory only.
   - `sandboxed` runs through `DockerBackend`, which must pass its own capability self-test (isolated filesystem,
     network denied, unprivileged user, process containment, no Docker socket, read-only bundle) before any run. If
     Docker or a capability is missing it raises `BackendUnavailable` before executing; it never falls back to the
     host.
5. **Environments.** Neither the solver nor the grader inherits `os.environ`. Both get an allowlisted minimal
   environment and a fresh `HOME` and temporary directory per trial; `PYTHON*` variables from the parent are never
   copied, and the grader never receives credentials or passthrough variables. The command executor keeps control
   files outside the fixture copy and gives the solver only the public request and an opaque trial id.
6. **`python -I`.** Manifests spell the interpreter `{python}` (resolved by the controller to `sys.executable`), and
   a confirmatory manifest is rejected when a `command` or `oracle` check runs `{python}` without `-I` as `argv[1]`.
7. **Candidate code needs oracle plus sandbox.** In a confirmatory manifest, a task with `runs_candidate_code: true`
   requires `environment.mode: sandboxed` and at least one `oracle` check. `runs_candidate_code` has no default in
   confirmatory mode.
8. **Controls.** Every validation and regression task carries positive and negative controls that are run through
   the real evaluator before any trial; a control that behaves wrongly, or errors, invalidates the experiment.

## Consequences

- Rewriting `check.py`, planting stdlib-named modules or startup hooks in the artifact, setting `PYTHONPATH` or
  `PYTHONSTARTUP` in the parent, printing `PASS` and exiting 0 do not turn a failing solution into a passing one.
- A canary secret in the parent environment reaches neither the solver nor the grader.
- Bounded streams and a wall-clock timeout end runaway solvers as `resource_limit` or `timeout`, and the whole
  process group is killed after the main process exits.
- A confirmatory run that needs isolation fails with an explicit error where Docker is not available. The sandbox
  integration tests are skipped unless `HARNESS_SANDBOX_TESTS=1` and a daemon passes the self-test; a skipped test
  is "not run", not "passed".

## Limits

- **`python -I` is not a trust boundary.** It stops the interpreter from putting the current directory on
  `sys.path` and from reading `PYTHON*` variables and the user site. It does not make code that the grader imports
  trustworthy. If a grader process imports candidate code, that code runs with the grader's privileges.
- **A `command` check can be defeated by candidate code that runs in the grader process.** The grader test
  `test_candidate_code_calling_os_exit_zero_passes_a_plain_command_check` documents this: a candidate module whose
  import calls `os._exit(0)` makes a plain `command` check exit 0 and pass with a wrong answer. The same artifact
  against an `oracle` check yields `evaluation_error` because the supervisor never receives the observation
  (`test_candidate_code_calling_os_exit_zero_cannot_pass_an_oracle_check`). This is why `oracle` checks plus
  `sandboxed` are mandatory for `runs_candidate_code` tasks, as the strongest combination the evaluator offers,
  not as a guarantee.
- **An oracle does not stop a late write from candidate code in the grader process.** The oracle defeats an early
  `os._exit(0)`, because that leaves no observation. It does not defeat candidate code that writes the observation
  itself after the grader's own write: the result directory `out` is writable by the grader process
  (`harness_workbench/experiments/checks.py`, `_run`: `Mounts(..., writable=(env.out, env.home, env.tmp))`), and the
  supervisor reads the observed file only after that process has exited (`_oracle`, `_read_observed`). A candidate
  that registers an `atexit` hook which writes the expected value to `$HARNESS_OUT/observed.json` while its code
  returns a wrong answer is therefore compared as if the grader had observed the expected value, and the check can
  be `passed`. `test_forged_observed_with_wrong_content_fails_and_correct_solution_passes` covers only a write
  during import, which the grader's own later write overwrites; it does not cover a write after the grader's
  write. `sandboxed` does not close this: `DockerBackend` mounts every `Mounts.writable` path read-write
  (`harness_workbench/experiments/backends.py`, `command_line`), so `out` is writable inside the container too, and
  the container contains the candidate only from the host, not from the grader's result directory. An oracle
  protects the verdict against a grader that exits early or prints `PASS`; it does not protect it against candidate
  code that runs in the grader process and outlives the grader's write. Until the evaluator stops candidate code
  from sharing a process with the observing grader (for example by observing in a separate process that never
  imports the candidate), a confirmatory result for a `runs_candidate_code` task whose grader imports the
  candidate must not be read as proof against a hostile candidate.
- **`trusted_local` is not isolation.** The solver runs as the same user and can read host files, including grader
  sources in the manifest directory and the library. Results for untrusted code under `trusted_local` are at most
  exploratory. Process groups do not contain descendants that start a new session (for example with `setsid()`):
  `run` returns within a bounded time and abandons the pipes, but such a process survives.
- The sandbox is only as strong as the Docker daemon and the image used. It has been exercised only where the
  integration tests could run; other platforms are unverified.
- The grader hash covers the bundle's checks and files, not every module an interpreter later imports from the
  image or the host.

## Enforced by

- `tests/unit/test_grader.py::EvaluateBundle::test_rewritten_check_script_in_artifact_does_not_matter`
- `tests/unit/test_grader.py::EvaluateBundle::test_bundle_is_materialized_read_only_and_only_after_solver_finished`
- `tests/unit/test_grader.py::EvaluateBundle::test_missing_grader_executable_is_evaluation_error_without_score`
- `tests/unit/test_grader.py::EvaluateBundle::test_output_checks_cannot_be_forged_by_artifact_contents`
- `tests/unit/test_grader.py::EvaluateBundle::test_oracle_passes_only_on_supervisor_side_comparison`
- `tests/unit/test_grader.py::EvaluateBundle::test_grader_does_not_inherit_parent_secret_or_python_variables`
- `tests/unit/test_grader.py::RunControls::test_controls_positive_must_pass_and_negatives_must_fail`
- `tests/adversarial/test_grader_shadowing.py::ShadowedModules::test_shadowed_stdlib_modules_in_artifact_are_not_imported`
- `tests/adversarial/test_grader_shadowing.py::ShadowedModules::test_pythonpath_and_startup_env_are_not_inherited`
- `tests/adversarial/test_grader_shadowing.py::RunnerTampering::test_printing_pass_and_exit_zero_without_running_checks_does_not_pass`
- `tests/adversarial/test_grader_shadowing.py::RunnerTampering::test_candidate_code_calling_os_exit_zero_cannot_pass_an_oracle_check`
- `tests/adversarial/test_grader_shadowing.py::RunnerTampering::test_candidate_code_calling_os_exit_zero_passes_a_plain_command_check`
- `tests/adversarial/test_grader_shadowing.py::RunnerTampering::test_forged_observed_with_wrong_content_fails_and_correct_solution_passes`
- `tests/adversarial/test_audit_reproductions.py::A01GraderIntegrity::test_rewritten_check_script_still_fails`
- `tests/adversarial/test_audit_reproductions.py::A01GraderIntegrity::test_shadowed_unittest_module_cannot_pass`
- `tests/adversarial/test_audit_reproductions.py::A05GraderErrors::test_missing_grader_is_not_verified`
- `tests/adversarial/test_audit_reproductions.py::A10EnvironmentSecrets::test_solver_does_not_inherit_parent_canary`
- `tests/adversarial/test_audit_reproductions.py::A10EnvironmentSecrets::test_grader_does_not_inherit_parent_canary`
- `tests/unit/test_manifest.py::LegacyAndValidationTests::test_v2_requires_explicit_environment_mode`
- `tests/unit/test_manifest.py::LegacyAndValidationTests::test_v1_manifest_loads_as_legacy_exploratory_with_mapped_policy`
- `tests/unit/test_manifest.py::ConfirmatoryTests::test_trusted_local_requires_acknowledgment`
- `tests/unit/test_manifest.py::ConfirmatoryTests::test_runs_candidate_code_requires_sandboxed_and_oracle`
- `tests/unit/test_manifest.py::ConfirmatoryTests::test_confirmatory_requires_splits_controls_mode_and_isolated_python`
- `tests/unit/test_local_backend.py::MinimalEnvTests::test_passthrough_is_explicit_and_grader_env_never_passes_credentials`
- `tests/unit/test_run_experiment_phases.py::IntegrityTests::test_control_failure_invalidates_before_any_trial`
- `tests/unit/test_local_backend.py::CapabilityTests::test_local_backend_declares_no_guarantees`
- `tests/unit/test_local_backend.py::LocalBackendRunTests::test_run_returns_even_when_a_setsid_descendant_holds_the_pipes`
- `tests/unit/test_local_backend.py::LocalBackendRunTests::test_residual_children_are_killed_after_main_process_exits`
- `tests/unit/test_local_backend.py::LocalBackendRunTests::test_endless_output_is_killed_as_resource_limit`
- `tests/unit/test_docker_backend_argv.py::RefusalTests::test_sandboxed_mode_without_daemon_raises_backend_unavailable_not_local_fallback`
- `tests/unit/test_docker_backend_argv.py::RefusalTests::test_require_backend_names_missing_capability_and_never_falls_back`
- `tests/unit/test_docker_backend_argv.py::RefusalTests::test_require_backend_trusted_local_is_not_sandbox_and_unknown_modes_raise`
- `tests/unit/test_command_adapter.py::PublicRequestTests::test_solver_sees_only_public_components_and_opaque_trial_id`
- `tests/unit/test_command_adapter.py::EnvironmentTests::test_parent_canary_not_inherited_unless_passthrough`
- `tests/integration/test_sandbox_backend.py::SandboxBackendTests::test_probe_self_test_passes` (runs only with `HARNESS_SANDBOX_TESTS=1` and a working Docker daemon)
