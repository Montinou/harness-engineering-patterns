"""SrtBackend unit tests. None of them needs Anthropic Sandbox Runtime, node or a network.

Where a ``node`` executable is needed, a fake script written to a temporary directory stands in for it. The
fake proves how the backend builds settings and reacts to output and exit codes; it says nothing about what
the real sandbox enforces (``tests/integration/test_srt_backend.py`` does, with ``HARNESS_SRT_TESTS=1``).
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from harness_workbench.core.harness import ALL_COMPONENTS
from harness_workbench.experiments.backends import (
    SANDBOX_REQUIRED,
    SRT_REQUIRED,
    BackendUnavailable,
    LocalBackend,
    Mounts,
    ProcessLimits,
    SrtBackend,
    require_backend,
)
from harness_workbench.experiments.command import CLAUDE_CODE_AGENT_PLACEHOLDER, CommandHarnessExecutor
from harness_workbench.experiments.contracts import PublicTrialRequest
from harness_workbench.experiments.manifest import (
    EnvironmentConfig,
    ExecutorConfig,
    ManifestError,
    SrtConfig,
)
from harness_workbench.experiments.status import ExecutionStatus
from tests.unit import test_manifest as tm

VERSION = "0.0.81"
LIMITS = ProcessLimits(timeout_seconds=10.0)
GATEWAY = "ai-gateway.vercel.sh:443"


def make_fake_node(directory: Path, *, version: str = VERSION, selftest: str = "HARNESS_SRT_OK\n",
                   rc: int = 0) -> tuple[Path, Path, Path]:
    """A fake ``node`` that logs ``{argv, settings, mode}`` per call. Returns (node, cli, log)."""
    log = directory / "calls.jsonl"
    cli = directory / "cli.js"
    cli.write_text("// fake srt cli\n", encoding="utf-8")
    node = directory / "node"
    node.write_text(
        f"#!{sys.executable}\n"
        "import json, os, stat, sys\n"
        "argv = sys.argv[1:]\n"
        "if '--version' in argv:\n"
        f"    print({version!r}); sys.exit(0)\n"
        "settings = mode = None\n"
        "if '--settings' in argv:\n"
        "    path = argv[argv.index('--settings') + 1]\n"
        "    settings = json.load(open(path))\n"
        "    mode = stat.S_IMODE(os.stat(path).st_mode)\n"
        "    settings_path = path\n"
        f"with open({str(log)!r}, 'a') as fh:\n"
        "    fh.write(json.dumps({'argv': argv, 'settings': settings, 'mode': mode,\n"
        "                         'settings_path': path if settings else None}) + '\\n')\n"
        "if '-c' in argv and any('HARNESS_SRT' in a for a in argv):\n"
        f"    sys.stdout.write({selftest!r})\n"
        f"sys.exit({rc})\n",
        encoding="utf-8")
    node.chmod(node.stat().st_mode | stat.S_IXUSR)
    return node, cli, log


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


class SrtCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.node, self.cli, self.log = make_fake_node(self.tools)
        self.bundle = self.root / "trial" / "bundle"
        self.work = self.root / "trial" / "work"
        for path in (self.bundle, self.work):
            path.mkdir(parents=True)
        self.mounts = Mounts(read_only=(self.bundle,), writable=(self.work,))

    def backend(self, **kwargs) -> SrtBackend:
        params = dict(node=str(self.node), cli=str(self.cli), version=VERSION, egress_allow=(GATEWAY,),
                      read_paths=())
        params.update(kwargs)
        return SrtBackend(**params)


class LocalAndDockerAcceptNetworkTests(unittest.TestCase):
    def test_local_and_docker_accept_network_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outcome = LocalBackend().run(
                [sys.executable, "-c", "print('ok')"], cwd=Path(tmp), env={}, limits=LIMITS,
                mounts=Mounts(), network=True)
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        import inspect
        from harness_workbench.experiments.backends import DockerBackend
        parameter = inspect.signature(DockerBackend.run).parameters["network"]
        self.assertIs(parameter.default, False)
        self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)


class SettingsTests(SrtCase):
    def test_settings_deny_home_and_tmp_roots(self) -> None:
        settings = self.backend().settings_for(self.mounts, network=False)
        deny = settings["filesystem"]["denyRead"]
        for root in ("/Users", "/home", "/private/tmp", "/tmp", "/var/folders", "/Volumes"):
            self.assertIn(root, deny)

    def test_deny_read_also_covers_the_parent_of_every_mount(self) -> None:
        # Siblings of a mount (other problems' graders, a pool next to the experiment root) stay unreadable
        # even when the experiment lives outside the fixed roots.
        settings = self.backend().settings_for(self.mounts, network=False)
        self.assertIn(str(self.bundle.parent), settings["filesystem"]["denyRead"])
        self.assertNotIn("/", settings["filesystem"]["denyRead"])

    def test_deny_read_covers_every_ancestor_of_every_mount_outside_the_fixed_roots(self) -> None:
        # An experiment at /data/exp/trials/t1/workspace with a pool at /data/pool.json: denying only the
        # immediate parent would leave /data/pool.json readable. Every ancestor is denied (never "/").
        mounts = Mounts(read_only=(Path("/data/exp/bundle"),),
                        writable=(Path("/data/exp/trials/t1/workspace"), Path("/data/exp/trials/t1/home")))
        deny = self.backend().settings_for(mounts, network=False)["filesystem"]["denyRead"]
        for ancestor in ("/data", "/data/exp", "/data/exp/trials", "/data/exp/trials/t1"):
            self.assertIn(ancestor, deny)
        self.assertNotIn("/", deny)

    def test_system_roots_are_never_denied_as_ancestors(self) -> None:
        # Denying /private broke DNS and certificates for the allowed host on macOS (real srt).
        # RESIDUAL: /usr, /etc and the other root-owned system roots are never denied; they are not plausible
        # out-tree locations (nothing the user or the importer creates lives there) and the runtime needs them.
        mounts = Mounts(read_only=(Path("/private/var/tmp/x/bundle"),), writable=(Path("/private/var/tmp/x/w"),))
        deny = self.backend().settings_for(mounts, network=True)["filesystem"]["denyRead"]
        for ancestor in ("/private/var/tmp/x", "/private/var/tmp", "/private/var"):
            self.assertIn(ancestor, deny)
        for root in ("/", "/private", "/etc", "/usr"):
            self.assertNotIn(root, deny)

    def test_trees_sharing_no_ancestor_are_both_covered(self) -> None:
        # Experiment under /data, importer out tree under /srv (and /var/tmp): no mount has them as an ancestor.
        from unittest import mock
        mounts = Mounts(read_only=(Path("/data/exp/bundle"),), writable=(Path("/data/exp/trial/work"),))
        names = ["data", "srv", "var", "etc", "usr", "private", "bin", "Library", "run", "opt"]
        with mock.patch("harness_workbench.experiments.backends._root_names", return_value=names):
            deny = self.backend().settings_for(mounts, network=True)["filesystem"]["denyRead"]
        for path in ("/data", "/srv"):
            self.assertIn(path, deny)
        self.assertIn(os.path.realpath("/var"), deny)  # macOS: /private/var, whose /var/tmp holds out trees
        for path in ("/", "/etc", "/usr", "/private", "/bin", "/Library", "/run", "/opt"):
            self.assertNotIn(path, deny)

    def test_children_of_partially_exempt_roots_are_denied_unless_the_runtime_needs_them(self) -> None:
        # /opt and /run cannot be denied whole (the Homebrew prefix lives in /opt on macOS), but an out tree
        # directly under them must not stay readable: each child is denied unless it is an ancestor-or-equal
        # of the interpreter / read paths. Residual (documented in _top_level_denials): children that are
        # themselves needed (here /opt/homebrew) and root-owned system dirs (/usr, /etc, /System, /Library).
        # Not covered by a real-srt case: creating /opt/<x> needs root.
        from unittest import mock
        children = {"/opt": ["homebrew", "lcb-out"], "/run": ["lcb-out", "systemd"]}
        mounts = Mounts(read_only=(Path("/data/exp/bundle"),), writable=(Path("/data/exp/trial/work"),))
        backend = self.backend(read_paths=(Path("/opt/homebrew/Cellar/claude/1.0"), Path("/run/systemd/resolve")))
        with mock.patch("harness_workbench.experiments.backends._root_names", return_value=["opt", "run", "usr"]), \
                mock.patch("harness_workbench.experiments.backends._child_names",
                           side_effect=lambda root: children.get(root, [])):
            deny = backend.settings_for(mounts, network=True)["filesystem"]["denyRead"]
        for path in ("/opt/lcb-out", "/run/lcb-out"):
            self.assertIn(path, deny)
        for path in ("/opt", "/run", "/opt/homebrew", "/run/systemd", "/usr"):
            self.assertNotIn(path, deny)

    def test_unreadable_root_directory_fails_closed(self) -> None:
        from unittest import mock
        from harness_workbench.experiments import backends
        with mock.patch.object(backends.os, "listdir", side_effect=OSError("denied")):
            with self.assertRaises(BackendUnavailable):
                self.backend().settings_for(self.mounts, network=False)

    def test_mounts_stay_allowed_under_the_denied_ancestors(self) -> None:
        mounts = Mounts(read_only=(Path("/data/exp/bundle"),), writable=(Path("/data/exp/trials/t1/workspace"),))
        allow = self.backend().settings_for(mounts, network=False)["filesystem"]["allowRead"]
        for mount in ("/data/exp/bundle", "/data/exp/trials/t1/workspace"):
            self.assertIn(mount, allow)

    def test_allow_read_covers_mounts_and_base_prefix(self) -> None:
        extra = self.root / "claude-install"
        settings = self.backend(read_paths=(extra,)).settings_for(self.mounts, network=False)
        allow = settings["filesystem"]["allowRead"]
        for path in (self.bundle, self.work, Path(sys.base_prefix).resolve(), extra):
            self.assertIn(str(path), allow)

    def test_allow_write_is_writable_mounts_only(self) -> None:
        settings = self.backend().settings_for(self.mounts, network=True)
        self.assertEqual(settings["filesystem"]["allowWrite"], [str(self.work)])
        self.assertEqual(settings["filesystem"]["denyWrite"], [])

    def test_mount_paths_are_resolved_real_paths(self) -> None:
        link = self.root / "link"
        link.symlink_to(self.work)
        settings = self.backend().settings_for(Mounts(writable=(link,)), network=False)
        self.assertEqual(settings["filesystem"]["allowWrite"], [str(self.work)])

    def test_grader_runs_get_no_domains(self) -> None:
        settings = self.backend().settings_for(self.mounts, network=False)
        self.assertEqual(settings["network"], {"allowedDomains": [], "deniedDomains": []})

    def test_solver_runs_get_egress_allow(self) -> None:
        settings = self.backend().settings_for(self.mounts, network=True)
        self.assertEqual(settings["network"]["allowedDomains"], [GATEWAY])

    def test_empty_egress_allow_stays_empty_for_the_solver(self) -> None:
        settings = self.backend(egress_allow=()).settings_for(self.mounts, network=True)
        self.assertEqual(settings["network"]["allowedDomains"], [])


class RunTests(SrtCase):
    def probed(self, **kwargs) -> SrtBackend:
        backend = self.backend(**kwargs)
        backend.probe()
        return backend

    def test_run_builds_the_srt_command_line(self) -> None:
        backend = self.probed()
        self.log.unlink(missing_ok=True)
        outcome = backend.run(["/bin/echo", "a b", "$HOME"], cwd=self.work, env={}, limits=LIMITS,
                              mounts=self.mounts)
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        (call,) = calls(self.log)
        argv = call["argv"]
        self.assertEqual(argv[0], str(self.cli))
        self.assertEqual(argv[argv.index("--") + 1:], ["/bin/echo", "a b", "$HOME"])

    def test_trial_tmpdir_reaches_the_command_but_srt_gets_a_short_private_one(self) -> None:
        backend = self.probed()
        self.log.unlink(missing_ok=True)
        trial_tmp = str(self.work / "tmp")
        backend.run(["/bin/true"], cwd=self.work, env={"TMPDIR": trial_tmp, "K": "v"}, limits=LIMITS,
                    mounts=self.mounts)
        (call,) = calls(self.log)
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--") + 1:], ["/usr/bin/env", f"TMPDIR={trial_tmp}", "/bin/true"])
        self.assertNotIn("K=v", argv, "only TMPDIR travels in argv; other values stay in the environment")

    def test_settings_file_is_0600_and_outside_every_mount(self) -> None:
        backend = self.probed()
        self.log.unlink(missing_ok=True)
        backend.run(["/bin/true"], cwd=self.work, env={}, limits=LIMITS, mounts=self.mounts, network=True)
        (call,) = calls(self.log)
        self.assertEqual(call["mode"], 0o600)
        path = Path(call["settings_path"]).resolve()
        for mount in (*self.mounts.read_only, *self.mounts.writable):
            self.assertNotIn(Path(mount).resolve(), (path, *path.parents))
        self.assertFalse(path.exists(), "the settings file is removed after the run")

    def test_run_passes_domains_only_when_network_is_requested(self) -> None:
        backend = self.probed()
        self.log.unlink(missing_ok=True)
        backend.run(["/bin/true"], cwd=self.work, env={}, limits=LIMITS, mounts=self.mounts)
        backend.run(["/bin/true"], cwd=self.work, env={}, limits=LIMITS, mounts=self.mounts, network=True)
        grader, solver = calls(self.log)
        self.assertEqual(grader["settings"]["network"]["allowedDomains"], [])
        self.assertEqual(solver["settings"]["network"]["allowedDomains"], [GATEWAY])

    def test_unwritable_settings_directory_is_backend_unavailable(self) -> None:
        from unittest import mock
        backend = self.backend()
        backend._probed = True
        with mock.patch("tempfile.mkdtemp", side_effect=OSError("disk full")):
            with self.assertRaises(BackendUnavailable) as caught:
                backend.run(["true"], cwd=self.work, env={}, limits=LIMITS, mounts=self.mounts)
        self.assertIn("disk full", str(caught.exception))

    def test_run_before_probe_is_refused(self) -> None:
        with self.assertRaises(BackendUnavailable):
            self.backend().run(["/bin/true"], cwd=self.work, env={}, limits=LIMITS, mounts=self.mounts)
        self.assertEqual(calls(self.log), [], "nothing may run before the self-test")

    def test_capabilities_unknown_until_probe_then_satisfy_srt_required(self) -> None:
        backend = self.backend()
        with self.assertRaises(BackendUnavailable):
            backend.capabilities()
        backend.probe()
        capabilities = backend.capabilities()
        self.assertEqual(capabilities.missing(SRT_REQUIRED), [])
        self.assertTrue(capabilities.network_allowlist)
        self.assertFalse(capabilities.network_denied, "the solver may reach its allowlist")
        self.assertFalse(capabilities.unprivileged_user, "same uid: recorded, not claimed")
        self.assertFalse(capabilities.no_docker_socket)
        self.assertNotEqual(capabilities.missing(SANDBOX_REQUIRED), [])

    def test_empty_egress_allow_reports_network_denied(self) -> None:
        backend = self.probed(egress_allow=())
        self.assertTrue(backend.capabilities().network_denied)

    def test_selftest_failure_names_the_failed_property(self) -> None:
        directory = self.root / "tools2"
        directory.mkdir()
        node, cli, _ = make_fake_node(
            directory,
            selftest="HARNESS_SRT_FAIL canary_readable\nHARNESS_SRT_FAIL write_outside\n", rc=1)
        backend = self.backend(node=str(node), cli=str(cli))
        with self.assertRaises(BackendUnavailable) as caught:
            backend.probe()
        self.assertIn("canary_readable", str(caught.exception))
        self.assertIn("write_outside", str(caught.exception))
        with self.assertRaises(BackendUnavailable):
            backend.capabilities()

    def test_selftest_without_the_ok_marker_fails(self) -> None:
        directory = self.root / "tools3"
        directory.mkdir()
        node, cli, _ = make_fake_node(directory, selftest="")
        with self.assertRaises(BackendUnavailable):
            self.backend(node=str(node), cli=str(cli)).probe()

    def test_version_mismatch_is_backend_unavailable(self) -> None:
        directory = self.root / "tools4"
        directory.mkdir()
        node, cli, _ = make_fake_node(directory, version="0.0.99")
        with self.assertRaises(BackendUnavailable) as caught:
            self.backend(node=str(node), cli=str(cli)).probe()
        self.assertIn("0.0.99", str(caught.exception))


class UnavailableTests(SrtCase):
    def test_missing_node_or_cli_is_backend_unavailable(self) -> None:
        with self.assertRaises(BackendUnavailable):
            self.backend(node=str(self.root / "no-node")).probe()
        with self.assertRaises(BackendUnavailable):
            self.backend(cli=str(self.root / "no-cli.js")).probe()
        self.assertEqual(calls(self.log), [], "nothing ran")

    def test_relative_node_or_cli_is_refused(self) -> None:
        with self.assertRaises(BackendUnavailable):
            self.backend(node="node").probe()

    def test_require_backend_never_falls_back_to_local(self) -> None:
        environment = EnvironmentConfig(
            mode="os_sandboxed", srt=SrtConfig(node=str(self.root / "no-node"), cli=str(self.cli), version=VERSION))
        with self.assertRaises(BackendUnavailable):
            require_backend(environment)

    def test_require_backend_returns_a_probed_srt_backend(self) -> None:
        environment = EnvironmentConfig(
            mode="os_sandboxed", srt=SrtConfig(node=str(self.node), cli=str(self.cli), version=VERSION),
            egress_allow=(GATEWAY,), solver_read_paths=(str(self.root),))
        backend = require_backend(environment)
        self.assertIsInstance(backend, SrtBackend)
        self.assertEqual(backend.name, "os_sandboxed")
        self.assertEqual(backend.capabilities().missing(SRT_REQUIRED), [])

    def test_provenance_describes_version_and_cli_hash(self) -> None:
        backend = self.backend()
        description = backend.describe()
        self.assertEqual(description["version"], VERSION)
        self.assertRegex(description["cli_sha256"], r"^[0-9a-f]{64}$")


class ManifestTests(unittest.TestCase):
    def test_existing_modes_unchanged(self) -> None:
        for mode in ("trusted_local", "sandboxed"):
            EnvironmentConfig(mode=mode)
        with self.assertRaises(Exception):
            EnvironmentConfig(mode="nonsense")
        self.assertEqual(EnvironmentConfig(mode="sandboxed").egress_allow, ())

    def test_os_sandboxed_requires_srt(self) -> None:
        with self.assertRaises(Exception):
            EnvironmentConfig(mode="os_sandboxed")

    def test_srt_fields_are_refused_in_other_modes(self) -> None:
        srt = SrtConfig(node="/n", cli="/c", version=VERSION)
        with self.assertRaises(Exception):
            EnvironmentConfig(mode="sandboxed", srt=srt)
        with self.assertRaises(Exception):
            EnvironmentConfig(mode="trusted_local", egress_allow=(GATEWAY,))

    def test_srt_config_validates_paths_and_version(self) -> None:
        with self.assertRaises(Exception):
            SrtConfig(node="", cli="/c", version=VERSION)
        with self.assertRaises(Exception):
            SrtConfig(node="/n", cli="/c", version="")


class ConfirmatoryTests(tm.ManifestCase):
    def candidate_code_data(self, environment: dict) -> dict:
        data = tm.v2_dict(environment=environment)
        for task in data["tasks"]:
            task["runs_candidate_code"] = True
            task["grader_files"] = "graders"
            task["checks"].append({"id": "o", "type": "oracle", "argv": ["{python}", "-I", "check.py"],
                                   "observed": "out.json", "expected": 1})
        return data

    def test_confirmatory_accepts_os_sandboxed_for_candidate_code(self) -> None:
        data = self.candidate_code_data({
            "mode": "os_sandboxed", "srt": {"node": "/n", "cli": "/c", "version": VERSION},
            "egress_allow": [GATEWAY], "solver_read_paths": ["/opt/claude"]})
        spec = self.load_dict(data)
        self.assertEqual(spec.validate_confirmatory(), [])
        self.assertEqual(spec.environment.mode, "os_sandboxed")
        self.assertEqual(spec.environment.egress_allow, (GATEWAY,))
        self.assertEqual(spec.environment.srt, SrtConfig(node="/n", cli="/c", version=VERSION))

    def test_confirmatory_still_rejects_trusted_local_for_candidate_code(self) -> None:
        data = self.candidate_code_data({"mode": "trusted_local", "trusted_solver_acknowledged": True})
        self.assert_confirmatory_problem(data, "candidate code", "sandboxed")

    def test_unknown_srt_field_is_a_manifest_error(self) -> None:
        data = self.candidate_code_data({
            "mode": "os_sandboxed", "srt": {"node": "/n", "cli": "/c", "version": VERSION, "extra": 1}})
        with self.assertRaises(ManifestError):
            self.load_dict(data)


class IsolationRequiredForClaudeCodeTests(unittest.TestCase):
    """The adapter runs Claude Code with bypassPermissions: it must never run on the host."""

    def request(self) -> PublicTrialRequest:
        return PublicTrialRequest(
            trial_id="9f1c2ab34d5e", task_description="t", domain="d", constraints=(),
            harness_public={}, fixture_hash=None, seed=1, budget={})

    def execute(self, backend, argv):
        with tempfile.TemporaryDirectory() as tmp:
            return CommandHarnessExecutor(argv).execute_trial(
                self.request(), workspace=Path(tmp) / "workspace", backend=backend, limits=LIMITS)

    class Spy:
        name = "spy"
        python = sys.executable

        def __init__(self, capabilities) -> None:
            self._capabilities = capabilities
            self.ran = False
            self.network = None

        def capabilities(self):
            if isinstance(self._capabilities, Exception):
                raise self._capabilities
            return self._capabilities

        def run(self, argv, *, cwd, env, limits, mounts, network=False):
            self.ran, self.network = True, network
            return LocalBackend().run([sys.executable, "-c", "pass"], cwd=cwd, env=env, limits=limits,
                                      mounts=mounts)

    def test_claude_code_agent_is_refused_without_isolation(self) -> None:
        from harness_workbench.experiments.backends import NO_GUARANTEES
        argv = ["{python}", "-I", CLAUDE_CODE_AGENT_PLACEHOLDER]
        for capabilities in (NO_GUARANTEES, BackendUnavailable("not probed")):
            spy = self.Spy(capabilities)
            result = self.execute(spy, argv)
            self.assertFalse(spy.ran, "the command must not start")
            self.assertEqual(result.status, ExecutionStatus.EXECUTION_ERROR)
            self.assertFalse(result.retryable)
            self.assertIn("isolat", result.error)

    def test_claude_code_agent_runs_on_an_isolating_backend(self) -> None:
        argv = ["{python}", "-I", CLAUDE_CODE_AGENT_PLACEHOLDER]
        for capabilities in (SRT_REQUIRED, SANDBOX_REQUIRED):
            spy = self.Spy(capabilities)
            self.execute(spy, argv)
            self.assertTrue(spy.ran)

    def test_other_commands_still_run_without_isolation(self) -> None:
        from harness_workbench.experiments.backends import NO_GUARANTEES
        spy = self.Spy(NO_GUARANTEES)
        self.execute(spy, ["{python}", "-c", "pass"])
        self.assertTrue(spy.ran)

    def test_the_solver_run_passes_network_true(self) -> None:
        spy = self.Spy(SRT_REQUIRED)
        self.execute(spy, ["{python}", "-I", CLAUDE_CODE_AGENT_PLACEHOLDER])
        self.assertIs(spy.network, True)


class IntegrationSuiteOptInTests(unittest.TestCase):
    """With HARNESS_SRT_TESTS=1 an unavailable runtime must FAIL the integration suite, not skip it."""

    def run_suite(self, **env: str):
        import subprocess
        root = Path(__file__).resolve().parents[2]
        full = {name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ}
        full.update(env)
        return subprocess.run(
            [sys.executable, "-m", "unittest", "tests.integration.test_srt_backend"], cwd=root, env=full,
            capture_output=True, text=True, timeout=120)

    def test_failed_self_test_fails_the_suite_and_prints_the_reason(self) -> None:
        result = self.run_suite(HARNESS_SRT_TESTS="1", HARNESS_SRT_NODE=sys.executable,
                                HARNESS_SRT_CLI="/nonexistent/srt/cli.js")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("srt cli not found at /nonexistent/srt/cli.js", result.stderr)
        self.assertNotIn("skipped", result.stderr)

    def test_without_the_opt_in_the_suite_is_skipped(self) -> None:
        result = self.run_suite()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped", result.stderr)


if __name__ == "__main__":
    unittest.main()
