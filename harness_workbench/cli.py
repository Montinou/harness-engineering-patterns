"""Command line for the Harness Workbench.

Commands: propose, probe, improve, report.
The default path is offline (rule proposer, mock executor).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness_workbench import __version__
from harness_workbench.core.executor import MockExecutor, Task
from harness_workbench.core.harness import Harness, load_harness
from harness_workbench.core.improver import DEFAULT_MARGIN, Improver
from harness_workbench.core.proposer import FileProposer, ProposerError, RuleBasedProposer
from harness_workbench.core.store import known_harness_ids, lineage_chain, load_versions


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run one command. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError, ProposerError, OSError) as exc:
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
    propose.add_argument("--k", type=int, default=1, help="candidates of each kind (default 1)")
    propose.add_argument("--edits-file", type=Path, help="JSON edits from an external model, instead of the rule proposer")
    propose.add_argument("--out", type=Path, help="write the candidate JSON here")
    propose.add_argument("--json", action="store_true", help="print machine-readable JSON")
    propose.set_defaults(func=_cmd_propose)

    probe = sub.add_parser("probe", help="execute a harness on a task with the mock executor")
    _add_task_args(probe)
    probe.add_argument("--json", action="store_true")
    probe.set_defaults(func=_cmd_probe)

    improve = sub.add_parser("improve", help="run Propose–Probe–Compose and save the result")
    _add_task_args(improve)
    improve.add_argument("--k", type=int, default=1)
    improve.add_argument("--mode", choices=("parallel", "sequential"), default="parallel")
    improve.add_argument("--margin", type=float, default=DEFAULT_MARGIN, help="minimum score delta to adopt an edit")
    improve.add_argument("--root", type=Path, default=Path("."), help="where runs/ and library/ are written")
    improve.add_argument("--edits-file", type=Path, help="JSON edits from an external model, instead of the rule proposer")
    improve.add_argument("--json", action="store_true")
    improve.set_defaults(func=_cmd_improve)

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


def _proposer(args: argparse.Namespace):
    if getattr(args, "edits_file", None):
        return FileProposer(args.edits_file.read_text(encoding="utf-8"))
    return RuleBasedProposer()


def _cmd_propose(args: argparse.Namespace) -> int:
    harness = load_harness(args.harness)
    task = _task(args)
    report = MockExecutor().execute(harness, task)
    candidates = _proposer(args).propose(task, harness, report, k=args.k)
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
    report = MockExecutor().execute(harness, task)
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
    harness = load_harness(args.harness)
    task = _task(args)
    improver = Improver(_proposer(args), MockExecutor(), margin=args.margin)
    result = improver.improve(harness, task, k=args.k, mode=args.mode, root=args.root)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
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
    return 0


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
