"""Build verified controls for an imported LiveCodeBench pool.

Usage (from a checkout)::

    python benchmarks/livecodebench/make_controls.py --pool <dir> --model anthropic/claude-opus-5.5 \\
        --environment <json file or inline json> --claude <claude 2.1.316 executable> \\
        [--parallelism N] [--limit N]

The experiment protocol requires, for every validation and regression task, a positive control that the
real grader passes and negative controls that it fails. LiveCodeBench has no reference solutions, so this
runs the Claude Code adapter once per problem with the base harness and the control model, and keeps the
solution only if the real grader passes it. The two negatives are an empty ``solution.py`` and a solution
that prints the public examples' outputs for their inputs and nothing else (the second proves that the
private tests decide the verdict). A problem is excluded, and listed in the report, when any control
behaves wrongly, because one wrong control makes ``run_controls`` invalidate the whole experiment.

Output under ``<pool>``::

    controls/<id>/pass/solution.py      the verified positive control
    controls/<id>/examples/solution.py  the examples-only negative
    controls/<id>/generation.json       model spend on this problem, summed over every attempt
    controls/empty/solution.py          the empty negative (shared by all problems)
    controls_report.json                kept ids, excluded ids with a reason, total_cost_usd

Exclusion reasons: ``no_passing_solution`` (no solution came back, or the grader rejected it),
``empty_passed``, ``examples_passed``, and ``grader_error`` (the grader could not decide).

Generation is a solver-role run (the backend's network allowlist applies, which the environment sets to the
Gateway); every grading run, positive and negative, goes through the backend without network. The negatives
are graded first, so a problem they already exclude costs no model spend. Kept controls are verified again
on a rerun but never regenerated; excluded problems are retried.

The expected outputs and their hashes live only in ``pool.json``; the solver sees a copy of
``fixtures/<id>`` and nothing else of the pool.
"""

import argparse
import concurrent.futures
import dataclasses
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import uuid
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.append(ROOT)

from harness_workbench.core.harness import harness_from_dict  # noqa: E402
from harness_workbench.experiments.backends import (  # noqa: E402
    BackendUnavailable, ProcessLimits, require_backend,
)
from harness_workbench.experiments.command import CommandHarnessExecutor  # noqa: E402
from harness_workbench.experiments.contracts import PublicTrialRequest  # noqa: E402
from harness_workbench.experiments.grader import (  # noqa: E402
    GraderBundle, SnapshotLimits, evaluate_bundle,
)
from harness_workbench.experiments.manifest import ManifestError  # noqa: E402
from harness_workbench.experiments.manifest import _environment as environment_from_json  # noqa: E402
from harness_workbench.experiments.status import EvaluationStatus, ExecutionStatus  # noqa: E402

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
DOMAIN = "competitive-programming"
GATEWAY_PASSTHROUGH = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")
CLAUDE_PATH_ENV = "HARNESS_CLAUDE_PATH"
DEFAULT_CLAUDE_VERSION = "2.1.316"
DEFAULT_MAX_BUDGET_USD = "0.50"
DEFAULT_TIMEOUT_SECONDS = 900.0
CHECK_TIMEOUT_SECONDS = 300
MAX_SOLUTION_BYTES = 1 << 20
DETAIL_CHARS = 300
DEFAULT_HARNESS = os.path.join(HERE, "base_harness.json")

NO_PASSING_SOLUTION = "no_passing_solution"
EMPTY_PASSED = "empty_passed"
EXAMPLES_PASSED = "examples_passed"
GRADER_ERROR = "grader_error"

EXIT_SETUP = 2
EXIT_EXECUTION_ERRORS = 3

