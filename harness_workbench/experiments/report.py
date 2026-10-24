"""Standalone, offline HTML report for experiment evidence."""
from __future__ import annotations

import json
from html import escape
from typing import Any

from harness_workbench.experiments.status import DecisionStatus

# Shared with the CLI so a terminal and the HTML report say the same thing.
EXPLORATORY_NOTICE = "Exploratory only: not promotion evidence"
SYNTHETIC_LABEL = "Synthetic data"
RESULTS_SCHEMA_VERSION = 2  # the layout of results.json; the manifest has its own schema_version


def _h(value: Any) -> str:
    return escape(str(value), quote=True)


def is_synthetic(payload: dict[str, Any]) -> bool:
    """True when the numbers came from the deterministic mock or from a model declared synthetic."""
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return False
    return (provenance.get("executor") == "MockExecutor"
            or str(provenance.get("model_requested", "")).startswith("synthetic"))


def is_exploratory(payload: dict[str, Any]) -> bool:
    """True for an exploratory run or an exploratory-only decision: neither is promotion evidence."""
    decision = payload.get("decision")
    status = decision.get("status") if isinstance(decision, dict) else None
    return payload.get("mode") == "exploratory" or status == DecisionStatus.EXPLORATORY_ONLY.value


def _number(value: Any, spec: str) -> str:
    """Format a number, or an em dash when the measurement does not exist."""
    return "—" if value is None else format(value, spec)


def _percent(value: Any) -> str:
    return "—" if value is None else f"{value:.1%}"


def _measurement(measurement: Any) -> str:
    """``value (provenance)`` for a recorded measurement; the dash keeps its provenance visible."""
    if not isinstance(measurement, dict):
        return "—"
    value = measurement.get("value")
    text = "—" if value is None else (str(value) if isinstance(value, int) else format(value, ".1f"))
    return f"{text} ({measurement.get('provenance', 'unknown')})"


def _statistic(value: Any) -> str:
    return "—" if value is None else (f"{value:.4f}" if isinstance(value, float) else str(value))


def _rows(pairs: list[tuple[str, str]]) -> str:
    return "<table class='facts'><tbody>" + "".join(
        f"<tr><th>{_h(label)}</th><td>{_h(value)}</td></tr>" for label, value in pairs) + "</tbody></table>"


def _decision_banner(payload: dict[str, Any]) -> str:
    decision = payload.get("decision") or {}
    status = str(decision.get("status", "unknown"))
    diagnostics = decision.get("diagnostics") or {}
    reasons = decision.get("reasons") or []
    pending = decision.get("pending_requirements") or []
    parts = [f"<section class='decision decision-{_h(status)}'><h2>Decision: {_h(status)}</h2>"]
    if is_exploratory(payload):
        parts.append(f"<p class='notice'><strong>{_h(EXPLORATORY_NOTICE)}</strong></p>")
    parts.append(_rows([
        ("candidate", decision.get("candidate_id") or "—"),
        ("estimate", _statistic(decision.get("estimate"))),
        ("lower_bound", _statistic(decision.get("lower_bound"))),
        ("n_units", _statistic(decision.get("n_units"))),
        ("n_regression_units", _statistic(diagnostics.get("n_regression_units"))),
        ("method", diagnostics.get("method") or "—"),
        ("mode", payload.get("mode") or "—"),
    ]))
    parts.append("<h3>Reasons</h3>" + (
        "<ul>" + "".join(f"<li>{_h(reason)}</li>" for reason in reasons) + "</ul>"
        if reasons else "<p class='muted'>No reasons recorded.</p>"))
    if pending:
        parts.append("<h3>Pending requirements</h3><ul>"
                     + "".join(f"<li>{_h(item)}</li>" for item in pending) + "</ul>")
    parts.append("</section>")
    return "".join(parts)


def _provenance_block(payload: dict[str, Any]) -> str:
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return ""
    keys = ("executor", "model_requested", "commit", "dirty", "implementation_hash", "python", "platform")
    pairs = [(key, str(provenance[key])) for key in keys if key in provenance]
    return "<h2>Provenance</h2>" + _rows(pairs) if pairs else ""


def _exposure_selection(payload: dict[str, Any]) -> str:
    selection = payload.get("selection")
    exposure = payload.get("exposure") or {}
    out = ["<h2>Selection</h2>"]
    if isinstance(selection, dict):
        out.append(_rows([(key, selection[key]) for key in (
            "finalist_id", "finalist_hash", "baseline_hash", "plan_hash", "policy_hash", "frozen_at")
            if key in selection]))
        out.append("<h3>Selection evidence</h3><pre>"
                   + _h(json.dumps(selection.get("evidence", {}), indent=2, sort_keys=True, ensure_ascii=False))
                   + "</pre>")
    else:
        out.append("<p class='muted'>No finalist was frozen for this experiment.</p>")
    out.append("<h2>Holdout exposure</h2>")
    fingerprints = exposure.get("fingerprints") or []
    block_reason = exposure.get("block_reason")
    if fingerprints:
        out.append(f"<p>{len(fingerprints)} holdout task fingerprint(s) were exposed or reserved:</p><ul>"
                   + "".join(f"<li><code>{_h(item)}</code></li>" for item in fingerprints) + "</ul>")
    else:
        out.append("<p class='muted'>No holdout content was exposed.</p>")
    if block_reason:
        out.append(f"<p><strong>Reservation blocked:</strong> {_h(block_reason)}</p>")
    return "".join(out)


