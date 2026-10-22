"""Task-clustered comparisons, rather than treating repeated trials as new tasks."""
from __future__ import annotations

import math
import random
from statistics import mean, pstdev


def paired_statistics(task_deltas: list[float], *, margin: float, alpha: float,
                      samples: int, seed: int) -> dict:
    """Percentile bootstrap of task means plus an exact one-sided sign test.

    The sign test tests consistency above the practical margin (median/task win
    probability), not a mean effect. Ties at the margin contribute no evidence.
    Both gates are reported separately. A small sample is never hidden by repeats.
    """
    if not task_deltas:
        return {"tasks": 0, "mean_delta": None, "ci_low": None, "ci_high": None,
                "sign_p": 1.0, "wins": 0, "losses": 0, "ties": 0}
    if any(not math.isfinite(v) for v in task_deltas):
        raise ValueError("nonfinite task deltas")
    rng = random.Random(seed)
    n = len(task_deltas)
    boot = sorted(mean(rng.choices(task_deltas, k=n)) for _ in range(samples))
    wins = sum(v > margin + 1e-12 for v in task_deltas)
    losses = sum(v < margin - 1e-12 for v in task_deltas)
    informative = wins + losses
    probability = sum(math.comb(informative, k) for k in range(wins, informative + 1)) / 2**informative if informative else 1.0
    return {"tasks": n, "mean_delta": mean(task_deltas), "std_delta": pstdev(task_deltas),
            "ci_low": boot[max(0, int(samples * alpha / 2))],
            "ci_high": boot[min(samples - 1, int(samples * (1 - alpha / 2)))],
            "sign_p": probability, "wins": wins, "losses": losses,
            "ties": n - informative, "confidence": 1 - alpha}


def summarize(records: list[dict]) -> dict:
    """Macro-averages give each task equal weight. Failures are NOT removed."""
    tasks: dict[str, list[dict]] = {}
    for record in records:
        tasks.setdefault(record["task_id"], []).append(record)
    by_task = {}
    for task, rows in sorted(tasks.items()):
        scores = [r["evaluation"]["score"] for r in rows]
        by_task[task] = {"score": mean(scores), "success_rate": mean(float(r["evaluation"]["success"]) for r in rows),
                         "std_score": pstdev(scores), "runs": len(rows)}
    return {"tasks": by_task, "runs": len(records),
            "score": mean(v["score"] for v in by_task.values()) if by_task else 0.0,
            "success_rate": mean(v["success_rate"] for v in by_task.values()) if by_task else 0.0,
            "mean_tokens": mean(r["report"]["cost_tokens"] for r in records) if records else 0.0,
            "total_tokens": sum(r["report"]["cost_tokens"] for r in records),
            "mean_latency_ms": mean(r["elapsed_ms"] for r in records) if records else 0.0,
            "mean_tool_calls": mean(sum(t.get("kind") == "tool" for t in r["report"]["traces"]) for r in records) if records else 0.0,
            "errors": sum(r["status"] != "completed" or r["evaluation"]["error"] for r in records),
            "usage_known": bool(records) and all(r["report"].get("metadata", {}).get("usage_known", False) for r in records)}


def pareto_frontier(summaries: dict[str, dict]) -> list[str]:
    """Maximize quality; minimize tokens/latency. Diagnostic, not a promotion gate."""
    frontier = []
    eligible = {k: v for k, v in summaries.items() if v["usage_known"] and not v["errors"]}
    for label, row in eligible.items():
        dominated = False
        for other, candidate in eligible.items():
            if label == other:
                continue
            weak = (candidate["score"] >= row["score"] and candidate["mean_tokens"] <= row["mean_tokens"]
                    and candidate["mean_latency_ms"] <= row["mean_latency_ms"])
            strict = (candidate["score"] > row["score"] or candidate["mean_tokens"] < row["mean_tokens"]
                      or candidate["mean_latency_ms"] < row["mean_latency_ms"])
            if weak and strict:
                dominated = True
                break
        if not dominated:
            frontier.append(label)
    return sorted(frontier)
