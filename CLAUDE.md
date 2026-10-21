# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Research notes on harness engineering (`docs/`, `patterns/`, `research/`, `references/`, `applications/`) plus one real implementation, the **Harness Workbench** (`harness_workbench/`), a stdlib-only Python package (>=3.10, zero dependencies). `examples/*.py` are standalone sketches that deliberately do not import the package; leave them that way. Architecture write-up: `docs/workbench.md`.

## Commands

```bash
python -m pip install -e .                  # installs the `harness-workbench` script
PYTHONPATH=. python -m harness_workbench --help   # run from a checkout without installing
python -m unittest discover -s tests -v     # all tests (no network, no API key)
python -m unittest tests.test_workbench.<Class>.<test_name>   # one test
```

No linter or formatter is configured. Run the loop offline (the default `--provider mock`):

```bash
harness-workbench improve --harness examples/workbench/base_research_harness.json \
  --task "Fact-check a claim" --domain research --root .
```

Other subcommands: `propose`, `probe`, `report --harness-id <id>`. `--provider anthropic` needs `ANTHROPIC_API_KEY`; models come from `--solver-model`/`--improver-model` or `HARNESS_SOLVER_MODEL`/`HARNESS_IMPROVER_MODEL` (defaults `claude-sonnet-5-5` solver, `claude-haiku-5-5` proposer and judge). Tests must not hit the network.

## Architecture

The loop is **Propose → Probe → Compose → Persist**, wired by `Improver` (`core/improver.py`) from three swappable parts:

- **Proposer** (`core/proposer.py`): `RuleBasedProposer` (no model), `LLMProposer` (takes a `(prompt) -> str` completer that returns edits JSON), `FileProposer` (`--edits-file`). All go through the same edit parser; ops are `append_prompt`, `add_guardrail`, `set_memory`, `add_tool`, `add_subagent`, `add_skill`, `add_script`, `add_plugin`, `bundle`.
- **Executor** (`core/executor.py`): one-method protocol `execute(harness, task) -> BehavioralReport`. `MockExecutor` is deterministic; `AnthropicHarnessExecutor` (`core/anthropic_backend.py`) has Sonnet answer with the harness flattened into the system prompt and Haiku judge the answer. There is no live tool loop, so tool edits only change prompt guidance.
- **Composer** (in `improver.py`): adopts an edit only if its probe score beats the base by `DEFAULT_MARGIN` (0.05); winners are applied together as a new version, losers are logged as `SKIP` lessons. `parallel` mode probes `k` invariant + `k` domain candidates against one base; `sequential` runs up to `k` steps from the previously adopted harness.

`Harness` (`core/harness.py`) has eight editable components (system prompt, guardrails, memory, tools, subagents, skills, scripts, plugins). `version` increments only on adoption; candidates carry provisional versions like `1+inv-verify-plan` and are never written to the library. The proposer and the mock executor share failure codes (`missing_verification`, `missing_plan`, `thin_memory`, `missing_domain_tool:<name>`, `tool_noise_unhandled`, `no_reviewer`), so a new code must be added on both sides.

`core/store.py` persists to `--root`: `runs/<timestamp>-<id>-v<version>/` (evidence), `library/harnesses/<id>/<version>.json` (with parent pointer), `library/EXPERIENCE.md`, `library/lessons/<domain>.md`, `library/index.json`. Re-running `improve` on the same parent version overwrites that next version; continue from the adopted file for a longer chain. `runs/` and `library/` hold generated output.

The Anthropic client is raw `urllib` against `POST /v1/messages` (no SDK); thinking blocks are dropped, only text blocks are kept.

<!-- progress-tracking:start (managed by /progress-tracking:progress-init; do not edit by hand) -->
## Progress tracking (strict, mandatory, store mode)

Tracking lives in the local tracking store, never in the working tree (skill `progress-tracking:progress-tracking`):
no `progress.md`, `status.md` or task ledger file is committed, and specs and plans go to the store's documents
directory.

- **Entries**: one per task, added when **each** task finishes. A task that ends in a commit lands its entry
  *before* that commit with `triqual tracking add <draft>`; the entry stays pending for this worktree until the
  commit stamps it. A task that ends with no commit (a review, a ruling, a status-only step, a spec or plan
  registration, the plan's close, a production operation) lands it with `triqual tracking add <draft> --no-commit`:
  a pending entry left behind would be stamped onto the next, unrelated commit and let it past the gate.
  `--corrects <id>` adds a correction.
- **Status**: read with `triqual tracking status`, and change one section with
  `triqual tracking status set --section <heading> <draft>`. The whole-file `triqual tracking status set <draft>` is
  only for a full rewrite or a new section.
- **Views**: `triqual tracking status` and `triqual tracking log [--last N]`.

A "task" is a plan task, a review fix round, a rollout operation (migration, deploy, env var) or any requested
change. Subagents too: a task is not finished until its entry is in the store.

Entry draft (markdown, or JSON with the keys `title`, `stage_task`, `done`, `files`, `verification`, `next`):

```markdown
## YYYY-MM-DD — <short title>
- **Stage/task:** <e.g. Plan x · T3, or "off-plan">
- **Done:** <what changed and why>
- **Files:** <main paths>
- **Verification:** <commands and real results>
- **Next:** <next concrete step>
```

Specs and plans: draft them in a `mktemp -d` directory under their final name and land them with
`triqual tracking write spec|plan <draft>` (a plan's task ledger is initialised in the store); never put them in the
working tree. Move tasks only with `triqual tasks start|review|rework|done|block|drop|close --plan <plan file name>`
(the bare name, never the docs-dir path), and run `triqual tasks check` to find drift (it takes no `--plan`). No
tracking file is ever staged or committed. Details: `store-mode.md` under `references` in skill
`progress-tracking:progress-tracking`.

An entry is never edited: a correction is a new entry. Results are the real ones.
Enforced by the store's git `pre-commit` (rejects a commit with changes when this worktree has no pending entry;
`--no-verify` is forbidden), its `post-commit` (stamps the entries with the commit) and the plugin hooks (Stop: a turn
with changes does not end without a pending entry; SessionStart: injects the state).
<!-- progress-tracking:end -->
