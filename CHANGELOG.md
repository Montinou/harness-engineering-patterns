# Branch Change Log

## Unreleased — audit remediation (feat/experimental-evaluation-layer)

Remediation of the external audit findings A1-A12, with the package version still **0.2.0** (see
[ADR 0004](docs/adr/0004-single-branch-delivery-and-versioning.md)). The map from each finding to its change,
tests, local result and remaining limitations is [docs/audit-remediation.md](docs/audit-remediation.md). No CI run
exists yet for this work; local evidence names its interpreter in [docs/verification.md](docs/verification.md).

### Added

- Experiment manifest **schema v2** (`mode`, splits, families, controls, `environment`, `statistics`, `budget`,
  `runs_candidate_code`, `duplicate_review`) and `harness-workbench migrate-manifest` for schema v1 manifests.
- Explicit execution, evaluation and decision statuses (`eligible`, `rejected`, `insufficient_evidence`,
  `invalid_experiment`, `exploratory_only`) and nullable usage measurements with provenance.
- Frozen grader bundle evaluated outside the solver's workspace, validated snapshots, supervisor-side `oracle`
  checks, per-task positive and negative controls, and an adversarial suite (rewritten `check.py`, shadowed
  stdlib modules, runner tampering).
- Execution backends: `trusted_local` (bounded process, minimal environment, not a sandbox) and `sandboxed`
  (Docker with a capability self-test that refuses to degrade). The sandbox integration tests need Docker and
  were not run locally.
- Confirmatory protocol: development selection, one frozen finalist, a holdout reserved by task content
  fingerprint, one recorded decision; fail-closed token and latency budgets; provenance with implementation hash,
  drift detection and `returned_model_status`.
- Statistics over independent units (cluster bootstrap), non-inferiority on a regression split, the published
  profile `binary_v1`, a Monte Carlo simulator over the real decision functions and a pre-registered calibration
  with its dated deviation ([docs/statistical-validation.md](docs/statistical-validation.md)). Results are
  synthetic.
- SQLite store as the source of truth with exported JSON views; `export-library`, `migrate-library` (idempotent,
  `--dry-run`, backups) and `experiment --list-incomplete`.
- A confirmatory synthetic demo (`examples/experiments/confirmatory-demo.json`, `sandboxed-demo.json` and the
  generator) whose units are template variants and whose agent is deterministic.
- CI workflow jobs: core matrix (3.10, 3.12, 3.13), no-install, Docker isolation, portability and smoke.
- Architecture decision records `docs/adr/0001` to `0004` and `docs/audit-remediation.md`.

### Changed

- The standalone experiment decision no longer comes from a hierarchical bootstrap over repetitions, and a legacy
  manifest (no `schema_version`) runs as exploratory and can never be `eligible`.
- `improve --validation-spec` accepts only a confirmatory manifest, confirms the final composed harness once and
  promotes by decision id; an exploratory spec is refused before anything runs.
- `results.json` top-level `schema_version` is now `2` and names the results layout; the manifest's version is in
  the new `manifest_schema_version`.
- Solvers and graders no longer inherit the parent environment; manifests use `{python}` instead of a bare
  `python`; duplicate check ids within a task are rejected at load.
- The base harness is never renumbered; concurrent processes keep one canonical base. A library written by an
  older checkout needs one `migrate-library` run.
- Pre-existing test expectations changed by these semantics are listed, with reasons, in
  [docs/audit-remediation.md](docs/audit-remediation.md#expectation-changes-in-pre-existing-tests).

### Known limitations

- No real-model validation; every simulation and demo result is synthetic.
- `trusted_local` is not a sandbox and lets the solver read host files; process groups do not contain
  descendants that start new sessions.
- An `oracle` verdict can be forged by candidate code in the grader process that writes the observation late
  (ADR 0001); not fixed in code.
- The holdout exposure registry is local to one library; the implementation hash does not cover dependencies.
- The Docker sandbox tests and the wheel/editable portability script were not run on the author's machine.

## feat/experimental-evaluation-layer — documentation alignment (2026-10-22)

_Historical entry, kept as written. The code it describes is superseded by the audit remediation above._

- Reviewed documentation **on this branch**, not on
  feat/harness-experimentation-layer.
- Updated architecture and Workbench status to describe the experimental
  capabilities without changing the reported package version (0.2.0).
- Corrected outdated claims about re-running Improve, combined validation,
  persistence, backend isolation and command adapter limitations.
- Corrected examples requirements path.
- Marked the initial v1 build brief and dated research plans as historical.
- Documented verified GitHub Actions evidence: 36 tests and synthetic
  end-to-end experiment passed on Python 3.12.
- Documentation-only changes; no agent backend, test, API or version bump.

## Initial experimentation layer (implemented before documentation alignment)

_Historical entry, kept as written. Items such as hierarchical-bootstrap intervals and non-overwriting file
allocation were replaced by the audit remediation above._

- Independent acceptance checks over outputs, JSON, files and trusted commands.
- Paired task repetitions, fixture workspaces, hierarchical-bootstrap
  intervals, per-task regressions and token/latency comparisons.
- Optional independent gate for legacy Improve, with composition re-probe.
- Non-overwriting filesystem version allocation, results.json/report.html,
  command-adapter contract and regression tests.
- The bundled command smoke test uses a synthetic program and cannot
  demonstrate improvement on real agents.
