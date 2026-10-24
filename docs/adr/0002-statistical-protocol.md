# ADR 0002: Statistical protocol of confirmatory experiments

- Status: Accepted, with the dated deviation below
- Date: 2026-10-24
- Spec: FIX-02, FIX-03 (sections 4 and 5). Audit findings: A2, A3, A8, A9.
- Evidence: `docs/statistical-validation.md` and the result files under `docs/statistical-validation/`.
  Every outcome in those files is **synthetic** (Bernoulli or Beta draws pushed through the real contracts,
  selection and decision functions). Nothing in this ADR measures a language model.

## Context

The audited runner promoted on a hierarchical bootstrap over repetitions, took the best of several candidates on
the same tasks that decided promotion, allowed one task with many repetitions to count as evidence, and let an
adaptive holdout be reused with renamed tasks. The auditor's own Monte Carlo script reported promotions on the
audited commit in 34/400 (8.5 %) for 3 tasks and 5 candidates with no effect, and in 47/300 (15.7 %) for 5 tasks
and one candidate with a true improvement from 0.5 to 0.7. Those numbers are the auditor's, reproduced unchanged
by running the auditor's script on the audited commit (`docs/statistical-validation/audit-recreation-73bcb13.txt`);
they are not a result of this work.

## Decision

**Estimand and units.** Per task, the mean over paired repetitions of candidate minus baseline score; per family,
the mean of its task deltas; the effect is the mean over families. The independent unit is the **family**.
Repetitions, checks, steps and retries never raise the unit count, and the last attempt of each
`(variant, task, repetition)` is used.

**Splits and one finalist.** Tasks belong to `development`, `validation` or `regression`; a family cannot span
splits, and duplicate or near-duplicate task content across splits is rejected unless reviewed. Candidates are
compared on development only. Exactly one finalist is frozen (hashes of plan, baseline, candidate and policy)
before any validation or regression trial runs, and only the finalist and the baseline run on the holdout. A
manifest that asks for more than one finalist is rejected. The exposure registry is keyed by the content
fingerprint of each holdout task, so renaming a task does not make it fresh.

**Hypotheses.** Primary: `H0: effect <= practical_margin` against `H1: effect > practical_margin`; the candidate
is eligible only if the one-sided lower bound at level `alpha` is above `practical_margin`. A lower bound at or
below the margin is `rejected`. Regression: for each predefined regression group, the one-sided lower bound of
the group's effect must be above `-regression_margin` (non-inferiority); a group whose upper bound is below
`-regression_margin` is a demonstrated regression (`rejected`), and a group the data cannot decide is
`insufficient_evidence`.

**Margins and level.** `alpha = 0.05` and `practical_margin = 0.05` are fixed before any simulation and are never
part of the calibration grid. `regression_margin` is a profile value (see the deviation and the limits).

**Bonferroni across regression groups.** `alpha` is divided by the number of regression groups for the
non-inferiority bounds (`group_alpha = alpha / groups`). With no explicit groups every regression family forms one
group and uses the full `alpha`. A regression family outside every group is `insufficient_evidence`.

**Zero-variance policy.** When every unit has the same value the spread is unobserved. The decision is
`insufficient_evidence` ("degenerate interval: zero variance across units") unless the manifest sets
`allow_zero_variance: true` with a non-empty `zero_variance_justification`, which is then recorded in the decision
diagnostics. The confirmatory demo uses this for its deterministic synthetic agent.

**Fail closed.** Too few independent units or regression units, incomplete pairs, an unusable baseline or finalist
trial, unknown token or latency usage, an integrity problem and a critical-check failure never produce `eligible`.

**Pre-registered grid and selection rule** (`CALIBRATION_GRID`, 64 points): method in
{`cluster_percentile_ci_v1`, `cluster_t_v1`}, validation units in {30, 40, 50, 60}, regression units in {30, 40,
60, 80}, `regression_margin` in {0.05, 0.10}, repetitions 5. Acceptance bounds, fixed before running, with
one-sided 95 % Wilson bounds on the promotion rate: every null scenario `upper_95 <= 0.06` and the pre-registered
target scenario (success 0.50 to 0.70, no regression) `lower_95 >= 0.80`. Selection among passing points: the
smallest `regression_margin`, then the smallest `units + regression_units`, then `cluster_percentile_ci_v1`
before `cluster_t_v1`.

