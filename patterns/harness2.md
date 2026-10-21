# Pattern: Harness² (Recursive Agent Harnessing)

**Source:** arXiv:2610.14224 (Xu et al., Google Cloud AI Research, Oct 2026)  
**Code:** https://github.com/google-research/harness2 (Apache-2.0)

## Idea

Plug-and-play recursive improvement of the execution harness in an open world (no ground-truth verifiers).

Two levels of recursion while keeping model weights frozen:

1. **Task-level recursion** — Propose–Probe–Compose contrastive step
2. **Harnessing-level recursion** — extract lessons into a continual improvement harness

## Propose–Probe–Compose

- Propose candidate edits (invariant + domain-specific)
- Probe them on the same task (behavioral contrasts)
- Compose a refined harness from the contrasts
- No need for gold labels / verifiers

## Key results (paper)

- Single step (k=1): up to +11.5 pts WorkBuddy-Bench, +4.5 JobBench, +11.8 pp LAB
- Weaker model + improved harness can beat stronger model + base harness
- Further gains with higher k (more probing → more tokens)

## Notes / caveats

- Probing multiplies cost
- Default editor in experiments was a frontier model (Claude Opus 4.8)
- Independent replication still limited
- Not an official Google product

## Applicability

Very high for production agents that face novel tasks (Quoth-2, Circuito research runs). Start with k=1 and measure token cost vs gain on real workflows.
