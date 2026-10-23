"""Process entry point for the SIGKILL test: run one experiment whose solver can be held on a chosen trial.

Usage: ``python -I _slow_experiment_worker.py <library root> <manifest> <marker dir> <hold trial>``.

The solver is a command (``sys.executable -I -c SOLVER``). On every trial it writes ``<marker dir>/marker-<n>``
(``n`` counts the trials started so far) and answers CORRECT. On trial ``<hold trial>`` it first waits for
``<marker dir>/go`` (at most ``HOLD_SECONDS``), so the parent can kill this process while that trial runs.
The held solver writes ``solver-<n>-done`` when it exits, and records its trial directory in its marker, so
the parent can release it and remove what the killed worker left behind.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOLD_SECONDS = 60

SOLVER = """
import json, os, sys, time
from pathlib import Path
markers, hold, limit = Path(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
n = len(list(markers.glob("marker-*"))) + 1
(markers / f"marker-{n}").write_text(str(Path(os.environ["HARNESS_WORKSPACE"]).parent), encoding="utf-8")
if n == hold:
    deadline = time.monotonic() + limit
    while not (markers / "go").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
with open(os.environ["HARNESS_RESULT_FILE"], "w", encoding="utf-8") as handle:
    json.dump({"outputs": {"answer": "CORRECT"}}, handle)
if n == hold:
    (markers / f"solver-{n}-done").write_text("done", encoding="utf-8")
"""


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from harness_workbench.experiments import ExperimentSpec, run_experiment
    from harness_workbench.experiments.command import CommandHarnessExecutor

    root, manifest, markers, hold = argv
    executor = CommandHarnessExecutor(
        [sys.executable, "-I", "-c", SOLVER, markers, hold, str(HOLD_SECONDS)], timeout_seconds=HOLD_SECONDS * 2)
    run_experiment(ExperimentSpec.load(manifest), executor, root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