**Selected profile `binary_v1`:** method `cluster_percentile_ci_v1` (10000 bootstrap resamples of whole units),
`alpha` 0.05, `practical_margin` 0.05, `regression_margin` 0.10, 50 independent validation units, 80 independent
regression units, 5 repetitions. Final validation on a fresh seed (202): worst null `upper_95` **0.0553**
(`boundary-d1`, 250/5000 promotions, bound 0.06) and target power `lower_95` **0.8359** (1699/2000, bound 0.80),
synthetic. Seed 203 was not needed and no `final-seed203.json` exists.

### Deviation (2026-10-24)

> **Deviation.** The pre-registered calibration at 1000 simulations (seed 101) had no passing grid point: the
> four points that reached the target power failed one null `upper_95` by Monte Carlo width (0.0604 to 0.0703).
> By human decision the calibration was tightened by simulation count: the four margin-0.10 candidates were
> re-run on every null scenario at 20000 simulations with a new seed (104), and the unchanged selection rule
> chose `cluster_percentile_ci_v1`, 50 validation units, 80 regression units, `regression_margin` 0.10. The
> thresholds (0.06 and 0.80), `alpha`, `practical_margin` and the selection rule were not changed. The
> 10-point regression tolerance was accepted. The final validation on seed 202 passed (worst null `upper_95`
> 0.0553, target `lower_95` 0.8359). Seed-101 20000-simulation numbers are supplementary only.

Why this is a deviation and not seed fitting: all four candidates and the rule were fixed by the grid before any
run, the recheck used a seed never used to look at these points, no scenario was dropped, and the profile is
judged by a separate fresh-seed run. It is still a deviation because the pre-registered 1000-simulation run
failed and the simulation count was raised after seeing that failure. The screening of the grid on the target
scenario first (59 of 64 points never ran the null matrix) is a second, smaller departure from "one full run per
point"; it does not depend on how a point was run because the draws are per simulation index.

### Plan-level notes recorded from the Phase 5 review

Both were disclosed and accepted by the human.

1. **The 1000-simulation check could not pass for a near-nominal test.** With 1000 simulations an exact one-sided
   5 % test that rejects 50 times in 1000 already has a one-sided Wilson `upper_95` of 0.0626, above the 0.06
   bound. The plan's Step 1 therefore could not succeed for any method whose boundary rate is near 0.05, which is
   what caused the stop.
2. **`regression_margin` was placed in the calibration grid.** The spec (section 5.3) says the tolerance is fixed
   by the domain before running. Treating it as a grid dimension is in tension with that text. The human accepted
   the grid, and afterwards the 0.10 result, instead of re-deriving a tolerance from a domain.

## Consequences

- Selection happens on development data only, one finalist is confirmed, and a one-task experiment with many
  repetitions is `insufficient_evidence`; the recreations of the audit scenarios A2, A3 and A8 (synthetic, seed
  303, 2000 simulations) show 0/2000 promotions and `insufficient_evidence` for each.
- A manifest's `statistics` block must match the profile's `method`, `alpha`, `practical_margin` and
  `regression_margin` exactly, and its units, repetitions and bootstrap iterations must not be below the profile
  floors.
- The offline simulator runs the real `select_finalist`, `freeze_selection` and `decide`, so a change to the
  decision rule can be re-calibrated by re-running `python3 -m harness_workbench.experiments.simulate`.

## Limits

- **The regression tolerance is 0.10.** A candidate that loses up to about ten points of success rate on the
  protected regression families can still be called non-inferior. That is twice the practical margin. At
  tolerance 0.05 even 80 regression units reach only about 41 % target power in the calibration grid. A domain
  that cannot accept this needs a smaller `regression_margin` and a larger regression sample, then a new
  calibration.
- **Synthetic outcomes.** The error rates and power are those of the simulator's generator (Bernoulli or Beta
  draws, independent repetitions, a random true candidate, uniform or Gaussian difficulty offsets). They are not
  the distribution of language-model scores.
- **Power covers the target scenario only.** Power is 0.8495 for the target (success 0.50 to 0.70, no regression,
  independent families, no missingness). It is 0.6785 when 25 % of the units carry the whole effect
  (`concentrated-power`, diagnostic) and 0 with 5 % execution errors (`missingness-5pct`, which fails closed).
