# Harness Experimentation Layer

**Branch:** feat/experimental-evaluation-layer
**Package version:** 0.2.0 (experimental branch features; no v0.3 package bump, see [ADR 0004](adr/0004-single-branch-delivery-and-versioning.md)).
**Implementation described:** the code at commit 80134da. The documentation commit that follows it changes no Python.
**Validation record:** [docs/verification.md](verification.md). **Audit map:** [docs/audit-remediation.md](audit-remediation.md).
**Decisions:** [ADR 0001 evaluator trust boundary](adr/0001-evaluator-trust-boundary.md),
[0002 statistical protocol](adr/0002-statistical-protocol.md),
[0003 persistence and migration](adr/0003-persistence-and-migration.md).

Branch: feat/experimental-evaluation-layer. Independent of Durable Harness or Vercel.

## Purpose

Run repeated, paired comparisons of versioned harness configurations using independent acceptance checks, and say
whether one candidate is **eligible** for promotion under a pre-registered statistical protocol. Existing
Propose → Probe → Compose → Persist stays intact. The **standalone experiment runner recommends** a variant
without promoting it. The separate legacy Improve command can persist an adopted version, and its optional
`--validation-spec` gate requires a confirmatory experiment to approve it first.

Two scores must not be confused:
- **Behavioral score:** the mock heuristic or the Anthropic LLM judge; a diagnostic, not proof of task correctness.
- **Objective score:** the weighted fraction of independent acceptance checks a trial passes. Only this score is
  used for promotion, and only a `confirmatory` experiment can promote.

Every demo and simulation result in this repository is **synthetic**. Nothing here measures a language model.

## Quickstart

From the repository root:

~~~bash
python -m pip install -e .
python -m unittest discover -s tests -v
# exploratory smoke test: a synthetic agent, one task, never promotable
harness-workbench experiment --provider command --spec examples/experiments/local-command.json --root .
# confirmatory synthetic demo (136 tasks, one finalist): allow a few minutes, and use a fresh --root
harness-workbench experiment --provider command --spec examples/experiments/confirmatory-demo.json --root "$(mktemp -d)/demo"
~~~

The holdout tasks are used up once per library (the `--root`). Running the confirmatory demo, or the
`improve --validation-spec` gate on the same manifest, a second time on the same root ends `exploratory_only` with
"already consumed", so give every confirmatory demo command its own fresh `--root`.

Open `experiments/<id>/report.html`. The first command ends `exploratory_only`: its manifest is
`mode: exploratory`, so it is not promotion evidence. The second ends `eligible` for the finalist `verification`
(the `noop` variant competes only in development); the report is labeled "Synthetic data". Its 50 validation and 80 regression
units are template variants of one sentence pattern, not semantically independent tasks, and its agent is a
deterministic script. It exercises the whole confirmatory path offline; it is **not evidence about real LLM
improvements**. A third manifest, `examples/experiments/sandboxed-demo.json`, is the same benchmark with
`environment.mode: sandboxed`; it needs a running Docker daemon.

Without installing, from a checkout: `PYTHONPATH=. python -m harness_workbench experiment ...`.

For text-only real-model testing use `--provider anthropic` and set `ANTHROPIC_API_KEY`. That backend returns the
full solver answer but has no tool runtime, and declares support for `system_prompt`, `guardrails` and
`memory_config` only. Use `--provider command` for tool-enabled agent processes, `--provider mock` for an offline
structural simulation.

## Modes and schema versions

- `mode: exploratory` runs every variant on every task and reports diagnostics. It can never be `eligible`.
- `mode: confirmatory` runs the pre-registered protocol below and can end `eligible`.
- A manifest without `schema_version`, or with `1`, is **legacy**: it loads as exploratory with a
  `trusted_local` environment and a warning, and the old numeric keys are mapped (`min_improvement` to
  `statistics.practical_margin`, `max_regression` to `statistics.regression_margin`, `confidence` to `alpha`,
  `max_cost_ratio` to `budget.max_token_ratio`, `min_samples` is dropped: every task needs all its repetitions).
  `harness-workbench migrate-manifest --spec old.json [--splits splits.json] [--out new.json] [--dry-run]`
  writes a schema v2 manifest that is always `exploratory`. Splits come from `--splits` (a task-id to split map)
  and are never invented; controls, `runs_candidate_code`, the environment decision, `duplicate_review` and `mode: confirmatory`
  are added by hand.
