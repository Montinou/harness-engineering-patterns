"""Split assignment and the pilot manifest for an imported LiveCodeBench pool.

Usage (from a checkout)::

    python benchmarks/livecodebench/make_manifests.py assign --pool <dir> --seed 20261101
    python benchmarks/livecodebench/make_manifests.py pilot --pool <dir> --environment <json file or inline> \\
        --out <manifest.json> --claude <claude 2.1.316 executable>

``assign`` reads ``<pool>/pool.json``, ``<pool>/import_report.json`` and ``<pool>/controls_report.json`` (run
``make_controls.py`` on the whole pool first: the pool is only the problems the report keeps, spec section 8)
and writes ``<pool>/splits.json``: 25
development problems (15 medium or hard, 10 easy), 50 validation problems from medium and hard, 80 regression
problems from easy and medium, drawn with a seeded generator and stratified by platform (each platform gets
its largest-remainder share of the quota, in proportion to what is still eligible). The same pool and seed
always give the same file. When a quota cannot be filled the command stops, names every short stratum with the
number needed and available (counted over the kept problems), and writes nothing: the floors of ``binary_v1`` are
not lowered. Without a controls report it refuses.

Every problem is its own family. The importer's near-duplicate pairs (easy and hard versions of one problem
and the like) are not independent units, so each connected group of them is reduced to one problem, the
easiest (ties: the lowest id); the others are dropped from the assignment and listed in
``dropped_near_duplicates``. Nothing is listed in ``duplicate_review`` for them.

``pilot`` writes the exploratory manifest of spec section 9: the development problems of ``splits.json`` only,
the base harness against ``pilot_candidate.json``, 3 repetitions, and the Claude Code adapter as a command
executor with a 900 s timeout. It refuses a development problem the controls report does not keep. The manifest is loaded with the real loader before it is written. It carries
each task's expected hashes in its ``oracle`` check (the supervisor compares them; the grader only observes), and
never a problem text or a test.
"""

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.append(ROOT)

from benchmarks.livecodebench import import_lcb, make_controls  # noqa: E402
from harness_workbench.core.harness import load_harness  # noqa: E402
from harness_workbench.experiments.claude_code_agent import (  # noqa: E402
    MIN_RUN_SECONDS, STOP_MARGIN_SECONDS, SUPPORTED,
)
from harness_workbench.experiments.manifest import ExperimentSpec, ManifestError  # noqa: E402
from harness_workbench.experiments.manifest import _environment as environment_from_json  # noqa: E402

BASE_HARNESS_PATH = os.path.join(HERE, "base_harness.json")
PILOT_CANDIDATE_PATH = os.path.join(HERE, "pilot_candidate.json")

DEFAULT_SEED = 20261101
DIFFICULTY_ORDER = ("easy", "medium", "hard")
# Allocation order is scarcest first (regression draws from the smaller easy and medium stock), not the
# reading order of the spec; the result does not depend on it beyond making a full pool fit.
ALLOCATION_ORDER = ("development", "regression", "validation")
QUOTAS = {
    "development": (
        {"difficulties": ("medium", "hard"), "count": 15},
        {"difficulties": ("easy",), "count": 10},
    ),
    "validation": ({"difficulties": ("medium", "hard"), "count": 50},),
    "regression": ({"difficulties": ("easy", "medium"), "count": 80},),
}

PILOT_REPETITIONS = 3
EXECUTOR_TIMEOUT_SECONDS = 900
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"
DEFAULT_CLAUDE_VERSION = make_controls.DEFAULT_CLAUDE_VERSION
DEFAULT_MAX_BUDGET_USD = make_controls.DEFAULT_MAX_BUDGET_USD
CLAUDE_PATH_ENV = make_controls.CLAUDE_PATH_ENV
ID_RE = import_lcb.ID_RE

EXIT_SETUP = 2


class ManifestToolError(Exception):
    """Bad input to the manifest tool; the message says what to change."""


