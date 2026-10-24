# Architecture decision records

Decisions made while remediating the experimental evaluation layer. Each record has Context, Decision,
Consequences, Limits and an "Enforced by" list of the tests that fail if the decision is broken. Test links use
`path::Class::test_name` and are run with `python -m unittest` (for example
`python -m unittest tests.unit.test_grader.EvaluateBundle.test_rewritten_check_script_in_artifact_does_not_matter`).

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-evaluator-trust-boundary.md) | The grader bundle lives outside the solver workspace; `trusted_local` is opt-in and not a sandbox; `oracle` checks plus `sandboxed` are required for tasks that run candidate code | Accepted |
| [0002](0002-statistical-protocol.md) | Estimand, independent units, pre-registered grid, the `binary_v1` profile and its numbers, margins, Bonferroni, zero-variance policy, single finalist, and the dated calibration deviation | Accepted, with deviation |
| [0003](0003-persistence-and-migration.md) | SQLite as the source of truth, JSON as exported views, `MigrationRequired`, no base renumbering, promotion by decision id, sequential Improve under a gate | Accepted |
| [0004](0004-single-branch-delivery-and-versioning.md) | Commits directly on `feat/experimental-evaluation-layer`, package stays 0.2.0, manifest schema v2, results schema v2, implementation order of spec section 10 | Accepted |

Related documents: [statistical validation](../statistical-validation.md), [audit remediation](../audit-remediation.md),
[experiments](../experiments.md).

Every number in ADR 0002 that comes from a simulation is synthetic. The auditor's figures quoted there (34/400 and
15.7 %) are the auditor's.
