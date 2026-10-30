# Self-Improving Harness Patterns — Research Compilation

> **Historical research snapshot (2026-10-21).** The phased build plan and
> minimum-feature checklist below describe the original proposal, **not**
> current implementation status on feat/experimental-evaluation-layer.
> The package is still 0.2.0 with an additional experimental layer.
> See [current architecture](../docs/workbench.md),
> [experiment contract](../docs/experiments.md), and
> [verification](../docs/verification.md). Papers and quoted research results
> are not experimental measurements produced by this repository.


**Compiled:** 2026-10-21  
**Sources:** Primary papers, official repos, AI Edge Tech adversarial audits (Oct 12–20 2026), and related implementations.

This document collects the actionable technical detail needed to build a **Harness Workbench** (generate → modify → measure → improve).

---

## 1. Core Concepts

**Agent = Model + Harness**

The harness is the editable runtime around a frozen model:
- System prompt
- Guardrails
- Memory / context management
- Tool guidance & schemas
- Subagents / routing
- Skills
- Scripts
- Plugins / middleware

**Self-improvement** means evolving these components using execution feedback, without updating model weights.

Two dominant styles:
1. **Test-time adaptation** (Harness Learning, Harness²) — revise harness on the current or future tasks.
2. **Training-time internalization** (Agent-Reflex, RLHarness) — train the model to reflect/recover, then optionally keep an external harness.

---

## 2. Harness Learning (arXiv:2609.35738)

**Paper:** https://arxiv.org/abs/2609.35738  
**Authors:** Zhang, Liu, Wang et al. (CMU et al.)  
**Key idea:** Train a *proposer* to emit code edits to a frozen *solver*'s harness. Revisions act as meta-learning updates. Reward = task performance of the revised harness.

### Mechanism
- Input to proposer: task + current harness + execution report
- Output: code edit (diff or full file)
- Training: RL (optionally warm-started with SFT) on proposer only
- Test time: successive revisions, both models frozen

### Results
- 4B proposer > 35B teacher on single-step revision (Reasoning Gym, unseen families)
- Transfer: HotpotQA → MuSiQue / 2Wiki
- Multi-round improvement possible

### Related implementations
- https://github.com/Singh-AP/agent-harness-learning — practical Terminal-Bench application of the same idea (proposer rewrites prompts/self-checks/reviewer from failure reports).
- https://github.com/DeepExperience/Harness-R1 — learns to edit executable runtime harnesses from failure trajectories.

### Implementation notes for our tool
- Represent harness as versioned files or structured objects that can be diffed.
- Proposer = LLM call that outputs a patch or structured edit.
- Executor runs the harness and returns a report (score, traces, failures).
- Loop: propose → apply → execute → reward → (optional) update proposer or just keep best.

---

## 3. Harness² (arXiv:2610.14224)

**Paper:** https://arxiv.org/abs/2610.14224  
**Code:** https://github.com/google-research/harness2 (Apache-2.0)  
**Site:** https://harness2.github.io/  
**Authors:** Xu et al. (Northwestern + Google Cloud AI Research)

### Architecture
Two levels of recursion, model weights frozen:

1. **Task-level recursion** — Propose–Probe–Compose (no ground-truth verifier)
   - Propose: invariant edits (planning, tool use, context) + domain-specific edits
   - Probe: execute candidates on the *same* task under matched conditions
   - Compose: select/merge from behavioral contrasts
2. **Harnessing-level recursion** — update a persistent *improvement harness* / library with adopted harnesses + evidence-supported lessons

### Eight editable components
1. System prompt
2. Guardrails
3. Memory
4. Tool guidance
5. Subagents
6. Skills
7. Scripts
8. Plugins

### Recursion modes
- `--k 1`: single Propose–Probe–Compose
- Parallel: k invariant + k domain candidates → k compositions
- Sequential: k successive refinements, carrying evidence forward

### Results (paper)
- Single step: up to +11.5 pts WorkBuddy-Bench, +4.5 JobBench, +11.8 pp LAB
- Weaker model + improved harness can beat stronger model + base harness
- Cost scales with k (probing multiplies tokens)

### Code structure (high-level)
```
harness2/
  core/
    proposer.py
    recursion.py
    library.py
    selection.py
    judge.py
    ...
scripts/
runs/   # task summaries, adopted harnesses, lessons
```

### Practical takeaways
- Start with k=1, measure token cost.
- Improvement library is the key to cross-task transfer.
- Contrastive probing is the way to improve without labels.
- Default editor in experiments was a frontier model; smaller proposers are an open question.

