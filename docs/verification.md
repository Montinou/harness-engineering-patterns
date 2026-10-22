# Verification and documentation status — experimental-evaluation-layer

**Reviewed branch:** feat/experimental-evaluation-layer
**Reviewed implementation commit:** fcb9dec86cdd747df76f1eebd76c31bbef2a3597
**Package version:** 0.2.0 (pyproject.toml and harness_workbench/__init__.py)
**Documentation alignment:** October 22, 2026.

This record covers the branch above, **not**
feat/harness-experimentation-layer (a separate implementation). Documentation
changes do not demonstrate correctness beyond the specific evidence below.

## Verified CI evidence

GitHub Actions workflow: Harness Workbench Tests

- [Run 39721851234](https://github.com/Montinou/harness-engineering-patterns/actions/runs/39721851234)
- Source commit: fcb9dec86cdd747df76f1eebd76c31bbef2a3597
- GitHub-reported conclusion: **success**.
- Python tested by this branch's workflow: **3.12**.
- Log: **36 tests passed** (unittest discovery).
- The offline synthetic command-experiment smoke step also succeeded.

A second successful workflow associated with the same head was reported as
run 39721848274. Neither a green suite nor a synthetic smoke test validates
real-model performance improvements or production security boundaries.

The package declares Python >=3.10, but the workflow at the reviewed commit
only tests 3.12. Do not present 3.10/3.13 support as CI-verified on this branch.

## Implementation boundaries checked against source

- The original Improver probes individual candidates, re-probes a combined
  harness, and may reject that composition; an optional promotion_gate can
  also reject it. Without an independent validation spec, it uses a behavioral
  model/heuristic score that is not an objective oracle.
- The standalone experiment command can run repeated variant comparisons
  with independent acceptance checks and produces results.json and report.html.
  Its recommendation is not an automatic Git merge or production deployment.
- The legacy Store avoids overwriting existing version files by allocating a
  different numeric version. Shared index.json and Markdown lessons remain
  nontransactional when concurrent processes write to the same root.
- A fresh local workspace/run ID is allocated per trial; whether sessions,
  external memory and provider context are truly reset is adapter-specific.
- Real tool execution requires an external command-aware agent integration.
  The built-in Anthropic probe is text-only; the bundled demo is synthetic.
- There is no durable execution/resume, distributed job queue, process-tree
  containment guarantee, sandbox isolation, or monetary billing enforcement.

## Not established by this verification

- Live Anthropic API responses, model-weight stability across aliases, or fees.
- Empirical improvements on Claude Code, Codex, OpenCode or other real agents.
- External service/data isolation and adversarial command security.
- Generalization to new benchmark families; held-out dataset management
  remains the caller's responsibility.
- Full statistical validity and freedom from evaluation bias under all task
  distributions. These require a separate independent research audit.

## Documentation maintenance

This review synchronized README.md, docs/workbench.md, docs/experiments.md,
examples/README.md and marked BUILD_INSTRUCTION.md and pre-implementation
research plans as historical. Application notes remain separate proposals.
All statements are intentionally limited to the correct feature branch.

See [experiments.md](experiments.md) and [workbench.md](workbench.md).
