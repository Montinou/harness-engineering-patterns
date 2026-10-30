"""The HTML report and results.json distinguish every decision status and label what the data is."""
from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness_workbench.experiments import ExperimentSpec, run_experiment
from harness_workbench.experiments.report import is_synthetic, render_html
from harness_workbench.experiments.status import DecisionStatus
from tests.support import ScriptedExecutor, exact_task, write_manifest

EXPLORATORY_SENTENCE = "Exploratory only: not promotion evidence"
SYNTHETIC_BADGE = "Synthetic data"


class ReportCase(unittest.TestCase):
    _base: dict[str, Any] | None = None

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = write_manifest(
                root / "m", tasks=[exact_task("t1"), exact_task("t2")],
                variants=[{"id": "cand", "system_prompt": "cand"}], repetitions=2)
            executor = ScriptedExecutor({"base": lambda _t: False, "cand": lambda _t: True})
            cls._base = run_experiment(ExperimentSpec.load(manifest), executor, root / "lib")
            cls._results = json.loads(Path(cls._base["results_path"]).read_text(encoding="utf-8"))
            cls._html = Path(cls._base["report_path"]).read_text(encoding="utf-8")

    def payload(self) -> dict[str, Any]:
        assert self._base is not None
        return copy.deepcopy(self._base)

    def rows(self, html: str) -> list[str]:
        return re.findall(r"<tr[^>]*>.*?</tr>", html, flags=re.S)


class DecisionBannerTests(ReportCase):
    def test_html_shows_status_reasons_and_statistics(self) -> None:
        payload = self.payload()
        payload["mode"] = "confirmatory"
        payload["decision"].update(
            status=DecisionStatus.INSUFFICIENT_EVIDENCE.value, estimate=0.1234, lower_bound=0.0321, n_units=42,
            reasons=["independent units 42 is below min_independent_units 50", "script <b>tag</b> & more"],
            diagnostics={**payload["decision"]["diagnostics"], "n_regression_units": 17, "method": "cluster_t"})
        html = render_html(payload)
        banner = html[html.index("class='decision"):html.index("</section>", html.index("class='decision"))]
        self.assertIn("insufficient_evidence", banner)
        for reason in ("independent units 42 is below min_independent_units 50", "script &lt;b&gt;tag&lt;/b&gt; &amp; more"):
            self.assertIn(reason, banner)
        self.assertNotIn("<b>tag</b>", html)
        for expected in ("0.1234", "0.0321", ">42<", ">17<", "cluster_t", "confirmatory"):
            self.assertIn(expected, banner)
        self.assertNotIn(EXPLORATORY_SENTENCE, html)

    def test_every_decision_status_has_its_own_banner(self) -> None:
        seen: set[str] = set()
        for status in DecisionStatus:
            with self.subTest(status=status.value):
                payload = self.payload()
                payload["mode"] = "confirmatory"
                payload["decision"]["status"] = status.value
                html = render_html(payload)
                match = re.search(r"class='decision decision-([a-z_]+)'", html)
                self.assertIsNotNone(match)
                self.assertEqual(match.group(1), status.value)
                self.assertIn(f"Decision: {status.value}", html)
                seen.add(match.group(1))
        self.assertEqual(len(seen), len(DecisionStatus))

    def test_unavailable_statistics_render_as_dash_not_zero(self) -> None:
        payload = self.payload()
        payload["decision"].update(estimate=None, lower_bound=None, n_units=0)
        payload["decision"]["diagnostics"] = {}
        html = render_html(payload)
        banner = html[html.index("class='decision"):html.index("</section>", html.index("class='decision"))]
        self.assertIn("<th>estimate</th><td>—</td>", banner)
        self.assertIn("<th>lower_bound</th><td>—</td>", banner)
        self.assertIn("<th>n_regression_units</th><td>—</td>", banner)

    def test_no_interim_contrast_is_called_eligible(self) -> None:
        payload = self.payload()
        for contrast in payload["summary"]["contrasts"].values():
            contrast["eligible"] = True
        payload["decision"]["status"] = DecisionStatus.EXPLORATORY_ONLY.value
        html = render_html(payload)
        self.assertNotIn(">eligible<", html)
        self.assertNotIn("not eligible", html)


class LabelTests(ReportCase):
    def test_exploratory_and_synthetic_labels(self) -> None:
        exploratory = self.payload()
        exploratory["mode"] = "exploratory"
        exploratory["provenance"].update(executor="SomeCommandExecutor", model_requested="real-model")
        html = render_html(exploratory)
        self.assertIn(EXPLORATORY_SENTENCE, html)
        self.assertNotIn(SYNTHETIC_BADGE, html)

        confirmatory = self.payload()
        confirmatory["mode"] = "confirmatory"
        confirmatory["decision"]["status"] = DecisionStatus.REJECTED.value
        confirmatory["provenance"].update(executor="SomeCommandExecutor", model_requested="real-model")
        html = render_html(confirmatory)
        self.assertNotIn(EXPLORATORY_SENTENCE, html)
        self.assertNotIn(SYNTHETIC_BADGE, html)

        by_executor = self.payload()
        by_executor["provenance"].update(executor="MockExecutor", model_requested="real-model")
        self.assertIn(SYNTHETIC_BADGE, render_html(by_executor))

        by_model = self.payload()
        by_model["provenance"].update(executor="SomeCommandExecutor", model_requested="synthetic-v1")
        self.assertIn(SYNTHETIC_BADGE, render_html(by_model))

    def test_exploratory_status_carries_the_sentence_even_in_confirmatory_mode(self) -> None:
        payload = self.payload()
        payload["mode"] = "confirmatory"
        payload["decision"]["status"] = DecisionStatus.EXPLORATORY_ONLY.value
        self.assertIn(EXPLORATORY_SENTENCE, render_html(payload))

    def test_is_synthetic_tolerates_missing_provenance(self) -> None:
        self.assertFalse(is_synthetic({}))
        self.assertFalse(is_synthetic({"provenance": {}}))
        self.assertTrue(is_synthetic({"provenance": {"executor": "MockExecutor"}}))
        self.assertTrue(is_synthetic({"provenance": {"model_requested": "synthetic"}}))
        self.assertFalse(is_synthetic({"provenance": {"model_requested": "claude-sonnet-5-5"}}))

    def test_provenance_values_are_escaped(self) -> None:
        payload = self.payload()
        payload["provenance"]["executor"] = "<img src=x onerror=alert(1)>"
        html = render_html(payload)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)


