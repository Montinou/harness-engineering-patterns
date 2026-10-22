# Harness experiments

## What is implemented

This layer runs the same task repeatedly under independently instantiated
harness variants, measures observable results, and records why a candidate was
accepted or rejected. It does not provide runtime durability or train a model.

The complete flow is:

1. Freeze a baseline, candidate set, three task splits, fixtures, acceptance
   checks, provider configuration and decision policy.
2. Run all candidates plus the baseline on development tasks, with repeated
   trials and randomized order within matched task/repetition blocks.
3. Nominate one candidate from development results and persist that choice.
4. Run only that candidate and a fresh baseline on validation and regression
   tasks. No runner-up is tried after seeing validation feedback.
5. Apply statistical, regression, error, cost and provenance gates. Store the
   decision and, only when accepted, an immutable child harness in one transaction.

The original `improve` loop remains useful for exploration. It is not a substitute
for this held-out validation process.

## Quickstart

```bash
python -m pip install -e .
harness-experiments run \
  --config examples/experiments/sort-demo.json \
  --root .harness-experiments --allow-commands
```

The sorting example executes 66 subprocess trials: 18 development, 36 validation,
12 regression. It is synthetic, incurs no model charges, and is deliberately
ineligible for production promotion. A second candidate merely claims to verify
its answer; that cannot improve its objective grade.

The command prints an experiment ID and paths to JSON, Markdown and standalone
HTML reports. `run --json` returns machine-readable output. By default a completed
experiment returns exit code 0 even when the baseline wins. `--require-promotion`
returns 1 when no candidate is accepted. Invalid inputs return 2.

```bash
harness-experiments list --root .harness-experiments
harness-experiments report --root .harness-experiments --experiment-id EXPERIMENT_ID
harness-experiments export --root .harness-experiments \
  --experiment-id EXPERIMENT_ID --out accepted.json
```

Export refuses to overwrite an existing file and cannot export a rejected or
synthetic candidate. No experiment writes into the source baseline or deploys it.

## Configuration

See the runnable demo for a complete JSON config. Paths to baseline harness files
and fixture directories are relative to the config file, not the shell's current
directory. A baseline or candidate harness may also be embedded as JSON.

Top-level fields:

| Field | Meaning |
|---|---|
| `name` | Safe experiment label; each invocation still receives a fresh UUID. |
| `baseline` | Harness object or path to a harness JSON file. |
| `variants` | Unique labels with exactly one of `harness` or structured `edits`. |
| `cases` | Task, split, objective checks and optional fixtures. |
| `policy` | Repetitions, statistics, budgets and concurrency. |
| `backend` | `mock`, `anthropic`, `command`, or Python-only `custom`. |
| `provenance` | User metadata such as source repository commit or dataset version. |

A task contains `id`, `description`, optional `domain`, and `constraints`.
Each case has `split: development|validation|regression` and nonempty `checks`.
All three splits are required. IDs are globally unique; exact duplicated task
content is rejected even under another ID. Near-duplicate/semantic leakage still
requires dataset review by the caller.

Fixtures are either a relative `fixture_dir` or an inline `fixtures` mapping of
relative filenames to UTF-8 text. They are snapshotted before execution. Symlinks,
path traversal, `.git`, `.runtime`, binary files, conflicting file/directory paths
and snapshots above 8 MiB are rejected. Use an exported working tree rather than
an entire checkout containing `.git`. Each trial gets its own copy; mutations do
not become another variant's initial state. External databases/services/memory
must be reset by the adapter; a local file snapshot cannot reset them.

### Policy defaults

| Key | Default | Semantics |
|---|---:|---|
| `repetitions` | 5 | Independent runs per task/variant. |
| `min_validation_tasks` | 5 | Minimum distinct held-out tasks. |
| `margin` | 0.05 | Minimum practical objective improvement. |
| `alpha` | 0.05 | Statistical significance/confidence parameter. |
| `regression_tolerance` | 0 | Maximum per-task score/success-rate drop. |
| `bootstrap_samples` | 4000 | Task-cluster bootstrap replicates. |
| `max_workers` | 1 | Concurrent trials; maximum 32. |
| `seed` | 1729 | Ordering/bootstrap seed and adapter seed input. |
| `max_total_tokens` | null | Optional experiment stop budget. |
| `max_tokens_ratio` | null | Optional candidate/base mean token ceiling. |
| `max_latency_ratio` | null | Optional candidate/base measured latency ceiling. |

