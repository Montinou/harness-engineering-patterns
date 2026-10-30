# Pattern: Agent-Reflex

**Source:** arXiv:2610.17953 (Dong et al., RUC-NLPIR, Oct 2026)  
**Code / Model / Data:** github.com/RUC-NLPIR/Agent-Reflex + HF collection

## Idea

Train agents to *proactively reflect* and recover from noisy tool feedback and user corrections.

Treat reflection as a learnable meta-skill.

## Three stages

1. **RSD** (Environment Reflection Skill Discovery)  
   Inject grounded perturbations into tool environments → mine matched success-failure pairs → summarize reusable reflection meta-skills (1,170 skills from 557 MCP envs in the paper).

2. **ReSFT** (Reflective Supervised Fine-Tuning)  
   Turn skills into supervision for self-reflection, skill recovery and user-centric correction.

3. **ORPO** (On-Policy Reflective Policy Optimization)  
   Agentic RL under noise + reflection-guided distillation.

## Key results (Qwen3-8B)

- 58.6 overall on 5 long-horizon tool-use benchmarks (+18.4 vs strong baselines)
- 29.1 on AgentNoiseBench
- Better robustness to noise, stable training dynamics

## Applicability

Useful when tools are noisy or users correct mid-trajectory.
