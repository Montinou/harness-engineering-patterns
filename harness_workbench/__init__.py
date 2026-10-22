"""Harness Workbench — propose, probe, compose and persist agent harnesses.

The harness is the editable runtime around a frozen model. This package runs
the test-time loop documented in ``research/SELF_IMPROVING_HARNESSES.md``:

Propose → Probe → Compose → Persist

No model weights are updated. The default proposer and executor are offline.
"""

from __future__ import annotations

__version__ = "0.3.0"

from harness_workbench.core.executor import BehavioralReport, MockExecutor, Task
from harness_workbench.core.harness import Harness, load_harness, save_harness
from harness_workbench.core.improver import Improver, ImprovementResult
from harness_workbench.core.proposer import LLMProposer, RuleBasedProposer

__all__ = [
    "BehavioralReport",
    "Harness",
    "ImprovementResult",
    "Improver",
    "LLMProposer",
    "MockExecutor",
    "RuleBasedProposer",
    "Task",
    "__version__",
    "load_harness",
    "save_harness",
]
