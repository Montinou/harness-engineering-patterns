# Harness Workbench — Current Architecture

**Branch:** feat/experimental-evaluation-layer
**Source revision described:** 80134da (the documentation commit after it changes no Python).
**Package version:** 0.2.0 in pyproject.toml and harness_workbench/__init__.py.
**Related:** [audit remediation map](audit-remediation.md), [ADRs](adr/README.md), [experiments](experiments.md).

This branch extends the original Workbench with opt-in controlled experiments.
It does not rename the package, train model weights, or depend on Durable Harness.
The original core and the new experimentation layer are distinct.

## Two complementary paths

### Exploratory Propose → Probe → Compose → Persist

- Input: one Task, one structured Harness, and an Executor.
- A proposer generates up to k invariant and k domain edits.
- The executor probes the unchanged base and each candidate individually.
- Individually qualifying edits are merged and the **combined harness is
  executed again**. If the combined score does not beat the base by the
  configured margin (default 0.05), the base is kept.
- An optional promotion_gate callback can veto the combined harness even when
  its behavioral score improved.
- Persist writes task, candidates, probe reports, lessons, version lineage and
  the adopted harness.

A legacy Improve invocation without an independent gate uses a mock or LLM-judge
*behavioral* score. This can be useful for generating hypotheses but must not
be interpreted as scientific evidence of an objective performance improvement.

### Controlled experiment (optional)

The harness_workbench/experiments package provides:

- A versioned manifest (schema v2) with `mode: exploratory | confirmatory`, task splits
  (`development`, `validation`, `regression`), families and per-task controls. A manifest without
  `schema_version` is legacy: exploratory only, convertible with `migrate-manifest`.
- Repetitions of multiple variants over the same tasks, with a paired seed and a fresh workspace, `HOME` and
  temporary directory per trial.
- Independent acceptance checks (`exact`, `contains`, `regex`, `json_equal`, `file_*`, `command` and `oracle`)
  evaluated by the supervisor from a frozen grader bundle outside the solver's workspace. The solver gets only a
  public request (the eight executable harness components and the task text).
- Explicit statuses: execution, evaluation and decision statuses are separate enums, and cost and latency are
  nullable measurements with provenance.
- A confirmatory protocol: one finalist chosen on development data only and frozen, a holdout reserved per task
  content fingerprint, and one recorded decision from a bootstrap over independent units (families) with
  non-inferiority on the regression split (profile `binary_v1`, [ADR 0002](adr/0002-statistical-protocol.md)).
- A separate experiment_id, individual trial ids, per-trial persistence in the SQLite store, and
  report.html/results.json with provenance and drift detection.
- Candidate recommendations only when the decision is `eligible`; no automatic production deployment or merge.

The standalone experiment command **recommends** a candidate; it does not
modify a repository branch or auto-promote a production harness. By contrast,
the legacy Improve command **does** persist an adopted harness. With
`--validation-spec <confirmatory manifest>`, Improve confirms the final composed
harness once and persists it only through an `eligible` decision; without that
flag, it only uses the behavioral path.

Held-out data management is part of the confirmatory protocol, with a local
scope: the exposure registry knows evaluations recorded in one library, not
those run elsewhere. See [experiments.md](experiments.md) for the protocol,
reproducibility, limits and the command adapter contract.

## Data model and extension interfaces

The structured Harness, defined in harness_workbench/core/harness.py, contains
eight editable components: system prompt, guardrails, memory config, tools,
subagents, skills, scripts and plugins. Candidates have provisional versions;
adopted versions keep parent pointers and lessons.

The existing Executor protocol is:

~~~python
execute(harness: Harness, task: Task) -> BehavioralReport
~~~

BehavioralReport includes a diagnostic score, traces, failure codes, token
estimate/usage, latency and an observable outputs mapping. The new experiment
runner can also use an adapter implementing:

~~~python
execute_case(harness: Harness, task: Task, context: RunContext) -> BehavioralReport
~~~

This extended contract provides an isolated local workspace and recorded seed.
Existing execute-only adapters still work, but do not receive the workspace or
seed and cannot claim controlled external session state merely because the
runner supplies a new run ID.

### Proposers

- RuleBasedProposer reads failure codes and emits deterministic structured edits.
- LLMProposer delegates proposal generation to a text completer that returns
  JSON edits.
- FileProposer accepts prepared structured edits from a file.

Supported operations: append_prompt, add_guardrail, set_memory, add_tool,
add_subagent, add_skill, add_script, add_plugin and bundle. No proposer training
occurs here. Adding a tool to the JSON does not make that tool executable unless
the chosen real-agent adapter implements it.

### Backends

- mock: deterministic structural scoring, offline, no actual task solution.
- anthropic: model called through the Anthropic Messages API; Sonnet 5.5
  solver and Haiku 5.5 proposer/judge are **defaults**. The harness is rendered
  into a system prompt. No live tool runtime exists in this backend.
