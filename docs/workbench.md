# Harness Workbench — Current Architecture

**Branch:** feat/experimental-evaluation-layer
**Verified source revision:** fcb9dec86cdd747df76f1eebd76c31bbef2a3597
**Package version:** 0.2.0 in pyproject.toml and harness_workbench/__init__.py.

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

The new harness_workbench/experiments package provides:

- Repetitions of multiple variants over the same tasks, with a paired seed and
  fresh fixture workspace per run.
- Harness-blind acceptance checks over observable outputs/artifacts, including
  exact/JSON comparisons and trusted commands.
- Per-task scores, success/failure rates, tokens, latency, hierarchical-bootstrap
  confidence intervals and regression/budget conditions.
- A separate experiment_id, individual run_id values and report.html/results.json.
- Candidate recommendations; no automatic production deployment or merge.

The standalone experiment command **recommends** a candidate; it does not
modify a repository branch or auto-promote a production harness. By contrast,
the legacy Improve command **does** persist an adopted harness when its checks
pass. With --validation-spec, Improve first requires an objective acceptance
gate; without that flag, it only uses the behavioral path.

The experiment runner does not implement fully held-out dataset management;
the caller must reserve independent tasks when using results to choose edits.
See [experiments.md](experiments.md) for reproducibility, limits and command
adapter contracts.

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
  program must apply the harness configuration and emit observable outputs.
  This is **not** a security sandbox.

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

The optional validation spec uses the experiment manifest's tasks, variants
and external acceptance checks. If it specifies an external command executor,
that executor is used for the acceptance trials even when --provider mock
generated the candidate. The returned evidence is stored independently under
experiments/<id>.

**Important:** the final adoption is determined by adopted version/report and
validation eligibility, not by individual ADOPT lesson strings. Older diagnostic
lesson strings for individually selected edits can remain in the record even
when the combined harness is rejected. The objective gate is opt-in, not global.

## Persistence

The legacy Improve path writes:

- runs/<timestamp>-<harness-id>-v<adopted-version>[-suffix]/
- library/harnesses/<harness-id>/<version>.json
- library/EXPERIENCE.md
- library/lessons/<domain>.md
- library/index.json

A collision in a version filename triggers allocation of a new numeric child
version instead of replacing an existing file. Repeating Improve from the same
parent **does not overwrite** its previously saved child version.

This is a *filesystem-level protection*, not a fully transactional or
distributed storage system. Concurrent writers can race while updating the
shared JSON index and Markdown lessons. A process interruption may leave some
files written without a complete run record. Experiment reports are written on
completion; automatic resume from partial runs is not implemented.

## CLI

~~~bash
# Original exploratory commands
harness-workbench propose --harness examples/workbench/base_research_harness.json --task "Fact-check a claim"
harness-workbench probe --harness examples/workbench/base_research_harness.json --task "Fact-check a claim"
harness-workbench improve --harness examples/workbench/base_research_harness.json --task "Fact-check a claim" --root .
harness-workbench report --harness-id research-agent

# Controlled experiment and offline report
harness-workbench experiment --provider command --spec examples/experiments/local-command.json --root .

# Apply independent task criteria before accepting a composed candidate
harness-workbench improve --provider mock --harness examples/workbench/base_research_harness.json --task "Fact-check a claim" --domain research --validation-spec examples/experiments/local-command.json --root .
~~~

No real model calls are made by the demonstration agent. The package runs on
Python 3.10+ with no required third-party runtime dependencies. GitHub Actions
in this branch tests Python 3.12.

## Research and scope

This implementation is inspired by Harness Learning, Harness² and Agent-Reflex
but is not a full reproduction of their reported results. The original design
brief and dated research roadmaps are archived as historical documents.
See [docs/verification.md](verification.md) for evidence and non-verified claims,
and [application proposals](../applications/quoth2-and-circuito.md) for
conceptual mappings to other independent systems.
