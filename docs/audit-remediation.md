# Audit remediation

Map of the audit findings to changes and tests. The full A1-A12 table is completed in the final documentation task;
this file currently records the pre-existing test expectations that new semantics changed.

## Expectation changes in pre-existing tests

| Test | Change | Reason |
|---|---|---|
| `tests/unit/test_run_experiment_phases.py::test_legacy_manifest_runs_all_variants_and_is_exploratory_only` | `payload["schema_version"]` is now `2` (was `1`) and `payload["manifest_schema_version"]` is asserted to be `1`. | T27: the top-level `schema_version` of `results.json` names the layout of the results file (2: decision statuses, per-trial execution/evaluation statuses, provenance). It no longer mirrors the manifest's version for a legacy v1 manifest, which moves to the new key `manifest_schema_version`. Existing keys are kept; one key was added. |
