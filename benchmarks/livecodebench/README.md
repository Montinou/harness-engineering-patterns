# LiveCodeBench experiment runbook

Scripts and data definitions for the real-model LiveCodeBench experiment (design: spec sections 4 to 9). Nothing
here runs by itself: every step below that talks to a model needs a Gateway key and the human's go-ahead.
Problem texts and tests are never committed; every step writes under a pool directory you choose (`$POOL`).

| File | What it is |
|---|---|
| `import_lcb.py` | pinned LiveCodeBench rows to `pool.json`, `fixtures/<id>`, `graders/<id>` (hash-only graders) |
| `make_controls.py` | verified positive control and two negatives per problem (`controls/`, `controls_report.json`) |
| `make_manifests.py` | `assign` (splits) and `pilot` (the exploratory pilot manifest) |
| `base_harness.json` | the baseline harness: solve the problem, write `solution.py`; tools Bash, Read, Write, Edit; nothing else |
| `pilot_candidate.json` | the base plus one guardrail, "Run solution.py on the examples in PROBLEM.md before finishing." Never a confirmatory variant |

`base_harness.json` is the same harness as `make_controls.BASE_HARNESS` (a test compares their
`execution_config_hash`), so a task and its controls are generated and graded with one baseline.

## Setup (once per shell)

```bash
export POOL=$HOME/lcb-pool                      # outside the checkout; holds expected hashes in pool.json
export HARNESS_CLAUDE_PATH=$HOME/.local/share/claude/versions/2.1.316
export ANTHROPIC_API_KEY=...                    # a Gateway key with a spend limit; never written to a file
cat > $POOL.environment.json <<'JSON'
{
  "mode": "os_sandboxed",
  "srt": {"node": "/opt/homebrew/bin/node",
          "cli": "/Users/agustinmontoya/.local/share/harness-tools/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js",
          "version": "0.0.81"},
  "egress_allow": ["ai-gateway.vercel.sh:443"],
  "solver_read_paths": ["/Users/agustinmontoya/.local/share/claude/versions/2.1.316"],
  "parallelism": 6,
  "limits": {"timeout_seconds": 900}
}
JSON
```

`make_controls.py` and the pilot manifest add `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` to the solver's
`solver_env_passthrough` themselves; graders never see them. The pinned Claude Code is 2.1.316 and the adapter
refuses any other version. Run the reach test first (`HARNESS_REAL_MODEL_TESTS=1`, see
`tests/integration/test_claude_code_reach.py`).

## 1. Import the pool

```bash
python benchmarks/livecodebench/import_lcb.py --revision <40-hex dataset commit> \
  --files test5.jsonl test6.jsonl --out $POOL
```

Read `import_report.json`: counts per platform and difficulty (the reviewed run imports 308 problems: easy 83,
medium 89, hard 136; AtCoder and LeetCode), exclusions with reasons, and `near_duplicates`.

## 2. Build the controls

```bash
python benchmarks/livecodebench/make_controls.py --pool $POOL --model anthropic/claude-opus-5.5 \
  --environment $POOL.environment.json --claude $HARNESS_CLAUDE_PATH
```

Runs on the whole pool, before the splits. Keeps a problem only when its generated solution passes the real
grader and both negatives (empty, examples only) fail. Read `controls_report.json`: its `kept` ids are the pool
(spec section 8); excluded problems are listed with a reason and never reach a split. A `--limit` run, or one
stopped before every problem was decided, writes `"complete": false` (and the `limit`); `make_manifests.py`
refuses such a report, so rerun on the whole pool.

## 3. Assign the splits

```bash
python benchmarks/livecodebench/make_manifests.py assign --pool $POOL --seed 20261101
```

Draws only from the `kept` ids of `$POOL/controls_report.json` and refuses (exit 2) when that report is
missing. Writes `$POOL/splits.json`: 25 development problems (15 medium or hard, 10 easy), 50 validation (medium or hard)
and 80 regression (easy or medium), stratified by platform, deterministic for a pool and seed. If a quota cannot
be filled, which includes the controls leaving too few problems, the command exits 2, prints every short stratum
with `need` and `have` (over the kept problems), and writes nothing; the `binary_v1` floors (50 validation and
80 regression units) are never lowered. Stop and report.

Every problem is its own family. Each connected group of the importer's `near_duplicates` is reduced to its
easiest member; the rest are dropped from the assignment and listed under `dropped_near_duplicates`. Because they
are not independent units, they do not go into `duplicate_review`.

## 4. Write the pilot manifest and run the pilot

```bash
python benchmarks/livecodebench/make_manifests.py pilot --pool $POOL \
  --environment $POOL.environment.json --out $POOL/pilot.json
harness-workbench experiment --provider command --spec $POOL/pilot.json --root $POOL/workbench
```

The manifest is exploratory and holds development problems only, each one with verified controls (the tool refuses any other; an exploratory run records holdout exposure,
which would block the later confirmatory reservation), the base harness against `pilot_candidate.json`, 3
repetitions, and the Claude Code adapter as the command executor:

```
{python} -I {claude_code_agent} --harness {harness_json} --task {task_json} --workspace {workspace}
  --result {result_json} --model anthropic/claude-haiku-5.5 --max-budget-usd 0.50
  --claude <HARNESS_CLAUDE_PATH> --claude-version 2.1.316
```

`executor.supports` is exactly the adapter's seven components (never `plugins`), `executor.timeout_seconds` is
900 (the tool refuses a timeout that does not exceed the adapter's 30 s stop margin), the environment's
`limits.timeout_seconds` is at least that (the run stops a trial at the smaller of the two and the loader
defaults the environment's to 300 s: the tool sets it to the executor's when you leave it out and refuses a
lower one), and `statistics.timeout_is_task_failure` is true. Each task's `oracle` check carries the expected hashes; the grader
only observes hashes and the supervisor compares them, so neither the expected outputs nor the hashes are in
`graders/` or in the solver's workspace.

Other options: `--model`, `--max-budget-usd`, `--claude-version`, `--repetitions`, `--timeout-seconds`,
`--splits`. About 150 trials (25 problems x 2 harnesses x 3 repetitions).

## 5. Gates

Record the pilot's gate results as a progress entry before anything confirmatory: G1 infrastructure (at least 95 %
of trials without an infrastructure status), G2 headroom (base pass rate on the 15 medium and hard development
problems between 0.15 and 0.85), G3 cost (projected confirmatory spend at most $65), G4 time (at most 24 h), G5
reach test on the pinned CLI. The confirmatory manifest and candidate generation are a later step.

## Tests

```bash
python -m unittest tests.lcb.test_lcb_manifests tests.lcb.test_lcb_controls -v
```
