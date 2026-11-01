# Verification and documentation status — experimental-evaluation-layer

**Reviewed branch:** feat/experimental-evaluation-layer
**Current implementation described:** commit 80134da (the documentation commit that follows it changes no Python).
**Earlier reviewed implementation commit (historical):** fcb9dec86cdd747df76f1eebd76c31bbef2a3597
**Package version:** 0.2.0 (pyproject.toml and harness_workbench/__init__.py)
**Documentation alignment:** October 24, 2026 (audit remediation). The previous alignment was October 22, 2026.

This record covers the branch above, **not**
feat/harness-experimentation-layer (a separate implementation). Documentation
changes do not demonstrate correctness beyond the specific evidence below.

## CI evidence for the current work

GitHub Actions run 41036918917 (`pull_request`, workflow `.github/workflows/harness-tests.yml`) on head
`7775390`, the last commit of PR #1 before its squash merge (`46e3413` on `main`). All seven jobs concluded
`success`: `core` on 3.10, 3.12 and 3.13, `no-install`, `isolation`, `portability` and `smoke`. The first run on
`91c29d1` failed only `portability`, at its precondition guard (the runner image ships `/usr/bin/python`); the job
now removes that alias before asserting its absence (`3d064da`).

A job that skips (Docker unavailable, `setuptools` missing) is reported as
**not run**, not as passed.

## Local evidence (not CI)

Each line names the interpreter that produced it. `python3` on the author's machine is Homebrew 3.15.0, which is
**outside** the CI matrix; matrix versions were run with `uv run --python <v> --no-project`.

