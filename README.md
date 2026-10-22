# Harness Engineering Patterns

Private collection of patterns and a working **Harness Workbench** for
**Agent = Model + Harness**. Model weights stay frozen; prompts, guardrails,
memory configuration, tools, subagents, skills, scripts and plugins are versioned.

## Workbench v0.3: controlled experiments

Two complementary loops are available:

- `harness-workbench`: Propose → Probe → Compose → Persist, for exploratory
  single-task changes. A composition must pass its own probe before adoption.
- `harness-experiments`: freeze inputs → repeated independent trials → select on
  development → validate one finalist on held-out tasks → regression/cost gates
  → immutable promotion. Objective acceptance tests, not harness adherence,
  decide whether a candidate wins.

No Durable Harness dependency, hosted service, database server, or new Python
package dependency is required. Python 3.10+ and the standard library are enough.

### Install and test

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

Without installing, use `python -m harness_workbench` and
`python -m harness_workbench.experiments` from the repository root.

### Run the complete offline experiment demo

```bash
python -m harness_workbench.experiments run \
  --config examples/experiments/sort-demo.json \
  --root .harness-experiments \
  --allow-commands
```

This runs a controlled sorting program, with a numeric-sort candidate and a
negative control that merely claims verification. It uses no API keys and makes
no model calls. It is explicitly **synthetic**: even if its statistical gates
pass, it cannot promote a production harness. The CLI writes JSON, Markdown and
a standalone HTML report, and prints the experiment ID.

### Inspect results and export accepted harnesses

```bash
harness-experiments list --root .harness-experiments
harness-experiments report --root .harness-experiments --experiment-id EXPERIMENT_ID
harness-experiments export --root .harness-experiments \
  --experiment-id EXPERIMENT_ID --out accepted-harness.json
```

Export requires an actually accepted experiment and refuses to overwrite a file.
An experiment never edits the source harness or a production configuration.

### Generate hypotheses from development tasks

```bash
harness-experiments propose \
  --config examples/experiments/sort-demo.json \
  --k 1 --out proposed-experiment.json
```

This uses the existing rule-based proposer and returns a self-contained config.
The Python API also accepts `LLMProposer` and externally generated edits. Neither
reference answers nor validation/regression cases are passed to the proposer.
Generation is a hypothesis-building step, not proof of improvement.

### Keep using the exploratory loop

```bash
harness-workbench improve \
  --harness examples/workbench/base_research_harness.json \
  --task "Fact-check a claim with primary sources and counterevidence" \
  --domain research --root .
```

The mock executor is deterministic and offline. `--provider anthropic` uses the
existing Messages API backend; configure `ANTHROPIC_API_KEY` and override model
IDs with `--solver-model` / `--improver-model` as needed. Its judge now sees only
the task, constraints and full answer, **not the candidate harness**. This is
still a model judgment, not an objective correctness oracle.

The Anthropic backend remains a **single-turn text probe**, not a live tool loop.
For real agent tools/workspaces, implement the experiment command protocol or
inject a Python trial executor. Explicit `--allow-network` is required for
Anthropic experiments; `--allow-commands` is required for local commands.
Local working directories are **not security sandboxes**.

## Documentation

- [Workbench architecture and migration](docs/workbench.md)
- [Experiment configuration, adapters, metrics and limitations](docs/experiments.md)
- [Architecture decision: experimentation layer](docs/adr/0001-experimentation-layer.md)
- [Implementation and verification record](docs/verification.md)
- [Changelog](CHANGELOG.md)

## Historical planning and research documents

[`BUILD_INSTRUCTION.md`](BUILD_INSTRUCTION.md) is the archived v1
implementation request, **not** a current task list.
The pre-v0.3 roadmaps in [research](research/) and conceptual
[application mappings](applications/quoth2-and-circuito.md) are preserved
for provenance and now explicitly labeled historical. Current behavior and
limitations are documented under [Documentation](#documentation).

## Research and patterns

`docs/`, `patterns/`, `references/`, `research/` and `applications/` retain the
research notes, pattern sketches and mappings to Quoth-2 and Circuito Ontológico.
`examples/` retains the original standalone demonstrations. Research claims in
those notes are not benchmark results produced by this workbench.

| Pattern | Existing research reference |
|---|---|
| Harness Learning | `patterns/harness-learning.md` |
| Harness² | `patterns/harness2.md` |
| Agent-Reflex | `patterns/agent-reflex.md` |

See `research/SELF_IMPROVING_HARNESSES.md` for the original design motivation.

*Private repository. No production harness, model subscription or main-branch
configuration is changed by running an experiment.*
