# Applications to Quoth-2 and Circuito Ontológico

> **Separate-application integration ideas.** The proposals below concern
> Quoth-2, Circuito Ontológico and Panelite; they are not a list of missing
> experimental workbench features. These applications were not audited while
> updating the documentation of feat/experimental-evaluation-layer. See
> [current experiments](../docs/experiments.md) for what this repository
> actually implements.


## Quoth-2

The platform already treats orchestration, policies, tool calling and memory as first-class.

Harness engineering fits naturally:

- Policies and routing rules = editable harness components
- Transactional outbox + adapters = execution environment
- Identity contracts + RLS = guardrails

**Next experiments**
- Version harness components (system prompts, tool descriptions, routing graphs)
- Prototype a Propose–Probe–Compose loop on a single policy
- Add reflection skill library for noisy tool failures

## Circuito Ontológico (Grok or Gemini)

Current design already has a strong harness:

- Discoverer → Evidence (precheck) → Packager → Orchestrator
- Explicit prohibition on final factual verdicts in Phase A
- Countersearch required
- Atomic claims

**Enhancements via these patterns**
- Make the Evidence precheck adaptive (Agent-Reflex style)
- Use Harness²-style recursion on the research harness when new domains appear
- Keep verified_cutoff and immutability rules intact

## Panelite

Scorecards + citations = evidence harness.

Reflection and verification loops can strengthen the “evidence, not vibe” claim.

## Practical starting point

1. Clone google-research/harness2 and run the smallest example
2. Map one existing bot/policy to the 8 editable components of Harness²
3. Measure token cost of k=1 probing on a real task
