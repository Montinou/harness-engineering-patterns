"""Objective acceptance tests. The evaluator is NEVER given a candidate harness."""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from harness_workbench.experiments.execution import populate, run_command
from harness_workbench.experiments.models import canonical, digest


@dataclass
class Evaluation:
    id: str
    rubric_hash: str
    score: float
    success: bool
    checks: list[dict]
    error: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate(output: str, artifacts: dict[str, str], checks: list[dict], *, allow_commands: bool = False) -> Evaluation:
    """Grade only observable output/artifacts against a frozen external rubric.

    A failing test is a quality failure; inability to run the grader is an
    infrastructure/evaluation error and blocks promotion. No self-reported model
    score participates in this objective score.
    """
    results, weighted, total, error = [], 0.0, 0.0, False
    for index, check in enumerate(checks):
        weight = float(check.get("weight", 1))
        passed, failed_to_evaluate = False, False
        detail = ""
        try:
            kind = check["kind"]
            if kind == "exact":
                passed = output == check["expected"]
            elif kind == "contains":
                passed = check["expected"] in output
            elif kind == "json_equals":
                try:
                    passed = canonical(json.loads(output)) == canonical(check["expected"])
                except (ValueError, TypeError):
                    passed = False
            elif kind == "file_equals":
                passed = artifacts.get(check["target"]) == check["expected"]
            elif kind == "command":
                if not allow_commands:
                    raise ValueError("grader command requires --allow-commands")
                # Hidden tests are injected only AFTER solver termination, in a
                # second fresh directory. They were never in the solver fixture.
                with tempfile.TemporaryDirectory(prefix="harness-grade-") as tmp:
                    root = Path(tmp)
                    populate(root, artifacts)
                    populate(root, check.get("files", {}))
                    code, _out, _err = run_command(check["command"], root, {"output": output},
                                                 timeout=check.get("timeout", 30))
                    passed = code == 0
                    detail = f"exit_code={code}"
            else:
                raise ValueError("unsupported evaluator")
        except Exception as exc:
            # No arbitrary exception text: external commands may echo secrets.
            failed_to_evaluate, detail = True, type(exc).__name__
        results.append({"index": index, "kind": check["kind"], "passed": passed,
                        "weight": weight, "error": failed_to_evaluate, "detail": detail})
        total += weight
        weighted += weight * passed
        error |= failed_to_evaluate
    if not results or total <= 0:
        raise ValueError("objective evaluation requires nonempty checks")
    return Evaluation("eval-" + uuid4().hex, digest(checks), weighted / total,
                      all(row["passed"] for row in results) and not error, results, error)
