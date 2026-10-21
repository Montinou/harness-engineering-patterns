# Harness Engineering Patterns

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

- `runs/<timestamp>-research-agent-v2/` — task, candidates, probe reports, lessons
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

## Status

Workbench v1 lives in `harness_workbench/`. The pattern notes were bootstrapped from AI Edge Tech adversarial audits (Oct 12–20 2026) plus primary sources.

---
*Private repo. For internal use on agent reliability and Quoth-2 evolution.*