class ShortfallError(ManifestToolError):
    """A quota cannot be filled. ``shortfalls`` holds one row per short stratum."""

    def __init__(self, shortfalls):
        self.shortfalls = shortfalls
        lines = [
            "%s %s: need %d, have %d (%s)" % (
                row["split"], row["difficulties"], row["need"], row["have"],
                ", ".join("%s %d" % item for item in sorted(row["by_platform"].items())) or "none")
            for row in shortfalls]
        super().__init__("the pool cannot fill the quotas, nothing was written: " + "; ".join(lines))


# ---------------------------------------------------------------- assignment

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise ManifestToolError("cannot read %s: %s" % (path, exc)) from exc


def _write_json(path, data):
    tmp = "%s.tmp" % path
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True, allow_nan=False)
        handle.write("\n")
    os.replace(tmp, path)


def load_pool(pool):
    """The pool's entries, each with a safe id (the ids become paths)."""
    entries = _read_json(os.path.join(pool, "pool.json"))
    if not isinstance(entries, list):
        raise ManifestToolError("pool.json must hold a list")
    seen = set()
    for entry in entries:
        qid = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(qid, str) or not ID_RE.match(qid) or qid in seen:
            raise ManifestToolError("pool.json holds an unsafe, duplicate or missing problem id: %r" % (qid,))
        seen.add(qid)
    return entries


def load_kept_ids(pool):
    """The ids ``<pool>/controls_report.json`` keeps (a passing positive control and two failing negatives)."""
    path = os.path.join(pool, "controls_report.json")
    if not os.path.isfile(path):
        raise ManifestToolError(
            "%s is missing: build the controls on the whole pool first (make_controls.py), the pool is only the "
            "problems with a verified positive control" % path)
    report = _read_json(path)
    kept = report.get("kept") if isinstance(report, dict) else None
    if not isinstance(kept, list) or not all(isinstance(qid, str) for qid in kept):
        raise ManifestToolError("%s has no 'kept' list of problem ids" % path)
    if report.get("complete") is not True:
        raise ManifestToolError(
            "%s is not a complete report (complete=%r, limit=%r): it came from a --limit or interrupted run and "
            "lists only some of the pool; rerun make_controls.py on the whole pool" % (
                path, report.get("complete"), report.get("limit")))
    return set(kept)


def _near_duplicate_pairs(pool):
    path = os.path.join(pool, "import_report.json")
    if not os.path.isfile(path):
        return []
    pairs = _read_json(path).get("near_duplicates") or []
    return [pair for pair in pairs if isinstance(pair, dict) and "a" in pair and "b" in pair]


def _reduce_near_duplicates(entries, pairs):
    """``(kept, dropped)``: one problem per connected group of near-duplicates, the easiest."""
    by_id = {entry["id"]: entry for entry in entries}
    parent = {qid: qid for qid in by_id}

    def find(qid):
        while parent[qid] != qid:
            parent[qid] = parent[parent[qid]]
            qid = parent[qid]
        return qid

    for pair in pairs:
        if pair["a"] in by_id and pair["b"] in by_id:
            parent[find(pair["a"])] = find(pair["b"])
    groups = {}
    for qid in by_id:
        groups.setdefault(find(qid), []).append(qid)

    def rank(qid):
        difficulty = by_id[qid].get("difficulty")
        return (DIFFICULTY_ORDER.index(difficulty) if difficulty in DIFFICULTY_ORDER else len(DIFFICULTY_ORDER), qid)

    dropped = []
    for members in groups.values():
        if len(members) < 2:
            continue
        keeper = min(members, key=rank)
        for qid in sorted(members):
            if qid != keeper:
                dropped.append({"id": qid, "duplicate_of": keeper, "difficulty": by_id[qid].get("difficulty")})
    dropped_ids = {row["id"] for row in dropped}
    kept = [entry for entry in entries if entry["id"] not in dropped_ids]
    return kept, sorted(dropped, key=lambda row: row["id"])


def _apportion(total, available):
    """Split ``total`` over the platforms in proportion to ``available`` (largest remainder, ties by name)."""
    whole = sum(available.values())
    shares = {name: total * count / whole for name, count in available.items()}
    result = {name: int(share) for name, share in shares.items()}
    leftover = total - sum(result.values())
    by_fraction = sorted(shares, key=lambda name: (-(shares[name] - result[name]), name))
    for name in by_fraction[:leftover]:
        result[name] += 1
    return result