# Used when no base_harness.json sits next to this script: the base harness of spec section 8.
BASE_HARNESS = {
    "id": "lcb-base",
    "version": "1",
    "name": "LiveCodeBench base",
    "domain": DOMAIN,
    "system_prompt": (
        "You solve competitive programming problems. Read PROBLEM.md in the working directory, write a "
        "correct and efficient solution to solution.py, test it on the examples, and finish. Only "
        "solution.py is graded."),
    "tools": [
        {"name": "Bash", "description": "Run shell commands.", "guidance": ""},
        {"name": "Read", "description": "Read files.", "guidance": ""},
        {"name": "Write", "description": "Write files.", "guidance": ""},
        {"name": "Edit", "description": "Edit files.", "guidance": ""},
    ],
}

STDIN_TEMPLATE = '''"""Negative control: prints the public examples' outputs for their inputs, nothing else."""
import json
import sys

EXAMPLES = json.loads(%s)
DATA = sys.stdin.buffer.read().decode("utf-8", "surrogatepass")
sys.stdout.write(EXAMPLES.get(DATA, ""))
'''

FUNCTIONAL_TEMPLATE = '''"""Negative control: answers the public examples' arguments, raises for anything else."""
import json

EXAMPLES = json.loads(%s)


class Solution:
    def %s(self, *args):
        return json.loads(EXAMPLES[json.dumps(list(args))])
'''


# ---------------------------------------------------------------- controls

def examples_solution(entry):
    """Source of the examples-only negative control for one pool entry."""
    mode = entry["mode"]
    examples = entry.get("public_examples") or []
    if mode == "stdin":
        table = {example["input"]: example["output"] for example in examples}
        return STDIN_TEMPLATE % repr(json.dumps(table))
    if mode == "functional":
        func_name = entry.get("func_name")
        if not isinstance(func_name, str) or not func_name.isidentifier():
            raise ValueError("pool entry %s has no usable func_name" % entry.get("id"))
        table = {json.dumps(json.loads(example["input"])): example["output"] for example in examples}
        return FUNCTIONAL_TEMPLATE % (repr(json.dumps(table)), func_name)
    raise ValueError("pool entry %s has unknown mode %r" % (entry.get("id"), mode))


def oracle_check(expected_hashes):
    """The task check: the grader only observes hashes, the supervisor compares them with the expected ones."""
    return {
        "id": "lcb-hashes", "type": "oracle", "weight": 1.0, "critical": True,
        "argv": ["{python}", "-I", "grader.py", "{artifact}", "{out}"],
        "observed": "observed.json", "compare": "json_equal",
        "expected": {"hashes": list(expected_hashes)},
        "timeout_seconds": CHECK_TIMEOUT_SECONDS,
    }


def default_adapter_argv(model, claude, claude_version, max_budget_usd):
    """The invocation of spec section 6, as an executor argv with placeholders."""
    return [
        "{python}", "-I", "{claude_code_agent}",
        "--harness", "{harness_json}", "--task", "{task_json}", "--workspace", "{workspace}",
        "--result", "{result_json}", "--model", model, "--max-budget-usd", str(max_budget_usd),
        "--claude", claude, "--claude-version", claude_version,
    ]


def with_gateway_passthrough(environment):
    """``environment`` whose solver may read the Gateway credentials (never the graders, never logged)."""
    missing = tuple(name for name in GATEWAY_PASSTHROUGH if name not in environment.solver_env_passthrough)
    if not missing:
        return environment
    return dataclasses.replace(
        environment, solver_env_passthrough=(*environment.solver_env_passthrough, *missing))


def load_base_harness(path=None):
    """``(harness, source)``: the file at ``path``, else base_harness.json beside this script, else built in."""
    chosen = path or (DEFAULT_HARNESS if os.path.isfile(DEFAULT_HARNESS) else None)
    if chosen is None:
        return harness_from_dict(dict(BASE_HARNESS)), "built-in"
    with open(chosen, encoding="utf-8") as handle:
        return harness_from_dict(json.load(handle)), os.path.abspath(chosen)


def _short(text):
    return " ".join(str(text).split())[:DETAIL_CHARS]