- There are **two `schema_version` keys with different meanings**. The manifest's `schema_version` (1 or 2) is the
  manifest layout. The top-level `schema_version` of `results.json` is `2` and names the layout of the results
  file (decision statuses, per-trial execution and evaluation statuses, provenance); the manifest's own version is
  in `manifest_schema_version`, and `provenance.schema_version` repeats the manifest's. For a schema v2 manifest
  all three read 2. See [the table in audit-remediation.md](audit-remediation.md#the-schema_version-keys).

## Manifest (schema v2)

~~~json
{
  "schema_version": 2,
  "name": "qa-reliability",
  "mode": "confirmatory",
  "family": "qa-agent",
  "dataset": {"id": "qa-suite", "version": "1"},
  "baseline": "../workbench/base_research_harness.json",
  "variants": [{"id": "verification", "harness": "verified_research_harness.json"}],
  "tasks": [{
    "id": "qa-001",
    "description": "Fix a defect and run the tests",
    "domain": "qa",
    "split": "validation",
    "family": "qa-001",
    "runs_candidate_code": true,
    "fixture": "fixture",
    "grader_files": "graders/qa-001",
    "checks": [
      {"id": "report", "type": "file_equals", "path": "report.txt", "value": "PASS", "critical": true},
      {"id": "tests", "type": "oracle", "argv": ["{python}", "-I", "observe.py", "{artifact}", "{out}"],
       "observed": "observed.json", "expected": {"passed": 5}, "compare": "json_equal"}
    ],
    "controls": {
      "positive": {"id": "pass", "outputs": {"answer": "done"}, "artifact": "controls/qa-001/pass"},
      "negatives": [{"id": "fail", "outputs": {"answer": ""}, "artifact": "controls/fail"}]
    }
  }],
  "repetitions": 5,
  "seed": 42,
  "statistics": {"profile": "binary_v1"},
  "budget": {"max_token_ratio": 1.5, "max_latency_ratio": 1.5},
  "environment": {"mode": "sandboxed"},
  "executor": {
    "type": "command",
    "argv": ["{python}", "-I", "my_adapter.py", "--harness", "{harness_json}",
             "--task", "{task_json}", "--workspace", "{workspace}", "--result", "{result_json}"],
    "model": "fixed-model-version",
    "timeout_seconds": 300,
    "supports": ["system_prompt", "tools"]
  }
}
~~~

The example is illustrative and shows one task; a confirmatory manifest needs tasks in both the `validation` and
`regression` splits, and `binary_v1` needs 50 and 80 independent families to reach a decision other than
`insufficient_evidence`. The shipped, loadable examples are under `examples/experiments/`.

All paths are relative to the manifest. The loader reads strict JSON (`NaN`, `Infinity` and `-Infinity` are
rejected) and validates every number for type, finiteness and range; a boolean is not an integer. A manifest error
is raised before anything runs. Rules that apply in every mode:

- Variant and task ids are unique; at least one candidate and one task are required.
- Every task has at least one independent check; check ids are unique within a task (a duplicate, explicit or
  implicit `check-N`, is rejected because the id names the critical invariants); each weight is positive
  (default 1); `critical: true` marks a deterministic invariant.
- `argv` entries never start with a bare `python` or `python3`: write `{python}`, which the controller resolves to
  its own interpreter (`sys.executable`, or the image's Python when sandboxed). A grader file named in `argv` must
  exist under the task's `grader_files`.
- `environment.mode` is required in schema v2 and has no default.

A **confirmatory** manifest must also (`ExperimentSpec.validate_confirmatory`, run at load and again when a spec
is rebuilt through the API):

- have at least one candidate, and a `development` split when several candidates compete; non-empty `validation`
  and `regression` splits; every task in a split; a `family` that does not span splits; no identical task content
  in two splits; no near-duplicate descriptions across families unless the pair is listed in `duplicate_review`
  (a hash does not prove semantic independence, so this stays a human review);
- give every validation and regression task `controls` (one positive case that must pass and at least one negative
  case that must fail), and state `runs_candidate_code` explicitly on every task;
- use `environment.mode: sandboxed` and at least one `oracle` check for any task with `runs_candidate_code: true`,
  and set `trusted_solver_acknowledged: true` to use `trusted_local` at all;
- run `{python}` in `command` and `oracle` checks with `-I` as `argv[1]`;
- declare a known `statistics.profile` and meet it: `repetitions`, `min_independent_units`,
  `min_regression_units` and `bootstrap_iterations` at or above the profile's floors, and `method`, `alpha`,
  `practical_margin` and `regression_margin` equal to the profile's values (`binary_v1`: 50 validation units,
  80 regression units, 5 repetitions, 10000 resamples, `regression_margin` 0.10);
- have candidates whose execution configuration differs from the baseline's, and at most one finalist
  (`finalists`, if given, must be 1).

## Supported independent checks

Each result has a `status`: `passed` and `failed` are verdicts about the solver; `error` means the evaluator could
not decide (a missing grader, an unreadable snapshot, a protocol violation) and never counts as a failed solution.

- `exact`: compare the output at `target` (default `answer`) with `value`.
- `contains`, `not_contains`, `regex`: text predicates on the output at `target`.
- `json_equal`: decode and compare a JSON structure.
- `file_exists`, `file_equals`: inspect the validated snapshot of the solver's workspace (traversal, symlinks and
  special files are rejected).
