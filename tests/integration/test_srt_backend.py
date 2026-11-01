"""Anthropic Sandbox Runtime integration tests.

Skipped (not run, not passed) unless ``HARNESS_SRT_TESTS=1``. With it set, a failing self-test or a missing
runtime FAILS the suite and prints the reason: a green run means the isolation was really exercised. Where
the runtime lives:

* ``HARNESS_SRT_NODE``: absolute path of a real ``node`` (not a version-manager shim that fails under an empty
  environment). Default: ``shutil.which("node")``, resolved.
* ``HARNESS_SRT_CLI``: ``@anthropic-ai/sandbox-runtime/dist/cli.js``. Default: the harness-tools install under
  ``~/.local/share/harness-tools`` (documented default for the author's machine).
* ``HARNESS_SRT_VERSION``: the pinned version (default ``0.0.81``).
* ``HARNESS_SRT_EGRESS``: the host allowed for the solver self-test (default ``ai-gateway.vercel.sh:443``).
  The self-test makes one real HTTPS request to it, so the tests need that host to be reachable.

On Linux the runtime needs bubblewrap, socat and ripgrep.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from harness_workbench.experiments.backends import (
    SRT_REQUIRED,
    BackendUnavailable,
    Mounts,
    ProcessLimits,
    SrtBackend,
    minimal_env,
)
from harness_workbench.experiments.status import ExecutionStatus

DEFAULT_CLI = Path.home() / ".local/share/harness-tools/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
DEFAULT_VERSION = "0.0.81"
DEFAULT_EGRESS = "ai-gateway.vercel.sh:443"
LIMITS = ProcessLimits(timeout_seconds=60.0)
OPT_IN = os.environ.get("HARNESS_SRT_TESTS") == "1"
SKIP_REASON = "HARNESS_SRT_TESTS is not 1"
REASON: list[str] = []
# A directory the user can write that is under none of the fixed deny roots (/Users, /home, /tmp,
# /private/tmp, /var/folders, /Volumes): /var/tmp on macOS and Linux.
OUTSIDE_FIXED_ROOTS = Path("/var/tmp")
FIXED_DENY_ROOTS = ("/Users", "/home", "/private/tmp", "/tmp", "/var/folders", "/Volumes")


def _node() -> str | None:
    configured = os.environ.get("HARNESS_SRT_NODE")
    found = configured or shutil.which("node")
    return os.path.realpath(found) if found else None


def _probe(egress: tuple[str, ...]) -> SrtBackend | None:
    if not OPT_IN:
        return None
    node = _node()
    cli = os.environ.get("HARNESS_SRT_CLI") or str(DEFAULT_CLI)
    if node is None:
        REASON.append("no node found")
        return None
    backend = SrtBackend(node, cli, os.environ.get("HARNESS_SRT_VERSION", DEFAULT_VERSION), egress, ())
    try:
        backend.probe()
    except BackendUnavailable as exc:
        REASON.append(str(exc))
        return None
    return backend


_EGRESS = (os.environ.get("HARNESS_SRT_EGRESS", DEFAULT_EGRESS),)
_BACKEND = _probe(_EGRESS)


REACH_CODE = (
    "import urllib.error, urllib.request\n"
    "class Stop(urllib.request.HTTPRedirectHandler):\n"
    "    def redirect_request(self, *a, **k):\n"
    "        return None\n"
    "try:\n"
    "    urllib.request.build_opener(Stop).open('https://{host}/', timeout=20)\n"
    "    print('ANSWERED')\n"
    "except urllib.error.HTTPError:\n"
    "    print('ANSWERED')\n"
    "except urllib.error.URLError as exc:\n"
    "    print('REFUSED', exc.reason)\n")


def _require_backend() -> None:
    if _BACKEND is None:
        raise AssertionError(
            "HARNESS_SRT_TESTS=1 but the srt backend is unavailable, so nothing was exercised: "
            + ("; ".join(REASON) or "unknown reason"))


class _TreeCase:
    """An experiment and a canary under ``self.top``, and an importer out-tree under ``make_out_top()``.

    The out tree is next to the experiment by default; subclasses move it (or the whole layout) to places
    that share no ancestor with the mounts."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _require_backend()

    def make_top(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(os.path.realpath(tmp.name))

    def make_out_top(self) -> Path:
        """Where the importer's out tree lives; by default next to the experiment."""
        return self.top

    def setUp(self) -> None:
        self.backend = _BACKEND
        self.top = self.make_top()
        # The importer's --out tree: the pool, a manifest, other problems' graders and a download cache.
        self.out_tree = self.make_out_top() / "importer-out"
        (self.out_tree / "problems" / "other" / "grader").mkdir(parents=True)
        (self.out_tree / "cache").mkdir()
        (self.out_tree / "pool.json").write_text('{"secret": "pool"}', encoding="utf-8")
        (self.out_tree / "manifest.json").write_text('{"secret": "manifest"}', encoding="utf-8")
        (self.out_tree / "problems" / "other" / "grader" / "tests.json").write_text("other", encoding="utf-8")
        (self.out_tree / "cache" / "download.bin").write_text("cache", encoding="utf-8")
        # An experiment root next to the pool, with one trial.
        self.experiment = self.top / "experiment"
        self.bundle = self.experiment / "bundle"
        self.trial = self.experiment / "trial"
        self.work, self.home, self.tmp = self.trial / "workspace", self.trial / "home", self.trial / "tmp"
        for path in (self.bundle, self.work, self.home, self.tmp):
            path.mkdir(parents=True)
        (self.bundle / "marker.txt").write_text("bundle", encoding="utf-8")
        (self.experiment / "sibling.txt").write_text("sibling", encoding="utf-8")
        self.canary = self.top / "canary.txt"
        self.canary.write_text("canary", encoding="utf-8")
        self.mounts = Mounts(read_only=(self.bundle,), writable=(self.work, self.home, self.tmp))

    def run_py(self, code: str, *args: str, network: bool = False, backend: SrtBackend | None = None):
        backend = backend or self.backend
        return backend.run(
            [backend.python, "-I", "-c", code, *args], cwd=self.work,
            env=minimal_env(home=self.home, tmp=self.tmp), limits=LIMITS, mounts=self.mounts, network=network)

    def check_solver_cannot_read_pool_or_manifest(self) -> None:
        # The planned solver_read_paths: only the Claude Code install (here, a stand-in directory).
        claude_install = self.top / "claude-install"
        claude_install.mkdir()
        (claude_install / "claude").write_text("binary", encoding="utf-8")
        backend = SrtBackend(self.backend.node, self.backend.cli, self.backend.version, _EGRESS,
                             (claude_install,))
        backend.probe()
        targets = [
            self.out_tree / "pool.json", self.out_tree / "manifest.json",
            self.out_tree / "problems" / "other" / "grader" / "tests.json",
            self.out_tree / "cache" / "download.bin", self.experiment / "sibling.txt", self.canary,
        ]
        code = (
            "import sys\n"
            "for path in sys.argv[1:]:\n"
            "    try:\n"
            "        open(path).read()\n"
            "        print('READ', path)\n"
            "    except OSError:\n"
            "        print('DENIED', path)\n"
            "    try:\n"
            "        import os; os.listdir(os.path.dirname(path))\n"
            "        print('LISTED', os.path.dirname(path))\n"
            "    except OSError:\n"
            "        pass\n")
        outcome = self.run_py(code, *map(str, targets), network=True, backend=backend)
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED, outcome.stderr)
        self.assertNotIn("READ", outcome.stdout)
        self.assertNotIn("LISTED", outcome.stdout)
        self.assertEqual(outcome.stdout.count("DENIED"), len(targets), outcome.stdout)
        # The explicitly allowed read path and the bundle stay readable.
        allowed = self.run_py(
            "import sys\nprint(open(sys.argv[1]).read(), open(sys.argv[2]).read())",
            str(claude_install / "claude"), str(self.bundle / "marker.txt"), backend=backend)
        self.assertIn("binary bundle", allowed.stdout, allowed.stderr)
        # With this layout the solver still reaches the allowed host (denied trees hide no runtime need).
        reach = self.run_py(REACH_CODE.format(host=_EGRESS[0].split(":")[0]), network=True, backend=backend)
        self.assertIn("ANSWERED", reach.stdout, f"{reach.stdout} {reach.stderr}")