def render_html(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    rows: list[str] = []
    for name, stats in summary["variants"].items():
        decision = summary["contrasts"].get(name, {})
        # The contrast is a diagnostic; the decision banner is the only verdict, so it never says "eligible".
        status = ("baseline" if name == "baseline"
                  else ("meets thresholds" if decision.get("eligible") else "below thresholds"))
        interval = decision.get("confidence_interval")
        ci = "—" if interval is None else f"[{interval[0]:+.4f}, {interval[1]:+.4f}]"
        reason = ", ".join(decision.get("reasons", [])) or "—"
        rows.append("<tr>" + "".join(
            f"<td>{_h(value)}</td>" for value in [
                name, status, _number(stats["mean_score"], ".3f"), _percent(stats["pass_rate"]),
                _percent(stats["failure_rate"]), _number(stats["mean_tokens"], ".1f"),
                _number(stats["mean_latency_ms"], ".1f"), ci, reason,
            ]
        ) + "</tr>")
    cases: list[str] = []
    for name, d in summary["contrasts"].items():
        for task_id, delta in d.get("per_task_delta", {}).items():
            cases.append(f"<tr><td>{_h(name)}</td><td>{_h(task_id)}</td><td>{delta:+.4f}</td></tr>")
    runs: list[str] = []
    for row in payload["runs"]:
        execution = row.get("execution") or {}
        evaluation = row.get("evaluation") or {}
        failed = [c["id"] for c in evaluation.get("checks", []) if isinstance(c, dict) and not c.get("passed")]
        if row.get("error"):
            failed.append(row["error"])
        score = row["objective_score"] if "objective_score" in row else evaluation.get("score")
        runs.append("<tr>" + "".join(f"<td>{_h(value)}</td>" for value in [
            str(row["trial_id"])[:10], row["task_id"], row["variant_id"], row["repetition"],
            execution.get("status", "—"), evaluation.get("status", "—"), _number(score, ".3f"),
            _measurement(execution.get("tokens")), _measurement(execution.get("latency_ms")),
            ", ".join(failed) or "—",
        ]) + "</tr>")
    css = """
    body { font-family: system-ui, sans-serif; color: #17212b; max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }
    table { width: 100%; border-collapse: collapse; font-size: .9rem; margin: 1rem 0 2rem; }
    th, td { text-align: left; border-bottom: 1px solid #ccd3da; padding: .55rem; vertical-align: top; }
    th { background: #f2f4f8; } tr:hover { background: #f8f9fa; }
    .muted { color: #495567; } code { overflow-wrap: anywhere; }
    .decision { border: 2px solid #495567; border-radius: .4rem; padding: .5rem 1rem; margin: 1rem 0; }
    .decision-eligible { border-color: #1b7f3b; } .decision-rejected { border-color: #b3261e; }
    .decision-insufficient_evidence { border-color: #9a6700; } .decision-invalid_experiment { border-color: #6f1d1b; }
    .decision-exploratory_only { border-color: #5b4bb7; }
    .badge { display: inline-block; background: #fff3cd; border: 1px solid #9a6700; border-radius: .3rem;
             padding: .1rem .5rem; font-weight: 600; }
    table.facts { width: auto; } table.facts th { background: none; width: 14rem; }
    pre { overflow-x: auto; background: #f2f4f8; padding: .5rem; }
    """
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Harness experiment {_h(payload['name'])}</title><style>{css}</style>"
        "</head><body>"
        f"<h1>Harness experiment: {_h(payload['name'])}</h1>"
        + (f"<p><span class='badge'>{_h(SYNTHETIC_LABEL)}</span> produced by a deterministic or synthetic "
           "executor, not a real model.</p>" if is_synthetic(payload) else "")
        + _decision_banner(payload) +
        f"<p class='muted'>ID: <code>{_h(payload['experiment_id'])}</code> · {_h(payload['created_at'])}</p>"
        "<p>Scores are independent acceptance checks; behavioral scores are NOT promotion evidence. "
        "Confidence intervals are hierarchical bootstrap estimates, not guarantees of deterministic model behavior.</p>"
        f"<p><strong>Recommended candidate:</strong> {_h(', '.join(summary['recommended']) or 'None validated')}</p>"
        f"<p><strong>Pareto frontier (quality / tokens / latency):</strong> {_h(', '.join(summary['pareto_frontier']))}</p>"
        "<h2>Variant comparison</h2><table><thead><tr>"
        "<th>Variant</th><th>Interim contrast (diagnostic)</th><th>Mean quality</th><th>Pass rate</th>"
        "<th>Execution failures</th><th>Mean tokens</th><th>Mean latency ms</th>"
        "<th>Paired delta CI</th><th>Reason</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        "<h2>Task-level differences</h2><table><thead><tr><th>Variant</th><th>Task</th>"
        "<th>Average paired delta</th></tr></thead><tbody>" + "".join(cases) + "</tbody></table>"
        + _exposure_selection(payload) + _provenance_block(payload) +
        "<h2>All repetitions</h2><table><thead><tr><th>Run</th><th>Task</th><th>Variant</th>"
        "<th>Repeat</th><th>Execution</th><th>Evaluation</th><th>Score</th><th>Tokens</th>"
        "<th>Latency ms (supervisor)</th><th>Failed checks / error</th></tr>"
        "</thead><tbody>" + "".join(runs) + "</tbody></table>"
        "<p class='muted'>Full output, check details, fixture hashes and provenance: results.json.</p>"
        "</body></html>"
    )
