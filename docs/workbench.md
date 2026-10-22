# Harness Workbench architecture — v0.3

The Python core remains provider-independent and standard-library only.
Existing imports and `propose`, `probe`, `improve`, `report` commands are retained.
The new `harness-experiments` CLI is an additional entry point, not a replacement.

## Layers

```text
Structured Harness + Task
        |
        +-- exploratory Proposer / Improver
        |      propose -> probe candidates -> compose -> probe composition
        |      -> keep only a validated composition -> persist evidence
        |
        +-- ExperimentSpec
               freeze harnesses, task splits, fixtures, checks and policy
               -> independent repeated trials
               -> objective evaluator, separate diagnostic report
               -> choose one development finalist
               -> held-out validation and regression tests
               -> accepted immutable harness object OR keep baseline
```

## Core model

`core/harness.py` defines the eight editable components and parent lineage.
`core/proposer.py` retains structured edits (`append_prompt`, `add_guardrail`,
`set_memory`, `add_tool`, `add_subagent`, `add_skill`, `add_script`, `add_plugin`,
`bundle`) and the rule, file and LLM proposer implementations.

`Executor.execute(harness, task) -> BehavioralReport` remains compatible.
`BehavioralReport` adds `output` (full observable answer) and `metadata` with safe
defaults. Existing backends that do not return output still work for exploratory
probes, but cannot establish objective answer quality in experiments.

## Corrected composition

Previously, individually successful changes were merged and kept even when the
combined harness performed worse. The composed harness is now executed and must
independently beat the baseline by the configured margin. Otherwise, the exact
base object is retained; proposed composition, report and rejection reasons are
still persisted. `SELECT` lessons become `ADOPT` only after that final check.
NaN/invalid scores cannot authorize adoption.

`StepRecord.total_probe_tokens` includes baseline, every candidate and the
composition, including rejected probes. The scope is explicit: legacy proposer
usage is not included because the existing completer protocol returns only text.

The legacy `parallel` mode means independent candidate comparisons against one
base; it does not suddenly make those old calls concurrent. Actual bounded
concurrency is provided by the new experiment runner's `max_workers` setting.

## Independent evaluation

The Anthropic judge is blind to the candidate system prompt/guardrails. It gets
the task, constraints and complete answer. Identical answers under different
harnesses therefore produce identical judge inputs. Its assessment is labeled
`blind_task_judge_not_objective`; it is not presented as external verification.

The experiment layer computes its own score from frozen acceptance checks over
outputs/artifacts. The backend diagnostic score remains in evidence but is not
used for selection or promotion. Hidden command checks run after the solver, in
a different temporary working directory. This is interface separation, not a
security boundary against malicious code running as the same OS user.

## Persistence and compatibility

Legacy `runs/` now uses UUID-suffixed IDs. The library writer uses a portable
SQLite writer lock and atomic file replacement for derived views. Existing
numeric version aliases are immutable: a conflicting `2.json` is rejected,
not overwritten. The attempted run's evidence remains available. Continue from
the adopted version, supply a distinct version, or use experiment variants for
branches. Existing libraries are not migrated or deleted automatically.

The experiment registry is separate: `.harness-experiments/experiments.sqlite3`.
Manifests, trials, evaluations and decisions are append-only through its API.
Variant IDs include a content hash; experiment/run/evaluation IDs are unique.
Accepted harnesses are content-addressed and carry an experiment-specific version
and parent references. Decisions and accepted artifacts are committed in one
SQLite transaction. JSON/Markdown/HTML reports are regenerable derived views.

## Extension points

`ExperimentRunner(..., executor=callable)` accepts a trial executor with
`(Harness, Task, Context) -> BehavioralReport`. Context supplies an isolated
working directory, run ID, repetition and paired seed. Built-in backends create
a fresh executor per trial; custom adapters must reset their external memory,
services, tools and sessions themselves and enforce their own deadlines.

`propose_development()` connects an existing proposer to development tasks only.
Individual proposals and their composition become independent hypotheses, each
subject to exactly the same experiment gates. It never forwards held-out cases
or acceptance answers to the proposer.

See [experiments.md](experiments.md) for the complete execution contract.