def _band(difficulties):
    return "+".join(difficulties)


def assign(pool, *, seed=DEFAULT_SEED, quotas=None):
    """Write ``<pool>/splits.json`` and return its content; raise ``ShortfallError`` and write nothing if short."""
    pool = os.path.abspath(pool)
    quotas = QUOTAS if quotas is None else quotas
    controlled = load_kept_ids(pool)
    entries = [entry for entry in load_pool(pool) if entry["id"] in controlled]
    kept, dropped = _reduce_near_duplicates(entries, _near_duplicate_pairs(pool))
    eligible = {entry["id"]: entry for entry in kept}
    assignments = {}
    shortfalls = []
    for split in (name for name in ALLOCATION_ORDER if name in quotas):
        for quota in quotas[split]:
            band = tuple(quota["difficulties"])
            candidates = {}
            for qid, entry in eligible.items():
                if entry.get("difficulty") in band:
                    candidates.setdefault(entry.get("platform"), []).append(qid)
            counts = {platform: len(ids) for platform, ids in candidates.items()}
            if sum(counts.values()) < quota["count"]:
                shortfalls.append({"split": split, "difficulties": _band(band), "need": quota["count"],
                                   "have": sum(counts.values()), "by_platform": dict(sorted(counts.items()))})
                continue
            targets = _apportion(quota["count"], counts)
            for platform in sorted(candidates):
                ids = sorted(candidates[platform])
                rng = random.Random("%s|%s|%s|%s" % (seed, split, _band(band), platform))
                rng.shuffle(ids)
                for qid in ids[:targets[platform]]:
                    assignments[qid] = {"split": split, "family": qid, "platform": platform,
                                        "difficulty": eligible[qid].get("difficulty")}
                    del eligible[qid]
    if shortfalls:
        raise ShortfallError(shortfalls)
    splits = {name: sorted(qid for qid, item in assignments.items() if item["split"] == name)
              for name in quotas}
    result = {
        "seed": seed,
        "controls_kept_count": len(entries),
        "quotas": {name: [{"difficulties": list(q["difficulties"]), "count": q["count"]} for q in items]
                   for name, items in quotas.items()},
        "splits": splits,
        "assignments": dict(sorted(assignments.items())),
        "dropped_near_duplicates": dropped,
    }
    _write_json(os.path.join(pool, "splits.json"), result)
    return result


# ---------------------------------------------------------------- pilot manifest

def validate_executor(timeout_seconds, supports):
    """The adapter stops Claude Code ``STOP_MARGIN_SECONDS`` before the executor timeout, so the timeout must
    leave it room to run, and the manifest may claim exactly the components the adapter applies."""
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) \
            or timeout_seconds <= STOP_MARGIN_SECONDS + MIN_RUN_SECONDS:
        raise ManifestToolError(
            "executor timeout_seconds (%r) must exceed the adapter's stop margin (%gs) plus its minimum run (%gs)"
            % (timeout_seconds, STOP_MARGIN_SECONDS, MIN_RUN_SECONDS))
    if sorted(supports) != sorted(SUPPORTED):
        raise ManifestToolError(
            "executor supports must be exactly the adapter's components %s, got %s"
            % (sorted(SUPPORTED), sorted(supports)))


def _relative(target, start):
    # real paths: the loader resolves symlinks (/var -> /private/var on macOS) before it follows "../"
    return os.path.relpath(os.path.realpath(target), os.path.realpath(start)).replace(os.sep, "/")


def _development_ids(pool, splits_path):
    splits = _read_json(splits_path)
    ids = (splits.get("splits") or {}).get("development") if isinstance(splits, dict) else None
    if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
        raise ManifestToolError("%s has no development problems (run assign first)" % splits_path)
    assignments = splits.get("assignments") or {}
    for qid in ids:
        other = assignments.get(qid, {}).get("split", "development")
        if other != "development":
            raise ManifestToolError("%s is assigned to %s, not development" % (qid, other))
    return ids, splits.get("seed", DEFAULT_SEED)


