#!/usr/bin/env python3
"""
Minimal Harness Learning demo (arXiv:2609.35738 style).

A small "proposer" revises a frozen solver's harness using execution feedback.
No real LLM calls — everything is mocked for offline execution.
"""

from dataclasses import dataclass
from typing import Callable, Dict, Any
import random


@dataclass
class Harness:
    system_prompt: str
    tool_descriptions: Dict[str, str]
    max_steps: int
    verification_enabled: bool

    def copy(self) -> "Harness":
        return Harness(
            system_prompt=self.system_prompt,
            tool_descriptions=self.tool_descriptions.copy(),
            max_steps=self.max_steps,
            verification_enabled=self.verification_enabled,
        )


def mock_solver(task: str, harness: Harness) -> Dict[str, Any]:
    """Frozen solver. Performance depends on harness quality."""
    score = 0.3
    if "verify" in harness.system_prompt.lower() or harness.verification_enabled:
        score += 0.25
    if harness.max_steps >= 5:
        score += 0.15
    if "search" in harness.tool_descriptions:
        score += 0.1
    # Small noise
    score += random.uniform(-0.05, 0.05)
    return {"score": min(1.0, max(0.0, score)), "steps_used": min(harness.max_steps, 4)}


def proposer(task: str, harness: Harness, report: Dict[str, Any]) -> Harness:
    """Simple rule-based proposer that improves the harness based on feedback."""
    new = harness.copy()
    if report["score"] < 0.6 and not new.verification_enabled:
        new.verification_enabled = True
        new.system_prompt += " Always verify intermediate results."
    if report["score"] < 0.7 and new.max_steps < 8:
        new.max_steps += 2
    if "search" not in new.tool_descriptions:
        new.tool_descriptions["search"] = "Search for supporting evidence."
    return new


def harness_learning_loop(task: str, initial: Harness, rounds: int = 3):
    harness = initial
    history = []
    for i in range(rounds):
        report = mock_solver(task, harness)
        history.append((i, harness, report))
        print(f"Round {i}: score={report['score']:.2f} | max_steps={harness.max_steps} | verify={harness.verification_enabled}")
        harness = proposer(task, harness, report)
    return harness, history


if __name__ == "__main__":
    initial = Harness(
        system_prompt="Solve the task carefully.",
        tool_descriptions={"calc": "Basic calculator."},
        max_steps=3,
        verification_enabled=False,
    )
    final, hist = harness_learning_loop("research a claim with evidence", initial)
    print("\nFinal harness:")
    print(f"  system: {final.system_prompt}")
    print(f"  tools: {list(final.tool_descriptions)}")
    print(f"  max_steps: {final.max_steps}, verify: {final.verification_enabled}")