- `command`: run a trusted argv (never `shell=True`) in the frozen grader bundle directory through the backend.
  The artifact snapshot is `{artifact}` (also `$HARNESS_ARTIFACT`), the bundle `{bundle}`, the result directory
  `{out}`; `exit_code` (default 0), optional `stdout_regex`, `timeout_seconds` up to 300. Its verdict is the exit
  code, so a process that exits 0 passes.
- `oracle`: run a grader program that only *observes*. It writes a value to the relative path `observed` under
  `{out}`; the **supervisor** reads that file and compares it with `expected` itself (`compare`: `exact` or
  `json_equal`). A grader that exits 0 without writing the observation is an `evaluation_error`. The program does
  not report a verdict.

## Evaluator trust boundary

Detailed in [ADR 0001](adr/0001-evaluator-trust-boundary.md). In short: each task has a `GraderBundle` frozen at
load time. After the solver finishes, the supervisor copies the workspace into a validated snapshot (regular files
and directories only), materializes the bundle read-only in a directory the solver never saw, and runs the checks
there with their own `HOME`, temporary directory and minimal environment. Rewriting `check.py`, planting
`unittest.py`, `json.py`, `sitecustomize.py` or `conftest.py`, setting `PYTHONPATH`, printing `PASS` or a missing
grader do not turn a failure into a pass (`tests/adversarial/`). Before any trial, every validation and regression
task's controls run through the real evaluator, and a control that misbehaves invalidates the experiment.

**Limits, stated plainly:**

- `python -I` is not a trust boundary. A `command` check that imports candidate code is defeated by candidate code
  that calls `os._exit(0)`; an `oracle` check survives that.
- **An `oracle` verdict can be forged by candidate code that runs in the grader process** and writes
  `$HARNESS_OUT/observed.json` after the grader's own write (for example from an `atexit` hook), because `out` is
  writable by the grader and `sandboxed` mounts it read-write as well. This is documented in ADR 0001 and not
  fixed in code. A confirmatory result for a `runs_candidate_code` task whose grader imports the candidate is not
  proof against a hostile candidate.
- `trusted_local` is not a sandbox: the solver runs as the same user and can read host files, including grader
  files in the manifest directory and the library.
- The bundle hash covers checks and grader files, not every module an interpreter imports.

## Execution backends and their guarantees

| Backend | Selected by | What it enforces | What it does not |
|---|---|---|---|
| `LocalBackend` (`trusted_local`) | `environment.mode: trusted_local` | Wall-clock timeout; a cap on captured output per stream (`resource_limit`); a minimal allowlisted environment with a fresh `HOME`, `TMPDIR` and cache directory per trial; no `PYTHON*` inheritance; the child runs in its own session and the whole process group is killed on a timeout and after the main process exits. | Isolation of any kind. A descendant that starts a new session (for example `setsid()`) survives the group kill; `run` stops waiting for its pipes after a fixed bound and returns. Declares `NO_GUARANTEES`. |
| `DockerBackend` (`sandboxed`) | `environment.mode: sandboxed` | A capability self-test before any run (isolated filesystem, network denied, unprivileged user, process containment, no Docker socket, read-only bundle); `--network none`, user 65534, all capabilities dropped, `no-new-privileges`, read-only root, memory and pid limits; the container is removed on a timeout or output overflow; the writable tree is opened for the container user before the run and reclaimed for the host after it. If Docker or a capability is missing it raises `BackendUnavailable` **before executing**, and it never falls back to the host. | Verified only where Docker is available: its integration tests were not run on the author's machine and CI has not run yet. A secret declared in `solver_env_passthrough` appears on the `docker run` command line. The sandbox is only as strong as the daemon and the image. |

