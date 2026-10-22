# Executable Examples

Simplified, runnable sketches of the main harness engineering patterns.

These are **educational prototypes**, not production implementations. They illustrate the control flow and core ideas with minimal dependencies.

## Requirements

From the **repository root**, these legacy examples use only the Python
standard library. The included `examples/requirements.txt` is intentionally
empty of third-party dependencies; installation is optional:

```bash
python -m pip install -r examples/requirements.txt
```

## Run

```bash
python examples/harness_learning_demo.py
python examples/harness2_demo.py
python examples/agent_reflex_demo.py
```

All demos run offline with mock models/solvers so they execute without API keys.

The versioned tool built from these sketches is Harness Workbench v0.3
(`harness_workbench/`; see [docs/workbench.md](../docs/workbench.md)).
These older sketches stay small on purpose and do not import that package.
For the new controlled experiment demonstration, run the command documented
in [examples/experiments](../docs/experiments.md#quickstart).

