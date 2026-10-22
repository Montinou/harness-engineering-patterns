# Changelog

## 0.3.0 — Harness experimentation layer

### Fixes

- Validate a composition before retaining it, and preserve rejected evidence.
- Prevent model judges from rewarding instructions that did not improve outputs.
- Store full answers, reject nonfinite judge scores and redact echoed API secrets.
- Prevent legacy version overwrites and serialize concurrent library writers.
- Account for all exploratory executor probes, not only the adopted probe.

### New

- Declarative experiment specs, frozen fixtures, paired repetitions and bounded
  concurrency with independent execution contexts.
- Objective output/artifact/command checks separated from backend diagnostics.
- Development-only finalist selection, task-clustered bootstrap intervals,
  exact sign tests, held-out validation and per-task regression gates.
- Token/latency constraints, failed-trial retention, synthetic-result safeguards,
  model-change detection and immutable provenance.
- SQLite experiment registry, content-addressed promotions and explicit export.
- Rule-based or custom proposer integration, command/Anthropic/custom executors,
  and JSON/Markdown/self-contained HTML reports.
- Offline demo, regression tests and CI configuration.

### Behavior changes

A legacy `improve` cannot overwrite a conflicting numeric library version.
An identical answer no longer receives a better judge input merely because its
harness was edited. These are intentional corrections, not compatibility bugs.

No Durable Harness integration or new third-party Python dependency is included.
