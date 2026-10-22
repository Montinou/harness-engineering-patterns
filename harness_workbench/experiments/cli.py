"""CLI: run, propose, report, list, export. Network and commands are opt-in."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from harness_workbench.experiments.models import load_spec
from harness_workbench.experiments.proposals import config_dict, propose_development
from harness_workbench.experiments.reporting import write_reports
from harness_workbench.experiments.runner import ExperimentRunner
from harness_workbench.experiments.store import ExperimentStore


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="harness-experiments")
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run independent trials, validate a finalist, record a decision")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--allow-commands", action="store_true", help="execute trusted local solver/grader commands; NOT a sandbox")
    run.add_argument("--allow-network", action="store_true", help="allow the configured Anthropic backend to incur API charges")
    run.add_argument("--require-promotion", action="store_true", help="exit 1 when gates keep the baseline")
    run.add_argument("--json", action="store_true")
    propose = sub.add_parser("propose", help="generate rule-based hypotheses from development tasks only")
    propose.add_argument("--config", required=True, type=Path)
    propose.add_argument("--out", required=True, type=Path)
    propose.add_argument("--k", type=int, default=1)
    report = sub.add_parser("report", help="export JSON, Markdown and a standalone HTML dashboard")
    report.add_argument("--experiment-id", required=True)
    report.add_argument("--out", type=Path)
    sub.add_parser("list", help="list persisted experiment IDs")
    export = sub.add_parser("export", help="export an accepted harness without overwriting an existing file")
    export.add_argument("--experiment-id", required=True)
    export.add_argument("--out", required=True, type=Path)
    for command in (run, report, sub.choices["list"], export):
        command.add_argument("--root", type=Path, default=Path(".harness-experiments"))
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "propose":
            result = propose_development(load_spec(args.config), k=args.k)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("x", encoding="utf-8") as stream:
                json.dump(config_dict(result), stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
            print(args.out)
            return 0
        store = ExperimentStore(args.root)
        if args.command == "list":
            print(json.dumps(store.list(), indent=2))
            return 0
        if args.command == "report":
            print(json.dumps(write_reports(store, args.experiment_id, args.out or args.root / "reports"), indent=2))
            return 0
        if args.command == "export":
            info = store.inspect(args.experiment_id)
            if not info["promoted_object"]:
                raise ValueError("experiment has no accepted harness to export")
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("x", encoding="utf-8") as stream:
                json.dump(store.object(info["promoted_object"]), stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
            print(args.out)
            return 0
        spec = load_spec(args.config)
        if spec.backend.get("kind") == "anthropic" and not args.allow_network:
            raise ValueError("Anthropic experiments require --allow-network and may incur charges")
        result = ExperimentRunner(store, allow_commands=args.allow_commands).run(spec)
        reports = write_reports(store, result["experiment_id"], args.root / "reports")
        if args.json:
            print(json.dumps({**result, "reports": reports}, indent=2, ensure_ascii=False, allow_nan=False))
        else:
            print(f"{result['experiment_id']}: {'PROMOTE' if result['promote'] else 'KEEP BASELINE'}")
            print(f"runs={result['total_runs']} tokens={result['total_tokens']} nominee={result['nominee']}")
            for reason in result["reasons"]:
                print(f"  {reason}")
            print(f"report: {reports['html']}")
        return 1 if args.require_promotion and not result["promote"] else 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
