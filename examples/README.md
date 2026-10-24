# Executable Examples

Simplified, runnable sketches of the main harness engineering patterns.

These are **educational prototypes**, not production implementations. They illustrate the control flow and core ideas with minimal dependencies.

## Requirements

From the **repository root**, the legacy examples have no third-party
runtime dependencies. The requirements file lives under examples/:

~~~bash
python -m pip install -r examples/requirements.txt
~~~


## Run

```bash
python examples/harness_learning_demo.py
python examples/harness2_demo.py
python examples/agent_reflex_demo.py
```

All demos run offline with mock models/solvers so they execute without API keys.

The versioned tool built from these sketches is Harness Workbench (package
version 0.2.0) in harness_workbench/; see
[docs/workbench.md](../docs/workbench.md). These examples remain standalone.
For the additional controlled-experiment smoke test, see
[docs/experiments.md](../docs/experiments.md#quickstart).

## Experiment examples (`examples/experiments/`)

These are manifests for `harness-workbench experiment`, not standalone sketches, and they import the package.
Every one of them uses a **deterministic synthetic agent** (`harness_workbench/experiments/demo_agent.py`), so
their results are synthetic and are not evidence about real agents.

| File | What it is |
|---|---|
| `local-command.json` | Schema v2, `mode: exploratory`, one task, `trusted_local`. A pipeline smoke test; it ends `exploratory_only` and can never be promotable. The `improve --validation-spec` gate refuses it. |
| `confirmatory-demo.json` | Schema v2, `mode: confirmatory`: 6 development, 50 validation and 80 regression tasks with positive and negative controls, the `binary_v1` profile, `trusted_local` acknowledged. It ends `eligible` for the candidate `verification`. Its 136 tasks are template variants of five sentences, so its units are **not** semantically independent (the manifest's `duplicate_review_note` says so). |
| `sandboxed-demo.json` | The same benchmark with `environment.mode: sandboxed`; it needs a running Docker daemon and was not run on the author's machine. |
| `make_confirmatory_demo.py` | Deterministic generator of the two confirmatory manifests and the `controls/` artifacts: `python3 examples/experiments/make_confirmatory_demo.py [--out <path>] [--sandboxed]`. |
| `verified_research_harness.json`, `noop_research_harness.json`, `fixture/`, `controls/` | The candidate harnesses, the task fixture and the control artifacts the manifests refer to. |

Manifests spell the interpreter `{python}` (the controller resolves it), never a bare `python`.

