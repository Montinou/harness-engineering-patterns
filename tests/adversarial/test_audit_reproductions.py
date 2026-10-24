"""Reproductions of the audit findings A1-A12 (run-02, commit 73bcb130).

Every test is decorated ``@repro`` (``tests.support``), so each one is an
*expected failure* while the defect exists: the default suite stays green, and a
test that starts passing is reported as an "unexpected success" the moment its
fix lands, which is the cue to drop the decorator. Run with
``HARNESS_REPRO_STRICT=1`` to execute them undecorated and read the real
assertion failures.

Coverage: sixteen reproductions for A1-A7, A9, A10 and A12 (the two A06 tests are undecorated since T5;
the A01 shadowing test is undecorated since T8 and runs through ``evaluate_bundle``;
the A04 latency, both A10 and the A12 output-limit tests are undecorated since T9, the grader one runs
through ``evaluate_bundle``; A02, A09 and the A12 persistence test are undecorated since T18, A02 and A09
on v2 confirmatory manifests, the A12 crash modelled as a ``BaseException``; A03 and A04 are undecorated since
T22, on v2 confirmatory manifests decided by ``stats.decide``; the A12 implementation-hash test since T25).
 * A8 (low statistical power) has no unit reproduction; it is recreated as a
   statistical simulation in T24.
 * A11 (``test_improve_cli_can_require_experiment_validation`` fails outside CI
   because it needs ``python`` on PATH and an importable package) is covered by
   that pre-existing failing test in ``tests/test_experiments.py``; it is not
   duplicated here.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from harness_workbench.core.db import WorkbenchDB
from harness_workbench.core.executor import BehavioralReport, MockExecutor, Task
from harness_workbench.core.harness import Harness, load_harness
from harness_workbench.core.improver import Improver
from harness_workbench.experiments import ExperimentSpec, RunContext, run_experiment
from harness_workbench.experiments.backends import LocalBackend, ProcessLimits
from harness_workbench.experiments.command import CommandHarnessExecutor
from harness_workbench.experiments.grader import GraderBundle, SnapshotLimits, evaluate_bundle
from harness_workbench.experiments.manifest import ManifestError
from harness_workbench.experiments.status import DecisionStatus, EvaluationStatus
from tests.support import ENV_CANARY, ScriptedExecutor, SimulatedCrash, exact_task, repro, write_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)


def _command_context(root: Path) -> RunContext:
    return RunContext("experiment", "run", "t1", "candidate", 0, 1, root / "work" / "workspace")


def _command_run(root: Path, code: str, timeout: float = 30) -> BehavioralReport:
    executor = CommandHarnessExecutor([sys.executable, "-c", code], timeout)
    harness = Harness(id="sample", version="1", system_prompt="candidate")
    return executor.execute_case(harness, Task.from_text("task", task_id="t1"), _command_context(root))


class A01GraderIntegrity(TempDirCase):
    def test_rewritten_check_script_still_fails(self) -> None:
        """A1: the verifier lives in the solver's workspace, so a solver can rewrite a failing check to pass."""
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "check.py").write_text("import sys; sys.exit(1)\n", encoding="utf-8")
        bundle_src = self.root / "graders"
        bundle_src.mkdir()
        (bundle_src / "check.py").write_text("import sys; sys.exit(1)\n", encoding="utf-8")
        chk = [{"id": "c", "type": "command", "argv": ["{python}", "-I", "check.py"]}]
        bundle = GraderBundle.build("t1", chk, bundle_src)

        def evaluate():
            return evaluate_bundle(bundle, {}, workspace, backend=LocalBackend(),
                                   limits=ProcessLimits(timeout_seconds=30),
                                   snapshot_limits=SnapshotLimits(100, 1_000_000), trial_id="trial")

        self.assertEqual(evaluate().status, EvaluationStatus.FAILED, "precondition: grader fails untampered")
        (workspace / "check.py").write_text("pass\n", encoding="utf-8")  # the "solver" rewrites the grader
        self.assertEqual(evaluate().status, EvaluationStatus.FAILED)

    def test_shadowed_unittest_module_cannot_pass(self) -> None:
        """A1: a command check run in the solver's workspace imports solver-controlled modules."""
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "unittest.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        (workspace / "run_tests.py").write_text("import unittest\nraise SystemExit(1)\n", encoding="utf-8")
        bundle_src = self.root / "graders"
        bundle_src.mkdir()
        (bundle_src / "run_tests.py").write_text("import unittest\nraise SystemExit(1)\n", encoding="utf-8")
        checks = [{"id": "tests", "type": "command", "argv": ["{python}", "-I", "run_tests.py"]}]
        result = evaluate_bundle(GraderBundle.build("t1", checks, bundle_src), {}, workspace,
                                 backend=LocalBackend(), limits=ProcessLimits(timeout_seconds=30),
                                 snapshot_limits=SnapshotLimits(100, 1_000_000), trial_id="trial")
        self.assertEqual(result.status, EvaluationStatus.FAILED)


