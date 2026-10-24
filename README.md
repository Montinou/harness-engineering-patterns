# Harness Engineering Patterns

> **Current branch: feat/experimental-evaluation-layer (documentation aligned October 24, 2026).**
> This branch adds experimental evaluation to the existing Workbench **package
> version 0.2.0**; it does not bump the published package version to 0.3.0.
> This is a separate implementation from feat/harness-experimentation-layer.
> It carries the remediation of an external audit (findings A1-A12): see the
> [audit remediation map](docs/audit-remediation.md), the
> [architecture decision records](docs/adr/README.md),
> [experiment contracts](docs/experiments.md) and
> [verification evidence](docs/verification.md). Historical build/research
> briefs are not active specifications.


Private collection of practical patterns and references for **Agent = Model + Harness**.

Focused on improving reliability, adaptability and auditability of agentic systems without retraining the base model.

## Core idea

The model reasons. The **harness** is everything else: tools, context assembly, state/memory, routing, guardrails, verification loops, orchestration and the executable programs that organize model calls.

Changing the harness alone can move performance by tens of points. Recent work (late 2026) shows how to *learn* and *recursively improve* harnesses.

## Contents

- `docs/` — Pattern write-ups and references
- `patterns/` — Concrete pattern descriptions + pseudocode sketches
- `references/` — Key papers and repos (as of Oct 2026)
- `applications/` — Mapping to Quoth-2, Circuito Ontológico, Panelite, etc.

## Key papers (Oct 2026)

| Pattern | Paper | Code |
|---------|-------|------|
| Harness Learning | arXiv:2609.35738 | (paper) |
| Harness² | arXiv:2610.14224 | github.com/google-research/harness2 |
| Agent-Reflex | arXiv:2610.17953 | github.com/RUC-NLPIR/Agent-Reflex |

## Harness Workbench

Python package for the self-improvement loop in `research/SELF_IMPROVING_HARNESSES.md`:

**Propose → Probe → Compose → Persist**

Offline by default: a rule-based proposer and a deterministic mock executor. No API keys and no extra dependencies.

To run it on Anthropic, set a key. The default solver identifier is Sonnet 5.5;
Haiku 5.5 proposes edits and judges each probe. Neither model is trained by
this workbench, but keeping a provider model alias fixed does not prove its
weights remain unchanged over time. A typical k=1 improvement with two
candidates makes about four solver and five improver/judge calls (depending
on which candidates are generated). Behavioral LLM-judge scores are not
independent evidence of task correctness.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
harness-workbench improve \
  --provider anthropic \
  --harness examples/workbench/base_research_harness.json \
  --task "Fact-check a claim with primary sources and counterevidence" \
  --domain research \
  --root .
```

Override models with `--solver-model` / `--improver-model`, or `HARNESS_SOLVER_MODEL` / `HARNESS_IMPROVER_MODEL`. The defaults are `claude-sonnet-5-5` and `claude-haiku-5-5`. This probe has no live tool runtime: tool guidance is in the prompt, and Haiku scores whether Sonnet followed the harness.

### Install

```bash
python -m pip install -e .
harness-workbench --help
```

From a checkout, without installing:

```bash
PYTHONPATH=. python -m harness_workbench --help
```

### Improve a harness

```bash
harness-workbench improve \
  --harness examples/workbench/base_research_harness.json \
  --task "Fact-check a claim with primary sources and counterevidence" \
  --domain research \
  --root .
```

That writes:

- `runs/<timestamp>-research-agent-v<version>[-suffix]/` — task, candidate probes, reports and lessons
- `library/workbench.sqlite3` — the source of truth for version identity and lineage; the files below are exported views
- `library/harnesses/research-agent/<version>.json` — versions with parent pointers
- `library/EXPERIENCE.md` — evidence-backed lessons (the improvement library)

A library written by an older checkout (JSON versions with no database) is refused until
`harness-workbench migrate-library --root .` has run; `export-library` regenerates the JSON views.

### Other commands

```bash
harness-workbench propose \
  --harness examples/workbench/base_research_harness.json \
  --task "Fact-check a claim" --domain research

harness-workbench probe \
  --harness examples/workbench/base_research_harness.json \
  --task "Fact-check a claim" --domain research

