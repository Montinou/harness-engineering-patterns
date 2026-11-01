"""Run a candidate solution over the hidden inputs and record hashes of its outputs.

Usage: ``python -I grader.py <artifact> <out>`` with the bundle directory as cwd.
``<artifact>`` is the solution file (or a directory holding ``solution.py``);
``<out>`` is the directory that receives ``observed.json``.

The grader decides nothing. It writes ``{"hashes": [hex|null, ...]}`` in input
order, where ``null`` means "produced no usable output for this input" (crash,
timeout, output over the cap, no solution, a value with no canonical form). The
supervisor compares those hashes with the expected ones on its own side.

Candidate code never runs in this process. Each input gets its own child
(``python -I``, a fresh working directory, a minimal environment, its own session)
and the whole process group is killed as soon as the child is done, so nothing the
solution forks survives to touch ``<out>`` after the observation is written. The
observation is the last thing written, to a random temporary name and then renamed
over ``observed.json``.

Everything this file needs sits in the bundle directory. ``-I`` keeps the script
directory off ``sys.path``, so ``canonical.py`` is loaded by path.
"""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))

TEST_TIMEOUT_ENV = "LCB_TEST_TIMEOUT"
TOTAL_SECONDS_ENV = "LCB_TOTAL_SECONDS"
DEFAULT_TEST_TIMEOUT_SECONDS = 6.0
# below the check's timeout_seconds (300) so a slow solution yields an observation
DEFAULT_TOTAL_SECONDS = 270.0
OUTPUT_CAP_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
READER_JOIN_SECONDS = 2.0


def _load_canonical():
    spec = importlib.util.spec_from_file_location(
        "canonical", os.path.join(BUNDLE_DIR, "canonical.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seconds_from_env(name, default):
    try:
        value = float(os.environ[name])
    except (KeyError, ValueError):
        return default
    return value if value > 0 else default


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_candidate(command, data, timeout, cwd):
    """Run ``command`` with ``data`` on stdin; return its stdout bytes or None.

    None means the run did not finish cleanly: non-zero exit, timeout, or more
    than OUTPUT_CAP_BYTES of output. Output is read by a thread that stops at the
    cap, so a flood never sits in memory.
    """
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    with tempfile.TemporaryFile() as stdin_file:
        stdin_file.write(data)
        stdin_file.flush()
        stdin_file.seek(0)
        proc = subprocess.Popen(
            command,
            stdin=stdin_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    chunks = []
    state = {"size": 0, "overflow": False}

    def read_output():
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, READ_CHUNK_BYTES)
            except OSError:
                return
            if not chunk:
                return
            state["size"] += len(chunk)
            if state["size"] > OUTPUT_CAP_BYTES:
                state["overflow"] = True
                _kill_group(proc.pid)
                return
            chunks.append(chunk)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        _kill_group(proc.pid)
    proc.wait()
    reader.join(READER_JOIN_SECONDS)
    try:
        proc.stdout.close()
    except OSError:
        pass
    if timed_out or state["overflow"] or proc.returncode != 0 or reader.is_alive():
        return None
    return b"".join(chunks)


def hash_stdin_output(canonical, raw):
    text = raw.decode("utf-8", errors="replace")
    return canonical.digest(canonical.canonical_stdout(text))


def hash_functional_output(canonical, raw):
    value = json.loads(raw.decode("utf-8"))
    return canonical.digest(canonical.canonical_value(value))


def grade(tests, solution, canonical, python=None):
    """The list of hashes, one per input, ``None`` where nothing usable came back."""
    python = python or sys.executable
    inputs = tests["inputs"]
    mode = tests["mode"]
    hashes = [None] * len(inputs)
    if solution is None or not os.path.isfile(solution):
        return hashes
    solution = os.path.abspath(solution)
    if mode == "stdin":
        command = [python, "-I", "-X", "utf8", solution]
        hasher = hash_stdin_output
    elif mode == "functional":
        call_py = os.path.join(BUNDLE_DIR, "call.py")
        command = [python, "-I", "-X", "utf8", call_py, solution, tests["func_name"]]
        hasher = hash_functional_output
    else:
        raise ValueError("unknown mode %r" % (mode,))

    test_timeout = _seconds_from_env(TEST_TIMEOUT_ENV, DEFAULT_TEST_TIMEOUT_SECONDS)
    deadline = time.monotonic() + _seconds_from_env(
        TOTAL_SECONDS_ENV, DEFAULT_TOTAL_SECONDS
    )
    for index, text in enumerate(inputs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        workdir = tempfile.mkdtemp(prefix="lcb-run-")
        try:
            raw = run_candidate(
                command,
                text.encode("utf-8", "surrogatepass"),
                min(test_timeout, remaining),
                workdir,
            )
        except OSError:
            raw = None
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if raw is None:
            continue
        try:
            hashes[index] = hasher(canonical, raw)
        except Exception:
            # no canonical form (NaN, huge ints, unsupported types) or unparsable output
            hashes[index] = None
    return hashes


def write_observation(out_dir, hashes):
    """Write ``observed.json`` atomically, under a name nobody can predict."""
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".observed-", suffix=".tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"hashes": hashes}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, os.path.join(out_dir, "observed.json"))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: grader.py <artifact> <out>\n")
        return 2
    artifact, out_dir = argv[1], argv[2]
    if os.path.isdir(artifact):
        artifact = os.path.join(artifact, "solution.py")
    with open(os.path.join(BUNDLE_DIR, "tests.json"), encoding="utf-8") as handle:
        tests = json.load(handle)
    hashes = grade(tests, artifact, _load_canonical())
    write_observation(out_dir, hashes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
