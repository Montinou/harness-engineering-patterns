# Harness Workbench

The workbench generates, probes, composes and stores agent harnesses. Model weights stay frozen. The loop is the one in [research/SELF_IMPROVING_HARNESSES.md](../research/SELF_IMPROVING_HARNESSES.md):

**Propose → Probe → Compose → Persist**

It is a small offline implementation of three patterns:

| Pattern | What v1 uses |
| --- | --- |
| Harness Learning | A proposer turns an execution report into a structured edit or a textual diff. |
| Harness² | Propose–Probe–Compose at `k=1`, plus a persistent improvement library. `k>1` and sequential mode are supported. |
| Agent-Reflex | Noisy tool feedback proposes a reflection skill and a reflection plugin. |

The sketches in `examples/` stay as readable demos. This package is the versioned tool.

## Architecture

```text
Task + Harness
      │
      ▼
 Proposer ── invariant edits + domain edits (structured ops + diff)
      │
      ▼
 Executor ── behavioral report (score, trace, failures, cost)
      │
      ▼
 Composer ── keep edits that beat the base by a margin
      │
      ▼
 Library   runs/<id>/   evidence for this call
           library/     versioned harnesses + EXPERIENCE.md
```

### Harness

`harness_workbench/core/harness.py` holds the eight editable components: system prompt, guardrails, memory, tools, subagents, skills, scripts, plugins. `id` is stable. `version` changes only when a composed harness is adopted. `lineage` stores the parent version, metrics and the lessons for that step.

Candidate edits use a provisional version such as `1+inv-verify-plan`. Those are not written to the library. Adopted versions are `1`, `2`, `3`, …

### Proposer

`RuleBasedProposer` reads the failure codes below and emits up to `k` invariant edits and `k` domain edits. It does not call a model.

`LLMProposer` takes a completer `(prompt: str) -> str`. The completer must return JSON:

```json
{"edits":[{"id":"llm-1","kind":"invariant","component":"guardrails","op":"add_guardrail","payload":{"text":"..."},"rationale":"..."}]}
```

Ops: `append_prompt`, `add_guardrail`, `set_memory`, `add_tool`, `add_subagent`, `add_skill`, `add_script`, `add_plugin`, `bundle`.

The CLI stays offline. Point `--edits-file` at JSON an external model already wrote; that path uses the same parser as `LLMProposer`.

### Executor

`Executor` is a one-method protocol: `execute(harness, task) -> BehavioralReport`. `MockExecutor` is deterministic and ships with the package. A real bot, Quoth-2 policy runner, or MCP loop can replace it without changing the improver. The mock score is behavioral fitness for contrast, not a gold label.

Failure codes the mock probe and the rule proposer share:

| Code | Meaning |
| --- | --- |
| `missing_verification` | No verify / cite / counterevidence language |
| `missing_plan` | System prompt does not ask for a step-by-step plan |
| `thin_memory` | Memory window under 4 |
| `missing_domain_tool:<name>` | Domain tool is absent (`search`, `repo_search`, `test_runner`) |
| `tool_noise_unhandled` | Task mentions noise and no reflection skill or plugin is armed |
| `no_reviewer` | Task asks for review or audit and no reviewer subagent exists |

### Compose

An edit is adopted only when its probe score beats the base by the margin (default `0.05`). Winners are applied together onto a new version. Losers are recorded as `SKIP` lessons. If nothing clears the margin, the base version is kept.

- `parallel` (default): one step, `k` invariant + `k` domain candidates, all probed against the same base.
- `sequential`: up to `k` steps of `k=1`, each starting from the harness the previous step adopted.

### Library

`improve --root .` writes:

- `runs/<timestamp>-<id>-v<version>/` — task, candidates, reports, lessons
- `library/harnesses/<id>/<version>.json` — the harness, including parent and metrics
- `library/EXPERIENCE.md` — lessons appended across tasks
- `library/lessons/<domain>.md` — the same block, split by domain
- `library/index.json` — version index

`report` walks `parent_version` from the newest file back to the seed.

Re-running `improve` on the same parent version overwrites that next version in the library. Continue from the adopted file when you want a longer chain.

## Extend

- Custom executor: pass any object with `execute(harness, task)` into `Improver`.
- Custom proposer: pass any object with `propose(task, harness, report, k=...)`.
- Trained proposer (Harness Learning, later): keep this edit language and swap the completer. v1 does not train.

Quoth-2 policies and Circuito's Evidence precheck map onto the same eight components. See [applications/quoth2-and-circuito.md](../applications/quoth2-and-circuito.md).
