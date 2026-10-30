"""Synthetic, deterministic example agent; NOT a real LLM or benchmark result.

It reads only the public harness components and the task text the adapter hands it (HARNESS_SPEC_FILE,
HARNESS_TASK_FILE). When the harness carries a ``Verify`` guardrail it answers the word the task description
names (the one all-caps token in it) and stores it in ``report.txt``; without one it answers ``INCORRECT``
and writes nothing. A description that names no single word is the original smoke-test task ("Return CORRECT
and write report.txt as PASS"), which is answered with ``CORRECT`` and ``PASS``.
"""
import json
import os
import re
from pathlib import Path

harness = json.loads(Path(os.environ["HARNESS_SPEC_FILE"]).read_text())
task = json.loads(Path(os.environ["HARNESS_TASK_FILE"]).read_text())
workspace = Path(os.environ["HARNESS_WORKSPACE"])
success = any("Verify" in instruction for instruction in harness["guardrails"])
named = sorted(set(re.findall(r"\b[A-Z]{3,}\b", task["description"])))
word = named[0] if len(named) == 1 else None
answer = "INCORRECT"
if success:
    answer = word or "CORRECT"
    (workspace / "report.txt").write_text(word or "PASS", encoding="utf-8")
result = {
    "outputs": {"answer": answer},
    "model": "synthetic-fixture-agent",
    "tokens": 10, "behavioral_score": 0.2 if success else 0.9,
}
Path(os.environ["HARNESS_RESULT_FILE"]).write_text(json.dumps(result), encoding="utf-8")
print(result["outputs"]["answer"])