@unittest.skipUnless(OPT_IN, SKIP_REASON)
class SrtBackendTests(_TreeCase, unittest.TestCase):
    def test_self_test_passes(self) -> None:
        capabilities = self.backend.capabilities()
        self.assertEqual(capabilities.missing(SRT_REQUIRED), [])
        self.assertTrue(capabilities.network_allowlist)
        self.assertFalse(capabilities.unprivileged_user)

    def test_runs_python_in_the_workspace(self) -> None:
        outcome = self.run_py("open('out.txt', 'w').write('hi'); print(open('out.txt').read())")
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED, outcome.stderr)
        self.assertEqual(outcome.stdout.strip(), "hi")
        self.assertEqual((self.work / "out.txt").read_text(), "hi")

    def test_canary_outside_mounts_unreadable(self) -> None:
        outcome = self.run_py("print(open(__import__('sys').argv[1]).read())", str(self.canary))
        self.assertNotEqual(outcome.exit_code, 0)
        self.assertNotIn("canary", outcome.stdout)
        self.assertIn("Operation not permitted", outcome.stderr)

    def test_write_outside_fails(self) -> None:
        target = self.top / "escaped.txt"
        outcome = self.run_py("open(__import__('sys').argv[1], 'w').write('x')", str(target))
        self.assertNotEqual(outcome.exit_code, 0)
        self.assertFalse(target.exists())

    def test_bundle_read_only(self) -> None:
        outcome = self.run_py(
            "import sys\nprint(open(sys.argv[1] + '/marker.txt').read())\n"
            "open(sys.argv[1] + '/planted.txt', 'w').write('x')", str(self.bundle))
        self.assertNotEqual(outcome.exit_code, 0)
        self.assertIn("bundle", outcome.stdout)
        self.assertFalse((self.bundle / "planted.txt").exists())

    def test_disallowed_host_refused(self) -> None:
        code = (
            "import urllib.error, urllib.request\n"
            "try:\n"
            "    urllib.request.urlopen('https://example.com/', timeout=20)\n"
            "    print('REACHED')\n"
            "except urllib.error.URLError as exc:\n"
            "    print('REFUSED', exc.reason)\n")
        for network in (False, True):
            outcome = self.run_py(code, network=network)
            self.assertIn("REFUSED", outcome.stdout, f"network={network}: {outcome.stdout} {outcome.stderr}")
            self.assertNotIn("REACHED", outcome.stdout)

    def test_graders_get_no_domains_even_for_the_allowed_host(self) -> None:
        code = REACH_CODE.format(host=_EGRESS[0].split(":")[0])
        self.assertIn("REFUSED", self.run_py(code, network=False).stdout)
        self.assertIn("ANSWERED", self.run_py(code, network=True).stdout)

    def test_solver_cannot_read_pool_or_manifest(self) -> None:
        self.check_solver_cannot_read_pool_or_manifest()

    def test_settings_file_does_not_survive_the_run(self) -> None:
        def leftovers() -> set[Path]:
            return {path for root in {Path("/tmp"), Path(tempfile.gettempdir())} if root.is_dir()
                    for path in root.glob("hw-srt-*") if "selftest" not in path.name}

        before = leftovers()
        self.run_py("pass")
        self.assertEqual(leftovers(), before)

    def test_timeout_kills_the_sandboxed_process(self) -> None:
        backend = self.backend
        outcome = backend.run(
            [backend.python, "-I", "-c", "import time; time.sleep(60)"], cwd=self.work,
            env=minimal_env(home=self.home, tmp=self.tmp), limits=ProcessLimits(timeout_seconds=3.0),
            mounts=self.mounts)
        self.assertEqual(outcome.status, ExecutionStatus.TIMEOUT)