Both modes give the solver and the grader an allowlisted environment (`PATH`, `LANG`, `LC_ALL`, `LC_CTYPE` and
`TZ` copied from the parent; a fresh `HOME`, `TMPDIR` and `XDG_CACHE_HOME`; `PYTHONNOUSERSITE`,
`PYTHONDONTWRITEBYTECODE` and `PYTHONSAFEPATH` set by the supervisor; plus names declared in
`environment.solver_env_passthrough` for the solver only).
`PYTHON*`, `HOME`, `TMPDIR` and `XDG_CACHE_HOME` cannot be passed through, and the grader never receives
credentials. Environment values are never written to control files, results or logs.

## Process adapter contract

A fresh workspace, `HOME`, `TMPDIR` and trial id are created for every task × repetition × variant, and a fresh
external process is launched by the command executor. The control files live *outside* the copied fixture. The
solver receives only the **public** request: the eight executable harness components, the task description,
domain, constraints, an opaque trial id and the seed. It does not receive the harness id, lineage or notes, the
task's split, expected values, other candidates' results or the validation history.

- Placeholders in `executor.argv`, expanded in one pass: `{workspace}`, `{harness_json}`, `{task_json}`,
  `{prompt_file}`, `{prompt}`, `{task}`, `{result_json}`, `{seed}`, `{python}`, `{demo_agent}`, `{trial_id}`.
- Environment: `HARNESS_WORKSPACE`, `HARNESS_SPEC_FILE`, `HARNESS_TASK_FILE`, `HARNESS_RESULT_FILE`,
  `HARNESS_SEED`, `HARNESS_TRIAL_ID`, on top of the allowlisted environment.
- `executor.supports` names the components the adapter really applies (default: all eight). In a confirmatory
  experiment a candidate that changes a component the adapter does not declare is the experiment is `invalid_experiment` before any execution,
  because the change could not reach the model. An adapter must **actually apply** the harness; passing JSON alone
  implements nothing.

Optional result JSON (strict JSON, a regular file no larger than `environment.limits.max_output_bytes`):

~~~json
{
  "outputs": {"answer": "Result", "details": {"test_count": 5}},
  "tokens": 7650,
  "latency_ms": 12230,
  "model": "returned-model-id"
}
~~~

Without the file, stdout becomes `outputs.answer`. Latency is measured by the supervisor with a monotonic clock;
whatever the adapter reports as `latency_ms` is kept as a diagnostic only. `tokens` is recorded as
`adapter_reported`. A crash, timeout, nonzero exit, output over the cap or malformed result is a typed failed trial
(`execution_error`, `timeout`, `resource_limit`, `task_failure`), never a silent retry. Tokens are a consumption
proxy, not billed cost.

## Statuses

- `execution_status`: `completed`, `task_failure`, `execution_error`, `timeout`, `resource_limit`, `cancelled`.
- `evaluation_status`: `passed`, `failed`, `evaluation_error`, `not_evaluated`. A score exists only for
  `passed` and `failed`.
- `decision_status`: `eligible`, `rejected`, `insufficient_evidence`, `invalid_experiment`, `exploratory_only`.
- Measurement provenance of tokens and latency: `measured`, `provider_reported`, `adapter_reported`, `estimated`,
  `unknown`, `not_applicable`. A zero is a known zero; an absent value is `unknown`, never zero.

A wrong solution is `failed`, not an infrastructure failure. A timeout is a task failure only when the manifest
declares `statistics.timeout_is_task_failure` beforehand. A trial is retried only for a retryable infrastructure
failure, up to `statistics.max_attempts` (default 1), every attempt is recorded, and the decision uses the last
attempt of each trial.

## The confirmatory protocol

1. **Controls.** Controls of every validation and regression task run through the real evaluator; a failure is
   `invalid_experiment` with no trials.
