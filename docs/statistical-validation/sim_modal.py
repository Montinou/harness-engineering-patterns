"""Run harness_workbench.experiments.simulate on Modal, sharded by simulation index.

Every simulation draws from random.Random(f"{seed}:{scenario.id}:{index}") (simulate._simulation_rng), so
splitting a scenario's index range into chunks and summing the per-chunk counters gives exactly the result
of a local run_scenario call. Only plain values cross the wire; each container rebuilds the scenario from the
uploaded package.

Usage (from the repo root):
  PYTHONPATH=. modal run <this file> --matrix full --seed 202 --null-simulations 5000 --power-simulations 2000 \
      [--method M --units N --regression-units R --regression-margin G --iterations I --unknown-usage] \
      [--null-only] [--simulations S] --out <path>

Default output is the simulate CLI's payload format (plus a "runner" field). --null-only runs only the null
scenarios in the risk-first order of docs/statistical-validation/null_recheck.py and writes that script's
payload format (scope, null_upper_bound, stopped_at, passes_null_bound); every scenario runs, and stopped_at
names the first one, in that order, whose upper_95 exceeds 0.06.
"""
from __future__ import annotations

import dataclasses
import json
import sys
import time
from collections import Counter
from pathlib import Path

import modal

REPO = Path.cwd()
CHUNK = 250
NULL_UPPER_BOUND = 0.06
FIRST = ("boundary-d1", "boundary-d5", "boundary-d20", "regression-blocked", "hetero-null", "famcorr-null")

image = modal.Image.debian_slim(python_version="3.14").add_local_dir(
    str(REPO / "harness_workbench"), remote_path="/root/harness_workbench")
app = modal.App("harness-workbench-sim", image=image)


def _profile(overrides: dict):
    from harness_workbench.experiments.profiles import PROFILES

    profile = dataclasses.replace(PROFILES["binary_v1"], **{k: v for k, v in overrides.items() if k != "method"})
    if "method" in overrides:
        profile = dataclasses.replace(profile, method=overrides["method"])
    return profile


@app.function(cpu=1.0, timeout=3600, max_containers=100, retries=2)
def chunk(job: dict) -> dict:
    import platform
    import sys as _sys

    _sys.path.insert(0, "/root")
    from harness_workbench.experiments import simulate as sim

    profile = _profile(job["overrides"])
    scenario = next(s for s in sim.build_matrix(job["matrix"], profile) if s.id == job["scenario_id"])
    resolved, method = sim._resolve(profile, profile.method)
    statuses, reasons = sim._run_chunk((scenario, resolved, method, job["seed"], job["start"], job["stop"],
                                        job["unknown_usage"]))
    return {"key": job["key"], "statuses": statuses, "reasons": reasons, "python": _sys.version,
            "machine": platform.machine()}


