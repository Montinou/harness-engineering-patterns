import dataclasses, json, sys, time
from harness_workbench.experiments import simulate as sim
from harness_workbench.experiments.profiles import PROFILES

def main():
    base = PROFILES["binary_v1"]; out = []
    for units, reg, ids in ((60, 80, ["boundary-d1"]), (50, 80, ["boundary-d1", "regression-blocked"])):
        profile = dataclasses.replace(base, min_independent_units=units, min_regression_units=reg, regression_margin=0.10)
        for sc in sim.build_matrix("full", profile):
            if sc.id in ids:
                t = time.perf_counter()
                r = sim.run_scenario(sc, profile, simulations=20000, seed=101, method=profile.method, workers=8)
                r["point"] = [profile.method, units, reg, 0.10]; r["wall"] = time.perf_counter() - t
                out.append(r)
                print(r["point"], sc.id, r["promoted"], r["rate"], r["lower_95"], r["upper_95"], flush=True)
    json.dump(out, open(sys.argv[1], "w"), indent=2)
if __name__ == "__main__":
    main()
