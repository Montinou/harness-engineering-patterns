#!/usr/bin/env python3
"""
Minimal Harness²-style Propose–Probe–Compose demo (arXiv:2610.14224).

Proposes candidate harness edits, probes them on the same task via behavioral
contrasts, and composes a refined harness. No ground-truth verifier required.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any
import random


@dataclass
class Harness:
    prompt: str
    tools: List[str] = field(default_factory=list)
    memory_k: int = 2
    guardrails: List[str] = field(default_factory=list)

    def copy(self) -> "Harness":
        return Harness(self.prompt, self.tools[:], self.memory_k, self.guardrails[:])


def probe(task: str, harness: Harness) -> float:
    """Mock execution. Higher score = better behavioral outcome."""
    s = 0.4
    if any(g in harness.guardrails for g in ("verify", "cite")):
        s += 0.2
    if "search" in harness.tools:
        s += 0.15
    if harness.memory_k >= 4:
        s += 0.1
    if "step-by-step" in harness.prompt.lower():
        s += 0.05
    return min(1.0, s + random.uniform(-0.03, 0.03))


def propose_candidates(base: Harness) -> List[Harness]:
    """Generate a small set of candidate edits (invariant + domain)."""
    cands = []
    # Invariant: add verification
    c1 = base.copy()
    c1.guardrails.append("verify")
    c1.prompt += " Verify claims."
    cands.append(c1)
    # Domain: add search tool
    c2 = base.copy()
    if "search" not in c2.tools:
        c2.tools.append("search")
    cands.append(c2)
    # Memory increase
    c3 = base.copy()
    c3.memory_k = max(base.memory_k, 5)
    cands.append(c3)
    return cands


def compose(base: Harness, probes: List[tuple]) -> Harness:
    """Compose from behavioral contrasts: keep edits that improved score."""
    base_score = next(s for h, s in probes if h is base)
    best = base.copy()
    for h, s in probes:
        if h is base:
            continue
        if s > base_score + 0.05:
            # Adopt positive changes
            if "verify" in h.guardrails and "verify" not in best.guardrails:
                best.guardrails.append("verify")
                best.prompt += " Verify claims."
            if "search" in h.tools and "search" not in best.tools:
                best.tools.append("search")
            best.memory_k = max(best.memory_k, h.memory_k)
    return best


def harness2_step(task: str, harness: Harness) -> Harness:
    candidates = [harness] + propose_candidates(harness)
    probes = [(h, probe(task, h)) for h in candidates]
    print("Probes:")
    for h, s in probes:
        print(f"  score={s:.2f} | tools={h.tools} | mem={h.memory_k} | guards={h.guardrails}")
    refined = compose(harness, probes)
    return refined


if __name__ == "__main__":
    base = Harness(
        prompt="Answer the research question.",
        tools=["calc"],
        memory_k=2,
        guardrails=[],
    )
    print("=== Initial ===")
    print(base)
    refined = harness2_step("fact-check a claim", base)
    print("\n=== Refined ===")
    print(refined)