2. **Development.** With more than one candidate, only the `development` split is used to choose a finalist
   (`select_finalist`, deterministic tie-break). The proposer never sees validation or regression data.
3. **Freeze.** One finalist is frozen with the hashes of the plan, baseline, candidate and policy
   (`selection_frozen`) before any validation or regression trial. Changing any of them afterwards invalidates the
   confirmation.
4. **Holdout reservation.** The holdout is reserved in one transaction before its first trial, keyed by the
   **content fingerprint of each holdout task** (description, domain, constraints, fixture hash, grader bundle
   hash), so renaming a task does not make it fresh. A second candidate on an already used holdout makes the
   result `exploratory_only`. A rerun is allowed only for the identical interrupted confirmation (same candidate,
   baseline, policy and plan hash) and is itself `exploratory_only` when it already has holdout trials; its trials
   are not pooled. The registry is **local to one library**: it cannot see evaluations run in another library or
   by another team, nor exposure through model pretraining.
5. **Holdout.** Only the finalist and the baseline run the validation and regression tasks.
6. **Decision**, recorded once with the plan, candidate, baseline and the exact evidence it rests on.

## Statistics and eligibility

The comparison is not a hierarchical bootstrap any more. Per task, the mean over the paired repetitions of
candidate minus baseline; per family, the mean of its task deltas; the effect is the mean over families. The
**independent unit is the family**; repetitions, checks, steps and retries never raise the unit count. The bound is
a one-sided lower percentile bound from a bootstrap over whole units (`cluster_percentile_ci_v1`, 10000
resamples; `cluster_t_v1` is the alternative method). `decide` works down this list:

1. an integrity problem (drift, failed controls, an unusable baseline or finalist trial) is `invalid_experiment`;
2. a non-confirmatory or legacy spec, or an exposed holdout, is `exploratory_only`;
3. a finalist trial that fails a `critical` check is `rejected`;
4. missing pairs, fewer independent units than `min_independent_units`, fewer regression units than
   `min_regression_units`, or a degenerate interval (zero variance across units, unless the manifest sets
   `allow_zero_variance` with a justification) is `insufficient_evidence`;
5. token and latency budgets (next section);
6. the lower bound must exceed `practical_margin` (0.05), otherwise `rejected`;
7. non-inferiority for each regression group at `alpha` divided across groups (Bonferroni): a group whose upper
   bound is below `-regression_margin` is a demonstrated regression (`rejected`); a group the data cannot decide
   is `insufficient_evidence`;
8. otherwise `eligible`.

Rules 4 to 7 are all evaluated, and the status is that of the first finding.

The published profile `binary_v1` and its numbers come from a pre-registered calibration with a dated deviation
([ADR 0002](adr/0002-statistical-protocol.md), [statistical validation](statistical-validation.md)). Its
false-promotion bound and power are **synthetic**, hold for the simulator's generator and the profile's plan
(50 validation and 80 regression units), and say nothing about language models. The regression tolerance is 0.10.
The auditor's figures of 34/400 (8.5 %) and 47/300 (15.7 %) are the auditor's.

## Budgets and usage

`budget` holds `max_token_ratio` and `max_latency_ratio` (default 1.5 each, `null` disables), an optional
`absolute_token_limit`, and `accepted_provenance` (default `measured`, `provider_reported`, `adapter_reported`).
Usage nobody measured blocks; it is never read as zero or as within budget. With a token gate enabled:

- unknown tokens or a provenance outside the accepted set: `insufficient_evidence`;
- a zero baseline token mean has an undefined ratio: `insufficient_evidence` unless an `absolute_token_limit` is
  declared, which is then checked on the candidate;
- a ratio above its limit: `rejected`;
- latency: skipped (and reported as such) when every trial is `not_applicable`, as for an in-process executor;
  unknown, mixed `not_applicable`, or a zero baseline: `insufficient_evidence`.

Token ratios compare tokens, not money, and not across models with different prices. The recorded usage is that
of the trial executor; usage outside it (an external proposer, for example) is not recorded.

## Provenance and drift

