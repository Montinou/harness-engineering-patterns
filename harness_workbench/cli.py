"""Command line for the Harness Workbench.

Commands: propose, probe, improve, experiment, migrate-manifest, export-library, migrate-library, report.
The default path is offline (rule proposer, mock executor).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from harness_workbench import __version__
from harness_workbench.core.anthropic_backend import (
    IMPROVER_MODEL,
    IMPROVER_SYSTEM,
    SOLVER_MODEL,
    AnthropicClient,
    AnthropicError,
    AnthropicHarnessExecutor,
)
from harness_workbench.core.executor import Executor, MockExecutor, Task
from harness_workbench.core.harness import Harness, load_harness
from harness_workbench.core.improver import DEFAULT_MARGIN, GateVerdict, Improver
from harness_workbench.core.proposer import FileProposer, LLMProposer, Proposer, ProposerError, RuleBasedProposer
from harness_workbench.core.db import LineageConflict, PromotionConflict, SchemaMismatch, StoreBusyError
from harness_workbench.experiments.backends import BackendUnavailable, require_backend
from harness_workbench.core.store import (
    ExportError,
    MigrationRequired,
    export_library,
    known_harness_ids,
    lineage_chain,
    load_versions,
)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run one command. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (
        FileNotFoundError, ValueError, ProposerError, AnthropicError, OSError,
        MigrationRequired, ExportError, LineageConflict, SchemaMismatch, StoreBusyError,
        BackendUnavailable, PromotionConflict,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness-workbench",
        description="Propose, probe, compose and persist agent harnesses.",
    )
    parser.add_argument("--version", action="version", version=f"harness-workbench {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    propose = sub.add_parser("propose", help="generate invariant and domain candidate edits")
    _add_task_args(propose)
    _add_provider_args(propose)
    propose.add_argument("--k", type=int, default=1, help="candidates of each kind (default 1)")
    propose.add_argument("--edits-file", type=Path, help="JSON edits from an external model, instead of the proposer")
    propose.add_argument("--out", type=Path, help="write the candidate JSON here")
    propose.add_argument("--json", action="store_true", help="print machine-readable JSON")
    propose.set_defaults(func=_cmd_propose)

    probe = sub.add_parser("probe", help="execute a harness on a task")
    _add_task_args(probe)
    _add_provider_args(probe)
    probe.add_argument("--json", action="store_true")
    probe.set_defaults(func=_cmd_probe)

    improve = sub.add_parser("improve", help="run Propose–Probe–Compose and save the result")
    _add_task_args(improve)
    _add_provider_args(improve)
    improve.add_argument("--k", type=int, default=1)
    improve.add_argument("--mode", choices=("parallel", "sequential"), default="parallel")
    improve.add_argument("--margin", type=float, default=DEFAULT_MARGIN, help="minimum score delta to adopt an edit")
    improve.add_argument("--root", type=Path, default=Path("."), help="where runs/ and library/ are written")
    improve.add_argument("--edits-file", type=Path, help="JSON edits from an external model, instead of the proposer")
    improve.add_argument("--validation-spec", type=Path, help="require independent repeated acceptance checks before adoption")
    improve.add_argument("--json", action="store_true")
    improve.set_defaults(func=_cmd_improve)

    experiment = sub.add_parser("experiment", help="run matched, repeated harness comparisons with acceptance checks")
    experiment.add_argument("--spec", type=Path, help="experiment manifest JSON (required unless --list-incomplete)")
    experiment.add_argument("--list-incomplete", action="store_true",
                            help="list experiments left running by an interrupted run and exit")
    experiment.add_argument("--root", type=Path, default=Path("."), help="where experiments/ is written")
    _add_provider_args(experiment)
    experiment.add_argument("--json", action="store_true")
    experiment.set_defaults(func=_cmd_experiment)

    migrate = sub.add_parser("migrate-manifest", help="convert a schema v1 experiment manifest to schema v2")
    migrate.add_argument("--spec", type=Path, required=True, help="schema v1 experiment manifest JSON")
    migrate.add_argument("--out", type=Path, help="where to write the schema v2 manifest")
    migrate.add_argument("--splits", type=Path, help="JSON object mapping task id to development|validation|regression")
    migrate.add_argument("--dry-run", action="store_true", help="print the result and write nothing")
    migrate.set_defaults(func=_cmd_migrate_manifest)

    export = sub.add_parser("export-library", help="regenerate the JSON library views from the database")
    export.add_argument("--root", type=Path, default=Path("."), help="where library/ lives")
    export.add_argument("--json", action="store_true", help="print the regenerated paths as a JSON list")
    export.set_defaults(func=_cmd_export_library)

    migrate_lib = sub.add_parser(
        "migrate-library", help="import legacy library JSON versions into the database (idempotent, backs up first)"
    )
    migrate_lib.add_argument("--root", type=Path, default=Path("."), help="where library/ lives")
    migrate_lib.add_argument("--dry-run", action="store_true", help="report what would be imported and write nothing")
    migrate_lib.add_argument("--json", action="store_true", help="print the report as JSON")
    migrate_lib.set_defaults(func=_cmd_migrate_library)

    report = sub.add_parser("report", help="show saved lineage, metrics and lessons")
    report.add_argument("--root", type=Path, default=Path("."))
    report.add_argument("--harness-id", help="harness id to inspect; omit to list saved harnesses")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=_cmd_report)
    return parser


def _add_task_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--harness", required=True, type=Path, help="path to a harness JSON file")
    parser.add_argument("--task", required=True, help="task description")
    parser.add_argument("--domain", default="general", help="task domain (research, evidence, code, qa, general)")
    parser.add_argument("--task-id", help="stable task id; derived from the description if omitted")
    parser.add_argument("--constraint", action="append", default=[], help="extra constraint; repeatable")


def _task(args: argparse.Namespace) -> Task:
    return Task.from_text(
        args.task,
        domain=args.domain,
        task_id=args.task_id,
        constraints=list(args.constraint or []),
    )


def _add_provider_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=("mock", "anthropic", "command"),
        default="mock",
        help="mock is offline; anthropic uses Sonnet to solve and Haiku to improve",
    )
    parser.add_argument("--solver-model", default="", help=f"solver model (default {SOLVER_MODEL})")
    parser.add_argument("--improver-model", default="", help=f"improver and judge model (default {IMPROVER_MODEL})")
    parser.add_argument("--max-tokens", type=int, default=4096, help="max tokens for the solver answer")


def _named_model(flag: str, env_name: str, default: str) -> str:
    return flag or os.environ.get(env_name, "") or default


def _backend(args: argparse.Namespace) -> tuple[Proposer, Executor]:
    """Return the proposer and executor for this command.

    ``mock`` never touches the network. ``anthropic`` reads ``ANTHROPIC_API_KEY``.
    """
    provider = getattr(args, "provider", "mock")
    edits = getattr(args, "edits_file", None)
    if provider == "mock":
        proposer: Proposer
        if edits:
            proposer = FileProposer(edits.read_text(encoding="utf-8"))
        else:
            proposer = RuleBasedProposer()
        return proposer, MockExecutor()
    if provider != "anthropic":
        raise ValueError(f"unknown provider: {provider}")

    client = AnthropicClient.from_env()
    solver = _named_model(args.solver_model, "HARNESS_SOLVER_MODEL", SOLVER_MODEL)
    improver_model = _named_model(args.improver_model, "HARNESS_IMPROVER_MODEL", IMPROVER_MODEL)
    executor = AnthropicHarnessExecutor(
        client,
        solver_model=solver,
        judge_model=improver_model,
        max_tokens=args.max_tokens,
    )
    if edits:
        proposer = FileProposer(edits.read_text(encoding="utf-8"))
    else:
        def complete(prompt: str) -> str:
            result = client.complete(
                model=improver_model,
                system=IMPROVER_SYSTEM,
                user=prompt,
                max_tokens=4096,
            )
            return result.text

        proposer = LLMProposer(complete)
    if not getattr(args, "json", False):
        print(f"provider anthropic  solver={solver}  improver={improver_model}")
    return proposer, executor


def _cmd_propose(args: argparse.Namespace) -> int:
    harness = load_harness(args.harness)
    task = _task(args)
    proposer, executor = _backend(args)
    report = executor.execute(harness, task)
    candidates = proposer.propose(task, harness, report, k=args.k)
    payload = {
        "harness_id": harness.id,
        "version": harness.version,
        "base_score": report.score,
        "failures": report.failures,
        "candidates": [candidate.to_dict() for candidate in candidates],
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(f"base {harness.id} v{harness.version}  score={report.score:.3f}  failures={report.failures or '[]'}")
    if not candidates:
        print("no candidates")
        return 0
    for candidate in candidates:
        print(f"\n[{candidate.edit.kind}] {candidate.edit.id}  {candidate.label}")
        print(f"  {candidate.edit.rationale}")
        print(_indent(candidate.edit.diff, "  "))
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    harness = load_harness(args.harness)
    task = _task(args)
    _proposer, executor = _backend(args)
    report = executor.execute(harness, task)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0
    print(f"{harness.id} v{harness.version}  score={report.score:.3f}  tokens≈{report.cost_tokens}  latency={report.latency_ms}ms")
    print(f"failures: {report.failures or '[]'}")
    for step in report.traces:
        mark = "ok" if step.ok else "FAIL"
        print(f"  [{mark}] {step.kind}: {step.detail}")
    return 0


def _cmd_improve(args: argparse.Namespace) -> int:
    from dataclasses import replace
    from harness_workbench.experiments import ExperimentSpec, run_experiment
    from harness_workbench.experiments.manifest import Variant
    from harness_workbench.experiments.status import DecisionStatus

    harness = load_harness(args.harness)
    task = _task(args)
    validation_spec = ExperimentSpec.load(args.validation_spec) if args.validation_spec else None
    if validation_spec is not None and validation_spec.mode != "confirmatory":
        raise ValueError(
            "validation spec is exploratory (schema v1 or mode exploratory); run 'harness-workbench "
            "migrate-manifest' and assign splits and controls")
    # An execution backend the spec asks for must exist before anything runs or is paid for (exit 2).
    validation_backend = require_backend(validation_spec.environment) if validation_spec is not None else None
    proposer, executor = _backend(args)
    gate = None
    validation_evidence: list[dict] = []
    if validation_spec is not None:
        if validation_spec.executor.type == "command":
            from harness_workbench.experiments.command import CommandHarnessExecutor
            validation_executor = CommandHarnessExecutor.from_config(validation_spec.executor, validation_spec.environment)
        else:
            validation_executor = executor

        def gate(before: Harness, candidate: Harness) -> GateVerdict:
            def variant(identity: str, value: Harness) -> Variant:
                fingerprint = hashlib.sha256(value.to_json().encode("utf-8")).hexdigest()
                return Variant(identity, value, Path("(dynamically-composed)"), fingerprint,
                               value.execution_config_hash())

            controlled = replace(validation_spec, variants=(
                variant("baseline", before), variant("candidate", candidate),
            ))
            outcome = run_experiment(controlled, validation_executor, args.root, backend=validation_backend)
            evidence = {
                "report": outcome["report_path"], "results": outcome["results_path"],
                "decision": outcome["decision"],
            }
            validation_evidence.append(evidence)
            # Only the persisted decision approves; the per-variant contrasts are diagnostics.
            return GateVerdict(outcome["decision"]["status"] == DecisionStatus.ELIGIBLE.value,
                               outcome["decision_id"], evidence)

    improver = Improver(proposer, executor, margin=args.margin, promotion_gate=gate)
    result = improver.improve(harness, task, k=args.k, mode=args.mode, root=args.root)
    if args.json:
        print(json.dumps(result.to_dict() | {"validation_evidence": validation_evidence}, indent=2, ensure_ascii=False))
        return 0
    for index, step in enumerate(result.steps, start=1):
        print(
            f"step {index}  base v{step.base.version} score={step.base_report.score:.3f}  "
            f"failures={step.base_report.failures or '[]'}"
        )
        for candidate, report in step.candidate_reports:
            delta = report.score - step.base_report.score
            print(f"  probe {candidate.label:28} {candidate.edit.id:22} score={report.score:.3f}  delta={delta:+.3f}")
        print(
            f"  adopted v{step.adopted.version} score={step.adopted_report.score:.3f}  "
            f"parent={step.adopted.lineage.parent_version or '—'}"
        )
    print("lessons:")
    for lesson in result.lessons:
        print(f"  - {lesson}")
    if result.run_dir is not None:
        print(f"saved {result.run_dir}")
    for evidence in validation_evidence:
        print(f"validation {evidence['report']}  decision={evidence['decision']['status']}")
    return 0


def _number(value: object, spec: str) -> str:
    return "unknown" if value is None else format(value, spec)


def _variant_line(name: str, stats: dict) -> str:
    pass_rate = "unknown" if stats["pass_rate"] is None else f"{stats['pass_rate']:.0%}"
    return (f"  {name:20} quality={_number(stats['mean_score'], '.3f')} pass={pass_rate} "
            f"tokens={_number(stats['mean_tokens'], '.0f')}")


def _cmd_experiment(args: argparse.Namespace) -> int:
    from harness_workbench.experiments import ExperimentSpec, run_experiment

    if args.list_incomplete:
        from harness_workbench.core.db import WorkbenchDB

        db = WorkbenchDB.open(args.root.resolve())
        try:
            incomplete = db.incomplete_experiments()
        finally:
            db.close()
        if args.json:
            print(json.dumps(incomplete, indent=2))
        else:
            for experiment_id in incomplete:
                print(experiment_id)
        return 0
    if args.spec is None:
        raise ValueError("--spec is required unless --list-incomplete is given")
    spec = ExperimentSpec.load(args.spec)
    if args.provider == "command":
        if spec.executor.type != "command":
            raise ValueError("--provider command requires executor.type 'command' in the manifest")
        from harness_workbench.experiments.command import CommandHarnessExecutor
        executor = CommandHarnessExecutor.from_config(spec.executor, spec.environment)
    else:
        _proposer, executor = _backend(args)
    result = run_experiment(spec, executor, args.root)
    if args.json:
        print(json.dumps({
            "experiment_id": result["experiment_id"],
            "mode": result["mode"],
            "decision": result["decision"],
            "decision_id": result["decision_id"],
            "summary": result["summary"],
            "report_path": result["report_path"],
            "results_path": result["results_path"],
        }, indent=2))
    else:
        _print_experiment(result, spec.name)
    return 0


def _statistic(value: object, spec: str = "+.4f") -> str:
    return "unavailable" if value is None else format(value, spec)


def _print_experiment(result: dict, name: str) -> None:
    """Text output: the decision status first, with what it rests on and what the data is."""
    from harness_workbench.experiments.report import EXPLORATORY_NOTICE, SYNTHETIC_LABEL, is_exploratory, is_synthetic

    decision = result["decision"]
    diagnostics = decision.get("diagnostics") or {}
    print(f"experiment {result['experiment_id']}  {name}")
    if is_synthetic(result):
        print(SYNTHETIC_LABEL)
    print(f"decision {decision['status']}")
    for reason in decision["reasons"]:
        print(f"  {reason}")
    print(f"estimate {_statistic(decision.get('estimate'))}  lower_bound {_statistic(decision.get('lower_bound'))}  "
          f"n_units {decision.get('n_units', 0)}  "
          f"n_regression_units {diagnostics.get('n_regression_units', 'unavailable')}  "
          f"method {diagnostics.get('method', 'unavailable')}")
    print(f"mode {result['mode']}")
    if is_exploratory(result):
        print(EXPLORATORY_NOTICE)
    for variant, stats in result["summary"]["variants"].items():
        print(_variant_line(variant, stats))
    print(f"report {result['report_path']}")
    print(f"data   {result['results_path']}")


def _rebase_paths(data: dict, source_dir: Path, target_dir: Path) -> None:
    """Rewrite the manifest's relative paths so they still resolve from ``target_dir``."""
    def rebased(value: str) -> str:
        return os.path.relpath((source_dir / value).resolve(), target_dir.resolve())

    if isinstance(data.get("baseline"), str):
        data["baseline"] = rebased(data["baseline"])
    for variant in data.get("variants") or []:
        if isinstance(variant, dict) and isinstance(variant.get("harness"), str):
            variant["harness"] = rebased(variant["harness"])
    for task in data.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        for key in ("fixture", "grader_files"):
            if isinstance(task.get(key), str) and task[key]:
                task[key] = rebased(task[key])


