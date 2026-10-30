# Deep Research: Self-Improving Harness Patterns (Oct 2026)

> **Historical research snapshot (October 2026).** The MVP suggestions and
> original next steps here are research context, not an active build request.
> For implemented behavior on feat/experimental-evaluation-layer consult
> [README](../README.md), [architecture](../docs/workbench.md),
> [experiment contract](../docs/experiments.md) and
> [verification](../docs/verification.md). No live-benchmark results can be
> inferred from the studies cited below.


Compilation of primary sources, methods, results, limitations and implementation details for building a harness generation / modification / measurement / improvement tool.

**Goal**: Provide the maximum actionable detail to move from research to a working Harness Workbench (generate → modify → measure → improve).

---

## 1. Core Framing

**Agent = Model + Harness**

The harness is the executable system that organizes model calls, tool use, information flow, memory, guardrails, routing and verification. Self-improvement patterns treat the harness as a first-class, editable, learnable object while keeping model weights frozen (or annealing them).

Main families of self-improvement:
1. **Test-time harness revision** (proposer revises harness from execution feedback)
2. **Recursive open-world harnessing** (contrastive Propose–Probe–Compose + continual library)
3. **Reflection skill mining** (learn to recover from noisy tools/user corrections)
4. **Harness internalization / annealing** (move control decisions into the model)
5. **Training-time co-evolving harness** (harness evolves with the policy during RL)

---

## 2. Harness Learning (arXiv:2609.35738)

**Paper**: https://arxiv.org/abs/2609.35738  
**Authors**: Zhang, Liu, Wang et al. (CMU et al.)  
**Date**: Sep 28, 2026

### Method
- Proposer model generates code edits (SEARCH/REPLACE style) to a Python harness.
- Solver (frozen) executes the harness.
- Reward = task performance of revised harness + validity bonus.
- Trained with GRPO (group-normalized policy optimization).
- At test time: iterative revision from execution reports, no weight updates.

### Key training details
- Proposer: Qwen3.5-4B or Qwen3-4B
- Solver: Qwen3.5-4B or Qwen3-8B (frozen)
- Reward components: parse success, runnable, completion rate + task score
- Single-step vs multi-step RL variants
- Optional SFT from teacher revisions

### Results (highlights)
- Reasoning Gym (21 unseen families): single-step RL proposer reaches 0.62 mean (beats 35B teacher 0.56)
- Multi-hop QA transfer: HotpotQA-trained proposer improves MuSiQue and 2Wiki
- Continues improving over multiple rounds

### Limitations
- Needs graded feedback during training
- Multistep not always better than single-step
- Limited diversity of revision strategies
- Real-world deployment may lack clean scores

### Pseudocode (test-time)
```python
h = seed_harness
for t in range(T):
    report = execute(h, feedback_questions)
    candidates = proposer.sample_edits(task, h, report, G=8)
    scored = [(apply(h, c), score(apply(h, c))) for c in candidates]
    h = best_valid(scored)
```

---

## 3. Harness² (arXiv:2610.14224)

**Paper**: https://arxiv.org/abs/2610.14224  
**Code**: https://github.com/google-research/harness2 (Apache-2.0)  
**Authors**: Xu et al. (Northwestern + Google Cloud AI Research)  
**Date**: Oct 9, 2026

### Method — Two recursion levels
**Task-level (Propose–Probe–Compose)**:
- Propose: invariant + domain-specific executable edits
- Probe: execute candidates on same task (behavioral contrasts)
- Compose: selective merge based on observed improvements (no ground-truth verifier)
- Supports parallel (k-way) or sequential recursion

**Harnessing-level**:
- After task: reflect and store adopted harness + EXPERIENCE.md lessons in a persistent improvement library
- Next tasks start from base harness but are guided by the library

### Editable components (8)
1. system prompt
2. guardrails
3. memory
4. tool guidance
5. subagents
6. skills
7. scripts
8. plugins / middleware

### Results (paper, means over 3 runs)
- Single step (k=1): up to +11.5 pts WorkBuddy-Bench, +4.5 JobBench, +11.8 pp LAB
- GLM-5 + improved harness can beat Gemini 3.5 Flash + base harness
- Further gains with k=5
- Editor used in experiments: Claude Opus 4.8

### Implementation notes (from repo)
- CLI: `harness2 --bench <name> --k 1 --mode parallel`
- Supported executors: OpenCode, Codex
- Benchmarks: WorkBuddy-Bench, JobBench, LAB
- File-based harness (md + py + ts)
- Cost scales with k (more probes = more tokens)

### Limitations
- Probing cost (tokens)
- Behavioral contrasts can share model biases
- Independent replication still limited
- Not an official Google product

---

## 4. Agent-Reflex (arXiv:2610.17953)