`results.json` carries a `provenance` record: the implementation hash (SHA-256 over `harness_workbench/**/*.py`),
the workbench checkout's `commit` and `dirty` flag, the manifest schema version, Python version, platform and
executable, the executor, `adapter_argv` and `adapter_supports`, the backend (name, declared capabilities, image
and digest when available), the requested model, the environment declaration (passthrough names, limits, mode),
`seed_scope` and `dependency_scope`, `manifest_sha256`, and `returned_model_status` with `returned_models`.
`returned_model_status` is `verified` (every completed trial reported the same model), `mixed` (more than one;
invalidates a confirmatory run as model drift), `partial` (some completed trials named a model and some did not;
a limitation, not drift) or `unknown` (none did, so reproducibility across trials cannot be claimed). A change of
implementation hash, rubric or returned model between trials invalidates a confirmatory run. No environment
values are recorded. The hash does not cover the interpreter, adapter dependencies or image contents, and
`adapter_argv` is stored verbatim, so a manifest must hold no secrets.

## Persistence and the CLI

Evidence is written to the SQLite database `<root>/library/workbench.sqlite3` as it happens: each trial in its
own short transaction, the frozen selection once, the holdout reservation atomically, the decision, and promotions
by decision id ([ADR 0003](adr/0003-persistence-and-migration.md)). `results.json` and `report.html` under
`experiments/<id>/` are written when the experiment finishes. An interrupted experiment stays `running`, with its
trials and no decision; there is no automatic resume.

~~~bash
harness-workbench experiment --provider command --spec <manifest> --root <dir> [--json]
harness-workbench experiment --list-incomplete --root <dir>        # experiments left running; creates an empty database on a root that has none
harness-workbench migrate-manifest --spec <v1> [--splits <json>] [--out <v2>] [--dry-run]
harness-workbench export-library --root <dir> [--json]             # regenerate the JSON views from the database
harness-workbench migrate-library --root <dir> [--dry-run] [--json] # import a legacy JSON library, with backups
~~~

A library that has JSON versions the database has never seen is refused until `migrate-library` has run. A
domain-level failure (a missing sandbox, a refused migration, a store conflict, a busy database) is an error with
exit code 2.

The text output and the HTML report state the mode, the decision and its reasons, the estimate and lower bound
(or "unavailable"), and distinguish exploratory, insufficient evidence, invalid, rejected and eligible results.
Synthetic and exploratory runs are labeled.

## Optional fail-closed integration with Improve

By default, Improve keeps its fast behavioral selection (plus the merged-candidate re-probe). For a real
independent gate, pass a **confirmatory** manifest:

~~~bash
harness-workbench improve \
  --provider mock \
  --harness examples/workbench/base_research_harness.json \
  --task "Fact-check a claim" --domain research \
  --validation-spec examples/experiments/confirmatory-demo.json \
  --root "$(mktemp -d)/gate"   # a fresh root: the holdout is used up once per library
~~~

- An exploratory or legacy `--validation-spec` is refused before anything runs (exit 2); run `migrate-manifest` and
  add splits and controls.
- The execution backend the spec asks for must exist before anything is paid for (exit 2 otherwise).
- The library is checked before the gate runs: an unmigrated JSON library (`migrate-library` first) or a base whose
  id and version are registered with different content is refused (exit 2) before the holdout is reserved.
  `experiment` and `experiment --list-incomplete` refuse an unmigrated root the same way.
- The gate runs **once**, on the final composed harness, against its baseline; Improve does not reuse the holdout
  inside its loop. In `sequential` mode only the final harness is confirmed.
- Only a persisted `eligible` decision approves, and the promotion is tied to that decision id and to the
  candidate that was evaluated. A rejection, an ordinary exception or a malformed verdict keeps the baseline; an
  infrastructure failure (`OSError`, a busy database, an unavailable backend) is not a rejection: it is reported
  and nothing is persisted.
- The confirmatory demo's command executor is synthetic: replace it with a real harness-aware adapter before
  drawing empirical conclusions.

Without `--validation-spec`, the LLM judge is still only a behavioral heuristic and can propose and adopt changes
that no held-out suite has confirmed.

## Existing Workbench corrections

1. The Improver re-probes the merged candidate and **rolls it back** if the composition fails the minimum margin.
2. Versions live in SQLite with unique `(harness_id, version)` identities. The base is registered once and never
   renumbered; a conflicting definition of the same identity is an error; children are allocated in short
   transactions. The JSON files, `index.json` and the Markdown lessons are exported views, each written
   atomically after the commit, not one transaction over all of them.

