# Verification record — experimentation layer v0.3

## Scope

Implemented on `feat/harness-experimentation-layer`, based on repository commit
`21cf1e159562514ca3b6d0a580266d442e1677c7`. No Durable Harness dependency or
integration. No change to `main` is required to use this branch.

## Local verification

Executed with Python 3.13.5, without provider API calls or credentials:

```sh
python -m unittest discover -s tests -v
```

**81 tests passed**, including existing workbench tests and standalone demos,
updated Anthropic tests, 13 core regression tests, and 43 experiment tests.

The Anthropic scripted regression now verifies that identical answers cannot
improve merely because the judge sees a different harness. A separate test
verifies that the judge input excludes the harness and preserves the full answer.

Core regression coverage includes composition rejection and evidence retention,
nonfinite scores, immutable legacy version conflicts, concurrent store writers,
path traversal rejection, and API-key redaction. Experiment tests cover isolated
fixtures and inputs, repetition/seed pairing, held-out selection, per-task
regression gates, budget/error/unknown-usage handling, model drift, synthetic
promotion rejection, command opt-ins, timeouts and output limits, immutable
records, report escaping, and development-only proposals.

## End-to-end command demo

```sh
python -m harness_workbench.experiments run \
  --config examples/experiments/sort-demo.json \
  --root /tmp/harness-smoke --allow-commands --json
```

Verified experiment: `exp-362c3d70a91242d7aa96d258c6668635`.
It completed **66 independent trials**: 18 development, 36 validation and 12
regression trials. `numeric` was selected on development data, then evaluated on
six held-out validation tasks and two regression tasks. All executions completed
without errors. The validation gates passed; the final decision was **keep the
base**, with reason `synthetic_backend_not_production_evidence`.

This is intentional. The demo is a deterministic protocol/fixture test, not
empirical evidence that an LLM harness improved. It incurs no model API cost.

A shorter container invocation was interrupted during a previous smoke run;
its partial evidence remained inspectable, with no final decision or promotion.
The separate complete run above is the verification result. Automatic resume is
not implemented or claimed.

## Remote CI verification (2026-10-22)

The workflow [Harness workbench tests](https://github.com/Montinou/harness-engineering-patterns/actions/runs/39724273286)
completed successfully for source commit
[`242ac0e4d83e5fa89968826e4fb981909b9ccc75`](https://github.com/Montinou/harness-engineering-patterns/commit/242ac0e4d83e5fa89968826e4fb981909b9ccc75).

All three jobs succeeded: **Python 3.10, 3.12 and 3.13**.
Each job reported **81 tests passed**, plus successful execution of the
offline command-adapter experiment smoke test. The CI outcome validates the
automated tests and synthetic example on those environments; it does **not**
establish live LLM quality, production-agent isolation or real provider costs.

## Not verified by this record

- Live Anthropic responses, billing, or comparative model performance.
- External coding-agent adapters, remote memory isolation or remote side effects.
- Hostile-code sandboxing: the command runner executes trusted local programs.
- Generalization beyond the declared validation tasks. Reusing a holdout while
  tuning candidates invalidates its independence.

See [experiments.md](experiments.md) for the full contracts and limitations.
