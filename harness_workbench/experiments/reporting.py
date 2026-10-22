"""Self-contained JSON, Markdown and HTML reports; no CDN or telemetry."""
from __future__ import annotations

import html
import json
from pathlib import Path

from harness_workbench.experiments.store import ExperimentStore


def report_data(store: ExperimentStore, experiment_id: str) -> dict:
    data = store.inspect(experiment_id)
    data["runs"] = store.records(experiment_id)
    return data


def markdown(data: dict) -> str:
    decision = data["decision"]
    lines = [f"# Harness experiment {data['experiment_id']}", "",
             f"Manifest SHA-256: `{data['manifest_hash']}`", ""]
    if decision is None:
        lines += ["**Incomplete/interrupted experiment. No promotion decision.**", ""]
    else:
        lines += [f"**Decision: {'PROMOTE' if decision['promote'] else 'KEEP BASELINE'}**", "",
                  f"Nominee: {decision['nominee'] or '(none)'}", "",
                  f"Runs: {decision['total_runs']} | Tokens: {decision['total_tokens']} "
                  f"(all usage known: {decision['tokens_known']})", ""]
        lines += [f"- {reason}" for reason in decision["reasons"]]
        for phase in ("development", "validation", "regression"):
            rows = decision.get(phase, {})
            if not rows:
                continue
            lines += ["", f"## {phase.title()}", "",
                      "| Variant | Objective score | Success rate | Runs | Tokens/run | Time ms/run | Errors |",
                      "|---|---:|---:|---:|---:|---:|---:|"]
            for label, row in rows.items():
                tokens = f"{row['mean_tokens']:.1f}" if row["usage_known"] else "unknown"
                lines.append(f"| {label} | {row['score']:.4f} | {row['success_rate']:.1%} | {row['runs']} | "
                             f"{tokens} | {row['mean_latency_ms']:.1f} | {row['errors']} |")
        if decision.get("statistics"):
            lines += ["", "## Held-out paired statistics", "", "```json",
                      json.dumps(decision["statistics"], indent=2), "```"]
    lines += ["", "## Interpretation", "",
              "Objective checks, not the executor's diagnostic score, determine selection. "
              "Repeated trials are averaged within each task before uncertainty estimates. "
              "The finalist is frozen before validation; failed validation does not trigger a runner-up test. "
              "A fresh dataset is needed after using holdout feedback to make edits.", "",
              "Filesystem separation is not a security sandbox. Provider seeds and model aliases may not "
              "guarantee determinism. Token counts are reported usage, not a monetary invoice.", ""]
    return "\n".join(lines)


def write_reports(store: ExperimentStore, experiment_id: str, directory: Path | str) -> dict[str, str]:
    data = report_data(store, experiment_id)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    document = markdown(data)
    # Escape every user- or model-controlled value. No executable embedded data.
    decision = data["decision"]
    sections = ["<h1>Harness experiment</h1><p><code>" + html.escape(experiment_id) + "</code></p>"]
    if decision:
        sections.append("<h2>" + ("Promote candidate" if decision["promote"] else "Keep baseline") + "</h2>")
        sections.append(f"<p>{decision['total_runs']} runs · {decision['total_tokens']} reported tokens</p>")
        sections.append("<ul>" + "".join("<li>" + html.escape(reason) + "</li>" for reason in decision["reasons"]) + "</ul>")
        for phase in ("development", "validation", "regression"):
            rows = decision.get(phase, {})
            if not rows:
                continue
            table = "<h2>" + phase.title() + "</h2><div class='table'><table><thead><tr>"
            table += "".join("<th scope='col'>" + title + "</th>" for title in ("Variant", "Objective score", "Success", "Runs", "Tokens/run", "Time ms/run", "Errors"))
            table += "</tr></thead><tbody>"
            for label, row in rows.items():
                values = (label, f"{row['score']:.4f}", f"{row['success_rate']:.1%}", str(row['runs']),
                          f"{row['mean_tokens']:.1f}" if row['usage_known'] else "unknown",
                          f"{row['mean_latency_ms']:.1f}", str(row['errors']))
                table += "<tr>" + "".join("<td>" + html.escape(value) + "</td>" for value in values) + "</tr>"
            sections.append(table + "</tbody></table></div>")
    else:
        sections.append("<h2>Incomplete experiment — no promotion decision</h2>")
    sections.append("<details><summary>Methodology and statistical report</summary><pre>" + html.escape(document) + "</pre></details>")
    sections.append("<details><summary>Full evidence (JSON)</summary><pre>" + html.escape(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False)) + "</pre></details>")
    web = ("<!doctype html><html lang='en'><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,initial-scale=1'>"
           "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\">"
           "<title>Harness experiment report</title><style>"
           ":root{color-scheme:light dark}body{max-width:1100px;margin:2rem auto;padding:0 1rem;font:16px system-ui;line-height:1.6}"
           "pre{white-space:pre-wrap;overflow-wrap:anywhere}details{margin:2rem 0}"
           "table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:.6rem;border-bottom:1px solid}"
           ".table{overflow-x:auto}summary{cursor:pointer;font-weight:600}"
           "</style><body><main>" + "".join(sections) + "</main></body></html>")
    payloads = {"json": json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                "md": document, "html": web}
    paths = {}
    for extension, content in payloads.items():
        path = directory / f"{experiment_id}.{extension}"
        path.write_text(content, encoding="utf-8")
        paths[extension] = str(path)
    return paths