## Reproducibility and scientific limits

- A new run id and workspace are created for every trial, and the command adapter starts a new external process.
  Whether provider sessions, memory and context are truly reset is adapter-specific. Do not use durable replay as
  a new sample.
- Fixture, harness, grader bundle and manifest SHA-256 fingerprints, the implementation hash, environment
  declaration, requested and returned model IDs and the per-trial seed are recorded. The seed is paired per
  repetition; whether the underlying agent honors it is adapter-specific, and the Anthropic adapter does not
  guarantee deterministic sampling.
- A confirmatory result supports a claim about tasks like the validation families of that manifest. It does not
  guarantee behavior on unseen tasks, and the independence of families is the author's assumption.
- Keep code revisions, models, dependencies, external resources and tool policies pinned. Uncontrolled network
  resources can make results incomparable.
- Full outputs are persisted: keep secrets and sensitive personal data out of benchmark fixtures.
- Trials run sequentially, even when the older Improver runs with `mode=parallel` (which means candidate
  comparisons against a common base). Multi-host persistence, a live dashboard and distributed workers are not
  included; the offline HTML report is read-only.
- The mock evaluator measures structural fitness, not task correctness. The Anthropic judge is not an oracle.

## Remaining limitations

- No validation against real models or real agents exists. Every calibration, simulation and demo result is
  synthetic.
- `trusted_local` is not a sandbox and lets the solver read host files. Process groups do not contain
  descendants that start new sessions.
- An `oracle` verdict can be forged by candidate code that runs in the grader process and writes the observation
  late (ADR 0001). `command` checks are weaker still.
- The sandbox backend and the install/portability checks have only run where their prerequisites exist. Docker
  integration tests and the wheel/editable script were not run on the author's machine, and CI has not run yet.
- The exposure registry is local; hashes do not prove semantic independence; one finalist and one primary metric
  only; several regression groups and plans above the profile floors are not calibrated; the regression
  tolerance is 0.10.
- Dependency hashing is partial: the implementation hash covers the package's Python files only.
- The confirmatory demo's tasks are template variants, so its units are not semantically independent, and its
  agent is deterministic.

## CI coverage

`.github/workflows/harness-tests.yml` defines five jobs: `core` (unit and regression suite on Python 3.10, 3.12 and
3.13 with an editable install), `no-install` (the suite from a clean checkout on 3.12 with the package not
installed), `isolation` (the Docker sandbox tests and the sandboxed demo, failing on any skip), `portability`
(wheel and editable installs from a foreign cwd with no `python` alias, failing on any skip and on a runner that
ships `/usr/bin/python`) and `smoke` (the confirmatory demo must end `eligible`, the local demo
`exploratory_only`). This describes the workflow file. **No run of it exists yet for this work**; results are
recorded in [verification.md](verification.md) when they do.

## Modules

- `experiments/manifest.py`: schema v2 manifest, legacy mapping, confirmatory validation, `migrate_manifest`.
- `experiments/core.py`: the phased runner, trial pipeline, persistence of evidence, `results.json`.
- `experiments/protocol.py`: plan, development selection, frozen finalist, holdout fingerprints.
- `experiments/stats.py`, `experiments/profiles.py`, `experiments/simulate.py`: independent-unit statistics, the
  decision function, published profiles, the Monte Carlo simulator.
- `experiments/grader.py`, `experiments/checks.py`: the frozen bundle, validated snapshot, supervisor-side checks.
- `experiments/backends.py`: `LocalBackend`, `DockerBackend`, capabilities, the minimal environment.
- `experiments/command.py`: the command executor and the public trial request.
- `experiments/contracts.py`, `experiments/status.py`, `experiments/validation.py`: typed records, statuses,
  strict numbers and JSON.
- `experiments/provenance.py`: provenance record, drift detection, adapter capabilities.
- `experiments/report.py`: standalone HTML dashboard.
- `experiments/demo_agent.py`: deterministic synthetic agent; a smoke test only.
- `core/db.py`, `core/store.py`, `core/migrate.py`: the SQLite store, exported views, library migration.
- `tests/`: `unit/`, `integration/`, `adversarial/` (including `test_audit_reproductions.py`), `statistical/`,
  and the original `test_experiments.py`, `test_workbench.py`, `test_anthropic.py`.
