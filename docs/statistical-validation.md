# Statistical validation of the `binary_v1` profile

This document is the pre-registered calibration and the final offline validation of the confirmatory decision
rule (spec FIX-03, section 5). Every number below comes from a result file under
`docs/statistical-validation/`; every outcome in those files is **synthetic** (Bernoulli or Beta draws through
the real contracts, selection and decision functions). Nothing here measures a language model.

## 1. Result in one table

| | Value |
|---|---|
| Profile | `binary_v1` |
| Method | `cluster_percentile_ci_v1` (10000 bootstrap resamples of whole independent units) |
| `alpha` / `practical_margin` | 0.05 / 0.05 (fixed before any simulation, never part of the grid) |
| Independent validation units (`min_independent_units`, N) | 50 |
| Independent regression units (`min_regression_units`, R) | 80 |
| `regression_margin` | **0.10** (non-inferiority tolerance, see the limit in section 9) |
| Repetitions | 5 |
| Final validation (seed 202, fresh) | worst null `upper_95` **0.0553** (`boundary-d1`, bound 0.06); target power `lower_95` **0.8359** (bound 0.80) |
| Seed 203 re-run | not needed (no null scenario had a point estimate below 0.06 and an `upper_95` above it) |

The calibration did **not** pass as the plan wrote it: at the plan's 1000 simulations no grid point satisfied
both bounds. The profile was selected after a human-approved deviation (section 5) that tightens the
Monte Carlo width of the null check and leaves every threshold, `alpha`, `practical_margin` and the selection
rule untouched. Read section 5 before relying on the result.

## 2. Estimand, units, hypotheses

Per task, the mean over the paired repetitions of the candidate minus baseline score; per family, the mean of
its task deltas; the effect is the mean over families (spec 5.1). Each independent unit has equal weight.

```text
d_task   = mean_repetitions(score_candidate - score_baseline)
d_family = mean_tasks_of_family(d_task)
effect   = mean_families(d_family)
```

The family is the independent unit: repetitions, checks, steps and retries never raise the unit count. The
last attempt of each `(variant, task, repetition)` is used. The population is "tasks like the validation
families of one manifest"; the independence hypothesis is that families are exchangeable draws, which the
exposure registry only checks locally (section 9).

* Primary hypothesis: `H0: effect <= practical_margin` against `H1: effect > practical_margin`. The candidate
  is promotable only if the one-sided lower bound at level `alpha` is above `practical_margin`.
* Regression gate: for each predefined regression group the one-sided lower bound of `effect_regression` must
  be above `-regression_margin` (non-inferior, alpha split across groups by Bonferroni). A group whose upper
  bound is below `-regression_margin` is a demonstrated regression (rejected); a group the data cannot decide
  is `insufficient_evidence`.
* Fail-closed: too few units, a degenerate interval, an unusable trial, unknown usage or an integrity block never
  produce `eligible`.

## 3. Methods and the reference check

* `cluster_percentile_ci_v1`: seeded percentile bootstrap over independent units (`stats.py`), 10000 resamples.
* `cluster_t_v1`: Student t bound over the same units.
* Reference check: `tests/statistical/test_stats.py::BoundTests::test_percentile_bound_matches_reference_within_tolerance`
  compares the implementation with an independent reference bootstrap (`reference_lower_bound`, 10000
  resamples, different seed) on 40 units; the bounds agree within 0.01. A second test in
  `test_simulate.py` checks the cached bound against the same reference within 0.02 at 300 resamples.

## 4. Simulator, scenarios, assumptions

`python3 -m harness_workbench.experiments.simulate` draws synthetic `TrialRecord`s, runs the real
`select_finalist` on the development records, freezes the choice with the real `freeze_selection` and hands
the holdout records to the real `decide`. The assumptions (task difficulty, family correlation, the Beta
concentration `K = 10`, a random true candidate among the development candidates, the fixed statistics seed of
the manifest) are in the module docstring of `harness_workbench/experiments/simulate.py`. The random draws of
simulation `i` of scenario `s` come from `random.Random(f"{seed}:{s.id}:{i}")`, so a result does not depend on
workers or on how the index range is split.