_CONTROLS = {"positive": {"outputs": {"answer": "CORRECT"}}, "negatives": [{"outputs": {"answer": "INCORRECT"}}]}


def _confirmatory(root: Path, *, tasks: list[dict], variants: list[dict]) -> Path:
    """A schema v2 confirmatory manifest: holdout tasks get controls, trusted_local is acknowledged."""
    tasks = [dict(task, runs_candidate_code=False,
                  **({"controls": _CONTROLS} if task.get("split") in ("validation", "regression") else {}))
             for task in tasks]
    return write_manifest(
        root, tasks=tasks, variants=variants, schema_version=2, mode="confirmatory", family="audit",
        dataset={"id": "audit", "version": "1"}, repetitions=5,
        environment={"mode": "trusted_local", "trusted_solver_acknowledged": True})


class A02SingleFinalist(TempDirCase):
    def test_only_one_variant_reaches_validation_tasks(self) -> None:
        """A2: no held-out split or multiplicity control; every variant is scored on every task and any can win."""
        # Converted (T18) to a v2 confirmatory manifest: five candidates, controls, acknowledged trusted_local.
        names = [f"n{i}" for i in range(5)]
        tasks = [exact_task(f"d{i}", split="development") for i in (1, 2, 3)]
        tasks += [exact_task(f"v{i}", split="validation") for i in (1, 2, 3)]
        tasks += [exact_task("r1", split="regression")]
        manifest = _confirmatory(self.root / "m", tasks=tasks,
                                 variants=[{"id": n, "system_prompt": n} for n in names])
        always = {prompt: (lambda task_id: True) for prompt in ("base", *names)}
        result = run_experiment(ExperimentSpec.load(manifest), ScriptedExecutor(always), self.root / "out")
        on_validation = {r["variant_id"] for r in result["runs"] if r["case_id"].startswith("v")} - {"baseline"}
        self.assertEqual(len(on_validation), 1)


class A03MinimumUnits(TempDirCase):
    def test_one_task_many_repetitions_is_not_eligible(self) -> None:
        """A3: one task is enough; repetitions of a single task are pseudo-replication yet qualify a candidate."""
        # Converted (T22) to a v2 confirmatory manifest with ONE validation task (and one regression task).
        manifest = _confirmatory(self.root / "m", tasks=[exact_task("only", split="validation"),
                                                         exact_task("guard", split="regression")],
                                 variants=[{"id": "candidate", "system_prompt": "cand"}])
        executor = ScriptedExecutor({"base": lambda t: False, "cand": lambda t: True})
        result = run_experiment(ExperimentSpec.load(manifest), executor, self.root / "out")
        decision = result["decision"]
        self.assertEqual(decision["status"], DecisionStatus.INSUFFICIENT_EVIDENCE.value)
        self.assertEqual(decision["n_units"], 1)
        self.assertTrue(any("min_independent_units 50" in reason for reason in decision["reasons"]),
                        decision["reasons"])


