# ADR 0005: Hash-only observation and the OS sandbox

- Status: Accepted
- Date: 2026-11-01
- Spec: real-model LiveCodeBench experiment design, sections 5 (verdict-free observation) and 7 (Sandbox: Anthropic
  Sandbox Runtime). The spec calls this record "ADR 0003"; that number was already taken by
  [ADR 0003](0003-persistence-and-migration.md), so the record is numbered 0005 and the spec's "ADR 0003" means this
  file.

## Context

[ADR 0001](0001-evaluator-trust-boundary.md) left a documented gap: candidate code that runs in the grader's
process or container can write `$HARNESS_OUT/observed.json` itself, after the grader's own write, and an `oracle`
check compares that file with `expected`. ADR 0001 closes the gap only for graders that never run candidate code in
their own process.

The real-model experiment grades LiveCodeBench problems, so candidate code has to run. Two constraints shape the
answer. First, Docker Desktop is not usable on the author's Mac (it stops at an admin-password prompt), and the human
ruled that Docker must not block the run, so the `sandboxed` mode cannot be the only isolating backend. Second, the
Claude Code adapter runs with `--permission-mode bypassPermissions`, which is only acceptable where the filesystem
and the network are confined.

## Decision

1. **Hash-only observation.** The grader bundle for a LiveCodeBench problem holds the hidden test inputs and the
   grader program, never the expected outputs. The grader runs each test in a child process, never imports the
   candidate, canonicalizes each output with the same function that reproduces LiveCodeBench's comparison
   semantics, and writes `{"hashes": [sha256-or-null, ...]}`. The importer computes the expected hashes on the host
   and keeps them outside the bundle (in `pool.json`); the supervisor compares the two lists with
   `compare: json_equal`. Forging `observed.json` therefore cannot produce a pass, because neither the expected
   outputs nor their hashes are visible to the solver or to the grader: passing requires producing correct outputs.
   This does not depend on any sandbox.
2. **A second isolating backend, `os_sandboxed`.** Backend `SrtBackend` (`harness_workbench/experiments/backends.py`)
   runs each process as `<node> <srt cli> --settings <file> -- <argv>` with Anthropic's Sandbox Runtime (Seatbelt on
   macOS, bubblewrap on Linux). The settings deny reading `/Users`, `/home`, `/private/tmp`, `/tmp`, `/var/folders`,
   `/Volumes`, every other top-level directory that is not a root-owned system root, and every ancestor of every
   mount; they allow reading the mounts, the interpreter prefix and `environment.solver_read_paths`, and allow
   writing the writable mounts only. Network access is limited to `environment.egress_allow` for the **solver**
   and is always empty for graders and controls. A capability self-test runs before any process: an unreadable
   canary outside the mounts, a refused write outside the writable mounts, a refused non-allowed host, an allowed
   host answering through the proxy when `egress_allow` is set, and a read-only bundle. If the runtime or a
   capability is missing the backend raises `BackendUnavailable` and never falls back to the host.
3. **Capabilities are recorded, not claimed.** `SRT_REQUIRED` demands an isolated filesystem, process
   containment, a read-only bundle and network that is denied or limited to an allowlist. `unprivileged_user` is
   reported **false** (the sandbox runs as the same uid) and `no_docker_socket` is false (no Docker is involved).
   `validate_confirmatory` accepts `os_sandboxed` wherever it requires `sandboxed` for candidate code, and the
   `{claude_code_agent}` placeholder is refused unless the backend satisfies `SRT_REQUIRED` or `SANDBOX_REQUIRED`.
   Provenance records the runtime version and the SHA-256 of the runtime's `cli.js`.

## Consequences

- A confirmatory result for LiveCodeBench does not rely on the sandbox for the integrity of the verdict; the sandbox
  limits what the agent can read and reach.
- `sandboxed` (Docker) and `os_sandboxed` are interchangeable for the confirmatory validation; a manifest chooses
  one in `environment.mode`.
- The importer must never write expected outputs or hashes into a grader bundle, and the grader must never import
  or execute the candidate in its own process. Both are properties of the benchmark code, not of the framework.

## Limits

- **A Seatbelt or bubblewrap boundary as the same user is weaker than a container.** There is no separate uid, no
  capability drop, no `no-new-privileges` and no memory or pid limit from the backend; a kernel or runtime bug in
  the sandbox is a bug in the boundary. This is the stated trade-off of accepting `os_sandboxed`.