The `full` matrix has 59 scenarios: 48 factorial null scenarios (1, 5 or 20 development candidates; 10 or N
units; 1, 3, 5 or 10 repetitions; binary or continuous outcome), plus `hetero-null/power` (difficulty spread
0.2), `famcorr-null/power` (families of 3 tasks, correlation 0.5), `concentrated-power` (25 % of the units
carry the whole effect), `boundary-d1/d5/d20` (true effect exactly the practical margin with 1, 5 and 20
development candidates), `regression-blocked` (effect 0.2 but regression -0.10), `missingness-5pct` (5 % of the
trials end in an execution error) and the pre-registered `target-power` (success 0.50 to 0.70, no regression).
At the final profile the null scenarios are 54 (including those named), the power scenarios 5.

Acceptance bounds (spec 5.4, fixed before running): every null scenario `upper_95 <= 0.06` and the target
scenario `lower_95 >= 0.80`, with one-sided 95 % Wilson bounds (`z = 1.6449`) on the promotion rate.

## 5. Calibration (seed 101, 1000 simulations): the stop

### 5.1 Pre-registered grid and selection rule

Grid (`CALIBRATION_GRID`, 64 points): method in {`cluster_percentile_ci_v1`, `cluster_t_v1`}; units in
{30, 40, 50, 60}; regression units in {30, 40, 60, 80}; `regression_margin` in {0.05, 0.10}; repetitions 5.
`alpha`, `practical_margin`, the seeds and the thresholds are not in the grid. Selection rule, fixed before any
run: among the passing points choose the smallest `regression_margin`, then the smallest
`units + regression_units`, then `cluster_percentile_ci_v1` before `cluster_t_v1`. Ties of the sum were broken
by the smaller `units` (not stated in the plan).

### 5.2 What was run