class A04UsageGates(TempDirCase):
    def test_unknown_baseline_tokens_block(self) -> None:
        """A4: the cost gate fails open; baseline tokens 0 with a 10M-token candidate is still eligible."""
        # Converted (T22) to a v2 confirmatory manifest: the zero baseline makes the token ratio undefined.
        tasks = [exact_task(f"t{i}", split="validation") for i in (1, 2, 3)] + [exact_task("g1", split="regression")]
        manifest = _confirmatory(self.root / "m", tasks=tasks, variants=[{"id": "candidate", "system_prompt": "cand"}])
        executor = ScriptedExecutor({"base": lambda t: False, "cand": lambda t: True},
                                    tokens={"base": 0, "cand": 10_000_000})
        result = run_experiment(ExperimentSpec.load(manifest), executor, self.root / "out")
        decision = result["decision"]
        self.assertEqual(decision["status"], DecisionStatus.INSUFFICIENT_EVIDENCE.value)
        self.assertIn("token ratio undefined: baseline usage is zero", decision["reasons"])
        self.assertIsNone(decision["diagnostics"]["token_ratio"])

    def test_adapter_latency_is_not_authoritative(self) -> None:
        """A4: the command agent self-reports latency (command.py:99), so a slow command can report zero."""
        code = ("import os, json, time; time.sleep(0.3); "
                "json.dump({'latency_ms': 0, 'outputs': {'answer': 'x'}}, open(os.environ['HARNESS_RESULT_FILE'], 'w'))")
        record = _command_run(self.root, code)
        self.assertGreaterEqual(record.latency_ms, 250)


class A05GraderErrors(TempDirCase):
    def test_missing_grader_is_not_verified(self) -> None:
        """A5: a grader infrastructure error counts as a quality failure and the run is still 'verified'."""
        bundle = GraderBundle.build("t1", [{"id": "g", "type": "command", "argv": ["/nonexistent/grader"]}], None)
        result = evaluate_bundle(bundle, {"answer": "CORRECT"}, None, backend=LocalBackend(),
                                 limits=ProcessLimits(timeout_seconds=30),
                                 snapshot_limits=SnapshotLimits(100, 1_000_000), trial_id="trial")
        self.assertEqual(result.status, EvaluationStatus.EVALUATION_ERROR)
        self.assertIsNone(result.score)


class A06StrictValues(TempDirCase):
    def _manifest(self, **overrides: object) -> Path:
        # json.dumps writes float('nan') / float('inf') as NaN / Infinity, which json.loads accepts.
        return write_manifest(self.root / "m", tasks=[exact_task(f"t{i}") for i in (1, 2, 3)],
                              variants=[{"id": "candidate", "system_prompt": "cand"}], **overrides)

    def test_nan_in_each_gate_parameter_is_rejected(self) -> None:
        """A6 (fixed in T5): NaN in a gate parameter is a ManifestError at load, not a disabled gate."""
        for key in ("max_regression", "min_improvement", "max_cost_ratio"):
            path = self._manifest(**{key: float("nan")})
            with self.assertRaises(ManifestError, msg=f"{key}=NaN"):
                ExperimentSpec.load(path)

    def test_bool_and_infinity_are_rejected(self) -> None:
        """A6 (fixed in T5): JSON booleans and Infinity are rejected instead of coerced by int()/float()."""
        # "repetitions": true was refused before only incidentally (1 < min_samples); it is now refused
        # because bool is not an integer. The seed, min_improvement and an infinite budget ratio were coerced.
        cases = [("repetitions", True), ("seed", True), ("min_improvement", True),
                 ("max_cost_ratio", float("inf"))]
        for key, value in cases:
            path = self._manifest(**{key: value})
            with self.assertRaises(ManifestError, msg=f"{key}={value!r}"):
                ExperimentSpec.load(path)


