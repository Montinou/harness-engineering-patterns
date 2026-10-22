# Executable Examples

Simplified, runnable sketches of the main harness engineering patterns.

These are **educational prototypes**, not production implementations. They illustrate the control flow and core ideas with minimal dependencies.

## Requirements

```bash
pip install -r requirements.txt
```

## Run

```bash
python examples/harness_learning_demo.py
python examples/harness2_demo.py
python examples/agent_reflex_demo.py
```

All demos run offline with mock models/solvers so they execute without API keys.

The versioned tool built from these sketches is the Harness Workbench (`harness_workbench/`, see `docs/workbench.md`). These files stay small on purpose and do not import that package.

