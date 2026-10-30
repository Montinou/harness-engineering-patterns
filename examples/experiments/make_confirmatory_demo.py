#!/usr/bin/env python3
"""Generate the synthetic confirmatory demo manifest and its control artifacts.

    python3 examples/experiments/make_confirmatory_demo.py [--out <path>] [--sandboxed]

The benchmark is SYNTHETIC and NOT evidence about real agents: every task asks for one distinct word to be
answered and stored in ``report.txt``, the five sentences are templates, and the example agent
(``harness_workbench/experiments/demo_agent.py``) is a deterministic script. It exists to exercise the whole
confirmatory path (controls, development selection, frozen finalist, holdout, decision, promotion) offline.

The output is deterministic: the same arguments always produce the same bytes. The manifest names its
baseline, candidate harnesses and fixture relative to its own directory, which must be laid out like
``examples/experiments`` (``../workbench/base_research_harness.json`` next to it, and ``fixture`` and the
two candidate harness files inside it). The control artifacts go to ``controls/`` beside the manifest:
``controls/<task-id>/pass/report.txt`` holds that task's word, ``controls/fail`` is empty.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # -I leaves the script directory and the checkout off sys.path

from harness_workbench.core.harness import ALL_COMPONENTS  # noqa: E402
from harness_workbench.experiments.profiles import PROFILES  # noqa: E402

PROFILE = PROFILES["binary_v1"]
DEVELOPMENT_TASKS = 6
JACCARD_THRESHOLD = 0.8  # the manifest loader's near-duplicate rule (ExperimentSpec.validate_confirmatory)
NAME = "synthetic-confirmatory-demo (not evidence about real agents)"
DUPLICATE_NOTE = "synthetic demo: tasks are template variants; units are not semantically independent"
ZERO_VARIANCE_JUSTIFICATION = "deterministic synthetic agent"

# One distinct word per task (6 + 50 + 80 = 136 tasks). None of them is a word of a template.
WORDS = """
AMBER ANCHOR ARROW ASPEN BADGER BALSAM BANYAN BEACON BIRCH BISON BRAMBLE BREEZE BRONZE CACTUS CANYON CARAVAN
CEDAR CINDER CLOVER COBALT COMET CORAL COTTON COYOTE CRICKET CYPRESS DAISY DELTA DUNE EAGLE EBONY ELDER EMBER
FALCON FERN FJORD FLINT FOREST FOSSIL GALAXY GARNET GLACIER GRANITE GROVE HARBOR HAZEL HERON HOLLOW HORIZON
INDIGO IRONWOOD ISLAND IVORY JASPER JUNIPER KESTREL LAGOON LANTERN LARCH LAUREL LICHEN LILAC LOTUS MAGNET MAPLE
MARBLE MEADOW MERCURY MOSAIC MOSS NEBULA NECTAR OAK OCELOT ONYX ORCHARD OTTER PEBBLE PELICAN PEPPER PINE PLATEAU
POPLAR PRAIRIE QUARTZ QUILL RAVEN REEF RIDGE RIVER ROBIN SAFFRON SAGE SALMON SAPPHIRE SEQUOIA SPARROW SPRUCE
STORM SUMMIT TAIGA THISTLE THUNDER TIMBER TOPAZ TUNDRA TURQUOISE VALLEY VELVET VIOLET WALNUT WILLOW WINTER ZEPHYR
ZENITH ALMOND BAMBOO CASCADE DRIFTWOOD AGATE BASALT CHESTNUT DOLPHIN EMERALD FIREFLY GECKO HAWTHORN IBIS JACKAL
KELP LYNX MARTEN NUTMEG OPAL PLUM QUAIL
""".split()

TEMPLATES = (
    "Reply with the single word {word} and store it in report.txt",
    "The expected token is {word}; write it to report.txt and answer with it",
    "Write {word} into report.txt, then answer with {word} alone",
    "Answer {word} and record the same word in report.txt",
    "Output the keyword {word} and save it as the contents of report.txt",
)


def _words_of(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"\w+", text.lower()))  # same tokenisation as the manifest loader


def _task_plan() -> list[tuple[str, str, str]]:
    """``(task id, split, word)`` for every task, development first, in a fixed order."""
    plan = [(f"dev-{n:02d}", "development") for n in range(1, DEVELOPMENT_TASKS + 1)]
    plan += [(f"val-{n:02d}", "validation") for n in range(1, PROFILE.min_independent_units + 1)]
    plan += [(f"reg-{n:02d}", "regression") for n in range(1, PROFILE.min_regression_units + 1)]
    if len(WORDS) < len(plan) or len(set(WORDS)) != len(WORDS):
        raise SystemExit(f"need {len(plan)} distinct words, have {len(set(WORDS))}")
    return [(task_id, split, WORDS[index]) for index, (task_id, split) in enumerate(plan)]


def _task(index: int, task_id: str, split: str, word: str) -> dict:
    others = [WORDS[(index + step) % len(WORDS)] for step in (1, 2)]
    base = f"controls/{task_id}"
    return {
        "id": task_id,
        "description": TEMPLATES[index % len(TEMPLATES)].format(word=word),
        "domain": "research",
        "fixture": "fixture",
        "checks": [
            {"id": "answer", "type": "exact", "target": "answer", "value": word},
            {"id": "artifact", "type": "file_equals", "path": "report.txt", "value": word, "critical": True},
        ],
        "split": split,
        "runs_candidate_code": False,
        "controls": {
            "positive": {"id": "positive", "outputs": {"answer": word}, "artifact": f"{base}/pass"},
            "negatives": [
                {"id": "wrong-answer", "outputs": {"answer": "INCORRECT"}, "artifact": "controls/fail"},
                {"id": "empty-answer", "outputs": {"answer": ""}, "artifact": "controls/fail"},
                {"id": "enumeration", "outputs": {"answer": " ".join([word, *others])},
                 "artifact": "controls/fail"},
            ],
        },
    }


def _duplicate_pairs(tasks: list[dict]) -> list[str]:
    """Every pair the loader's Jaccard rule flags (each task is its own family, so none is exempt)."""
    words = [_words_of(task["description"]) for task in tasks]
    pairs = []
    for first in range(len(tasks)):
        for second in range(first + 1, len(tasks)):
            a, b = words[first], words[second]
            if a and b and len(a & b) / len(a | b) >= JACCARD_THRESHOLD:
                pairs.append(f"{tasks[first]['id']}:{tasks[second]['id']}")
    return pairs