---

## 4. Agent-Reflex (arXiv:2610.17953)

**Paper:** https://arxiv.org/abs/2610.17953  
**Code:** https://github.com/RUC-NLPIR/Agent-Reflex  
**Model:** https://huggingface.co/dongguanting/Agent-Reflex-8B  
**Authors:** Dong et al. (RUC-NLPIR)

### Three stages
1. **RSD** (Environment Reflection Skill Discovery)
   - Perturb tool returns in 557 MCP-anchored environments
   - Mine matched success/failure pairs → 1,170 reflection meta-skills
2. **ReSFT** (Reflective Supervised Fine-Tuning)
   - Convert skills into self-reflection, skill-recovery, and user-correction data
   - SFT the model
3. **ORPO** (On-Policy Reflective Policy Optimization)
   - Noisy agentic RL + reflection-guided on-policy distillation
   - Skills used only during training; deployed agent is skill-free

### Results (Qwen3-8B)
- 58.6 overall on 5 long-horizon tool-use benchmarks
- 29.1 on AgentNoiseBench
- Better noise robustness

### Relevance to external harness
Even though the final agent internalizes reflection, the RSD skill library and the idea of mining success/failure pairs are directly usable in an external harness (e.g., a reflection plugin or verification layer).

---

## 5. Related Work (2026)

| Work | Focus | Link |
|------|-------|------|
| Harness-R1 | Learn to edit runtime harnesses from failures | github.com/DeepExperience/Harness-R1 |
| Harness Annealing | Internalize harness control | arXiv:2610.01235 |
| RLHarness | Co-evolve procedural skills + policy | github.com/Ziqiao-Shang/RLHarness |
| HarnessRL / CAPO | Multi-agent harness RL | github.com/jiangxinke/Harness-RL |
| HarnessPro | Pareto performance-cost | arXiv:2610.26770 |
| RiseHarness | Self-evolution for terminal agents | arXiv:2610.27435 |
| Harness-RSI survey | Reading list & theory | github.com/zgcagi/Harness-RSI |

---

## 6. Design Implications for a Harness Workbench

### Data model
```python
@dataclass
class Harness:
    id: str
    version: str
    system_prompt: str
    guardrails: list[str]
    memory_config: dict
    tools: list[ToolSpec]
    subagents: list[SubagentSpec]
    skills: list[Skill]
    scripts: list[Script]
    plugins: list[Plugin]
    metadata: dict  # parent, metrics, lessons
```

### Core loop (unified)
1. **Generate / Propose** — LLM or rule-based proposer emits candidate edits (invariant + domain).
2. **Modify** — apply patches, version the harness, keep lineage.
3. **Measure / Probe** — execute against task(s), collect behavioral traces + metrics (score, cost, latency, failure modes).
4. **Improve / Compose** — contrastive selection or reward-based choice; write lessons to improvement library.
5. **Persist** — store adopted harness + evidence for future tasks.

### Minimal viable features
- Harness as files or structured JSON/YAML (easy to diff).
- Pluggable executor (mock, hosted bot, MCP).
- Simple contrastive composer (no training required for v1).
- Improvement library (Markdown or JSON per domain).
- CLI: `propose`, `probe`, `improve`, `report`.

### Phased build
1. **v0** — Offline mock + structured Harness + Propose–Probe–Compose (extend current examples).
2. **v1** — Adapter to a real model/bot + persistence + library.
3. **v2** — Optional trained proposer or reflection skill injection.
4. **v3** — Integration with an existing agent's policies or evidence loop.

---

## 7. Primary Sources & Links

### Papers
- Harness Learning: https://arxiv.org/abs/2609.35738
- Harness²: https://arxiv.org/abs/2610.14224
- Agent-Reflex: https://arxiv.org/abs/2610.17953
- Harness Annealing: https://arxiv.org/abs/2610.01235
- HarnessPro: https://arxiv.org/abs/2610.26770

### Code
- https://github.com/google-research/harness2
- https://github.com/RUC-NLPIR/Agent-Reflex
- https://github.com/Singh-AP/agent-harness-learning
- https://github.com/DeepExperience/Harness-R1
- https://github.com/zgcagi/Harness-RSI

### Extraction origin
AI Edge Tech adversarial briefs (Oct 12–20 2026) in Drive (`AI-Edge-Tech-Pipeline/03_BRIEFS`) plus primary sources verified at compilation time.

---

*This document preserves the original research and build plan; refer to the linked feature-branch documentation for implemented behavior.*