def _pilot_environment(raw, timeout_seconds):
    """The manifest's environment block: ``raw`` checked, with the Gateway credentials readable by the solver
    and ``limits.timeout_seconds`` at least the executor's. The run stops a trial at the smaller of the two, and
    the loader defaults the environment's to 300 s, so a block that leaves it out would cut every trial short."""
    if not isinstance(raw, dict):
        raise ManifestToolError("--environment must be a JSON object")
    try:
        config = environment_from_json(raw)
    except ManifestError as exc:
        raise ManifestToolError("bad environment: %s" % exc) from exc
    if config.mode != "os_sandboxed":
        raise ManifestToolError(
            "the Claude Code adapter runs with bypassPermissions and needs environment.mode os_sandboxed, got %r"
            % config.mode)
    block = dict(raw)
    passthrough = list(raw.get("solver_env_passthrough") or [])
    for name in make_controls.GATEWAY_PASSTHROUGH:
        if name not in passthrough:
            passthrough.append(name)
    block["solver_env_passthrough"] = passthrough
    if config.limits.timeout_seconds < timeout_seconds and "timeout_seconds" in (raw.get("limits") or {}):
        raise ManifestToolError(
            "environment.limits.timeout_seconds (%g) is below the executor timeout_seconds (%g): trials would "
            "stop at the smaller one; raise it or leave it out" % (config.limits.timeout_seconds, timeout_seconds))
    if config.limits.timeout_seconds < timeout_seconds:
        block["limits"] = dict(raw.get("limits") or {}, timeout_seconds=timeout_seconds)
    return block


def _check_candidate(path):
    harness = load_harness(path)
    if harness.plugins:
        raise ManifestToolError("%s uses plugins, which the Claude Code adapter does not support" % path)
    return harness


def build_pilot(pool, out, *, environment, claude, claude_version=DEFAULT_CLAUDE_VERSION, model=DEFAULT_MODEL,
                max_budget_usd=DEFAULT_MAX_BUDGET_USD, repetitions=PILOT_REPETITIONS,
                timeout_seconds=EXECUTOR_TIMEOUT_SECONDS, splits_path=None, baseline=BASE_HARNESS_PATH,
                candidate=PILOT_CANDIDATE_PATH):
    """Write the pilot manifest to ``out`` and return it as a dict, after loading it with the real loader."""
    pool = os.path.abspath(pool)
    out = os.path.abspath(out)
    base_dir = os.path.dirname(out)
    if not claude:
        raise ManifestToolError("the adapter needs the pinned Claude Code executable (--claude)")
    validate_executor(timeout_seconds, SUPPORTED)
    block = _pilot_environment(environment, timeout_seconds)
    base_harness, pilot_harness = _check_candidate(baseline), _check_candidate(candidate)
    if base_harness.execution_config_hash() == pilot_harness.execution_config_hash():
        raise ManifestToolError("the candidate must differ from the baseline in its executable components")
    ids, seed = _development_ids(pool, splits_path or os.path.join(pool, "splits.json"))
    by_id = {entry["id"]: entry for entry in load_pool(pool)}
    missing = [qid for qid in ids if qid not in by_id]
    if missing:
        raise ManifestToolError("splits.json names problems that are not in the pool: %s" % ", ".join(missing[:5]))
    controlled = load_kept_ids(pool)
    uncontrolled = [qid for qid in ids if qid not in controlled]
    if uncontrolled:
        raise ManifestToolError(
            "development problems without verified controls in controls_report.json (re-run assign): %s"
            % ", ".join(sorted(uncontrolled)[:5]))
    tasks = []
    for qid in sorted(ids):
        entry = by_id[qid]
        tasks.append({
            "id": qid, "description": entry["description"], "domain": make_controls.DOMAIN,
            "split": "development", "family": qid, "runs_candidate_code": True,
            "fixture": _relative(os.path.join(pool, "fixtures", qid), base_dir),
            "grader_files": _relative(os.path.join(pool, "graders", qid), base_dir),
            "checks": [make_controls.oracle_check(entry["expected_hashes"])],
        })
    revision = ""
    report_path = os.path.join(pool, "import_report.json")
    if os.path.isfile(report_path):
        revision = str(_read_json(report_path).get("revision") or "")
    manifest = {
        "schema_version": 2,
        "name": "lcb-pilot",
        "mode": "exploratory",
        "family": "livecodebench",
        "dataset": {"id": import_lcb.DATASET, "version": revision},
        "baseline": _relative(baseline, base_dir),
        "variants": [{"id": "pilot", "harness": _relative(candidate, base_dir)}],
        "tasks": tasks,
        "repetitions": repetitions,
        "seed": seed,
        "statistics": {"timeout_is_task_failure": True},
        "environment": block,
        "executor": {
            "type": "command",
            "argv": make_controls.default_adapter_argv(model, claude, claude_version, max_budget_usd),
            "timeout_seconds": timeout_seconds,
            "model": model,
            "supports": sorted(SUPPORTED),
        },
    }
    try:
        ExperimentSpec.from_dict(manifest, base_dir)
    except ManifestError as exc:
        raise ManifestToolError("the pilot manifest does not load: %s" % exc) from exc
    os.makedirs(base_dir, exist_ok=True)
    tmp = "%s.tmp" % out
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, allow_nan=False)
        handle.write("\n")
    os.replace(tmp, out)
    return manifest