def _cost_of(outputs):
    details = outputs.get("details") if isinstance(outputs, dict) else None
    value = details.get("total_cost_usd") if isinstance(details, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _read_solution(workspace):
    """The bytes of ``solution.py`` when it is a regular file of sane size, else None. Never follows a link."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(os.path.join(workspace, "solution.py"), flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOLUTION_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_SOLUTION_BYTES + 1)
    finally:
        os.close(fd)
    return data if len(data) <= MAX_SOLUTION_BYTES else None


def _write_solution(directory, data):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "solution.py"), "wb") as handle:
        handle.write(data)


def _is_regular(path):
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, allow_nan=False)
        handle.write("\n")
    os.replace(tmp, path)


class _Excluded(Exception):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class ControlsBuilder:
    def __init__(self, pool, *, model, environment, backend, adapter_argv, harness, timeout_seconds):
        self.pool = os.path.abspath(pool)
        self.controls = os.path.join(self.pool, "controls")
        self.model = model
        self.backend = backend
        self.harness_public = harness.public_components()
        env_limits = ProcessLimits(
            timeout_seconds=environment.limits.timeout_seconds,
            max_output_bytes=environment.limits.max_output_bytes,
            max_files=environment.limits.max_files,
            max_total_bytes=environment.limits.max_total_bytes)
        self.grade_limits = env_limits
        self.snapshot_limits = SnapshotLimits(env_limits.max_files, env_limits.max_total_bytes)
        self.solver_limits = dataclasses.replace(env_limits, timeout_seconds=float(timeout_seconds))
        self.executor = CommandHarnessExecutor(
            list(adapter_argv), float(timeout_seconds), model, tuple(environment.solver_env_passthrough))
        self.empty_dir = os.path.join(self.controls, "empty")

    # -- grading --------------------------------------------------------------

    def _bundle(self, entry):
        grader_dir = Path(self.pool) / "graders" / entry["id"]
        if not grader_dir.is_dir():
            raise ValueError("pool entry %s has no graders directory" % entry["id"])
        return GraderBundle.build(entry["id"], [oracle_check(entry["expected_hashes"])], grader_dir)

    def _grade(self, bundle, directory, label):
        """The evaluation status of the solution in ``directory``; graded with no network."""
        result = evaluate_bundle(
            bundle, {}, Path(directory), backend=self.backend, limits=self.grade_limits,
            snapshot_limits=self.snapshot_limits, trial_id="control-%s-%s" % (bundle.task_id, label))
        if result.status == EvaluationStatus.EVALUATION_ERROR:
            raise _Excluded(GRADER_ERROR, "%s control: %s" % (label, _short(result.error)))
        return result.status

    def _check_negatives(self, bundle, examples_dir):
        for label, directory, reason in (("empty", self.empty_dir, EMPTY_PASSED),
                                         ("examples", examples_dir, EXAMPLES_PASSED)):
            if self._grade(bundle, directory, label) != EvaluationStatus.FAILED:
                raise _Excluded(reason)

    # -- generation -----------------------------------------------------------

    def _ledger_path(self, qid):
        return os.path.join(self.controls, qid, "generation.json")

    def _read_ledger(self, qid):
        try:
            with open(self._ledger_path(qid), encoding="utf-8") as handle:
                data = json.load(handle)
            return {"attempts": int(data["attempts"]), "cost_usd": float(data["cost_usd"]),
                    "cost_unknown_attempts": int(data.get("cost_unknown_attempts", 0))}
        except (OSError, ValueError, KeyError, TypeError):
            return {"attempts": 0, "cost_usd": 0.0, "cost_unknown_attempts": 0}

    def _record_spend(self, qid, cost):
        ledger = self._read_ledger(qid)
        ledger["attempts"] += 1
        if cost is None:
            ledger["cost_unknown_attempts"] += 1
        else:
            ledger["cost_usd"] += cost
        ledger["model"] = self.model
        os.makedirs(os.path.join(self.controls, qid), exist_ok=True)
        _write_json(self._ledger_path(qid), ledger)

    def _generate(self, entry, stage, outcome):
        """Run the adapter in a copy of the fixture and return the bytes of its ``solution.py`` (or raise)."""
        qid = entry["id"]
        run = os.path.join(stage, "run")
        workspace = Path(run) / "workspace"
        fixture = Path(self.pool) / "fixtures" / qid
        if not fixture.is_dir():
            raise ValueError("pool entry %s has no fixtures directory" % qid)
        os.makedirs(run)
        shutil.copytree(fixture, workspace, symlinks=True)
        request = PublicTrialRequest(
            trial_id=uuid.uuid4().hex, task_description=entry["description"], domain=DOMAIN,
            constraints=(), harness_public=self.harness_public, fixture_hash=None, seed=0,
            budget={"timeout_seconds": self.solver_limits.timeout_seconds,
                    "max_output_bytes": self.solver_limits.max_output_bytes})
        try:
            result = self.executor.execute_trial(
                request, workspace=workspace, backend=self.backend, limits=self.solver_limits)
        except Exception as exc:  # an adapter or backend failure is recorded, never fatal for the others
            outcome["execution_error"] = True
            raise _Excluded(NO_PASSING_SOLUTION, "generation raised %s: %s" % (type(exc).__name__, _short(exc)))
        cost = _cost_of(result.outputs)
        outcome["cost"] = cost or 0.0
        self._record_spend(qid, cost)
        if result.status == ExecutionStatus.EXECUTION_ERROR:
            outcome["execution_error"] = True
            raise _Excluded(NO_PASSING_SOLUTION, "execution_error: %s" % _short(result.error or result.stderr_tail))
        data = _read_solution(workspace)
        if data is None:
            raise _Excluded(NO_PASSING_SOLUTION, "no solution.py (%s)" % result.status.value)
        return data

    # -- one problem ----------------------------------------------------------

    def _cached(self, entry, bundle):
        """True when the controls already on disk still verify.

        Stale ones (a definite mismatch under the grader) are removed. When the grader cannot decide
        (a ``grader_error``) the files stay and the error propagates: a flake must neither throw away a
        verified control nor pay for a new generation.
        """
        cdir = os.path.join(self.controls, entry["id"])
        passed, examples = os.path.join(cdir, "pass"), os.path.join(cdir, "examples")
        if not (_is_regular(os.path.join(passed, "solution.py"))
                and _is_regular(os.path.join(examples, "solution.py"))):
            return False
        try:
            self._check_negatives(bundle, examples)
            if self._grade(bundle, passed, "pass") == EvaluationStatus.PASSED:
                return True
        except _Excluded as excluded:
            if excluded.reason == GRADER_ERROR:
                raise
        shutil.rmtree(passed, ignore_errors=True)
        shutil.rmtree(examples, ignore_errors=True)
        return False

    def process(self, entry):
        qid = entry["id"]
        outcome = {"id": qid, "kept": False, "cost": 0.0, "execution_error": False, "cached": False}
        stage = tempfile.mkdtemp(prefix="lcb-controls-")
        try:
            bundle = self._bundle(entry)
            if self._cached(entry, bundle):
                outcome.update(kept=True, cached=True)
                return outcome
            examples_dir = os.path.join(stage, "examples")
            _write_solution(examples_dir, examples_solution(entry).encode("utf-8"))
            self._check_negatives(bundle, examples_dir)
            data = self._generate(entry, stage, outcome)
            pass_dir = os.path.join(stage, "pass")
            _write_solution(pass_dir, data)
            if self._grade(bundle, pass_dir, "pass") != EvaluationStatus.PASSED:
                raise _Excluded(NO_PASSING_SOLUTION, "the grader rejected the generated solution")
            cdir = os.path.join(self.controls, qid)
            for name in ("examples", "pass"):  # pass last: its presence is what marks a kept control
                shutil.rmtree(os.path.join(cdir, name), ignore_errors=True)
                os.makedirs(cdir, exist_ok=True)
                shutil.copytree(os.path.join(stage, name), os.path.join(cdir, name))
            outcome["kept"] = True
        except _Excluded as excluded:
            outcome.update(reason=excluded.reason, detail=excluded.detail)
        except Exception as exc:  # one broken problem must not stop the others
            outcome.update(reason=GRADER_ERROR, detail="%s: %s" % (type(exc).__name__, _short(exc)))
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return outcome

    def total_cost(self, ids):
        return sum(self._read_ledger(qid)["cost_usd"] for qid in ids)

    def unknown_cost_attempts(self, ids):
        return sum(self._read_ledger(qid)["cost_unknown_attempts"] for qid in ids)


# ---------------------------------------------------------------- driver

def _load_pool(pool):
    with open(os.path.join(pool, "pool.json"), encoding="utf-8") as handle:
        entries = json.load(handle)
    if not isinstance(entries, list):
        raise ValueError("pool.json must hold a list")
    for entry in entries:
        qid = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(qid, str) or not ID_RE.match(qid):
            raise ValueError("pool.json holds an unsafe or missing problem id: %r" % (qid,))
    return entries


def build_controls(pool, *, model, environment, backend=None, adapter_argv=None, harness=None,
                   parallelism=None, limit=None, claude=None, claude_version=DEFAULT_CLAUDE_VERSION,
                   max_budget_usd=DEFAULT_MAX_BUDGET_USD, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                   log=print):
    """Build the controls of ``pool`` and write ``controls_report.json``; return the report."""
    pool = os.path.abspath(pool)
    entries = _load_pool(pool)
    if adapter_argv is None:
        if not claude:
            raise ValueError("the default adapter needs the pinned Claude Code executable (--claude)")
        adapter_argv = default_adapter_argv(model, claude, claude_version, max_budget_usd)
        environment = with_gateway_passthrough(environment)
    if parallelism is not None:
        environment = dataclasses.replace(environment, parallelism=parallelism)
    harness_obj, harness_source = (harness, "caller") if harness is not None else load_base_harness()
    if backend is None:
        backend = require_backend(environment)
    builder = ControlsBuilder(pool, model=model, environment=environment, backend=backend,
                              adapter_argv=adapter_argv, harness=harness_obj, timeout_seconds=timeout_seconds)
    selected = entries if limit is None else entries[:max(0, int(limit))]
    os.makedirs(builder.empty_dir, exist_ok=True)
    _write_solution(builder.empty_dir, b"")

    outcomes = {}
    lock = threading.Lock()
    workers = environment.parallelism
    pool_executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool_executor.submit(builder.process, entry) for entry in selected]
        for future in concurrent.futures.as_completed(futures):
            outcome = future.result()
            with lock:
                outcomes[outcome["id"]] = outcome
            if outcome["kept"]:
                log("kept     %s%s" % (outcome["id"], " (verified again)" if outcome["cached"] else ""))
            else:
                log("excluded %s: %s" % (outcome["id"], outcome["reason"]))
    finally:
        pool_executor.shutdown(wait=True, cancel_futures=True)
        report = _report(builder, pool, model, harness_obj, harness_source, entries, selected, outcomes, limit)
        _write_json(os.path.join(pool, "controls_report.json"), report)
    return report


def _report(builder, pool, model, harness, harness_source, entries, selected, outcomes, limit=None):
    ordered = [outcomes[entry["id"]] for entry in selected if entry["id"] in outcomes]
    kept = [item["id"] for item in ordered if item["kept"]]
    excluded = []
    reasons = {}
    for item in ordered:
        if item["kept"]:
            continue
        row = {"id": item["id"], "reason": item["reason"]}
        if item.get("detail"):
            row["detail"] = item["detail"]
        excluded.append(row)
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    ids = [item["id"] for item in ordered]
    return {
        "model": model,
        "harness": {"source": harness_source, "execution_config_hash": harness.execution_config_hash()},
        "pool_size": len(entries),
        "attempted": len(ordered),
        # False for a --limit run or one stopped before every problem was decided: ``kept`` is then a subset of
        # the pool and make_manifests refuses to draw splits from it
        "complete": len(ordered) == len(entries),
        "limit": limit,
        "kept_count": len(kept),
        "excluded_count": len(excluded),
        "kept": kept,
        "excluded": excluded,
        "exclusion_counts": reasons,
        "execution_errors": sum(1 for item in ordered if item["execution_error"]),
        "grader_errors": sum(1 for item in ordered if item.get("reason") == GRADER_ERROR),
        "run_cost_usd": round(sum(item["cost"] for item in ordered), 6),
        "total_cost_usd": round(builder.total_cost(ids), 6),
        "cost_unknown_attempts": builder.unknown_cost_attempts(ids),
    }


# ---------------------------------------------------------------- command line

def load_environment(value):
    """An ``EnvironmentConfig`` from inline JSON or the path of a JSON file (the manifest's ``environment``)."""
    text = value if value.lstrip().startswith("{") else Path(value).read_text(encoding="utf-8")
    return environment_from_json(json.loads(text))


def _fail(message):
    sys.stderr.write("make_controls: %s\n" % message)
    return EXIT_SETUP


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pool", required=True, help="the importer's output directory")
    parser.add_argument("--model", required=True, help="e.g. anthropic/claude-opus-5.5")
    parser.add_argument("--environment", required=True, help="the manifest's environment block, as a JSON file or inline")
    parser.add_argument("--parallelism", type=int, help="problems at once (default: environment.parallelism)")
    parser.add_argument("--limit", type=int, help="process only the first N problems of the pool")
    parser.add_argument("--harness", help="base harness JSON (default: base_harness.json beside this script, else built in)")
    parser.add_argument("--claude", default=os.environ.get(CLAUDE_PATH_ENV),
                        help="pinned Claude Code executable (default: $%s)" % CLAUDE_PATH_ENV)
    parser.add_argument("--claude-version", default=DEFAULT_CLAUDE_VERSION)
    parser.add_argument("--max-budget-usd", default=DEFAULT_MAX_BUDGET_USD, help="per problem, passed to the adapter")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                        help="per generation run")
    parser.add_argument("--adapter-argv", help="JSON array replacing the Claude Code adapter command (tests, other adapters)")
    args = parser.parse_args(argv)

    try:
        environment = load_environment(args.environment)
        adapter_argv = None
        if args.adapter_argv is not None:
            adapter_argv = json.loads(args.adapter_argv)
            if not isinstance(adapter_argv, list) or not all(isinstance(item, str) for item in adapter_argv):
                return _fail("--adapter-argv must be a JSON array of strings")
        harness = load_base_harness(args.harness)[0] if args.harness else None
    except (ManifestError, ValueError, OSError) as exc:
        return _fail("bad arguments: %s" % exc)
    if adapter_argv is None:
        if not args.claude:
            return _fail("--claude (or $%s) must name the pinned Claude Code executable" % CLAUDE_PATH_ENV)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return _fail("ANTHROPIC_API_KEY is not set (a Gateway key with a spend limit)")
    try:
        report = build_controls(
            args.pool, model=args.model, environment=environment, adapter_argv=adapter_argv, harness=harness,
            parallelism=args.parallelism, limit=args.limit, claude=args.claude,
            claude_version=args.claude_version, max_budget_usd=args.max_budget_usd,
            timeout_seconds=args.timeout_seconds)
    except (BackendUnavailable, ManifestError, ValueError, OSError) as exc:
        return _fail(str(exc))
    print("controls: kept %d of %d attempted (pool %d), excluded %d %s, spend $%.4f this run, $%.4f in total"
          % (report["kept_count"], report["attempted"], report["pool_size"], report["excluded_count"],
             json.dumps(report["exclusion_counts"], sort_keys=True), report["run_cost_usd"],
             report["total_cost_usd"]))
    if report["execution_errors"] or report["grader_errors"]:
        sys.stderr.write("make_controls: %d generation(s) failed and %d grading(s) errored, for reasons that "
                         "are not the model's (see controls_report.json); fix them and rerun\n"
                         % (report["execution_errors"], report["grader_errors"]))
        return EXIT_EXECUTION_ERRORS
    return 0


if __name__ == "__main__":
    sys.exit(main())