harness-workbench report --harness-id research-agent
```

`--edits-file edits.json` feeds structured edits from an external model into `propose` or `improve`. In Python, pass a completer to `LLMProposer` or any `execute()` implementation to `Improver`.

Architecture: [docs/workbench.md](docs/workbench.md).

### Tests

```bash
python -m unittest discover -s tests -v
```

The scripts in `examples/` stay standalone sketches. The workbench is the versioned implementation of the same loop.


## Controlled Harness Experiments (feature branch)

The experimental evaluation layer executes *fresh, paired repetitions* of
multiple harness variants against the same tasks and fixtures and decides, under
a pre-registered statistical protocol, whether one candidate is **eligible** for
promotion. Unlike the original behavioral probe, the objective score comes from
independent acceptance checks (answer assertions, files, commands/tests, and
`oracle` checks whose verdict the supervisor computes) that run outside the
solver's workspace from a frozen grader bundle.

- New CLI: `harness-workbench experiment --provider command --spec <manifest> --root .`. A manifest is
  `mode: exploratory` (runs every variant on every task, never promotable) or `mode: confirmatory`
  (development selection of one finalist, a frozen plan, a reserved holdout, one decision). A manifest without
  `schema_version` is legacy and exploratory; `harness-workbench migrate-manifest` converts it.
- Decisions are explicit: `eligible`, `rejected`, `insufficient_evidence`, `invalid_experiment` or
  `exploratory_only`; trials carry their own execution and evaluation statuses. Unknown cost or latency, an
  evaluator error, too few independent units or a used holdout never produce `eligible`.
- Optional adoption gate: `harness-workbench improve ... --validation-spec <confirmatory manifest>` confirms the
  final composed harness once and promotes it by decision id, or keeps the baseline. An exploratory spec is
  refused before anything runs.
- Backends: `trusted_local` (a bounded child process; **not** a sandbox) and `sandboxed` (Docker, with a
  capability self-test; it refuses to run rather than fall back to the host). Provider options: `--provider
  command` (external tool-enabled agent process), `anthropic` (text-only responses and LLM-judge diagnostics),
  `mock` (offline structural simulation).
- Evidence lives in a SQLite database under `library/` with exported JSON views (`export-library`,
  `migrate-library`, `experiment --list-incomplete`), plus `results.json` and a standalone `report.html` per
  experiment, with provenance (implementation hash, backend, returned model status) and drift detection.
- The workbench validates a *composed* harness before adopting it, and version identities are allocated
  transactionally: the base is never renumbered and concurrent processes keep one canonical base.

**Documentation:** [docs/experiments.md](docs/experiments.md) (contracts, protocol, limits),
[docs/audit-remediation.md](docs/audit-remediation.md) (A1-A12 map, changed test expectations, what was not run),
[docs/statistical-validation.md](docs/statistical-validation.md) and [docs/adr/](docs/adr/README.md).

This system records seeds and environment fingerprints, but does not guarantee
LLM determinism. Demo and simulation results are synthetic, not benchmark claims.
There is no Durable Harness dependency or automatic merge/promotion.

## Status and boundaries

This branch retains Workbench **package 0.2.0** and adds an experimental runner,
an optional independent acceptance gate for Improve, adapter support, statistics
and HTML/JSON reports. These are feature-branch capabilities, not proof of a
stable v0.3 release.

Remaining limitations, stated plainly:

- **No real-model validation.** Every calibration, simulation and demo result is synthetic; the
  confirmatory demo's 136 tasks are template variants whose units are not semantically independent, and its
  agent is a deterministic script.
- `trusted_local` is not a sandbox and lets the solver read host files. Process groups do not contain
  descendants that start new sessions. The Docker sandbox and the install/portability checks have only run
  where Docker and `setuptools` exist; they were not run on the author's machine.
- An `oracle` verdict can be forged by candidate code that runs in the grader process and writes the observation
  late ([ADR 0001](docs/adr/0001-evaluator-trust-boundary.md)); this is not fixed in code.
- The holdout exposure registry is local to one library. Hashes do not prove semantic independence. The
  implementation hash covers the package's Python files, not the interpreter or an adapter's dependencies.
- The existing Anthropic backend is a text-only solver plus an LLM judge, **not** a live tool runtime. The
  command adapter launches trusted external processes, and the external adapter must apply the harness
  configuration itself. SQLite coordinates processes on one host only; there is no distributed storage and no
  automatic resume.

Local verification, recorded at c7a923e (the commit that added the CI workflow; only documentation changed
after it): the full suite passed on Python 3.10.19, 3.12.12 and 3.13.14 (619 tests, 8 skipped: the Docker
sandbox tests and the opt-in portability script, which were not run locally). No GitHub
Actions run exists for this work yet; the 36-test, Python 3.12 run in
[the verification record](docs/verification.md) belongs to an older commit.

## Historical sources

[BUILD_INSTRUCTION.md](BUILD_INSTRUCTION.md) is an archived v1 work request.
The original [research roadmaps](research/) are dated research snapshots, not
the current implementation backlog. [Application mappings](applications/quoth2-and-circuito.md)
describe ideas for other projects, not integrations verified in this branch.

Refer to [docs/workbench.md](docs/workbench.md) and
[docs/experiments.md](docs/experiments.md) for implemented behavior.

---
*Private repo. For internal use on agent reliability and Quoth-2 evolution.*
