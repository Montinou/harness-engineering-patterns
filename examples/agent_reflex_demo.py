#!/usr/bin/env python3
"""
Minimal Agent-Reflex style demo (arXiv:2610.17953).

Shows reflection meta-skills mined from success/failure pairs and applied
when tool feedback is noisy.
"""

from dataclasses import dataclass
from typing import List, Dict
import random


@dataclass
class ReflectionSkill:
    name: str
    trigger: str
    action: str


# Simulated skill library (RSD output)
SKILL_LIBRARY: List[ReflectionSkill] = [
    ReflectionSkill("verify_tool_output", "tool returned unexpected format", "Re-call tool with clearer args or fall back to alternative"),
    ReflectionSkill("recover_from_noise", "high noise or contradiction", "Cross-check with second source before accepting"),
    ReflectionSkill("user_correction", "user said that is wrong", "Acknowledge, store correction, re-plan next step"),
]


def noisy_tool(query: str) -> Dict:
    """Mock tool that sometimes returns noisy results."""
    if random.random() < 0.4:
        return {"ok": False, "result": None, "noise": "timeout or malformed"}
    return {"ok": True, "result": f"evidence for {query}", "noise": None}


def agent_with_reflex(task: str, use_reflex: bool = True) -> Dict:
    steps = []
    for i in range(3):
        out = noisy_tool(task)
        steps.append(out)
        if not out["ok"] and use_reflex:
            # Apply matching skill
            skill = next((s for s in SKILL_LIBRARY if "noise" in s.trigger or "unexpected" in s.trigger), None)
            if skill:
                steps.append({"reflex": skill.name, "action": skill.action})
                # Retry
                out = noisy_tool(task + " (retry)")
                steps.append(out)
        if out["ok"]:
            break
    success = any(s.get("ok") for s in steps if isinstance(s, dict))
    return {"success": success, "steps": steps}


if __name__ == "__main__":
    print("Without reflex:")
    print(agent_with_reflex("verify claim X", use_reflex=False))
    print("\nWith reflex:")
    print(agent_with_reflex("verify claim X", use_reflex=True))
