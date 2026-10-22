# ADR 0001: separate experimental evidence from agent execution

Status: implemented on `feat/harness-experimentation-layer`.

## Context

The existing POC had a useful proposer/executor boundary, but single-run
behavioral scores could reward a changed harness without a better answer.
Composition did not veto combined regressions. Numeric library aliases could
be overwritten by independent branches from the same parent.

## Decision

Preserve the Python core and add a provider-independent experiment package.
Do not introduce Durable Harness or couple experiment decisions to a hosted
runtime. A runtime executes; an external evaluator checks artifacts; a frozen
policy decides whether evidence is sufficient.

Select exactly one finalist on development tasks. Record that choice before
any validation run. Evaluate that finalist and a freshly executed baseline on
held-out validation and regression tasks. Do not test the next candidate after
seeing a failed validation result. Repetitions estimate variability on a task;
they do not create new independent tasks.

Treat every trial as a new session/context. Never reuse completed model/tool
results as an experimental repetition. Store immutable IDs, complete outputs,
fixtures, rubric hashes, reported usage, failures and selection reasons.

Keep score, success, cost, latency and diagnostic behavior as separate metrics.
Quality gates take precedence over the diagnostic Pareto frontier. A synthetic
backend cannot publish a production harness.

Use SQLite transactions for experiment records and accepted artifacts. Preserve
legacy files for compatibility, but reject conflicting aliases rather than
silently replacing evidence. Require explicit export to create an accepted
harness file; never modify the user's baseline or deploy automatically.

## Consequences

The system can evaluate real agent runtimes via a local command protocol or
Python adapter without choosing a single runtime vendor. The existing Anthropic
adapter is still a text-only probe; it does not execute tools merely because a
harness lists them.

Objective checks are only as good as their acceptance criteria. Bootstrap and
sign-test results do not establish universal improvement, and a small,
unrepresentative task set is still a weak benchmark. Repeatedly reusing a
holdout across edited experiments compromises the interpretation; the caller
must rotate/reserve datasets.

Temporary directories are not OS sandboxes. Untrusted agents or grader code
require external isolation. API and subprocess permissions are opt-in, but a
trusted subprocess may use network or other host resources available to it.
No exact-execution recovery, hard wallet cap or live-model determinism is claimed.