**Paper**: https://arxiv.org/abs/2610.17953  
**Code**: https://github.com/RUC-NLPIR/Agent-Reflex  
**Model/Data**: HF collection dongguanting/agent-reflex

### Method
Three stages for proactive reflection in noisy tool environments:
1. **RSD**: inject tool-feedback perturbations → mine success-failure pairs → extract reflection meta-skills (1,170 skills from 557 MCP envs)
2. **ReSFT**: turn skills into supervision for self-reflection, recovery, user correction
3. **ORPO**: on-policy RL under noise + reflection-guided distillation

### Results (Qwen3-8B)
- 58.6 overall on 5 long-horizon tool-use benchmarks (+18.4 vs strong baselines)
- 29.1 on AgentNoiseBench
- Better noise robustness

### Applicability
Strong for tools that return noisy/malformed results or mid-trajectory user corrections.

---

## 5. Additional Related Patterns

### Harness Annealing (arXiv:2610.01235)
- Objective: **harness internalization** — model learns control decisions so weaker harnesses still work.
- HAT curriculum: teacher trajectories under progressively weaker harnesses.
- On SWE-QA / SWE-QA-Pro: selected checkpoints with tools-only approach full-harness performance of starting checkpoints.
- Implication: co-evolution of model and harness; not pure external improvement.

### HarnessRL (arXiv:2610.26426)
- Training-time harness that co-evolves with the policy.
- Three interfaces: task sampling, rollout guidance, rubric criteria.
- Proposer + just-in-time critic loop.
- On Rubric-Mix-10k: +2.8–3.1 pts and nearly halves steps to best checkpoint (4B/9B).

### Harness-RL (arXiv:2608.29641) — multi-agent
- Black-box RL for central-agent multi-agent harnesses.
- Conflict-Aware Policy Optimization (CAPO) + interface-level trajectory trees.
- Code: github.com/jiangxinke/Harness-RL

### Others noted
- HarnessTTT (test-time harness optimization for scientific discovery)
- Harness Compilation (offline allocation of decisions between small VLM and harness)
- Industry framing: Hashimoto, OpenAI, Anthropic, Fowler — “harness engineering” as discipline

---

## 6. Practical Building Blocks for a Harness Workbench

| Capability | Best starting pattern | Key artifact |
|------------|-----------------------|--------------|
| Generate candidates | Harness Learning proposer / Harness² Propose | Code edit or file diffs |
| Modify (versioned) | File-based components (Harness²) | 8 editable files + git-like versioning |
| Measure | Execution traces + behavioral contrasts | Score, cost, latency, reliability, counterevidence |
| Improve without labels | Propose–Probe–Compose (Harness²) | Contrastive selection |
| Improve with labels | GRPO-style proposer (Harness Learning) | Reward from graded tasks |
| Noise recovery | Agent-Reflex skills | Meta-skill library |
| Long-term memory of improvements | Harnessing-level library (Harness²) | EXPERIENCE.md + adopted harnesses |
| Reduce external control | Annealing curriculum | Progressive harness weakening |

### Recommended MVP architecture
1. **Harness representation**: file-based (systemprompt.md, guardrails.md, tools/, skills/, memory.md, scripts/) — compatible with Harness².
2. **Proposer interface**: LLM that outputs SEARCH/REPLACE or structured diffs given (task, current harness, execution report).
3. **Executor**: run harness against task set (mock first, then real model/bot).
4. **Metrics**: task score, token cost, success rate, failure modes, counterevidence presence.
5. **Improver**:
   - Default: Propose–Probe–Compose (k=1 or k=3)
   - Optional: store lessons in improvement library
6. **Versioning**: every harness revision is a commit or snapshot with parent + metrics.

### Cost & risk notes
- Probing multiplies tokens (especially k>1).
- Behavioral contrasts can inherit model biases — keep primary-source / counterevidence checks.
- Always version and audit harness changes.
- Start with k=1 and measure before scaling.

---

## 7. Primary Sources (as of Oct 21, 2026)

- Harness Learning: https://arxiv.org/abs/2609.35738
- Harness²: https://arxiv.org/abs/2610.14224 + https://github.com/google-research/harness2
- Agent-Reflex: https://arxiv.org/abs/2610.17953 + https://github.com/RUC-NLPIR/Agent-Reflex
- Harness Annealing: https://arxiv.org/abs/2610.01235
- HarnessRL: https://arxiv.org/abs/2610.26426
- Harness-RL (multi-agent): https://arxiv.org/abs/2608.29641 + https://github.com/jiangxinke/Harness-RL

All numbers and method details above are taken from the primary papers and repositories as of the research date. Independent replication remains limited for the largest gains.

---

*Original next step (superseded): implement the MVP skeleton. The feature branch already includes the workbench and an experimental evaluation layer; follow the current docs linked above.*