# ---------------------------------------------------------------- command line

def _load_environment_json(value):
    text = value if value.lstrip().startswith("{") else Path(value).read_text(encoding="utf-8")
    return json.loads(text)


def _fail(message):
    sys.stderr.write("make_manifests: %s\n" % message)
    return EXIT_SETUP


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    assign_parser = sub.add_parser("assign", help="write <pool>/splits.json")
    assign_parser.add_argument("--pool", required=True, help="the importer's output directory")
    assign_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    pilot_parser = sub.add_parser("pilot", help="write the exploratory pilot manifest")
    pilot_parser.add_argument("--pool", required=True)
    pilot_parser.add_argument("--environment", required=True,
                              help="the manifest's environment block, as a JSON file or inline")
    pilot_parser.add_argument("--out", required=True, help="where to write the manifest")
    pilot_parser.add_argument("--splits", help="default: <pool>/splits.json")
    pilot_parser.add_argument("--claude", default=os.environ.get(CLAUDE_PATH_ENV),
                              help="pinned Claude Code executable (default: $%s)" % CLAUDE_PATH_ENV)
    pilot_parser.add_argument("--claude-version", default=DEFAULT_CLAUDE_VERSION)
    pilot_parser.add_argument("--model", default=DEFAULT_MODEL, help="the solver model")
    pilot_parser.add_argument("--max-budget-usd", default=DEFAULT_MAX_BUDGET_USD, help="per trial, passed to the adapter")
    pilot_parser.add_argument("--repetitions", type=int, default=PILOT_REPETITIONS)
    pilot_parser.add_argument("--timeout-seconds", type=float, default=EXECUTOR_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.command == "assign":
            result = assign(args.pool, seed=args.seed)
            print("assigned %s (seed %d); dropped %d near-duplicate(s)"
                  % (", ".join("%s %d" % (name, len(ids)) for name, ids in result["splits"].items()),
                     result["seed"], len(result["dropped_near_duplicates"])))
            return 0
        environment = _load_environment_json(args.environment)
        manifest = build_pilot(
            args.pool, args.out, environment=environment, claude=args.claude,
            claude_version=args.claude_version, model=args.model, max_budget_usd=args.max_budget_usd,
            repetitions=args.repetitions, timeout_seconds=args.timeout_seconds, splits_path=args.splits)
        print("pilot manifest: %d development problems x 2 harnesses x %d repetitions = %d trials -> %s"
              % (len(manifest["tasks"]), manifest["repetitions"],
                 2 * len(manifest["tasks"]) * manifest["repetitions"], args.out))
        return 0
    except (ManifestToolError, ValueError, OSError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