def _cmd_migrate_manifest(args: argparse.Namespace) -> int:
    from harness_workbench.experiments.manifest import ManifestError, migrate_manifest
    from harness_workbench.experiments.validation import json_dumps_strict, json_loads_strict

    if not args.dry_run and args.out is None:
        raise ValueError("--out is required unless --dry-run is given")
    spec_path = args.spec.resolve()
    if args.out is not None and args.out.resolve() == spec_path:
        raise ValueError("refusing to overwrite the input manifest; choose a different --out")
    try:
        data = json_loads_strict(spec_path.read_text(encoding="utf-8"))
        splits = None
        if args.splits is not None:
            splits = json_loads_strict(args.splits.read_text(encoding="utf-8"))
            if not isinstance(splits, dict):
                raise ManifestError("--splits must be a JSON object mapping task id to split")
        migrated = migrate_manifest(data, splits=splits)
    except ManifestError:
        raise
    except ValueError as exc:  # includes ValidationError and JSON decode errors
        raise ManifestError(f"{args.spec}: {exc}") from exc
    target_dir = (args.out.resolve().parent if args.out is not None else spec_path.parent)
    _rebase_paths(migrated, spec_path.parent, target_dir)
    for note in migrated["_migration_notes"]:
        print(f"warning: {note}", file=sys.stderr)
    text = json_dumps_strict(migrated, indent=2, ensure_ascii=False) + "\n"
    if args.dry_run:
        print(text, end="")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


