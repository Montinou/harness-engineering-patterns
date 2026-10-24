"""Seed-104 null recheck of one calibration candidate at a high simulation count (ADR 0002 deviation).

Runs every null scenario of the ``full`` matrix through the same ``run_scenario`` the CLI uses, with the
profile overrides of the candidate, and writes a payload in the CLI's format (``scope`` says it holds null
scenarios only; the power results of the candidate are the seed-101 calibration files). The scenarios that
carry the risk (boundary, regression-blocked, heterogeneity, family correlation) run first and the candidate
stops at the first scenario whose ``upper_95`` exceeds the pre-registered 0.06 bound; the partial file is kept.

usage: python3 null_recheck.py <method> <units> <regression_units> <regression_margin> <simulations> <seed> <out>
"""
from __future__ import annotations

import dataclasses
import json
import sys
import time
from pathlib import Path

from harness_workbench.experiments import simulate as sim
from harness_workbench.experiments.profiles import PROFILES

NULL_UPPER_BOUND = 0.06
FIRST = ("boundary-d1", "boundary-d5", "boundary-d20", "regression-blocked", "hetero-null", "famcorr-null")


def main(argv: list[str]) -> int:
    method, units, reg_units, margin, simulations, seed, out = argv
    units, reg_units, simulations, seed, margin = int(units), int(reg_units), int(simulations), int(seed), float(margin)
    profile = dataclasses.replace(PROFILES["binary_v1"], min_independent_units=units,
                                  min_regression_units=reg_units, regression_margin=margin)
    profile = dataclasses.replace(profile, method=method)
    scenarios = [s for s in sim.build_matrix("full", profile) if s.kind == "null"]
    scenarios.sort(key=lambda s: (FIRST.index(s.id) if s.id in FIRST else len(FIRST)))
    started = time.perf_counter()
    results: list[dict] = []
    stopped_at = None

    def write() -> None:
        payload = {
            "synthetic": True, "profile": "binary_v1", "scope": "null scenarios only (selection recheck, ADR 0002)",
            "overrides": {"method": method, "min_independent_units": units, "min_regression_units": reg_units,
                          "regression_margin": margin},
            "method": method, "seed": seed, "simulations": {"null": simulations}, "matrix": "full",
            "unknown_usage": False,
            "interval": {"kind": "wilson", "z": sim.Z_ONE_SIDED_95,
                         "meaning": "one-sided 95 % bounds on the promotion rate (spec 5.4)"},
            "null_upper_bound": NULL_UPPER_BOUND, "scenarios_expected": len(scenarios),
            "stopped_at": stopped_at, "scenarios": results,
            "passes_null_bound": stopped_at is None and len(results) == len(scenarios),
            "python": sys.version, "wall_seconds": time.perf_counter() - started,
        }
        Path(out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    for scenario in scenarios:
        result = sim.run_scenario(scenario, profile, simulations=simulations, seed=seed, method=method, workers=10)
        results.append(result)
        print(f"{scenario.id:<26}{result['promoted']:>7}/{simulations} rate {result['rate']:.4f} "
              f"upper {result['upper_95']:.4f} wall {time.perf_counter() - started:.0f}s", flush=True)
        if result["upper_95"] > NULL_UPPER_BOUND:
            stopped_at = scenario.id
            write()
            print(f"STOP: {scenario.id} upper_95 {result['upper_95']:.4f} > {NULL_UPPER_BOUND}", flush=True)
            return 1
        write()
    print("ALL NULL SCENARIOS PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
