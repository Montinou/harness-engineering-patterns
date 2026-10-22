"""Synthetic, deterministic example agent; NOT a real LLM or benchmark result."""
import json
import os
from pathlib import Path

harness = json.loads(Path(os.environ["HARNESS_SPEC_FILE"]).read_text())
workspace = Path(os.environ["HARNESS_WORKSPACE"])
success = any("Verify" in instruction for instruction in harness["guardrails"])
if success:
    (workspace / "report.txt").write_text("PASS", encoding="utf-8")
result = {
    "outputs": {"answer": "CORRECT" if success else "INCORRECT"},
    "tokens": 10, "behavioral_score": 0.2 if success else 0.9,
}
Path(os.environ["HARNESS_RESULT_FILE"]).write_text(json.dumps(result), encoding="utf-8")
print(result["outputs"]["answer"])