def _cmd_export_library(args: argparse.Namespace) -> int:
    paths = export_library(args.root)
    if args.json:
        print(json.dumps([str(path) for path in paths], indent=2))
    else:
        for path in paths:
            print(path)
    return 0


def _cmd_migrate_library(args: argparse.Namespace) -> int:
    from harness_workbench.core.migrate import migrate_library

    report = migrate_library(args.root, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        verb = "would import" if report.dry_run else "imported"
        for harness_id, version in report.imported:
            print(f"{verb} {harness_id} v{version}")
        for harness_id, version in report.skipped:
            print(f"skipped {harness_id} v{version} (already in the database)")
        for conflict in report.conflicts:
            print(f"conflict {conflict}")
        for backup in report.backups:
            print(f"backup {backup}")
        print(f"{verb} {len(report.imported)}, skipped {len(report.skipped)}, conflicts {len(report.conflicts)}")
    return 1 if report.conflicts else 0


def _cmd_report(args: argparse.Namespace) -> int:
    root = args.root
    if args.harness_id:
        versions = load_versions(root, args.harness_id)
        if not versions:
            raise FileNotFoundError(f"no saved harness '{args.harness_id}' under {root / 'library' / 'harnesses'}")
        chain = lineage_chain(versions)
        if args.json:
            print(json.dumps([_version_view(item) for item in chain], indent=2, ensure_ascii=False))
            return 0
        print(f"{args.harness_id}  {len(chain)} version(s)")
        for item in chain:
            metrics = item.lineage.metrics or {}
            score = metrics.get("score")
            score_text = f"{score:.3f}" if isinstance(score, (int, float)) else "—"
            delta = metrics.get("delta")
            delta_text = f"{delta:+.3f}" if isinstance(delta, (int, float)) else "—"
            parent = item.lineage.parent_version or "—"
            print(f"  v{item.version:6}  {item.lineage.source:8}  parent={parent:6}  score={score_text:6}  delta={delta_text}")
            for lesson in item.lineage.lessons:
                print(f"      {lesson}")
        return 0

    ids = known_harness_ids(root)
    if args.json:
        print(json.dumps({"harnesses": ids}, indent=2))
        return 0
    if not ids:
        print(f"no harnesses saved under {root / 'library'}")
        return 0
    print("saved harnesses:")
    for harness_id in ids:
        versions = load_versions(root, harness_id)
        latest = lineage_chain(versions)[-1]
        score = latest.lineage.metrics.get("score") if latest.lineage.metrics else None
        score_text = f"{score:.3f}" if isinstance(score, (int, float)) else "—"
        print(f"  {harness_id}  versions={len(versions)}  latest=v{latest.version}  score={score_text}")
    return 0


def _version_view(harness: Harness) -> dict:
    return {
        "id": harness.id,
        "version": harness.version,
        "parent_version": harness.lineage.parent_version,
        "source": harness.lineage.source,
        "metrics": harness.lineage.metrics,
        "lessons": list(harness.lineage.lessons),
        "tools": [tool.name for tool in harness.tools],
        "guardrails": list(harness.guardrails),
        "skills": [skill.name for skill in harness.skills],
    }


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())