@app.local_entrypoint()
def main(matrix: str, seed: int, out: str, simulations: int = 0, null_simulations: int = 0,
         power_simulations: int = 0, method: str = "", units: int = 0, regression_units: int = 0,
         regression_margin: float = -1.0, iterations: int = 0, unknown_usage: bool = False,
         null_only: bool = False, only: str = ""):
    from harness_workbench.experiments import simulate as sim

    overrides: dict = {}
    notes: list[str] = []
    if method:
        overrides["method"] = method
    if units:
        overrides["min_independent_units"] = units
    if regression_units:
        overrides["min_regression_units"] = regression_units
    if regression_margin >= 0:
        overrides["regression_margin"] = regression_margin
    if iterations:
        overrides["bootstrap_iterations"] = iterations
    elif matrix == "smoke":
        overrides["bootstrap_iterations"] = sim.SMOKE_ITERATIONS
        notes.append(f"the smoke matrix uses {sim.SMOKE_ITERATIONS} bootstrap iterations unless --iterations is given")
    profile = _profile(overrides)
    scenarios = sim.build_matrix(matrix, profile)
    if null_only:
        scenarios = [s for s in scenarios if s.kind == "null"]
        scenarios.sort(key=lambda s: (FIRST.index(s.id) if s.id in FIRST else len(FIRST)))
    if only:
        keep = set(only.split(","))
        scenarios = [s for s in scenarios if s.id in keep]
    counts = {"null": null_simulations or simulations or None, "power": power_simulations or simulations or None}
    needed = {s.kind for s in scenarios}
    missing = sorted(k for k in needed if counts[k] is None)
    if missing:
        raise SystemExit(f"no simulation count for {missing[0]} scenarios")

    jobs = []
    for s in scenarios:
        n = counts[s.kind]
        for start in range(0, n, CHUNK):
            jobs.append({"key": s.id, "matrix": matrix, "scenario_id": s.id, "overrides": overrides, "seed": seed,
                         "start": start, "stop": min(n, start + CHUNK), "unknown_usage": unknown_usage})
    print(f"{len(scenarios)} scenarios, {len(jobs)} chunks of <= {CHUNK} simulations", flush=True)
    started = time.perf_counter()
    statuses = {s.id: Counter() for s in scenarios}
    reasons = {s.id: Counter() for s in scenarios}
    remote_python = set()
    done = 0
    for part in chunk.map(jobs, order_outputs=False):
        statuses[part["key"]].update(part["statuses"])
        reasons[part["key"]].update(part["reasons"])
        remote_python.add(f"{part['python'].split()[0]} {part['machine']}")
        done += 1
        if done % 200 == 0 or done == len(jobs):
            print(f"  {done}/{len(jobs)} chunks, {time.perf_counter() - started:.0f}s", flush=True)

    resolved, resolved_method = sim._resolve(profile, profile.method)
    results = []
    for s in scenarios:
        n = counts[s.kind]
        if sum(statuses[s.id].values()) != n:
            raise SystemExit(f"{s.id}: {sum(statuses[s.id].values())} decisions, expected {n}")
        promoted = statuses[s.id].get(sim.DecisionStatus.ELIGIBLE.value, 0)
        lower, upper = sim.wilson_interval(promoted, n, z=sim.Z_ONE_SIDED_95)
        results.append({
            "synthetic": True, "scenario": dataclasses.asdict(s), "simulations": n, "promoted": promoted,
            "rate": promoted / n, "lower_95": lower, "upper_95": upper,
            "status_counts": dict(sorted(statuses[s.id].items())),
            "reason_counts": dict(sorted(reasons[s.id].items(), key=lambda item: (-item[1], item[0]))),
            "method": resolved_method, "seed": seed,
        })
    runner = {"kind": "modal", "chunk_simulations": CHUNK, "chunks": len(jobs), "remote_python": sorted(remote_python),
              "equivalence": "per-index RNG random.Random(f'{seed}:{scenario.id}:{index}'); chunk counters summed"}
    interval = {"kind": "wilson", "z": sim.Z_ONE_SIDED_95,
                "meaning": "one-sided 95 % bounds on the promotion rate (spec 5.4)"}
    wall = time.perf_counter() - started
    if null_only:
        stopped_at = next((r["scenario"]["id"] for r in results if r["upper_95"] > NULL_UPPER_BOUND), None)
        payload = {
            "synthetic": True, "profile": "binary_v1", "scope": "null scenarios only (selection recheck, ADR 0002)",
            "overrides": overrides, "method": resolved_method, "seed": seed,
            "simulations": {"null": counts["null"]}, "matrix": matrix, "unknown_usage": unknown_usage,
            "interval": interval, "null_upper_bound": NULL_UPPER_BOUND, "scenarios_expected": len(scenarios),
            "stopped_at": stopped_at, "scenarios": results,
            "passes_null_bound": stopped_at is None, "python": sorted(remote_python)[0],
            "wall_seconds": wall, "runner": runner,
        }
    else:
        payload = {
            "synthetic": True, "profile": "binary_v1", "overrides": overrides, "method": resolved_method,
            "seed": seed, "simulations": {k: counts[k] for k in sorted(needed)}, "matrix": matrix,
            "unknown_usage": unknown_usage, "interval": interval, "notes": notes, "scenarios": results,
            "implementation_hash": sim._implementation_hash(), "python": sorted(remote_python)[0],
            "wall_seconds": wall, "runner": runner,
        }
    Path(out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    for r in results:
        print(f"{r['scenario']['id']:<30}{r['scenario']['kind']:<7}{r['promoted']:>7}/{r['simulations']} "
              f"rate {r['rate']:.4f} lower {r['lower_95']:.4f} upper {r['upper_95']:.4f}")
    if null_only:
        print("ALL NULL SCENARIOS PASS" if stopped_at is None else f"FIRST FAILURE: {stopped_at}")
    print(f"wrote {out} in {wall:.0f}s")