class TrialRowTests(ReportCase):
    def test_evaluation_error_rows_have_no_score(self) -> None:
        payload = self.payload()
        run = payload["runs"][0]
        run["evaluation"].update(status="evaluation_error", score=None, error="grader crashed <oops>")
        run["objective_score"] = None
        run["error"] = "grader crashed <oops>"
        run["trial_id"] = "ZZERRORROW" + run["trial_id"]
        html = render_html(payload)
        row = next(r for r in self.rows(html) if "ZZERRORROW" in r)
        cells = re.findall(r"<td>(.*?)</td>", row, flags=re.S)
        self.assertIn("completed", cells)
        self.assertIn("evaluation_error", cells)
        self.assertEqual(cells[6], "—")  # score column
        self.assertNotIn("PASS", row)
        self.assertIn("grader crashed &lt;oops&gt;", row)
        self.assertNotIn("<oops>", html)

    def test_rows_show_statuses_score_tokens_and_supervisor_latency_with_provenance(self) -> None:
        payload = self.payload()
        run = payload["runs"][0]
        run["trial_id"] = "ZZSTATUS" + run["trial_id"]
        run["execution"].update(status="task_failure")
        run["execution"]["tokens"] = {"value": 321, "provenance": "adapter_reported"}
        run["execution"]["latency_ms"] = {"value": 12.5, "provenance": "measured"}
        run["objective_score"] = 0.25
        row = next(r for r in self.rows(render_html(payload)) if "ZZSTATUS" in r)
        cells = re.findall(r"<td>(.*?)</td>", row, flags=re.S)
        self.assertIn("task_failure", cells)
        self.assertIn("0.250", cells)
        self.assertIn("321 (adapter_reported)", cells)
        self.assertIn("12.5 (measured)", cells)

    def test_unknown_measurements_show_dash_with_their_provenance(self) -> None:
        payload = self.payload()
        run = payload["runs"][0]
        run["trial_id"] = "ZZUNKNOWN" + run["trial_id"]
        run["execution"]["tokens"] = {"value": None, "provenance": "unknown"}
        run["execution"]["latency_ms"] = {"value": None, "provenance": "not_applicable"}
        row = next(r for r in self.rows(render_html(payload)) if "ZZUNKNOWN" in r)
        cells = re.findall(r"<td>(.*?)</td>", row, flags=re.S)
        self.assertIn("— (unknown)", cells)
        self.assertIn("— (not_applicable)", cells)


class ExposureAndSelectionTests(ReportCase):
    def test_exposure_and_selection_blocks_are_rendered_and_escaped(self) -> None:
        payload = self.payload()
        payload["exposure"] = {"fingerprints": ["fp-aaa", "fp-bbb"], "block_reason": "holdout used <twice>"}
        payload["selection"] = {
            "finalist_id": "cand&1", "finalist_hash": "f" * 64, "baseline_hash": "b" * 64, "plan_hash": "p" * 64,
            "policy_hash": "q" * 64, "frozen_at": "2026-10-24T00:00:00+00:00", "evidence": {"note": "<x>"}}
        html = render_html(payload)
        self.assertIn("fp-aaa", html)
        self.assertIn("holdout used &lt;twice&gt;", html)
        self.assertIn("cand&amp;1", html)
        self.assertIn("f" * 64, html)
        self.assertIn("&lt;x&gt;", html)
        self.assertNotIn("<twice>", html)

    def test_absent_selection_and_exposure_say_so(self) -> None:
        payload = self.payload()
        payload["selection"] = None
        payload["exposure"] = {"fingerprints": [], "block_reason": None}
        html = render_html(payload)
        self.assertIn("No finalist was frozen", html)
        self.assertIn("No holdout content was exposed", html)


class ResultsJsonTests(ReportCase):
    def test_results_json_carries_schema_version_2_at_the_top_level(self) -> None:
        self.assertEqual(self._results["schema_version"], 2)
        self.assertEqual(self._base["schema_version"], 2)
        # the manifest's own version stays available under a separate key (this fixture is a legacy v1 manifest)
        self.assertEqual(self._results["manifest_schema_version"], 1)

    def test_written_report_matches_render_of_payload_status(self) -> None:
        html = self._html
        self.assertIn(f"Decision: {self._base['decision']['status']}", html)
        self.assertIn(EXPLORATORY_SENTENCE, html)


if __name__ == "__main__":
    unittest.main()
