"""Offline protocol demo, NOT an LLM benchmark or evidence of model improvement."""
import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
values = json.loads(Path("numbers.json").read_text(encoding="utf-8"))
prompt = request["harness"]["system_prompt"].lower()
# Deliberately controlled behavior to exercise an experiment's acceptance gates.
answer = sorted(values) if "numeric" in prompt else sorted(values, key=str)
print(json.dumps({
    "output": json.dumps(answer),
    "cost_tokens": 0,
    "behavioral_score": 1.0,
    "traces": [{"kind": "tool", "detail": "read numbers.json and sort", "ok": True}],
    "metadata": {"synthetic": True, "solver_returned": "offline-sort-demo-v1"},
}))