def _outside_fixed_roots(case: unittest.TestCase) -> Path:
    """A fresh directory under /var/tmp, which is under none of the fixed deny roots. Fails, never skips."""
    if not (OUTSIDE_FIXED_ROOTS.is_dir() and os.access(OUTSIDE_FIXED_ROOTS, os.W_OK)):
        case.fail(f"HARNESS_SRT_TESTS=1 needs {OUTSIDE_FIXED_ROOTS} to be writable for this layout")
    top = Path(tempfile.mkdtemp(prefix="hw-srt-it-", dir=OUTSIDE_FIXED_ROOTS))
    case.addCleanup(shutil.rmtree, top, True)
    real = Path(os.path.realpath(top))
    for root in FIXED_DENY_ROOTS:
        case.assertNotIn(Path(root), (real, *real.parents), f"{real} is under the fixed root {root}")
    return real


@unittest.skipUnless(OPT_IN, SKIP_REASON)
class SrtOutsideFixedRootsTests(_TreeCase, unittest.TestCase):
    """The tree lives outside every fixed deny root: only the ancestors of the mounts protect the pool."""

    def make_top(self) -> Path:
        return _outside_fixed_roots(self)

    def test_solver_cannot_read_pool_or_manifest_outside_the_fixed_roots(self) -> None:
        self.check_solver_cannot_read_pool_or_manifest()


@unittest.skipUnless(OPT_IN, SKIP_REASON)
class SrtDisjointTreesTests(_TreeCase, unittest.TestCase):
    """The experiment (under the user's home) and the importer out tree (/var/tmp) share no ancestor."""

    def make_top(self) -> Path:
        top = Path(tempfile.mkdtemp(prefix="hw-srt-it-", dir=Path.home()))
        self.addCleanup(shutil.rmtree, top, True)
        return Path(os.path.realpath(top))

    def make_out_top(self) -> Path:
        return _outside_fixed_roots(self)

    def test_solver_cannot_read_an_out_tree_that_shares_no_ancestor_with_the_mounts(self) -> None:
        self.assertFalse(set(self.out_tree.parents) & set(self.bundle.parents) - {Path("/")},
                         "the layout must share no ancestor except /")
        self.check_solver_cannot_read_pool_or_manifest()


if __name__ == "__main__":
    unittest.main()