- command: launches a trusted external program for experimental trials; that
  program must apply the harness configuration and emit observable outputs. It
  runs on one of two execution backends chosen by the manifest's
  `environment.mode`: `trusted_local` (a bounded child process with a minimal
  environment; **not** a security sandbox, and the solver can read host files)
  or `sandboxed` (a Docker container behind a capability self-test; it refuses
  to run rather than fall back to the host, and its integration tests have not
  run on the author's machine). See [experiments.md](experiments.md#execution-backends-and-their-guarantees).

The Anthropic judge receives the candidate harness in the existing exploratory
path, so its score may reflect instruction adherence and should not substitute
for independent task acceptance checks. Provider model aliases are not proof of
unchanged weights between runs.

## Composition and optional objective validation

For Improve, the candidate-composition sequence is:

~~~text
baseline probe → candidate probes → combine individually qualifying edits
    → re-probe combined harness → reject/keep by behavioral margin
    → [optional --validation-spec acceptance experiment]
    → persist adopted version or keep baseline
~~~

The optional validation spec must be a **confirmatory** manifest (an exploratory
or legacy one is refused before anything runs, exit 2). If it specifies an
external command executor, that executor is used for the acceptance trials even
when --provider mock generated the candidate. The gate runs once, on the final
composed harness against its baseline, and the returned evidence is stored
independently under experiments/<id>. Only a persisted `eligible` decision
approves, and the adopted version is promoted by that decision id.

**Important:** the final adoption is determined by adopted version/report and
validation eligibility, not by individual ADOPT lesson strings. Older diagnostic
lesson strings for individually selected edits can remain in the record even
when the combined harness is rejected. The objective gate is opt-in, not global.
In `sequential` mode with a gate, only the base and the final harness are
persisted, the final as a direct child of the base.

## Persistence

The source of truth is the SQLite database `library/workbench.sqlite3`
(standard library `sqlite3`, short write transactions, a bounded busy timeout
that raises `StoreBusyError`). It holds harness versions and lineage,
experiments, trials, frozen selections, holdout exposure, decisions and
promotions ([ADR 0003](adr/0003-persistence-and-migration.md)). The files below
are **exported views**, each written to a temporary file and replaced
atomically after the commit:

- runs/<timestamp>-<harness-id>-v<adopted-version>[-suffix]/
- library/harnesses/<harness-id>/<version>.json
- library/EXPERIENCE.md
- library/lessons/<domain>.md
- library/index.json

Identity is `(harness_id, version)`. The base harness is registered once and
never renumbered; the same identity with different executable content is a
`LineageConflict`; children are allocated as the next decimal version inside a
transaction, so concurrent processes starting from the same base keep one
canonical base (tested with 6 and 12 processes). A promotion is tied to a
decision id and is idempotent.

- If an export fails after the commit, `harness-workbench export-library
  --root <dir>` regenerates every view from the database without creating
  versions.
- A library written by an older checkout (JSON versions the database has not
  seen) is refused with `MigrationRequired` until `harness-workbench
  migrate-library --root <dir>` has run. It supports `--dry-run`, makes
  `<name>.bak-<UTC stamp>` backups of `index.json` and the database, skips rows
  already imported, reports differing content as conflicts and never rewrites
  or deletes the legacy files.
- A process interruption leaves an experiment `running` with its completed
  trials and no decision; `harness-workbench experiment --root <dir>
  --list-incomplete` lists it. Automatic resume is not implemented. The
  experiment's results.json and report.html are written when it finishes.

This is a single-host design. SQLite coordinates processes on one machine; it
is not a distributed store and a network filesystem may not honor its locking.
The exported files are replaced one by one, so a crash between two exports
leaves views that disagree until `export-library` runs, and hashes detect
accidental inconsistency, not an administrator who can alter both database and
hashes.

## CLI

~~~bash
# Original exploratory commands
harness-workbench propose --harness examples/workbench/base_research_harness.json --task "Fact-check a claim"
harness-workbench probe --harness examples/workbench/base_research_harness.json --task "Fact-check a claim"
harness-workbench improve --harness examples/workbench/base_research_harness.json --task "Fact-check a claim" --root .
harness-workbench report --harness-id research-agent

# Controlled experiment and offline report (exploratory smoke test; never promotable)
harness-workbench experiment --provider command --spec examples/experiments/local-command.json --root .
# Confirmatory synthetic demo (eligible; a few minutes)
harness-workbench experiment --provider command --spec examples/experiments/confirmatory-demo.json --root .
harness-workbench experiment --root . --list-incomplete
harness-workbench migrate-manifest --spec old-manifest.json --out new-manifest.json

# Library maintenance
harness-workbench export-library --root .
harness-workbench migrate-library --root . --dry-run

# Apply independent confirmatory criteria before accepting a composed candidate
harness-workbench improve --provider mock --harness examples/workbench/base_research_harness.json --task "Fact-check a claim" --domain research --validation-spec examples/experiments/confirmatory-demo.json --root .
~~~

No real model calls are made by the demonstration agent. The package runs on
Python 3.10+ with no required third-party runtime dependencies. The CI workflow
defines a 3.10/3.12/3.13 matrix plus no-install, sandbox isolation, portability
and smoke jobs; no run of it exists yet for this work (see
[verification.md](verification.md)).

## Research and scope

This implementation is inspired by Harness Learning, Harness² and Agent-Reflex
but is not a full reproduction of their reported results. The original design
brief and dated research roadmaps are archived as historical documents.
See [docs/verification.md](verification.md) for evidence and non-verified claims,
[docs/audit-remediation.md](audit-remediation.md) for the audit map and remaining limitations,
and [application proposals](../applications/quoth2-and-circuito.md) for
conceptual mappings to other independent systems.
