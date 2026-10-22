# Harness Experimentation Layer

Branch: feat/experimental-evaluation-layer. Independent of Durable Harness or Vercel.

## Purpose

Run repeated, paired comparisons of versioned harness configurations using independent acceptance checks. Existing Propose → Probe → Compose → Persist stays intact. The experiment runner **recommends** variants but never writes/promotes a harness automatically.

Two scores must not be confused:
- **Behavioral score:** existing mock heuristic or Anthropic LLM judge; useful as a diagnostic, not proof of task correctness.
- **Objective score:** independent acceptance checks supplied with each task; the evaluator does not receive harness text, variant names, or judge scores. Only this score is used for promotion eligibility.

## Quickstart

From repository root:

~~~bash
python -m pip install -e .
python -m unittest discover -s tests -v
harness-workbench experiment --provider command --spec examples/experiments/local-command.json --root .
~~~

Open experiments/<id>/report.html. This demo uses a deterministic synthetic agent solely for testing the pipeline. It is **not evidence about real LLM improvements**.

For text-only real-model testing use --provider anthropic and set ANTHROPIC_API_KEY. The original Anthropic adapter returns the full solver answer, but has no real tool runtime. Use --provider command for actual tool-enabled agent processes.

## Manifest

~~~json
{
  "name": "qa-reliability",
  "baseline": "../workbench/base_research_harness.json",
  "variants": [{"id": "verification", "harness": "verified_research_harness.json"}],
  "tasks": [{
    "id": "qa-001",
    "description": "Fix a defect and run the tests",
    "domain": "qa",
    "fixture": "fixture",
    "checks": [
      {"id": "report", "type": "file_equals", "path": "report.txt", "value": "PASS"},
      {"id": "tests", "type": "command",
       "argv": ["python", "-m", "unittest", "discover", "-s", "tests"],
       "exit_code": 0, "timeout_seconds": 60}
    ]
  }],
  "repetitions": 5,
  "min_samples": 3,
  "seed": 42,
  "min_improvement": 0.02,
  "max_regression": 0.05,
  "max_cost_ratio": 1.5,
  "max_latency_ratio": 1.5,
  "confidence": 0.95,
  "bootstrap_iterations": 2000,
  "executor": {
    "type": "command",
    "argv": ["my-agent-adapter", "--harness", "{harness_json}",
             "--task", "{task_json}", "--workspace", "{workspace}",
             "--result", "{result_json}"],
    "model": "fixed-model-version",
    "timeout_seconds": 300
  }
}
~~~

All paths are relative to the manifest. Variant and task IDs must be unique. At least one candidate, one task and an independent check per task are mandatory. Each check has positive weight (default 1).

## Supported independent checks

- exact: compare output at target (default answer) with value.
- contains, not_contains, regex: text predicates on output at target.
- json_equal: decode/compare JSON structure.
- file_exists, file_equals: verify workspace file (path traversal/symlink escapes rejected).
- command: trusted subprocess argv run in the copied workspace (never shell=True); check exit_code, optional stdout_regex; configurable timeout.

**Security:** command verifiers and the command executor execute arbitrary programs from a trusted manifest. Copying a fixture is not a security sandbox. For untrusted agent code, isolate it in a container/VM, restrict permissions and network access, and avoid inherited secrets.

## Process adapter contract

A fresh workspace is created for every task × repetition × variant. The command executor places control files *outside* the copied fixture and exposes:

- HARNESS_SPEC_FILE: full variant harness JSON.
- HARNESS_TASK_FILE: task JSON.
- HARNESS_WORKSPACE: isolated working directory.
- HARNESS_RESULT_FILE: optional result JSON.
- HARNESS_SEED: paired seed.
- Supported command placeholders: {workspace}, {harness_json}, {task_json}, {prompt_file}, {prompt}, {task}, {result_json}, {seed}.

An agent adapter **must actually apply** the harness configuration, including tools, skills, prompts, memory or plugins. Passing JSON alone does not implement them.

Optional result JSON:

~~~json
{
  "outputs": {"answer": "Result", "details": {"test_count": 5}},
  "tokens": 7650,
  "latency_ms": 12230,
  "behavioral_score": 0.73
}
~~~

