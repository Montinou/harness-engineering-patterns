# Branch Change Log

## feat/experimental-evaluation-layer — documentation alignment (2026-10-22)

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

- Independent acceptance checks over outputs, JSON, files and trusted commands.
- Paired task repetitions, fixture workspaces, hierarchical-bootstrap
  intervals, per-task regressions and token/latency comparisons.
- Optional independent gate for legacy Improve, with composition re-probe.
- Non-overwriting filesystem version allocation, results.json/report.html,
  command-adapter contract and regression tests.
- The bundled command smoke test uses a synthetic program and cannot
  demonstrate improvement on real agents.