The total-token budget is checked between batches and against returned usage.
It can overshoot by one in-flight batch; it is **not a hard billing cap**. Missing
usage makes a configured token budget fail closed. Set low concurrency and
provider-side caps when that matters. Token totals include solver/judge calls in
the built-in adapter, including recognized cached-input usage fields; they are
not monetary invoices. Custom adapters must report actual usage. External
proposal-model calls are outside an experiment's execution budget.

## Objective checks

Checks are frozen external criteria, identical for all variants of a task.
The evaluator receives outputs/artifacts and the rubric, **not the harness**.

| Kind | Required fields | Pass condition |
|---|---|---|
| `exact` | `expected` string | Full output equals expected text. |
| `contains` | `expected` string | Expected text occurs in the output. |
| `json_equals` | `expected` JSON | Parsed output equals canonical expected JSON. |
| `file_equals` | `target`, `expected` | Artifact content exactly matches. |
| `command` | `command` argv | Trusted external test exits with code 0. |

Every check may have a positive `weight`; the objective score is the weighted
pass fraction. `success` requires every check to pass. Malformed JSON answers
are task failures, not silently excluded data. An unavailable/crashed evaluator
is an evaluation error and blocks promotion.

A command check may include `files`, injected into a separate grading directory
only after the solver has finished, plus a positive `timeout` (default 30s).
The grader receives the answer as `{"output": "..."}` on stdin. Its working
directory contains the solver artifacts and trusted grading files; grading files
overwrite matching artifact names. Expected answers are never part of the
solver request. This is an API boundary, not secrecy from malicious host-level
code; use external sandboxing for adversarial agents.

A model's diagnostic score and a claim such as "I ran the tests" remain evidence
for inspection only. Neither can override failing objective checks.

## Executors

### Existing Anthropic text probe

Set the backend kind to `anthropic`, and explicitly configure `solver_model`,
`judge_model` and optional `max_tokens`. Run with `--allow-network` and supply
`ANTHROPIC_API_KEY` through the environment. No key is stored in the manifest.

A fresh client and executor are constructed for every trial. Full responses,
requested/returned model IDs and usage are recorded. A changed returned solver
model prevents promotion. Pin actual model versions when the provider supports
it; an alias is not a guarantee of fixed weights.

This adapter is single-turn text-only. Its model sees the task text and harness
instructions, not fixture files, and it does not execute declared tools, scripts,
subagents or memory policies. Put required input in the task text, or use a real
agent runtime through the command/Python contract below. The independent task
judge is still fallible; experiment promotion comes from objective checks.

### Command protocol for a real runtime

A command backend needs an argv list (never a shell string), a positive timeout
(default 120s), optional `env_allow` names and optional `files` paths identifying
external adapter code. Declared code files are hashed and rechecked before every
trial. `{python}`, `{workspace}`, and `{config_dir}` placeholders are supported
in backend argv; `{config_dir}` is resolved when the config is loaded.

The process runs once per trial in a fresh directory and receives JSON on stdin:

```json
{
  "harness": {"id": "agent", "version": "1", "system_prompt": "..."},
  "task": {"id": "task-1", "description": "...", "domain": "qa", "constraints": []},
  "seed": 1234,
  "run_id": "run-...",
  "repetition": 0,
  "workspace": "/temporary/workspace"
}
```

Stdout must contain one JSON object:

```json
{
  "output": "the complete observable answer",
  "cost_tokens": 123,
  "behavioral_score": 0.6,
  "traces": [{"kind": "tool", "detail": "tests executed", "ok": true}],
  "failures": [],
  "metadata": {"solver_returned": "pinned-model-version"}
}
```