- **The null bound rests on a recheck chosen after a failure.** If the true boundary promotion rate is above 6 %,
  the profile needs a larger sampling plan.
- **Several regression groups.** Bonferroni with two or more groups, and manifests above the profile floors, were
  not calibrated; the spec asks for recalibration when groups are added.
- **Local exposure registry.** It cannot see evaluations run in another library.
- **One finalist, one primary metric.**
- **Interpreter.** Calibration and validation ran on Python 3.14 (local 3.14.7 arm64; Modal 3.14.2 x86_64). CI
  exercises 3.10, 3.12 and 3.13 but the simulations were not re-run there.

## Enforced by

- `tests/statistical/test_simulate.py::ProfileTests::test_binary_v1_is_published_with_the_selected_values`
- `tests/statistical/test_simulate.py::ProfileTests::test_the_calibration_grid_is_the_pre_registered_one`
- `tests/statistical/test_simulate.py::ProfileEnforcementTests::test_profile_mismatch_is_rejected_at_load`
- `tests/statistical/test_simulate.py::ProfileEnforcementTests::test_floors_are_read_from_the_profile_table`
- `tests/statistical/test_simulate.py::WilsonTests::test_wilson_interval_known_values`
- `tests/statistical/test_simulate.py::MatrixTests::test_full_matrix_has_every_required_dimension_and_the_target_scenario`
- `tests/statistical/test_simulate.py::MatrixTests::test_recreations_are_a2_a3_a8`
- `tests/statistical/test_simulate.py::ProtocolTests::test_null_smoke_rate_is_small_and_power_smoke_rate_is_large`
- `tests/statistical/test_stats.py::BoundTests::test_percentile_bound_matches_reference_within_tolerance`
- `tests/statistical/test_stats.py::BoundTests::test_zero_variance`
- `tests/statistical/test_stats.py::NoninferiorityTests::test_noninferiority_three_outcomes`
- `tests/unit/test_decide.py::EvidenceTests::test_zero_variance_is_insufficient_unless_allowed_with_justification`
- `tests/unit/test_decide.py::EvidenceTests::test_bound_below_margin_is_rejected_not_insufficient`
- `tests/unit/test_decide.py::RegressionGroupTests::test_bonferroni_across_groups`
- `tests/unit/test_decide.py::RegressionGroupTests::test_default_group_covers_all_regression_families_with_the_full_alpha`
- `tests/unit/test_decide.py::RegressionGroupTests::test_regression_group_demonstrated_regression_rejects_and_wide_uncertainty_is_insufficient`
- `tests/unit/test_decide.py::ModeTests::test_a_confirmatory_spec_with_several_candidates_needs_a_frozen_selection`
- `tests/unit/test_protocol.py::SelectionTests::test_select_finalist_uses_development_only_and_breaks_ties_deterministically`
- `tests/unit/test_protocol.py::FrozenSelectionTests::test_freeze_selection_binds_the_hashes_and_round_trips`
- `tests/unit/test_manifest.py::ConfirmatoryTests::test_finalists_must_be_one`
- `tests/unit/test_manifest.py::ConfirmatoryTests::test_confirmatory_rejects_duplicate_content_across_splits_and_unreviewed_near_duplicates`
- `tests/unit/test_manifest.py::ConfirmatoryTests::test_confirmatory_rejects_a_family_spanning_splits`
- `tests/unit/test_run_experiment_phases.py::FinalistTests::test_only_finalist_and_baseline_run_on_holdout`
- `tests/unit/test_run_experiment_phases.py::ExposureTests::test_second_experiment_with_other_candidate_on_same_holdout_is_exploratory_only_even_with_renamed_tasks`
- `tests/adversarial/test_audit_reproductions.py::A02SingleFinalist::test_only_one_variant_reaches_validation_tasks`
- `tests/adversarial/test_audit_reproductions.py::A03MinimumUnits::test_one_task_many_repetitions_is_not_eligible`
- `tests/adversarial/test_audit_reproductions.py::A09HoldoutReuse::test_renamed_holdout_second_candidate_is_not_eligible`

The calibration numbers themselves are data, not tests: `docs/statistical-validation/final-seed202.json`,
`docs/statistical-validation/selection-seed104-*.json`, `docs/statistical-validation/calibration/` and
`docs/statistical-validation/recreations.json`.
