# Pattern: Harness Learning

**Source:** arXiv:2609.35738 (Zhang et al., CMU et al., Sep 2026)

## Idea

Train a small *proposer* model to revise the executable harness of a frozen *solver* using execution feedback.

Harness revisions play the role of weight updates in meta-learning.

## How it works

1. Proposer sees: task + current harness + execution report
2. Proposer emits a code edit to the harness
3. Revised harness is executed → performance = reward
4. Proposer is trained with RL (GRPO-style)
5. At test time: successive revisions on new tasks, no parameter updates to the solver

## Key results

- 4B proposer beats 35B teacher on single-step revision (Reasoning Gym, unseen families)
- Transfer: HotpotQA → MuSiQue / 2WikiMultihopQA
- Policies trained on single revisions continue improving over multiple rounds

## Pseudocode sketch

```python
def harness_learning_step(task, harness, solver):
    report = execute(solver, harness, task)
    edit = proposer.propose(task, harness, report)
    new_harness = apply(edit, harness)
    reward = evaluate(solver, new_harness, task)
    return new_harness, reward
```

## Applicability

High for Quoth-2 policies and Circuito Evidence/Packager loops where the “harness” is already code (routing, verification rules, tool selection).
