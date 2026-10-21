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

## Status

Bootstrap. Patterns extracted from AI Edge Tech adversarial audits (Oct 12–20 2026) + primary sources.

---
*Private repo. For internal use on agent reliability and Quoth-2 evolution.*
