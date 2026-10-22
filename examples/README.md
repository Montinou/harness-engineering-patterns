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