def build_manifest(*, sandboxed: bool) -> dict:
    template_words = set().union(*(_words_of(t.replace("{word}", "")) for t in TEMPLATES))
    clash = sorted(word for word in WORDS if word.lower() in template_words)
    if clash:
        raise SystemExit(f"words that are template words: {clash}")
    tasks = [_task(index, *entry) for index, entry in enumerate(_task_plan())]
    environment = ({"mode": "sandboxed"} if sandboxed
                   else {"mode": "trusted_local", "trusted_solver_acknowledged": True})
    return {
        "schema_version": 2,
        "name": NAME,
        "mode": "confirmatory",
        "family": "demo-research-agent",
        "dataset": {"id": "synthetic-demo", "version": "1"},
        "baseline": "../workbench/base_research_harness.json",
        "variants": [
            {"id": "verification", "harness": "verified_research_harness.json"},
            {"id": "noop", "harness": "noop_research_harness.json"},
        ],
        "tasks": tasks,
        "duplicate_review": _duplicate_pairs(tasks),
        "duplicate_review_note": DUPLICATE_NOTE,
        "repetitions": PROFILE.repetitions,
        "seed": 42,
        "statistics": {
            "profile": PROFILE.id,
            "method": PROFILE.method,
            "regression_margin": PROFILE.regression_margin,
            "allow_zero_variance": True,
            "zero_variance_justification": ZERO_VARIANCE_JUSTIFICATION,
        },
        "budget": {},
        "environment": environment,
        "executor": {
            "type": "command",
            "argv": ["{python}", "-I", "{demo_agent}"],
            "model": "synthetic-fixture-agent",
            "timeout_seconds": 30,
            "supports": list(ALL_COMPONENTS),
        },
    }


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_controls(manifest: dict, directory: Path) -> None:
    """The artifact directories the manifest's controls name (the shared failing one stays empty)."""
    for task in manifest["tasks"]:
        word = task["checks"][0]["value"]
        _write(directory / task["controls"]["positive"]["artifact"] / "report.txt", word)
    _write(directory / "controls" / "pass" / "report.txt", "PASS")  # generic passing artifact for smoke tests
    _write(directory / "controls" / "fail" / ".gitkeep", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the synthetic confirmatory demo benchmark.")
    parser.add_argument("--out", type=Path, help="manifest path (default: confirmatory-demo.json, or "
                        "sandboxed-demo.json with --sandboxed, beside this script)")
    parser.add_argument("--sandboxed", action="store_true", help='use environment {"mode": "sandboxed"}')
    args = parser.parse_args(argv)
    out = args.out or HERE / ("sandboxed-demo.json" if args.sandboxed else "confirmatory-demo.json")
    manifest = build_manifest(sandboxed=args.sandboxed)
    write_controls(manifest, out.parent)
    _write(out, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out} ({len(manifest['tasks'])} tasks, {len(manifest['duplicate_review'])} reviewed pairs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
