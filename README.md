# Harness Engineering Patterns

> **Current branch: feat/experimental-evaluation-layer (October 22, 2026).**
> This branch adds experimental evaluation to the existing Workbench **package
> version 0.2.0**; it does not bump the published package version to 0.3.0.
> This is a separate implementation from feat/harness-experimentation-layer.
> See [experiment contracts](docs/experiments.md) and
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
- `library/harnesses/research-agent/<version>.json` — versions with parent pointers
- `library/EXPERIENCE.md` — evidence-backed lessons (the improvement library)

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
multiple harness variants against the same tasks and fixtures. Unlike the
original behavioral probe, it uses harness-blind acceptance checks for
objective promotion evidence (answer assertions, files, commands/tests).

- New CLI: harness-workbench experiment --provider command --spec examples/experiments/local-command.json --root .
- Optional adoption gate: harness-workbench improve ... --validation-spec examples/experiments/local-command.json (fail-closed objective evaluation before persisting a new harness).
- Other backends: --provider anthropic (text-only responses and LLM-judge diagnostics), --provider mock (offline structural simulation).
- External command agents receive full harness/task JSON and independent fixture workspaces. The adapter is responsible for applying the specified harness.
- JSON evidence + standalone report.html; paired hierarchical bootstrap,
  task regressions, budgets and quality/cost/latency Pareto comparisons.
- The workbench now validates a *composed* harness before adopting it and
  allocates non-overwriting numbered versions for divergent branches (the shared index and Markdown lessons are not transactional across concurrent writers).

**Documentation:** [docs/experiments.md](docs/experiments.md).

This system records seeds and environment fingerprints, but does not guarantee
LLM determinism or container-level sandboxing. Demo results are synthetic,
not benchmark claims. There is no Durable Harness dependency or automatic
merge/promotion.

## Status and boundaries

This branch retains Workbench **package 0.2.0** and adds an experimental runner,
an optional independent acceptance gate for Improve, adapter support, statistics
and HTML/JSON reports. These are feature-branch capabilities, not proof of a
stable v0.3 release.

The existing Anthropic backend is a text-only solver plus an LLM judge, **not**
a live tool runtime. The command adapter can launch trusted external processes,
but the external adapter must apply the harness configuration itself.
Experiments use new run IDs and copied local fixtures, not guaranteed isolated
provider sessions for every backend. No container/VM security isolation,
distributed transaction handling, automatic recovery or production-agent
benchmark results are claimed. A synthetic smoke test is not empirical evidence
of real-model performance improvement.

Verification of the previous code commit found **36 tests passed** and a
successful synthetic command experiment on Python 3.12 in GitHub Actions;
see [the verification record](docs/verification.md).

## Historical sources

[BUILD_INSTRUCTION.md](BUILD_INSTRUCTION.md) is an archived v1 work request.
The original [research roadmaps](research/) are dated research snapshots, not
the current implementation backlog. [Application mappings](applications/quoth2-and-circuito.md)
describe ideas for other projects, not integrations verified in this branch.

Refer to [docs/workbench.md](docs/workbench.md) and
[docs/experiments.md](docs/experiments.md) for implemented behavior.

---
*Private repo. For internal use on agent reliability and Quoth-2 evolution.*
