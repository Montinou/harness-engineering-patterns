# ADR 0004: Single-branch delivery and versioning

- Status: Accepted
- Date: 2026-10-24
- Spec: sections 0, 1 and 10 (branch protection, version policy, implementation order).

## Context

The executing-plans workflow this work followed defaults to one branch and one draft pull request per plan phase,
stacked on each other. The spec (section 10) says the changes stay exclusively on the authorized branch and that
the existing pull request is neither replaced nor merged. The spec (section 1) says the package version at the
starting point is 0.2.0, that 0.3.0 must not be declared by resemblance to another branch, and that a version
change needs an ADR and synchronized metadata.

## Decision

1. **Commits land directly on `feat/experimental-evaluation-layer`.** No phase branches and no per-phase pull
   requests were created. Each phase is a sequence of commits on that branch, with the phase review and check
   before its last commit. Pull request #1 stays a draft; nothing marks it ready for review, and nothing was merged
   into `main` or cherry-picked from `feat/harness-experimentation-layer`. A push happens only after an explicit
   question to the human at the delivery step.
2. **The package stays 0.2.0.** `pyproject.toml` and `harness_workbench/__init__.py` both keep `0.2.0`. The changes
   are internal to an experimental layer on a branch that is not released, so a package bump is not justified by
   them. No metadata, CLI banner, README or changelog claims another version.
3. **Manifest `schema_version` 2.** The experiment manifest gains `schema_version: 2` (splits, families, controls,
   `environment`, `statistics`, `budget`, `mode`). A manifest without `schema_version`, or with `1`, is legacy: it
   loads as exploratory, with a trusted-local environment and a warning, and `migrate-manifest` converts it. This
   is the compatibility policy for the one incompatible area; it is separate from the package version.
4. **Results file `schema_version` 2.** The top-level `schema_version` of `results.json` names the layout of the
   results file (decision statuses, per-trial execution and evaluation statuses, provenance). The manifest's own
   version moved to the new key `manifest_schema_version`; the other keys are kept. This changed the expectation
   of one pre-existing test, which is listed in `docs/audit-remediation.md`.
5. **Implementation order followed spec section 10.** Seven logical groups, in this order: reproductions, statuses
   and schema validation; protected evaluator, supervisor and environments; transactional registry, lineage and
   holdout exposure; splits, finalist and non-contaminating Improve; inference, non-inferiority and Monte Carlo
   calibration; budgets, traceability, portability and reports; documentation, examples, CI and end-to-end tests.
   No dependency forced a reordering. Each audit reproduction was committed failing before its fix.

## Consequences

- One linear history on one branch, reviewable per commit, with the pull request untouched until a human decides.
- Consumers reading `results.json` must read `schema_version` as the results layout and
  `manifest_schema_version` as the manifest's.
- A future package version bump is a separate decision that must synchronize `pyproject.toml`,
  `harness_workbench/__init__.py`, the README and the changelog.

## Limits

- Stacked phase pull requests would have given smaller review units. A single branch gives one large pull
  request; per-commit review is the substitute.
- Keeping 0.2.0 means the package version does not signal the schema and persistence changes. The schema versions
  and the migration commands carry that signal instead.
- The delivery evidence for this ADR is the commit history and the checks in the repository; this ADR does not
  assert the outcome of CI runs.

## Enforced by

- `tests/unit/test_run_experiment_phases.py::LegacyTests::test_legacy_manifest_runs_all_variants_and_is_exploratory_only` (results `schema_version` 2 and `manifest_schema_version` 1)
- `tests/unit/test_manifest.py::LegacyAndValidationTests::test_v1_manifest_loads_as_legacy_exploratory_with_mapped_policy`
- `tests/unit/test_manifest.py::LegacyAndValidationTests::test_v2_requires_explicit_environment_mode`
- `tests/unit/test_manifest.py::MigrationTests::test_migrate_manifest_maps_keys_and_marks_missing_splits`
- `tests/unit/test_manifest.py::MigrationTests::test_cli_migrate_manifest_writes_a_loadable_file`
