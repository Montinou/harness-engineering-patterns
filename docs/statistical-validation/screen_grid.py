"""Screen every CALIBRATION_GRID point on the pre-registered target power scenario (seed 101, 1000 sims)."""
import dataclasses, json, sys, time
from pathlib import Path
from harness_workbench.experiments import simulate as sim
from harness_workbench.experiments.profiles import PROFILES, grid_points

def main():
    OUT = Path(sys.argv[1]); WORKERS = int(sys.argv[2])
    METHOD_RANK = {"cluster_percentile_ci_v1": 0, "cluster_t_v1": 1}
    points = sorted(grid_points(), key=lambda p: (p["regression_margin"], p["min_independent_units"] + p["min_regression_units"],
                    p["min_independent_units"], METHOD_RANK[p["method"]]))
    base = PROFILES["binary_v1"]
    for pt in points:
        name = f"grid-seed101-{pt['method']}-{pt['min_independent_units']}-{pt['min_regression_units']}-{pt['regression_margin']}.json"
        path = OUT / name
        if path.exists():
            print("skip existing", name, flush=True); continue
        profile = dataclasses.replace(base, method=pt["method"], min_independent_units=pt["min_independent_units"],
                                      min_regression_units=pt["min_regression_units"], regression_margin=pt["regression_margin"])
        t = time.perf_counter()
        res = sim.run_scenario(sim.target_scenario(profile), profile, simulations=1000, seed=101, method=profile.method, workers=WORKERS)
        payload = {"synthetic": True, "profile": "binary_v1", "matrix": "target-screen",
                   "overrides": {k: pt[k] for k in pt if k != "repetitions"}, "method": pt["method"], "seed": 101,
                   "simulations": {"power": 1000}, "scenarios": [res],
                   "note": "screened on the target power scenario only; null scenarios not run because the point failed it",
                   "python": sys.version, "wall_seconds": time.perf_counter() - t}
        path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"{name} rate={res['rate']:.3f} lower95={res['lower_95']:.3f} wall={payload['wall_seconds']:.1f}s", flush=True)


if __name__ == '__main__':
    main()
