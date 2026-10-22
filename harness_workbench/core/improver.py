"""Propose–Probe–Compose.

One step probes the base harness and each candidate on the same task, then
keeps the edits that beat the base by a margin. That is the Harness²
contrastive step at ``k=1``. ``mode="sequential"`` repeats the step, carrying
the adopted harness forward. Nothing here trains a model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path

from harness_workbench.core.executor import BehavioralReport, Executor, MockExecutor, Task
from harness_workbench.core.harness import Harness
from harness_workbench.core.proposer import Candidate, Proposer, RuleBasedProposer, apply_edit

DEFAULT_MARGIN = 0.05


@dataclass
class StepRecord:
    """One Propose–Probe–Compose step, including the contrasts that were skipped."""

    base: Harness
    base_report: BehavioralReport
    candidates: list[Candidate]
    candidate_reports: list[tuple[Candidate, BehavioralReport]]
    adopted: Harness
    adopted_report: BehavioralReport
    lessons: list[str]
    composition: Harness | None = None
    composition_report: BehavioralReport | None = None

    @property
    def total_probe_tokens(self) -> int:
        reports = [self.base_report, *[report for _, report in self.candidate_reports]]
        if self.composition_report is not None:
            reports.append(self.composition_report)
        return sum(report.cost_tokens for report in reports)


@dataclass
class ImprovementResult:
    """Full improve() result. ``versions`` is the lineage touched by this call."""

    task: Task
    steps: list[StepRecord]
    lessons: list[str]
    versions: list[Harness]
    mode: str
    k: int
    margin: float
    run_dir: Path | None = None

    @property
    def base(self) -> Harness:
        return self.steps[0].base

    @property
    def adopted(self) -> Harness:
        return self.steps[-1].adopted

    @property
    def base_report(self) -> BehavioralReport:
        return self.steps[0].base_report

    @property
    def adopted_report(self) -> BehavioralReport:
        return self.steps[-1].adopted_report

    def to_dict(self) -> dict:
        return {
            "task": self.task.to_dict(),
            "mode": self.mode,
            "k": self.k,
            "margin": self.margin,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "base": {"id": self.base.id, "version": self.base.version, "score": self.base_report.score},
            "adopted": {
                "id": self.adopted.id,
                "version": self.adopted.version,
                "score": self.adopted_report.score,
                "parent_version": self.adopted.lineage.parent_version,
            },
            "lessons": list(self.lessons),
            "versions": [harness.version for harness in self.versions],
            "total_probe_tokens": sum(step.total_probe_tokens for step in self.steps),
            "cost_scope": "executor calls only; proposer usage is not included",
            "steps": [
                {
                    "base_version": step.base.version,
                    "base_score": step.base_report.score,
                    "adopted_version": step.adopted.version,
                    "adopted_score": step.adopted_report.score,
                    "composition_report": step.composition_report.to_dict() if step.composition_report else None,
                    "total_probe_tokens": step.total_probe_tokens,
                    "lessons": list(step.lessons),
                    "probes": [
                        {
                            "label": "base",
                            "score": step.base_report.score,
                            "failures": list(step.base_report.failures),
                        },
                        *[
                            {
                                "label": candidate.label,
                                "kind": candidate.edit.kind,
                                "edit_id": candidate.edit.id,
                                "score": report.score,
                                "failures": list(report.failures),
                                "rationale": candidate.edit.rationale,
                            }
                            for candidate, report in step.candidate_reports
                        ],
                    ],
                }
                for step in self.steps
            ],
        }


class Improver:
    """Runs Propose–Probe–Compose with a pluggable proposer and executor.

    The default pair is offline: :class:`RuleBasedProposer` and
    :class:`MockExecutor`. Pass a root path to persist the run and the
    improvement library.
    """

    def __init__(
        self,
        proposer: Proposer | None = None,
        executor: Executor | None = None,
        *,
        margin: float = DEFAULT_MARGIN,
    ) -> None:
        if not math.isfinite(margin) or margin < 0:
            raise ValueError("margin must be finite and >= 0")
        self.proposer = proposer or RuleBasedProposer()
        self.executor = executor or MockExecutor()
        self.margin = margin

    def improve(
        self,
        harness: Harness,
        task: Task,
        *,
        k: int = 1,
        mode: str = "parallel",
        root: Path | str | None = None,
    ) -> ImprovementResult:
        """Improve ``harness`` on ``task``.

        ``parallel`` probes ``k`` invariant and ``k`` domain edits against the
        same base, then composes the winners. ``sequential`` repeats a ``k=1``
        step up to ``k`` times, carrying evidence forward in the parent chain.
        """
        if k < 1:
            raise ValueError("k must be >= 1")
        if mode not in {"parallel", "sequential"}:
            raise ValueError("mode must be 'parallel' or 'sequential'")

        if mode == "sequential":
            result = self._sequential(harness, task, k=k)
        else:
            step = self._step(harness, task, k=k, mode="parallel")
            result = self._result_from_steps([step], task=task, mode="parallel", k=k)

        if root is not None:
            from harness_workbench.core.store import persist_result

            result.run_dir = persist_result(Path(root), result)
        return result

    def _sequential(self, harness: Harness, task: Task, *, k: int) -> ImprovementResult:
        current = harness
        steps: list[StepRecord] = []
        for _ in range(k):
            step = self._step(current, task, k=1, mode="sequential")
            steps.append(step)
            if step.adopted.version == current.version:
                break
            current = step.adopted
        return self._result_from_steps(steps, task=task, mode="sequential", k=k)

    def _result_from_steps(
        self,
        steps: list[StepRecord],
        *,
        task: Task,
        mode: str,
        k: int,
    ) -> ImprovementResult:
        lessons = [lesson for step in steps for lesson in step.lessons]
        versions: list[Harness] = [steps[0].base]
        for step in steps:
            if step.adopted.version != versions[-1].version:
                versions.append(step.adopted)
        return ImprovementResult(
            task=task,
            steps=steps,
            lessons=lessons,
            versions=versions,
            mode=mode,
            k=k,
            margin=self.margin,
        )

    def _step(self, harness: Harness, task: Task, *, k: int, mode: str) -> StepRecord:
        base_report = self.executor.execute(harness.copy(), task)
        if not _valid_score(base_report):
            raise ValueError("baseline returned an invalid score; no comparison is possible")
        candidates = self.proposer.propose(task, harness.copy(), base_report, k=k)
        probed = []
        for candidate in candidates:
            report = self.executor.execute(candidate.harness.copy(), task)
            if not _valid_score(report):
                report = replace(report, score=0.0, failures=[*report.failures, "invalid_score"])
            probed.append((candidate, report))
        composed, lessons = self._compose(harness, base_report, probed)
        composition_report = None
        adopted, adopted_report = harness, base_report
        if composed is not harness:
            composition_report = self.executor.execute(composed.copy(), task)
            if not _valid_score(composition_report):
                composition_report = replace(
                    composition_report, score=0.0,
                    failures=[*composition_report.failures, "invalid_score"],
                )
            delta = round(composition_report.score - base_report.score, 4)
            if "invalid_score" not in composition_report.failures and delta > self.margin:
                adopted, adopted_report = composed, composition_report
                lessons = [line.replace("SELECT ", "ADOPT ", 1) for line in lessons]
                lessons.append(f"Composition validated: delta {delta:+.3f} > margin {self.margin:.3f}.")
                adopted.lineage.metrics = _metrics(base_report, adopted_report, probed, k=k, mode=mode)
                adopted.lineage.lessons = list(lessons)
            else:
                lessons = [line.replace("SELECT ", "REJECT ", 1) for line in lessons]
                lessons.append(
                    f"REJECT composition: delta {delta:+.3f} did not clear margin "
                    f"{self.margin:.3f}; base harness kept."
                )
        return StepRecord(
            base=harness, base_report=base_report, candidates=candidates,
            candidate_reports=probed, adopted=adopted, adopted_report=adopted_report,
            lessons=lessons, composition=composed if composed is not harness else None,
            composition_report=composition_report,
        )

    def _compose(
        self,
        base: Harness,
        base_report: BehavioralReport,
        probed: list[tuple[Candidate, BehavioralReport]],
    ) -> tuple[Harness, list[str]]:
        ranked: list[tuple[Candidate, BehavioralReport, float]] = []
        lessons: list[str] = []
        for candidate, report in probed:
            delta = round(report.score - base_report.score, 4)
            if "invalid_score" not in report.failures and delta > self.margin:
                ranked.append((candidate, report, delta))
                lessons.append(
                    "SELECT "
                    f"{candidate.edit.kind}/{candidate.edit.component} [{candidate.edit.id}]: "
                    f"{candidate.edit.rationale} "
                    f"(score {base_report.score:.3f} → {report.score:.3f}, delta {delta:+.3f})."
                )
            else:
                lessons.append(
                    "SKIP "
                    f"{candidate.edit.kind}/{candidate.edit.component} [{candidate.edit.id}]: "
                    f"{candidate.edit.rationale} "
                    f"(score {base_report.score:.3f} → {report.score:.3f}, delta {delta:+.3f}; "
                    f"margin {self.margin:.3f})."
                )
        if not ranked:
            lessons.append("No candidate cleared the contrast margin; base harness kept.")
            return base, lessons

        # Apply the strongest contrast last so it wins a same-field collision.
        ranked.sort(key=lambda item: item[2])
        adopted = base.fork(source="compose")
        for candidate, _report, _delta in ranked:
            adopted = apply_edit(adopted, candidate.edit)
        return adopted, lessons


def _metrics(
    base_report: BehavioralReport,
    adopted_report: BehavioralReport,
    probed: list[tuple[Candidate, BehavioralReport]],
    *,
    k: int,
    mode: str,
) -> dict:
    return {
        "score": adopted_report.score,
        "base_score": base_report.score,
        "delta": round(adopted_report.score - base_report.score, 4),
        "k": k,
        "mode": mode,
        "cost_tokens": adopted_report.cost_tokens,
        "total_probe_tokens": base_report.cost_tokens + adopted_report.cost_tokens
            + sum(report.cost_tokens for _, report in probed),
        "cost_scope": "executor calls only; proposer usage is not included",
        "probes": [
            {"label": "base", "score": base_report.score, "failures": list(base_report.failures)},
            *[
                {"label": candidate.label, "score": report.score, "failures": list(report.failures)}
                for candidate, report in probed
            ],
        ],
    }


def _valid_score(report: BehavioralReport) -> bool:
    return isinstance(report.score, (int, float)) and not isinstance(report.score, bool) \
        and math.isfinite(report.score) and 0.0 <= report.score <= 1.0