The plan asked for one full-matrix run per point. One full-matrix point costs about 4500 CPU-seconds
(22 to 30 minutes wall on a heavily loaded laptop), 64 of them about ten hours of CPU. I therefore screened
every point on the pre-registered target power scenario first (`screen_grid.py`: `run_scenario` on
`target_scenario(profile)`, seed 101, 1000 simulations, the same code path and the same draws as the full
matrix, so the target result of a point does not depend on which of the two ways it was run). A point whose
target `lower_95` is below 0.80 fails the point; its null scenarios were not run. This is a deviation from "one
full run per point". Files: 64 under `calibration/grid-seed101-<method>-<N>-<R>-<margin>.json`; 59 are
`target-screen` files (target scenario only), 5 are full-matrix files (the points that cleared the target bound
plus the plan's reference point 30/30/0.05).

Local command of a full-matrix point (what the five full files are):

```bash
python3 -m harness_workbench.experiments.simulate --profile binary_v1 --matrix full \
  --null-simulations 1000 --power-simulations 1000 --seed 101 --method <m> --units <N> \
  --regression-units <R> --regression-margin <g> --workers 8 \
  --out docs/statistical-validation/calibration/grid-seed101-<m>-<N>-<R>-<g>.json
```

### 5.3 Result: target power `rate / lower_95` of all 64 points (seed 101, 1000 simulations)

`regression_margin = 0.05` (no point reaches 0.80):

| N | R | percentile | t |
|---|---|---|---|
| 30 | 30 | 0.221 / 0.200 | 0.195 / 0.175 |
| 30 | 40 | 0.259 / 0.237 | 0.243 / 0.221 |
| 30 | 60 | 0.312 / 0.288 | 0.303 / 0.280 |
| 30 | 80 | 0.364 / 0.339 | 0.359 / 0.334 |
| 40 | 30 | 0.207 / 0.187 | 0.183 / 0.164 |
| 40 | 40 | 0.242 / 0.220 | 0.232 / 0.211 |
| 40 | 60 | 0.309 / 0.286 | 0.314 / 0.290 |
| 40 | 80 | 0.363 / 0.338 | 0.362 / 0.337 |
| 50 | 30 | 0.199 / 0.179 | 0.178 / 0.159 |
| 50 | 40 | 0.222 / 0.201 | 0.218 / 0.197 |
| 50 | 60 | 0.296 / 0.273 | 0.298 / 0.275 |
| 50 | 80 | 0.372 / 0.347 | 0.373 / 0.348 |
| 60 | 30 | 0.225 / 0.204 | 0.205 / 0.185 |
| 60 | 40 | 0.254 / 0.232 | 0.252 / 0.230 |
| 60 | 60 | 0.333 / 0.309 | 0.332 / 0.308 |
| 60 | 80 | 0.408 / 0.383 | 0.413 / 0.388 |

`regression_margin = 0.10` (only the starred rows reach `lower_95 >= 0.80`):

| N | R | percentile | t |
|---|---|---|---|
| 30 | 30 | 0.484 / 0.458 | 0.463 / 0.437 |
| 30 | 40 | 0.558 / 0.532 | 0.539 / 0.513 |
| 30 | 60 | 0.691 / 0.666 | 0.676 / 0.651 |
| 30 | 80 | 0.743 / 0.720 | 0.732 / 0.708 |
| 40 | 30 | 0.493 / 0.467 | 0.481 / 0.455 |
| 40 | 40 | 0.582 / 0.556 | 0.574 / 0.548 |
| 40 | 60 | 0.727 / 0.703 | 0.728 / 0.704 |
| 40 | 80 | 0.796 / 0.774 | 0.794 / 0.772 |
| 50 | 30 | 0.485 / 0.459 | 0.481 / 0.455 |
| 50 | 40 | 0.603 / 0.577 | 0.598 / 0.572 |
| 50 | 60 | 0.755 / 0.732 | 0.756 / 0.733 |
| **50 \*** | **80 \*** | **0.843 / 0.823** | **0.843 / 0.823** |
| 60 | 30 | 0.520 / 0.494 | 0.512 / 0.486 |
| 60 | 40 | 0.634 / 0.609 | 0.631 / 0.606 |
| 60 | 60 | 0.800 / 0.778 | 0.799 / 0.777 |
| **60 \*** | **80 \*** | **0.888 / 0.871** | **0.890 / 0.873** |

The regression non-inferiority gate cannot be cleared at tolerance 0.05 with R up to 80, which is why the
power stays at or below 0.41. The point (percentile, N = 30, R = 30, 0.05) is the full-matrix reference; its worst null
`upper_95` was 0.0240 (`boundary-d1`) and its target `lower_95` 0.200.

### 5.4 Result: the four target-passing points on the full null matrix (seed 101, 1000 simulations)

| Point | target `lower_95` | null scenarios with `upper_95 > 0.06` |
|---|---|---|
| percentile, 50, 80, 0.10 | 0.823 | `boundary-d1` 57/1000, rate 0.057, upper 0.0703; `regression-blocked` 48/1000, rate 0.048, upper 0.0604 |
| t, 50, 80, 0.10 | 0.823 | `boundary-d1` 54/1000, rate 0.054, upper 0.0670 |
| percentile, 60, 80, 0.10 | 0.871 | `boundary-d1` 50/1000, rate 0.050, upper 0.0626 |
| t, 60, 80, 0.10 | 0.873 | `boundary-d1` 53/1000, rate 0.053, upper 0.0659 |

Every other null scenario passes at all four points. The cause is the width of the interval and not the rate:
with 1000 simulations a perfectly exact one-sided 5 % test that rejects at 0.050 already has `upper_95 =
0.0626`, so the bound `upper_95 <= 0.06` cannot be met by any method whose boundary rate is near 0.05. At
the plan's own rule, **no point passed and the calibration stopped**. The thresholds, `alpha`,
`practical_margin` and the seeds were not touched.

## 6. Selection recheck (seed 104, 20000 null simulations): the deviation

### 6.1 Decision

The stop was escalated to the human. Ruling (recorded in the plan ledger): treat the near-misses as Monte
Carlo width, the same tightening the plan's Step 2 already allows for a null scenario whose point estimate is
below 0.06 and whose `upper_95` exceeds it by width alone. Re-run the four margin-0.10 candidates (the points
of 5.4) on **every null scenario of the full matrix** at **20000 simulations** with a **new seed, 104** (never
101, 202 or 203), select by the unchanged pre-registered rule, then run Step 2's final validation on fresh
seeds 202 (and 203 if needed). Not changed: `alpha`, `practical_margin`, the 0.06 and 0.80 thresholds, the
selection rule, the seeds of the final validation. Accepted by the human: the 10-point regression tolerance
(section 9).

Why this is not seed fitting: the candidates and the rule were fixed by the grid before any run, the recheck
uses a seed never used to look at these points, all four candidates were rechecked (not only the favorite), no
scenario was dropped, and the power evidence the final profile is judged by is a separate fresh-seed run
(section 7). Why it is nonetheless a deviation: the pre-registered 1000-simulation run failed, and the
count was raised after seeing that failure.

### 6.2 Result (all four pass `upper_95 <= 0.06`)

Null scenarios only, 54 scenarios x 20000 simulations, seed 104. Files:
`selection-seed104-<method>-<N>-<R>-0.1.json`.

| Candidate | `boundary-d1` | `boundary-d5` | `boundary-d20` | `regression-blocked` | `hetero-null` | worst of all 54 |
|---|---|---|---|---|---|---|
| percentile 50/80 | 968 (0.0484, upper 0.0510) | 666 (0.0355) | 432 (0.0234) | 971 (0.0511) | 48 (0.0030) | 0.0511 `regression-blocked` |
| t 50/80 | 889 (0.0469) | 616 (0.0329) | 389 (0.0211) | 975 (0.0513) | 41 (0.0026) | 0.0513 `regression-blocked` |
| percentile 60/80 | 882 (0.0466) | 665 (0.0354) | 469 (0.0253) | 969 (0.0510) | 33 (0.0022) | 0.0510 `regression-blocked` |
| t 60/80 | 871 (0.0460) | 664 (0.0353) | 473 (0.0255) | 977 (0.0514) | 33 (0.0022) | 0.0514 `regression-blocked` |

(Cells are promotions out of 20000, with the Wilson `upper_95` in parentheses; `famcorr-null` is 0/20000 at
all four.) By the pre-registered rule: smallest `regression_margin` is a tie at 0.10, smallest
`units + regression_units` is 50 + 80, percentile before t: **`cluster_percentile_ci_v1`, N = 50, R = 80,
`regression_margin = 0.10`**. The power of the four candidates in the selection came from the seed-101,
1000-simulation grid (5.3); the profile is judged by the fresh seed in section 7, not by that estimate (which
carries the usual winner's-curse optimism).

### 6.3 Supplementary only: seed 101 at 20000 simulations

Before the ruling, the failing null scenarios of the best candidates were re-run at 20000 simulations on the
**same seed 101** as the grid (`supplementary-seed101-20000-sims.json`, script
`supplementary_seed101_recheck.py`). This is supplementary information, not a selection: percentile 60/80
`boundary-d1` 916/20000 (upper 0.0483); percentile 50/80 `boundary-d1` 1003/20000 (upper 0.0527) and
`regression-blocked` 950/20000 (upper 0.0500). It motivated the human decision; the selection itself rests on
seed 104.

## 7. Final validation (seed 202, fresh)

Profile set to the selected values, **no overrides**:

```bash
python3 -m harness_workbench.experiments.simulate --profile binary_v1 --matrix full \
  --null-simulations 5000 --power-simulations 2000 --seed 202 --workers 8 \
  --out docs/statistical-validation/final-seed202.json
```

File: `final-seed202.json` (59 scenarios; implementation hash `ee30a2d0857e8822436dba882a30a7d91f7780591653cdef90adb57dfd94edae`).

| Scenario | Kind | Promoted / sims | Rate | `lower_95` | `upper_95` | Bound |
|---|---|---|---|---|---|---|
| `boundary-d1` (effect = margin, 1 candidate) | null | 250 / 5000 | 0.0500 | 0.0452 | **0.0553** | <= 0.06 ok |
| `boundary-d5` (5 candidates) | null | 177 / 5000 | 0.0354 | 0.0313 | 0.0400 | <= 0.06 ok |
| `boundary-d20` (20 candidates) | null | 109 / 5000 | 0.0218 | 0.0187 | 0.0255 | <= 0.06 ok |
| `regression-blocked` | null | 206 / 5000 | 0.0412 | 0.0368 | 0.0461 | <= 0.06 ok |
| `hetero-null` | null | 11 / 5000 | 0.0022 | 0.0013 | 0.0036 | <= 0.06 ok |
| `famcorr-null` | null | 0 / 5000 | 0.0000 | 0.0000 | 0.0005 | <= 0.06 ok |
| 48 factorial nulls (max) | null | `null-d20-n50-r1-binary` 40 / 5000 | 0.0080 | 0.0062 | 0.0104 | <= 0.06 ok |
| **`target-power`** (0.50 to 0.70) | power | 1699 / 2000 | **0.8495** | **0.8359** | 0.8622 | `lower_95` >= 0.80 ok |
| `hetero-power` | power | 1741 / 2000 | 0.8705 | 0.8576 | 0.8824 | diagnostic |
| `famcorr-power` | power | 2000 / 2000 | 1.0000 | 0.9986 | 1.0000 | diagnostic |
| `concentrated-power` | power | 1357 / 2000 | 0.6785 | 0.6611 | 0.6954 | diagnostic |
| `missingness-5pct` | power | 0 / 2000 | 0.0000 | 0.0000 | 0.0014 | diagnostic (fails closed) |

Factorial nulls (true effect 0, 5000 simulations): all 24 scenarios with 10 units are `insufficient_evidence`
(0 promotions, below the plan floor). At 50 units the promotions are 26/17/17/3 (binary, 1 candidate, 1/3/5/10
repetitions), 30/16/12/2 (5 candidates), 40/28/18/3 (20 candidates) and at most 4 for continuous outcomes.

* Every null scenario has `upper_95 <= 0.06` (worst 0.0553): **no scenario needed the seed-203 re-run**, which
  the plan allows only for a point estimate below 0.06 whose `upper_95` exceeds it by Monte Carlo width. No
  `final-seed203.json` exists.
* Target power `lower_95` is 0.8359, above 0.80.
* `concentrated-power` (25 % of units carry the effect) is below 0.80 and is **not** an acceptance scenario:
  the profile does not promise power when the improvement is concentrated in a few tasks.
* `missingness-5pct` never promotes: with 5 % of trials as execution errors every simulated experiment is
  `invalid_experiment` (fail-closed). A real run with infrastructure errors therefore needs the retry policy
  to clear them first.

## 8. Recreations of the audit scenarios A2, A3, A8

These are diagnostics, not the auditor's numbers (spec 5.4 asks for no equality of percentages).

New protocol (`recreations.json`, seed 303, 2000 simulations, `--matrix recreations`; the scenarios do not
depend on the profile): A2 (3 tasks, 5 candidates, no effect) 0/2000 promotions, all
`insufficient_evidence`; A3 (1 task x 5 repetitions) 0/2000; A8 (5 tasks, 0.5 to 0.7, 1 candidate) 0/2000.
Each is held below the sampling plan floor and returns `insufficient_evidence` with the reasons recorded.

Audited commit (`audit-recreation-73bcb13.txt`): the auditor's own `repro/u2_mc.py`, unchanged, run on a
worktree detached at 73bcb1306509dde7fff2f15bc43504b100f83984 (recreation of the auditor's script on the
audited commit, **not** a claim of this work):

```text
tasks=3 variants=5 p_base=0.5 p_var=0.5: recommended in 34/400 = 8.5% (52s)
tasks=1 variants=1 p_base=0.5 p_var=0.5: recommended in 12/400 = 3.0% (6s)
tasks=5 variants=1 p_base=0.5 p_var=0.7: recommended in 47/300 = 15.7% (21s)
```

These equal the auditor's published 34/400, 12/400 and 47/300 exactly (the script seeds each run).

## 9. Limits of extrapolation

* **The regression tolerance is 0.10.** A candidate that loses up to about ten points of success rate on the
  protected regression families can still be called non-inferior. This is twice the practical margin of the
  primary comparison, it is what made the plan reach 80 % power at an affordable size, and the human accepted
  it. A domain that cannot tolerate that loss needs a smaller `regression_margin` and therefore a larger R
  (at 0.05 even R = 80 reaches only about 41 % power, section 5.3).
* **Synthetic outcomes.** Bernoulli and Beta draws with independent repetitions, a random true candidate and
  uniform or Gaussian difficulty offsets are not the distribution of language-model scores (heavy tails,
  within-task dependence of repetitions, drift). The error rates are those of this generator.
* **Local scope of the exposure registry.** The registry that keeps development exposure out of validation is
  local to one library; it cannot see evaluations run elsewhere.
* **Calibration deviation.** The selection rests on a 20000-simulation recheck chosen after the 1000-simulation
  failure (section 6); the thresholds were not changed, but the count was. If the true boundary rate is above 6 %
  the profile needs a larger sampling plan.
* **Power covers the target only.** 0.50 to 0.70 with no regression, independent families, no missingness.
  `concentrated-power` is 0.68 and `missingness-5pct` is 0.
* **One finalist, one primary metric.** More than one confirmatory finalist is rejected by the manifest.
* **Interpreter.** Results were produced on Python 3.14 (local 3.14.7 arm64 for the calibration, Modal 3.14.2
  x86_64 for the later runs); CI exercises 3.10, 3.12 and 3.13. Draws use `random.Random(str)` and the generator
  methods `random`, `gauss`, `uniform`, `betavariate`, `sample`, `randrange`; they were not re-run on the other interpreters.

## 10. Reproducibility

### 10.1 Equivalence of the Modal runner and the local CLI

The calibration (section 5) ran locally. The 20000-simulation runs of sections 6 and 7 and the recreations ran on
Modal because the local machine was overloaded. The Modal runner `sim_modal.py` (in this directory, unchanged
from what ran) splits the simulation index range of each scenario into chunks of 250, runs each chunk with the
same `simulate._run_chunk` and sums the status counters. Because simulation `i` of scenario `s` draws from
`random.Random(f"{seed}:{s.id}:{i}")`, the sum equals a local `run_scenario` call. Checks:

* pilot: `boundary-d1`, percentile 50/80/0.10, seed 104, 20000 simulations: **968/20000 on Modal and
  968/20000 locally** (the same count is in `selection-seed104-cluster_percentile_ci_v1-50-80-0.1.json`);
* second check, with the seed-101 calibration file as the local reference:
  `equivalence-modal-seed101-subset.json` (percentile 50/80/0.10, seed 101, 1000 simulations) gives
  `boundary-d1` 57/1000 and `regression-blocked` 48/1000, equal to the local CLI counts in
  `calibration/grid-seed101-cluster_percentile_ci_v1-50-80-0.1.json`.

The runner uploads the checked-out `harness_workbench/`, so it runs the code of the commit that contains it.
The Modal files carry a `runner` field and `python` `3.14.2 x86_64`.

### 10.2 Commands, Modal and local equivalent

All Modal commands run from the repository root with `PYTHONPATH=. modal run docs/statistical-validation/sim_modal.py ...`.

| Run | Modal | Local equivalent | Wall |
|---|---|---|---|
| Calibration, 5 full points + 59 screens (seed 101) | not run on Modal | `python3 -m harness_workbench.experiments.simulate ...` (section 5.2) and `screen_grid.py <out dir> <workers>` | about 22 to 30 min per full point, loaded laptop |
| Selection recheck (seed 104), per candidate | `--matrix full --seed 104 --null-simulations 20000 --null-only --method <m> --units <N> --regression-units <R> --regression-margin 0.10 --out docs/statistical-validation/selection-seed104-<m>-<N>-<R>-0.1.json` | `python3 docs/statistical-validation/null_recheck.py <m> <N> <R> 0.1 20000 104 <out>` (with `PYTHONPATH=.`) | 1393 s, 1430 s (percentile), 223 s, 232 s (t) |
| Final validation (seed 202) | `--matrix full --seed 202 --null-simulations 5000 --power-simulations 2000 --out docs/statistical-validation/final-seed202.json` | `python3 -m harness_workbench.experiments.simulate --profile binary_v1 --matrix full --null-simulations 5000 --power-simulations 2000 --seed 202 --workers 8 --out ...` | 366 s |
| Recreations (seed 303) | `--matrix recreations --simulations 2000 --seed 303 --out docs/statistical-validation/recreations.json` | `python3 -m harness_workbench.experiments.simulate --matrix recreations --simulations 2000 --seed 303 --out ...` | 107 s |
| Audit recreation | local only | `AUDIT_WORKTREE=<worktree at 73bcb13> python3 audits/2026-10-22_run-02_experimental-evaluation-layer_73bcb130/repro/u2_mc.py <tasks> <variants> <p_base> <p_var> <N>` with `3 5 0.5 0.5 400`, `1 1 0.5 0.5 400`, `5 1 0.5 0.7 300` | 52 s, 6 s, 21 s |

The selection recheck ran the candidates through explicit overrides on the then-provisional profile
(30/30/0.05, percentile); the final run and the recreations use the committed profile with no overrides.
`seed 203` was not run. The audit recreation used a worktree detached at the audited commit; no worktree was
removed.

### 10.3 Files

Every file this task commits under `docs/statistical-validation/` (the paths below are relative to that directory).
The `.json` files are generated output; the `.py` files are the drivers that produced or can reproduce them.

| File | What it is | Used in |
|---|---|---|
| `calibration/grid-seed101-<method>-<N>-<R>-<margin>.json` | The 64 per-point results of the pre-registered grid at seed 101, 1000 simulations: 5 full-matrix points (target power plus the null matrix) and 59 target-only screens (null scenarios not run for points that fail the power bound). | 5.2, 5.3, 5.4 |
| `screen_grid.py` | Driver of the target-only screen: `run_scenario` on the pre-registered target scenario for each grid point (seed 101, 1000 simulations), the same code path and draws as the full matrix. Produces the 59 screening files. | 5.2 |
| `null_recheck.py` | Local driver of the seed-104 null recheck of one candidate: every null scenario of the `full` matrix at a given simulation count, risky scenarios first, stopping at the first `upper_95 > 0.06`, writing a payload in the CLI's format. It is the local equivalent of the Modal command; the committed seed-104 files come from `sim_modal.py`. A local seed-104 run was interrupted (terminal crash) and its partial file was discarded in favor of the full Modal files. | 6.1, 6.2, 10.2 |
| `selection-seed104-<method>-<N>-<R>-0.1.json` | The four selection-recheck results (percentile and t, N/R 50/80 and 60/80, margin 0.10): the 54 null scenarios at 20000 simulations, seed 104; the file the selection rule is applied to. | 6.2, 10.1 |
| `sim_modal.py` | Modal runner: shards each scenario by simulation index (per-index RNG `seed:scenario:index`) and sums the counters, so it equals a local `run_scenario`. Produced the seed-104, seed-202 and seed-303 files and the equivalence subset. | 10.1, 10.2 |
| `equivalence-modal-seed101-subset.json` | Second equivalence check: Modal, percentile 50/80/0.10, seed 101, 1000 simulations, scenarios `boundary-d1` and `regression-blocked` only; the counts equal the local CLI counts in the seed-101 calibration file. | 10.1 |
| `supplementary_seed101_recheck.py` | Driver of the supplementary seed-101 recheck at 20000 simulations (percentile 60/80 `boundary-d1`; percentile 50/80 `boundary-d1` and `regression-blocked`). | 6.3 |
| `supplementary-seed101-20000-sims.json` | Output of that script. SUPPLEMENTARY: it motivated the human decision, it is not a selection. | 6.3 |
| `final-seed202.json` | The final validation of the selected profile on a fresh seed (59 scenarios, null at 5000 and power at 2000 simulations). | 1, 7 |
| `recreations.json` | The audit scenarios A2, A3, A8 run on the new protocol (seed 303, 2000 simulations). | 8 |
| `audit-recreation-73bcb13.txt` | Output of the auditor's own script run unchanged on the audited commit 73bcb13 (34/400, 12/400, 47/300). | 8 |

Machine of the local runs: Apple M4 (10 cores), Python 3.14.7, load average 100 to 220 during the calibration
(so wall times are inflated; results do not depend on workers or load).

## 11. Record for the ADR on the statistical protocol (ADR 0002, task 30)

The repository has no `docs/adr/` yet (task 30 creates it). ADR 0002 must carry, dated 2026-10-24:

> **Deviation.** The pre-registered calibration at 1000 simulations (seed 101) had no passing grid point: the
> four points that reached the target power failed one null `upper_95` by Monte Carlo width (0.0604 to 0.0703).
> By human decision the calibration was tightened by simulation count: the four margin-0.10 candidates were
> re-run on every null scenario at 20000 simulations with a new seed (104), and the unchanged selection rule
> chose `cluster_percentile_ci_v1`, 50 validation units, 80 regression units, `regression_margin` 0.10. The
> thresholds (0.06 and 0.80), `alpha`, `practical_margin` and the selection rule were not changed. The
> 10-point regression tolerance was accepted. The final validation on seed 202 passed (worst null `upper_95`
> 0.0553, target `lower_95` 0.8359). Seed-101 20000-simulation numbers are supplementary only.
