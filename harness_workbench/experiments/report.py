"""Standalone, offline HTML report for experiment evidence."""
from __future__ import annotations

from html import escape
from typing import Any


def _h(value: Any) -> str:
    return escape(str(value), quote=True)


def render_html(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    rows: list[str] = []
    for name, stats in summary["variants"].items():
        decision = summary["contrasts"].get(name, {})
        status = "baseline" if name == "baseline" else ("eligible" if decision.get("eligible") else "not eligible")
        interval = decision.get("confidence_interval")
        ci = "—" if interval is None else f"[{interval[0]:+.4f}, {interval[1]:+.4f}]"
        reason = ", ".join(decision.get("reasons", [])) or "—"
        rows.append("<tr>" + "".join(
            f"<td>{_h(value)}</td>" for value in [
                name, status, f'{stats["mean_score"]:.3f}', f'{stats["pass_rate"]:.1%}',
                f'{stats["failure_rate"]:.1%}', f'{stats["mean_tokens"]:.1f}',
                f'{stats["mean_latency_ms"]:.1f}', ci, reason,
            ]
        ) + "</tr>")
    cases: list[str] = []
    for name, d in summary["contrasts"].items():
        for task_id, delta in d.get("per_task_delta", {}).items():
            cases.append(f"<tr><td>{_h(name)}</td><td>{_h(task_id)}</td><td>{delta:+.4f}</td></tr>")
    runs: list[str] = []
    for row in payload["runs"]:
        failed = [c["id"] for c in row["checks"] if not c["passed"]]
        if row.get("error"):
            failed.append(row["error"])
        runs.append("<tr>" + "".join(f"<td>{_h(value)}</td>" for value in [
            row["run_id"][:10], row["case_id"], row["variant_id"], row["repetition"],
            f'{row["objective_score"]:.3f}', "PASS" if row["passed"] else "FAIL",
            ", ".join(failed) or "—",
        ]) + "</tr>")
    css = """
    body { font-family: system-ui, sans-serif; color: #17212b; max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }
    table { width: 100%; border-collapse: collapse; font-size: .9rem; margin: 1rem 0 2rem; }
    th, td { text-align: left; border-bottom: 1px solid #ccd3da; padding: .55rem; vertical-align: top; }
    th { background: #f2f4f8; } tr:hover { background: #f8f9fa; }
    .muted { color: #495567; } code { overflow-wrap: anywhere; }
    """
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Harness experiment {_h(payload['name'])}</title><style>{css}</style>"
        "</head><body>"
        f"<h1>Harness experiment: {_h(payload['name'])}</h1>"
        f"<p class='muted'>ID: <code>{_h(payload['experiment_id'])}</code> · {_h(payload['created_at'])}</p>"
        "<p>Scores are independent acceptance checks; behavioral scores are NOT promotion evidence. "
        "Confidence intervals are hierarchical bootstrap estimates, not guarantees of deterministic model behavior.</p>"
        f"<p><strong>Recommended candidate:</strong> {_h(', '.join(summary['recommended']) or 'None validated')}</p>"
        f"<p><strong>Pareto frontier (quality / tokens / latency):</strong> {_h(', '.join(summary['pareto_frontier']))}</p>"
        "<h2>Variant comparison</h2><table><thead><tr>"
        "<th>Variant</th><th>Promotion</th><th>Mean quality</th><th>Pass rate</th>"
        "<th>Execution failures</th><th>Mean tokens</th><th>Mean latency ms</th>"
        "<th>Paired delta CI</th><th>Reason</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        "<h2>Task-level differences</h2><table><thead><tr><th>Variant</th><th>Task</th>"
        "<th>Average paired delta</th></tr></thead><tbody>" + "".join(cases) + "</tbody></table>"
        "<h2>All repetitions</h2><table><thead><tr><th>Run</th><th>Task</th><th>Variant</th>"
        "<th>Repeat</th><th>Objective score</th><th>Result</th><th>Failed checks</th></tr>"
        "</thead><tbody>" + "".join(runs) + "</tbody></table>"
        "<p class='muted'>Full output, check details, fixture hashes and provenance: results.json.</p>"
        "</body></html>"
    )