| What | Interpreter | Result |
|---|---|---|
| Full suite (`python -m unittest discover -s tests`), recorded by T29 at c7a923e; only documentation changed afterwards | 3.10.19, 3.12.12, 3.13.14 | each `OK`, 619 tests, skipped=8 (7 Docker sandbox tests and the opt-in portability script, not run) |
| Focused run per audit finding A1-A12 (commands in [audit-remediation.md](audit-remediation.md#local-commands)) | 3.12.12 | all `OK` (A11 skipped=1) |
| `tests.integration.test_confirmatory_demo` | 3.12.12 | 5 tests `OK` |
| `tests.integration.test_sandbox_backend`, `tests.unit.test_docker_backend_argv`, `tests.unit.test_sandbox_reclaim` | 3.12.12 | 60 tests `OK`, skipped=7: no Docker daemon, so the real sandbox tests **did not run** |
| The 36 tests that predate this work (`tests/test_experiments.py`, `tests/test_workbench.py`, `tests/test_anthropic.py`) | 3.15.0 | 36 tests `OK` |
| `tests.integration.test_sandbox_backend tests.integration.test_portability` | 3.15.0 | 9 tests, `OK (skipped=8)`: the always-on foreign-cwd test passed, the 7 sandbox tests and the portability script did not run |
| `harness-workbench experiment ... confirmatory-demo.json` | 3.15.0 | decision `eligible`, estimate +1.0000, lower bound +1.0000, 50 validation and 80 regression units, labeled "Synthetic data" |
| `harness-workbench experiment ... local-command.json` | 3.15.0 | decision `exploratory_only` ("mode is exploratory") |

The confirmatory demo number comes from a deterministic synthetic agent and template-variant tasks. It shows that
the pipeline runs end to end; it says nothing about any model.

Calibration and final validation of the statistical profile `binary_v1` (worst null `upper_95` 0.0553, target power
`lower_95` 0.8359, seed 202) are **synthetic**, were produced on Python 3.14 (local 3.14.7 and Modal 3.14.2), and
followed a dated deviation from the pre-registered plan: see [statistical-validation.md](statistical-validation.md)
and [ADR 0002](adr/0002-statistical-protocol.md). The auditor's own figures (34/400, 12/400, 47/300) are the
auditor's; the audited commit reproduces them with the auditor's script.

## Historical CI evidence (earlier commit, not evidence for the current code)

GitHub Actions workflow: Harness Workbench Tests, **at commit fcb9dec86cdd747df76f1eebd76c31bbef2a3597**. That
commit predates the audit remediation: the numbers below describe the older code and an older workflow.

- [Run 39721851234](https://github.com/Montinou/harness-engineering-patterns/actions/runs/39721851234)
- Source commit: fcb9dec86cdd747df76f1eebd76c31bbef2a3597
- GitHub-reported conclusion: **success**.
- Python tested by that workflow: **3.12**.
- Log: **36 tests passed** (unittest discovery).
- The offline synthetic command-experiment smoke step also succeeded.

A second successful workflow associated with the same head was reported as
run 39721848274. Neither a green suite nor a synthetic smoke test validates
real-model performance improvements or production security boundaries.

The package declares Python >=3.10, but the workflow at that commit only tested
3.12. The current workflow's 3.10/3.12/3.13 matrix has since run: see
[CI evidence for the current work](#ci-evidence-for-the-current-work).

## Implementation boundaries checked against source

Checked against the code at 80134da:

- The original Improver probes individual candidates, re-probes a combined
  harness, and may reject that composition. Without an independent validation
  spec it uses a behavioral model/heuristic score that is not an objective
  oracle. With `--validation-spec <confirmatory manifest>`, the final composed
  harness is confirmed once and persisted only through an `eligible` decision.
- The standalone experiment command runs repeated variant comparisons with
  independent acceptance checks and produces results.json and report.html. Its
  recommendation is not an automatic Git merge or production deployment. Only a
  `confirmatory` experiment can end `eligible`; a legacy or exploratory manifest
  ends `exploratory_only`.
- Execution, evaluation and decision outcomes are separate statuses. An
  evaluator that cannot run is an `evaluation_error`, never a pass; unknown
  cost or latency blocks the decision.
- The grader bundle is frozen and evaluated outside the solver's workspace;
  `oracle` checks compare a value with `expected` in the supervisor, not in the
  grader process.
- Version identity and lineage live in SQLite (`library/workbench.sqlite3`),
  the base is never renumbered, and the JSON files are exported views
  regenerated by `export-library`; `migrate-library` imports a legacy library.
  Shared Markdown lessons and exported files are replaced one by one, not in one
  transaction.
- A fresh local workspace/run ID is allocated per trial; whether sessions,
  external memory and provider context are truly reset is adapter-specific.
- Real tool execution requires an external command-aware agent integration.
  The built-in Anthropic probe is text-only; the bundled demos are synthetic.
- There is no durable execution/resume, distributed job queue, or monetary
  billing enforcement. `trusted_local` has no isolation guarantee and process
  groups do not contain descendants that start a new session.

## Not established by this verification

- Live Anthropic API responses, model-weight stability across aliases, or fees.
- Empirical improvements on Claude Code, Codex, OpenCode or other real agents;
  every result in this repository is synthetic.
- External service/data isolation and adversarial command security. The Docker
  sandbox tests pass in CI (`isolation` job) but have not been exercised on the author's machine; they cover
  the backend's capability self-test, not adversarial attacks.
- **An `oracle` check's verdict can be forged** by candidate code that runs in the
  grader process and writes the observation late (for example from an `atexit`
  hook), because the result directory is writable by the grader and `sandboxed`
  mounts it read-write too ([ADR 0001](adr/0001-evaluator-trust-boundary.md)). It
  is not fixed in code, so a confirmatory result for a task that runs candidate
  code inside the grader process is not proof against a hostile candidate.
- Generalization to new benchmark families. The holdout exposure registry covers
  one library only, hashes do not prove semantic independence of tasks, and the
  confirmatory demo's units are template variants.
- Full statistical validity and freedom from evaluation bias under all task
  distributions: the profile is calibrated on a synthetic generator, its power is
  shown for one target scenario at 50 validation and 80 regression units, and
  its regression tolerance is 0.10. These require a separate independent audit.
- Wheel and editable installs on a clean machine, and Linux, macOS and Windows
  portability beyond the local macOS runs above.

## Documentation maintenance

The October 24, 2026 review synchronized README.md, docs/workbench.md,
docs/experiments.md, examples/README.md and CHANGELOG.md with the code at
80134da, and added docs/audit-remediation.md (the A1-A12 map, the changed test
expectations, what was not reproduced and what was not run) next to the
architecture decision records in docs/adr/. The earlier alignment (October 22,
2026) had marked BUILD_INSTRUCTION.md and pre-implementation research plans as
historical. Application notes remain separate proposals. All statements are
intentionally limited to the correct feature branch.

See [experiments.md](experiments.md), [workbench.md](workbench.md) and
[audit-remediation.md](audit-remediation.md).
