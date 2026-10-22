"""Bridge existing proposers to an experiment without exposing held-out cases."""
from __future__ import annotations

import copy
from dataclasses import asdict

from harness_workbench.core.proposer import Proposer, RuleBasedProposer, apply_edit
from harness_workbench.experiments.models import ExperimentSpec, Variant, digest


def propose_development(spec: ExperimentSpec, proposer: Proposer | None = None, *, k: int = 1,
                        reports: dict | None = None, compose: bool = True) -> ExperimentSpec:
    """Return frozen hypotheses. This does not promote or claim improvement.

    Custom proposer instances receive only a development Task, copied baseline,
    and that development task's report. Reference answers/checks are not passed.
    The caller remains responsible for external proposer usage and credentials.
    """
    result = copy.deepcopy(spec)
    proposer = proposer or RuleBasedProposer()
    edits, variants, seen = [], [], set()
    for case in result.cases:
        if case.split != "development":
            continue
        candidates = proposer.propose(copy.deepcopy(case.task), result.baseline.copy(),
                                      copy.deepcopy((reports or {}).get(case.task.id)), k=k)
        for candidate in candidates:
            fingerprint = digest({"op": candidate.edit.op, "payload": candidate.edit.payload})
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            edits.append(candidate.edit)
            variants.append(Variant("edit-" + fingerprint[:16], candidate.harness.copy()))
    if compose and len(edits) > 1:
        combined = result.baseline.copy()
        for edit in edits:
            combined = apply_edit(combined, edit)
        variants.append(Variant("composition-" + digest([e.to_dict() for e in edits])[:16], combined))
    if not variants:
        raise ValueError("proposer produced no new development hypotheses")
    result.variants = variants
    result.provenance = {**result.provenance, "proposal_scope": "development_only",
                         "proposal_edits": [edit.to_dict() for edit in edits]}
    result.validate()
    return result


def config_dict(spec: ExperimentSpec) -> dict:
    """Portable, self-contained config accepted by load_spec()."""
    backend = copy.deepcopy(spec.backend)
    backend["files"] = list(backend.get("files", {}))
    return {"name": spec.name, "baseline": spec.baseline.to_dict(),
            "variants": [{"label": v.label, "harness": v.harness.to_dict()} for v in spec.variants],
            "cases": [c.to_dict() for c in spec.cases], "policy": asdict(spec.policy),
            "backend": backend, "provenance": spec.provenance}
