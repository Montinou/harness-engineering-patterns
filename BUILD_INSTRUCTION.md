# BUILD INSTRUCTION — Harness Workbench

> **ARCHIVED v1 IMPLEMENTATION BRIEF (historical).** This file records the
> original assignment, not unfinished work in the active feature branch.
> The exploratory workbench and the newer experimental evaluation layer are
> already implemented on feat/experimental-evaluation-layer. The package
> version remains 0.2.0. Current behavior and limitations are described in
> [README.md](README.md), [docs/workbench.md](docs/workbench.md),
> [docs/experiments.md](docs/experiments.md) and
> [docs/verification.md](docs/verification.md). Original instructions below
> remain intact for provenance.


You are building on the private repository:
**https://github.com/Montinou/harness-engineering-patterns**

## Goal

Implement a practical **Harness Workbench** that can generate, modify, measure and improve agent harnesses using the self-improvement patterns documented in this repo.

The tool must support the core loop:
**Propose → Probe → Compose / Improve → Persist**

Focus on the patterns from:
- Harness Learning (arXiv:2609.35738)
- Harness² (arXiv:2610.14224, https://github.com/google-research/harness2)
- Agent-Reflex (arXiv:2610.17953)

## Required reading inside this repo

Read these files first and use them as the source of truth:
- `research/SELF_IMPROVING_HARNESSES.md`
- `research/SOURCES.md`
- `patterns/harness-learning.md`
- `patterns/harness2.md`
- `patterns/agent-reflex.md`
- `examples/` (existing demos)
- `applications/quoth2-and-circuito.md`

## What to build (v1)

Create a Python package / CLI with the following:

### 1. Core data model (`harness_workbench/core/harness.py`)
- Structured, versionable `Harness` dataclass covering at least:
  - system_prompt
  - guardrails
  - memory_config
  - tools
  - subagents
  - skills
  - scripts
  - plugins
- Support lineage (parent version, metrics, lessons)

### 2. Proposer (`harness_workbench/core/proposer.py`)
- Interface that can generate candidate edits (invariant + domain-specific)
- At minimum a rule-based + LLM-callable proposer
- Must be able to emit structured edits or diffs

### 3. Executor / Probe (`harness_workbench/core/executor.py`)
- Pluggable executor interface
- Include a mock executor for offline use
- Return behavioral reports (score, traces, failures, cost if available)

### 4. Improver (`harness_workbench/core/improver.py`)
- Implement Propose–Probe–Compose contrastive step (Harness² style)
- Support k=1 first, with hooks for parallel/sequential later
- Simple improvement library persistence (JSON or Markdown)

### 5. CLI (`harness_workbench/cli.py` or entry point)
Commands:
- `propose` — generate candidates from a harness + task
- `probe` — execute a harness against a task
- `improve` — run full Propose–Probe–Compose and save result
- `report` — show lineage and metrics

### 6. Persistence
- Save harnesses and lessons under `runs/` or `library/`
- Keep versions and parent references

### 7. Tests
- Basic unit tests for the loop with the mock executor
- Ensure the three existing examples still make sense or are migrated

## Constraints

- Pure Python, minimal dependencies for the core (stdlib + optional LLM adapter later)
- Must work offline with the mock executor
- Keep the code clean, typed and documented
- Do not break existing files; extend the repo
- Follow the design notes already written in `research/SELF_IMPROVING_HARNESSES.md`

## Success criteria

After implementation it must be possible to:
1. Load a base harness
2. Run `improve` on a task using the mock executor
3. Obtain a refined harness + recorded lessons
4. Inspect the lineage

## Output

- Commit the new package and CLI to this repository
- Update the main README with how to install and run the workbench
- Add a short `docs/workbench.md` explaining the architecture

Start by reading the research documents, then implement v1 of the workbench.