class _BarrierExecutor:
    """MockExecutor that makes all concurrent improvers advance in lockstep."""

    def __init__(self, barrier: threading.Barrier) -> None:
        self.inner = MockExecutor()
        self.barrier = barrier

    def execute(self, harness: Harness, task: Task):
        report = self.inner.execute(harness, task)
        self.barrier.wait(timeout=60)
        return report


class A07ConcurrentLineage(TempDirCase):
    def test_six_concurrent_improvers_keep_a_single_base(self) -> None:
        """A7: concurrent library writers create phantom copies of the base under new version numbers."""
        base = load_harness(REPO_ROOT / "examples/workbench/base_research_harness.json")
        task = Task.from_text("Fact-check a claim", domain="research")
        workers = 6
        gate = threading.Barrier(workers)
        errors: list[str] = []
        shared = threading.Barrier(workers)  # every improver makes the same number of probes

        def run() -> None:
            try:
                gate.wait(timeout=60)
                Improver(executor=_BarrierExecutor(shared)).improve(base.copy(), task, root=self.root)
            except Exception as exc:  # reported below
                errors.append(repr(exc))

        threads = [threading.Thread(target=run) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        files = sorted((self.root / "library/harnesses" / base.id).glob("*.json"))
        parents = {p.stem: json.loads(p.read_text())["lineage"]["parent_version"] for p in files}
        roots = [v for v, parent in sorted(parents.items()) if parent is None]
        self.assertEqual(roots, ["1"], f"improver errors: {errors}")
        self.assertEqual(errors, [], "every concurrent improver must complete")
        db = WorkbenchDB.open(self.root)
        try:
            stored = db.versions("research-agent")
        finally:
            db.close()
        self.assertEqual(len([v for v in stored if v.lineage.parent_version is None]), 1)


class A09HoldoutReuse(TempDirCase):
    def test_renamed_holdout_second_candidate_is_not_eligible(self) -> None:
        """A9: the validation set is reused at every step and run; renaming task ids hides the reuse."""
        executor = ScriptedExecutor({"base": lambda t: False, "cand-a": lambda t: True, "cand-b": lambda t: True})
        library = self.root / "library-root"

        def holdout(prefix: str) -> list[dict]:  # same content, different ids
            tasks = [exact_task(f"{prefix}{i}", f"holdout case {i}", split="validation") for i in (1, 2, 3)]
            return tasks + [exact_task(f"{prefix}-regression", "holdout regression case", split="regression")]

        # Converted (T18) to v2 confirmatory manifests; the second run is EXPLORATORY_ONLY with the exposure reason.
        first = run_experiment(ExperimentSpec.load(_confirmatory(
            self.root / "first", tasks=holdout("h"), variants=[{"id": "candidate", "system_prompt": "cand-a"}])),
            executor, library)
        self.assertIsNone(first["exposure"]["block_reason"], "precondition: the first use reserves the holdout")
        self.assertNotEqual(first["decision"]["status"], "exploratory_only")
        second = run_experiment(ExperimentSpec.load(_confirmatory(
            self.root / "second", tasks=holdout("r"), variants=[{"id": "candidate", "system_prompt": "cand-b"}])),
            executor, library)
        self.assertEqual(second["decision"]["status"], "exploratory_only")
        self.assertIsNotNone(second["exposure"]["block_reason"])
        self.assertIn(second["exposure"]["block_reason"], second["decision"]["reasons"])


class A10EnvironmentSecrets(TempDirCase):
    def test_solver_does_not_inherit_parent_canary(self) -> None:
        """A10: the full parent environment is inherited by the agent command."""
        # Report only the canary variable, never the whole environment: a failing assertion must not print secrets.
        code = ("import os, json; json.dump({'outputs': {'answer': os.environ.get('HARNESS_TEST_CANARY', '')}}, "
                "open(os.environ['HARNESS_RESULT_FILE'], 'w'))")
        with mock.patch.dict(os.environ, {"HARNESS_TEST_CANARY": ENV_CANARY}):
            report = _command_run(self.root, code)
        self.assertNotIn(ENV_CANARY, report.outputs["answer"])

    def test_grader_does_not_inherit_parent_canary(self) -> None:
        """A10 (fixed in T9): graders run with the minimal environment, so the parent canary is absent."""
        workspace = self.root / "ws"
        workspace.mkdir()
        probe = "import os, sys; sys.exit(%s)"

        def evaluate(exit_code: str):
            checks = [{"id": "env", "type": "command", "argv": ["{python}", "-I", "-c", probe % exit_code]}]
            return evaluate_bundle(GraderBundle.build("t1", checks, None), {}, workspace, backend=LocalBackend(),
                                   limits=ProcessLimits(timeout_seconds=30),
                                   snapshot_limits=SnapshotLimits(100, 1_000_000), trial_id="trial")

        with mock.patch.dict(os.environ, {"HARNESS_TEST_CANARY": ENV_CANARY}):
            # Precondition: the probe passes when the canary is absent, so FAILED below means "present".
            self.assertEqual(evaluate("1 if 'HARNESS_TEST_CANARY' in os.environ else 0").status, EvaluationStatus.PASSED)
            # The original reproduction's check: exit 0 only if the canary is visible.
            self.assertEqual(evaluate("0 if 'HARNESS_TEST_CANARY' in os.environ else 1").status, EvaluationStatus.FAILED)


class _ProcessCrash(BaseException):
    """A process-level crash (like KeyboardInterrupt): unlike an adapter exception, no trial isolation absorbs it."""


class _Crashing:
    """Turns the scripted executor's ``SimulatedCrash`` into a :class:`_ProcessCrash` that ends the run.

    Restructured in T18: a ``SimulatedCrash`` (an ``Exception``) raised by an adapter is, by design, one failed
    trial (``EXECUTION_ERROR``), so the crash of the whole run is modelled as a ``BaseException``.
    """

    def __init__(self, inner: ScriptedExecutor) -> None:
        self.inner = inner
        self.usage_provenance = inner.usage_provenance

    def execute_case(self, harness: Harness, task: Task, context: RunContext) -> BehavioralReport:
        try:
            return self.inner.execute_case(harness, task, context)
        except SimulatedCrash as exc:
            raise _ProcessCrash(str(exc)) from exc


class A12Traceability(TempDirCase):
    def test_unbounded_solver_output_is_limited(self) -> None:
        """A12: capture_output=True has no size cap; a 20 MB stdout is held whole in the record."""
        record = _command_run(self.root, "import sys; sys.stdout.write('x' * 20_000_000)")
        self.assertLess(len(json.dumps(record.outputs)), 2_000_000)

    def test_results_carry_implementation_hash(self) -> None:
        """A12: adapter and workbench code are not hashed, so results cannot be tied to an implementation."""
        manifest = write_manifest(self.root / "m", tasks=[exact_task("t1")],
                                  variants=[{"id": "candidate", "system_prompt": "cand"}])
        payload = run_experiment(ExperimentSpec.load(manifest),
                                 ScriptedExecutor({"base": lambda t: True, "cand": lambda t: True}), self.root / "out")
        self.assertIn("implementation_hash", payload["provenance"])

    def test_trial_evidence_persisted_before_suite_end(self) -> None:
        """A12: results are persisted only at the end; a crash mid-suite loses every completed trial."""
        manifest = write_manifest(self.root / "m", tasks=[exact_task("t1"), exact_task("t2")],
                                  variants=[{"id": "candidate", "system_prompt": "cand"}])
        executor = _Crashing(ScriptedExecutor({"base": lambda t: True, "cand": lambda t: True}, crash_on_call=3))
        root = self.root / "out"
        with self.assertRaises(_ProcessCrash):
            run_experiment(ExperimentSpec.load(manifest), executor, root)
        self.assertTrue(any(p.suffix == ".sqlite3" for p in (root / "library").rglob("*")))
        db = WorkbenchDB.open(root)
        try:
            (experiment_id,) = db.incomplete_experiments()
            self.assertEqual(len(db.trials(experiment_id)), 2)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