Without the result file, stdout becomes outputs.answer. A crash, timeout, nonzero exit or unreadable JSON records a failed trial rather than a silent retry. Tokens are a consumption proxy, not billed monetary cost.

## Statistical analysis and eligibility

A trial receives weighted fraction of passing checks (0 to 1). Records are paired by task ID and repetition with the same seed. Execution order alternates by repetition to limit temporal bias.

The comparison uses **hierarchical bootstrap** (sample tasks with replacement, then repetitions inside each task) and stores a confidence interval for paired mean quality delta. A candidate is eligible only if:

1. It has min_samples paired repetitions for **every task**.
2. Every relevant run has completed independent verification.
3. Paired mean improvement >= min_improvement and the lower confidence bound is > 0.
4. No task-level mean regression is less than -max_regression.
5. Mean token and latency consumption respect specified relative budgets, where baseline measurements exist.

The static HTML dashboard and JSON evidence include every run, check, failure, output, fixture fingerprint, model/provider and quality/cost/latency Pareto frontier. The Pareto frontier is informational; only eligible variants are recommended. No writes to main and no automatic adoption.

## Optional fail-closed integration with Improve

By default, Improve retains its existing fast behavioral/heuristic selection
(plus merged-candidate re-probe). For a **real independent acceptance gate**,
supply --validation-spec when invoking improve:

~~~bash
harness-workbench improve \
  --provider mock \
  --harness examples/workbench/base_research_harness.json \
  --task "Fact-check a claim" --domain research \
  --validation-spec examples/experiments/local-command.json \
  --root .
~~~

The final composed candidate is then tested against the baseline with fresh
paired repetitions using the validation manifest's tasks/acceptance checks.
When the validation manifest specifies a command executor, that executor is
used for real acceptance trials independently of the proposer/provider.
Only objectively eligible candidates can be persisted as improved versions.
A failed check, execution error, missing independent evidence or gate exception
**rejects** the candidate and keeps the old harness. Each gate call writes its
separate JSON/HTML evidence to experiments/. Sequential improvement validates
every adoption step. The sample command executor is synthetic: replace it
with a real harness-aware adapter before drawing empirical conclusions.

IMPORTANT: Without --validation-spec, the original LLM judge is still only a
behavioral heuristic and can propose/adopt changes that have not passed a
held-out task suite.

## Existing Workbench corrections

1. The Improver now re-probes the merged candidate and **rolls it back** if composition fails the minimum margin, instead of adopting a bad combination of individually positive edits.
2. The Store reserves **immutable unique version files** with exclusive creation. Branching twice from the same base no longer overwrites v2; descendant parent versions are remapped when necessary. Shared index and experience Markdown remain single-writer metadata (not a distributed transaction).

## Reproducibility and scientific limits

- A new session/process and copied fixture is created for every trial; **do not use durable replay** as a new experimental sample.
- Fixture, harness and manifest SHA-256 fingerprints, environment, model name, IDs and per-run seed are recorded.
- The command executor consumes the seed, but seed support by the underlying agent is adapter-specific. The legacy Executor interface has no seed/workspace hook. The Anthropic adapter does not guarantee deterministic sampling.
- Use a separate held-out task/regression suite for final promotion. Candidate development against the same benchmark introduces selection bias and overstates confidence.
- Keep code revisions, models, dependencies, external resources and tool policies pinned. Uncontrolled network resources can make results incomparable.
- Full outputs are persisted: avoid including secrets or sensitive personal data in benchmark fixtures.
- Trials run sequentially in this version. Multi-host transactional persistence, live dashboard and distributed workers are not included; the offline HTML report covers read-only inspection.
- The mock evaluator measures structural fitness, not real task correctness. The Anthropic judge is not an objective oracle.
- The statistical checks support evidence for these tasks; they do not guarantee behavior on unseen tasks.

## Modules

- experiments/core.py: paired runner, manifests, hashes, statistics, eligibility gates, evidence.
- experiments/checks.py: harness-blind acceptance verifiers.
- experiments/command.py: local agent runner.
- experiments/report.py: standalone HTML dashboard.
- experiments/demo_agent.py: deterministic smoke test only.
- tests/test_experiments.py: objective checks, pairing, regression, composition and version safety tests.
