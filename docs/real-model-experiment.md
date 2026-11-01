# First real-model experiment: where it stands

Status at 2026-11-01. The build is four of five phases done. **No experiment trial has run yet**, and model calls
through the AI Gateway are on hold by the owner's decision until they lift it.

The code lives in one GitHub stack of pull requests, #3 → #4 → #6 → #7, one per phase. Until that stack merges,
the paths below exist on those branches, not on `main`.

## The question

Does a harness that the Workbench's LLM proposer writes from development-split evidence make Claude Code, driven
by Claude Haiku 5.5, pass more held-out LiveCodeBench problems than its base harness, without a demonstrated
regression on a separate regression split? The experiment reports `eligible`, `rejected` or
`insufficient_evidence`, and each is a publishable result.

Rule the design follows: reuse existing tools. Headless Claude Code is the agent, LiveCodeBench supplies the tasks,
Anthropic's Sandbox Runtime (`srt`) isolates processes, Gateway keys cap spend, and this repo's experiment runner
makes the decision. The new code is the glue between them.

## What is built

| Phase | What it adds | Where |
|---|---|---|
| 1 | Hash-only grader and canonicalizer equivalent to LiveCodeBench's comparison; importer that decodes private tests without `pickle.loads` (opcode allowlist) and pins the dataset by SHA-256 | `benchmarks/livecodebench/{canonical,grader,call,import_lcb}.py`, `dataset_lock.json` |
| 2 | Single-file Claude Code adapter that applies seven harness components (system prompt, guardrails, memory, tools, subagents, skills, scripts) to `claude -p`; `{claude_code_agent}` placeholder; opt-in reach test | `harness_workbench/experiments/claude_code_agent.py`, `tests/integration/test_claude_code_reach.py` |
| 3 | `os_sandboxed` backend on `srt` 0.0.81 (Seatbelt on macOS, bubblewrap on Linux) with a Gateway-only egress allowlist and a capability self-test; opt-in `environment.parallelism`; ADR 0005 | `harness_workbench/experiments/backends.py`, `core.py`, `docs/adr/0005-hash-only-observation-and-os-sandbox.md` |
| 4 | Controls builder (Opus-generated positive control, two negatives, problems with a misbehaving control excluded); base harness and pilot candidate; split assignment; pilot manifest; runbook | `benchmarks/livecodebench/{make_controls,make_manifests}.py`, `base_harness.json`, `pilot_candidate.json`, `README.md` |

Three design points:

- **A forged pass is useless.** The grader never imports candidate code and records only SHA-256 hashes of what
  the candidate printed. Expected outputs and their hashes never enter the sandbox. The supervisor compares the
  hashes itself.
- **`--bare` is never used.** It switches Claude Code to a three-tool "simple mode" with no skills or subagents.
  Isolation comes from `env -i`, a fresh `HOME` and `CLAUDE_CONFIG_DIR`, `--setting-sources ""` and
  `--strict-mcp-config`.
- **Docker is not required.** Docker Desktop on the author's Mac stops at an admin-password prompt, so trials run
  under `srt`. The adapter refuses to run on any backend without an egress allowlist.

## Evidence so far

| Check | Result |
|---|---|
| Full suite on macOS | 846 tests OK, 21 opt-in skips |
| `srt` integration tests on macOS (`HARNESS_SRT_TESTS=1`) | 12 tests OK: repository and pool unreadable, non-Gateway hosts refused, writes outside the trial refused |
| Reach test on the real model (Claude Code 2.1.316, Haiku 5.5) | 7 of 7 components reached the model; the tool case read a per-run nonce file; negative control held. $0.0066 |
| Real dataset import (revision `0fe84c3`) | 308 of 342 problems kept (32 over the 8 MiB bundle limit, 2 in LiveCodeBench's errata): easy 83, medium 89, hard 136 |
| Total model spend through the Gateway | about $0.02, all probes and reach tests |

Each phase went through per-task reviews (safety gate and spec review), then a phase review: one or two hunters
and an adversarial reviewer, with up to two fix rounds.

## Known limits

- **Contamination.** LiveCodeBench's last problem is dated 2025-04-06, and Haiku 5.5's training cutoff is June
  2026. The paired design keeps memorization from favouring either harness, but it lowers the headroom. Pilot gate
  G2 checks the headroom, and SWE-rebench is the fallback.
- **Same-user sandbox.** The `srt` boundary runs as the same user. System roots such as `/usr`, `/etc` and
  `/opt/homebrew` stay readable (ADR 0005).
- **Linux CI is red on PRs #6 and #7, and this blocks merging the stack.** The `isolation` job runs the `srt`
  tests on Ubuntu 24.04, and their self-test fails with:

  `apply-seccomp: write /proc/self/setgroups (nested userns is capability-restricted; caller must provide CAP_SYS_ADMIN)`

  What has been ruled out:
  - srt's seccomp helper is visible now, after `vendor/seccomp` was added to `allowRead`.
  - Both kernel switches srt's own CI sets are on: `kernel.apparmor_restrict_unprivileged_userns=0` and
    `kernel.unprivileged_userns_clone=1`.
  - `bwrap --unshare-user --cap-drop ALL` succeeds on the runner.

  So the cause is in the settings our backend generates, or in how it invokes `srt`. The next step is a CI probe
  that runs `srt -- true` with srt's default settings, then with ours. The macOS sandbox, where the experiment
  runs, is verified.
- Open minor findings are listed in the plan's ledger and carried to the final cross-phase review.

## What is left

1. **Phase 5 (build, offline).** `propose_from_dev.py` turns the pilot's development results into proposer input
   with failure categories and drops candidates that change `plugins` or name an unknown tool. Confirmatory
   manifest generation uses profile `binary_v1`.
2. **Final cross-phase review** of all five phases. The two minors that had to be fixed before the confirmatory
   run, parallel crash handling and the adapter refusal on Docker, are already fixed in Phase 4.
3. **Rollout, which needs the hold lifted.** Each step runs through the AI Gateway:

| Step | Key | Estimate |
|---|---|---|
| Opus 5.5 controls for the whole pool | `lcb-setup` ($30 limit) | $5–15 |
| Pilot: 25 development problems, base and one candidate, 3 repetitions | `lcb-setup` | about $1 |
| Pilot gates G1–G5 (infrastructure, headroom, cost, time, reach) | — | stop if any fails |
| Proposer candidates (Sonnet 5.5) | `lcb-setup` | under $1 |
| Confirmatory run: 50 validation and 80 regression problems, 5 repetitions, parallelism 6 | `lcb-confirmatory` ($100 limit) | $20–60, 5–10 h |

4. **Publish the result**, whatever it is, under `docs/results/`.

## Local state on the author's machine

- **Gateway keys:** `~/.config/ai-gateway/lcb-setup.env` and `lcb-confirmatory.env`, both mode 0600. They were
  created 2026-11-01 with 30-day expiry and alerts at 75 % and 100 %.
- **Sandbox runtime:** `srt` is under `~/.local/share/harness-tools`.
- **Claude Code:** pinned at `~/.local/share/claude/versions/2.1.316`.
- **Dataset cache:** `~/.cache/harness-lcb/<revision>`, checksums verified.
- **Imported pool:** `~/lcb-run/pool-trial`.

The runbook is `benchmarks/livecodebench/README.md`.