Only `output` is mandatory; omitted usage is marked unknown, not free. The
backend's score is diagnostic. Write artifact files to the current directory.
The runner snapshots them after process termination and sends only copies to
evaluation. No expected answers, other candidates or split labels are supplied
as protocol fields. Task descriptions can still reveal their own contents.

Use `--allow-commands` only with trusted commands. `shell=False`, reset HOME/cache,
an environment allowlist, fresh directories and timeouts do **not** constitute a
security sandbox. A permitted command can still access the host/network. Supply
an external container/VM adapter for untrusted agents. Output above 2 MiB is
rejected with periodic checks; filesystem/CPU/network quotas require external
isolation. POSIX process groups are killed on timeout and after the parent exits;
Windows descendant containment requires a job/container supplied externally.

### Python adapter

```python
from harness_workbench.experiments import ExperimentRunner, ExperimentStore, load_spec

# trial(harness, task, context) -> BehavioralReport
# context.workspace, context.run_id, context.seed, context.repetition
result = ExperimentRunner(
    ExperimentStore(".harness-experiments"), executor=trial
).run(load_spec("experiment.json"))
```

Return the full `BehavioralReport.output`, valid identity fields and metadata
`{"usage_known": true}` only when token counts are known. Do not mutate the
supplied task or harness. Custom callbacks must create/reset their own external
sessions and implement deadlines; Python threads cannot safely force-kill
arbitrary callbacks. They must be thread-safe when `max_workers > 1`.

## Proposals and composition

`harness-experiments propose` uses the existing rule proposer on development
cases only, writes individual variants plus a composition, and refuses to
overwrite its output file. `propose_development()` also accepts a custom
`Proposer`, optional development reports and `compose=False`.

The composition is a candidate, not automatically the winner. Any method that
edits a harness using validation feedback needs a new held-out set for its next
claimed validation. Cross-experiment holdout reuse is not automatically prevented.

## Statistical interpretation and promotion gates

Within each task, repetitions are averaged. Validation uncertainty is computed
across **paired task-level deltas**; 100 retries of one task are not 100 new tasks.
A percentile bootstrap reports a two-sided `(1-alpha)` interval for the mean
paired improvement. A separate exact one-sided sign test measures consistency
above the practical margin. It tests task win probability/median tendency, not
the mean effect. Ties at the margin supply no sign-test evidence.

Promotion requires enough independent validation tasks, mean and lower confidence
bound above `margin`, and sign-test p-value at most `alpha`. Every validation and
regression task must also satisfy score and success-rate non-regression within
tolerance. Incomplete trial sets, execution/evaluation errors, exceeded budgets,
changed model/workbench code or synthetic evidence block promotion. Failed trials
are retained as zero score and are never filtered out to improve an average.

Only one finalist is selected before validation, so the held-out round does not
rank many candidates and then test its own winner. This does not cure poor dataset
coverage, correlated task families, ambiguous acceptance criteria, or repeated
peeking across experiments. Five tasks is a conservative software gate, not a
universal sufficient sample size. Treat small-sample intervals as exploratory.

The development Pareto frontier maximizes objective quality and minimizes reported
tokens/measured latency. Unknown-cost/error candidates are excluded from that
frontier. Pareto membership never bypasses validation. Token and latency gates
are separate from quality; no arbitrary weighted score hides trade-offs.

## Evidence and recovery boundaries

SQLite stores frozen manifests, append-only trial/evaluation records, ordered
events, one final decision per experiment, and content-addressed accepted
harnesses. IDs follow `experiment -> variant -> run -> evaluation`; repeated
experiments receive new IDs. Full outputs, observable traces, fixtures, rubric
hashes, artifact snapshots, statuses and usage are retained.

Completed trials are recorded independently. An interrupted experiment remains
inspectable and is never promoted automatically. There is no resume/replay
mechanism in this release: start a new experiment for new independent evidence.
The registry API is append-only, but it is not tamper-proof against somebody who
can edit the SQLite file; manifest/object integrity checks detect mismatches.
Reports are regenerated views, not the source of truth.

No Durable Harness, production deployment, subscription reuse or live benchmark
improvement is implied by this implementation.