- **Read-deny has known residuals.** Root-owned system roots stay readable in full (`/usr`, `/etc`, `/bin`,
  `/sbin`, `/lib`, `/System`, `/dev`, `/proc`, `/sys`, `/Library`), and so does `/opt/homebrew` (the interpreter's
  prefix on macOS). They are not plausible locations for the importer's output or the pool, and the runtime needs
  them, but anything a user places there is readable by the solver.
- The hash-only observation proves that a forged observation is useless, not that the grader is safe from other
  attacks: candidate code that crashes or exhausts the grader's resources produces `null` hashes and a failed
  trial, which is the intended outcome. ADR 0001's general limit still applies to other `oracle` checks whose
  graders import candidate code.
- A candidate can still read the public examples in `PROBLEM.md`; a solution that prints them is a negative control,
  not a pass.
- The Linux path (bubblewrap) has only been verified in CI, in the `isolation` job; the macOS path has been
  verified on the author's machine. This ADR does not assert the outcome of any CI run.
- The self-test proves the properties it checks at the moment it runs; it does not prove the absence of other
  escapes.

## Enforced by

Hash-only observation:

- `tests/lcb/test_lcb_grader.py::IsolationTests::test_solution_writing_observed_json_cannot_pass`
- `tests/lcb/test_lcb_grader.py::IsolationTests::test_grader_never_imports_solution`
- `tests/lcb/test_lcb_grader.py::IsolationTests::test_grader_never_imports_functional_solution`
- `tests/lcb/test_lcb_import.py::ImportTests::test_graders_dir_contains_no_expected_outputs`
- `tests/lcb/test_lcb_canonical.py::StdoutEquivalence::test_stdout_equivalence_matches_reference`
- `tests/lcb/test_lcb_canonical.py::ValueEquivalence::test_value_equivalence_matches_reference`
- `tests/adversarial/test_grader_shadowing.py::RunnerTampering::test_forged_observed_with_wrong_content_fails_and_correct_solution_passes`

OS sandbox, with no sandbox runtime needed (argv, settings, manifest and gating):

- `tests/unit/test_srt_backend_argv.py::SettingsTests::test_settings_deny_home_and_tmp_roots`
- `tests/unit/test_srt_backend_argv.py::SettingsTests::test_deny_read_covers_every_ancestor_of_every_mount_outside_the_fixed_roots`
- `tests/unit/test_srt_backend_argv.py::SettingsTests::test_allow_write_is_writable_mounts_only`
- `tests/unit/test_srt_backend_argv.py::SettingsTests::test_grader_runs_get_no_domains`
- `tests/unit/test_srt_backend_argv.py::SettingsTests::test_solver_runs_get_egress_allow`
- `tests/unit/test_srt_backend_argv.py::RunTests::test_run_builds_the_srt_command_line`
- `tests/unit/test_srt_backend_argv.py::RunTests::test_capabilities_unknown_until_probe_then_satisfy_srt_required`
- `tests/unit/test_srt_backend_argv.py::RunTests::test_selftest_failure_names_the_failed_property`
- `tests/unit/test_srt_backend_argv.py::UnavailableTests::test_require_backend_never_falls_back_to_local`
- `tests/unit/test_srt_backend_argv.py::UnavailableTests::test_provenance_describes_version_and_cli_hash`
- `tests/unit/test_srt_backend_argv.py::ConfirmatoryTests::test_confirmatory_accepts_os_sandboxed_for_candidate_code`
- `tests/unit/test_srt_backend_argv.py::ConfirmatoryTests::test_confirmatory_still_rejects_trusted_local_for_candidate_code`
- `tests/unit/test_srt_backend_argv.py::IsolationRequiredForClaudeCodeTests::test_claude_code_agent_is_refused_without_isolation`

OS sandbox, against the real runtime (opt-in with `HARNESS_SRT_TESTS=1`; skipped otherwise, and failing when the
runtime is unavailable):

- `tests/integration/test_srt_backend.py::SrtBackendTests::test_self_test_passes`
- `tests/integration/test_srt_backend.py::SrtBackendTests::test_canary_outside_mounts_unreadable`
- `tests/integration/test_srt_backend.py::SrtBackendTests::test_write_outside_fails`
- `tests/integration/test_srt_backend.py::SrtBackendTests::test_bundle_read_only`
- `tests/integration/test_srt_backend.py::SrtBackendTests::test_disallowed_host_refused`
- `tests/integration/test_srt_backend.py::SrtBackendTests::test_graders_get_no_domains_even_for_the_allowed_host`
- `tests/integration/test_srt_backend.py::SrtBackendTests::test_solver_cannot_read_pool_or_manifest`
- `tests/integration/test_srt_backend.py::SrtDisjointTreesTests::test_solver_cannot_read_an_out_tree_that_shares_no_ancestor_with_the_mounts`
