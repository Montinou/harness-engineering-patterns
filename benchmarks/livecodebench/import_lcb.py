"""Turn pinned LiveCodeBench rows into experiment tasks, without trusting the data.

Usage (from a checkout)::

    python benchmarks/livecodebench/import_lcb.py --revision <40-hex hf commit> \\
        --files test5.jsonl test6.jsonl --out <dir> [--source-dir <local jsonl dir>]

Output under ``<dir>``::

    pool.json                  one entry per imported problem, with the expected hashes
    import_report.json         counts, exclusions with reasons, near-duplicate pairs
    fixtures/<id>/PROBLEM.md   statement and public examples (+ solution.py for functional)
    graders/<id>/              canonical.py, grader.py, call.py, tests.json (inputs only)

Expected outputs and their hashes exist only in ``pool.json``, outside ``graders/``.

The downloaded rows are untrusted. Private tests are ``base64(zlib(pickle(json)))``;
``decode_private`` never calls ``pickle.loads``. It walks the pickle's opcodes with
``pickletools.genops`` and accepts only the ones a pickled ``str`` needs, with exactly
one string, so nothing in the data can run. Decompression stops at a cap. Every file is
checked against the SHA-256 recorded in ``dataset_lock.json``.

Problem texts and tests are never committed: the lock holds only hashes, and the
importer writes its output wherever ``--out`` says.
"""

import argparse
import base64
import binascii
import hashlib
import importlib.util
import json
import os
import pickletools
import re
import shutil
import sys
import tempfile
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))

DATASET = "livecodebench/code_generation_lite"
DATASET_URL = "https://huggingface.co/datasets/%s/resolve/{revision}/{name}" % DATASET
DEFAULT_LOCK = os.path.join(HERE, "dataset_lock.json")
DEFAULT_ERRATA = os.path.join(HERE, "errata.json")
BUNDLE_SOURCES = ("canonical.py", "grader.py", "call.py")

MAX_DECODED_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 120
CHUNK_BYTES = 1024 * 1024
DUPLICATE_JACCARD = 0.8

# the only opcodes a pickled str needs (protocol 4); anything else is refused unexecuted
ALLOWED_OPCODES = frozenset(
    ("PROTO", "FRAME", "BINUNICODE", "SHORT_BINUNICODE", "MEMOIZE", "STOP"))
STRING_OPCODES = frozenset(("BINUNICODE", "SHORT_BINUNICODE"))

REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.jsonl$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")

START_LINE = "Write your solution to solution.py."
STDIN_LINE = "Your program must read standard input and write standard output."
FUNCTIONAL_LINE = (
    "Implement `Solution.{func}`; the starter code in solution.py has its signature. "
    "The grader adds no imports to your file, so import everything you use "
    "(for example `from typing import List`; the starter begins with `from typing import *`).")
TYPING_HEADER = "from typing import *\n\n"


class LockMismatch(Exception):
    """A dataset file does not match the SHA-256 recorded for it."""


def _load_canonical():
    spec = importlib.util.spec_from_file_location(
        "lcb_canonical", os.path.join(HERE, "canonical.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


canonical = _load_canonical()


# ---------------------------------------------------------------- private tests

def _check_tests(tests):
    if not isinstance(tests, list):
        raise ValueError("test cases must be a JSON list")
    for item in tests:
        if not isinstance(item, dict):
            raise ValueError("a test case must be an object")
        if not isinstance(item.get("input"), str) or not isinstance(item.get("output"), str):
            raise ValueError("a test case needs string input and output")
        if "testtype" in item and not isinstance(item["testtype"], str):
            raise ValueError("testtype must be a string")
    return tests


def _inflate(blob, max_bytes):
    inflater = zlib.decompressobj()
    data = inflater.decompress(blob, max_bytes + 1)
    if len(data) > max_bytes or inflater.unconsumed_tail:
        raise ValueError("decompressed data is larger than %d bytes" % max_bytes)
    if not inflater.eof:
        raise ValueError("truncated zlib stream")
    if inflater.unused_data:
        raise ValueError("data after the end of the zlib stream")
    return data


def _string_from_pickle(data):
    """The one string a pickle holds, read by its opcodes alone; nothing is executed."""
    strings = []
    position = None
    for opcode, arg, pos in pickletools.genops(data):
        if opcode.name not in ALLOWED_OPCODES:
            raise ValueError("pickle opcode %s is not allowed" % opcode.name)
        if opcode.name in STRING_OPCODES:
            strings.append(arg)
        if opcode.name == "STOP":
            position = pos
    if position is None:
        raise ValueError("pickle has no STOP")
    if position + 1 != len(data):
        raise ValueError("data after the pickle's STOP")
    if len(strings) != 1:
        raise ValueError("pickle must hold exactly one string, found %d" % len(strings))
    return strings[0]


def decode_private(s, max_bytes):
    """Decode a ``private_test_cases`` value into a list of ``{input, output, ...}``.

    Accepts a plain JSON list, or ``base64(zlib(pickle(json string)))`` read through an
    opcode allowlist. ``max_bytes`` caps the decompressed size. Raises ValueError on
    anything else.
    """
    try:
        if not isinstance(s, str):
            raise ValueError("expected a string")
        text = s.strip()
        if text.startswith("["):
            if len(text.encode("utf-8", "surrogatepass")) > max_bytes:
                raise ValueError("test cases are larger than %d bytes" % max_bytes)
            return _check_tests(json.loads(text))
        if not text:
            raise ValueError("empty test case string")
        blob = base64.b64decode(text, validate=True)
        pickled = _inflate(blob, max_bytes)
        return _check_tests(json.loads(_string_from_pickle(pickled)))
    except ValueError:
        raise
    except (binascii.Error, zlib.error, RecursionError, MemoryError) as exc:
        raise ValueError("cannot decode test cases: %s" % exc) from exc


# ---------------------------------------------------------------- row helpers

def _text(row, key):
    value = row.get(key)
    if not isinstance(value, str):
        raise ValueError("field %s must be a string" % key)
    return value


def mode_of(row):
    """``("stdin", None)`` or ``("functional", func_name)`` from the row's metadata."""
    raw = row.get("metadata")
    try:
        metadata = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except ValueError as exc:
        raise ValueError("metadata is not JSON") from exc
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    func_name = metadata.get("func_name")
    if func_name is None:
        return "stdin", None
    if not isinstance(func_name, str) or not func_name.isidentifier():
        raise ValueError("func_name is not an identifier")
    return "functional", func_name


def description_for(row):
    """Title, full statement, the mode line, then where to write the solution."""
    mode, func_name = mode_of(row)
    if mode == "functional":
        mode_line = FUNCTIONAL_LINE.format(func=func_name)
    else:
        mode_line = STDIN_LINE
    return "\n\n".join(
        [_text(row, "question_title").strip(), _text(row, "question_content").strip(),
         mode_line, START_LINE])


def functional_args(raw_input):
    """One JSON argument list from LiveCodeBench's one-JSON-value-per-line input."""
    lines = raw_input.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return json.dumps([json.loads(line) for line in lines])


def _fence(text):
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def problem_md(row, mode, func_name, examples):
    parts = ["# %s" % _text(row, "question_title").strip(), "",
             _text(row, "question_content").strip(), ""]
    if mode == "functional":
        parts += ["Implement `Solution.%s` in solution.py." % func_name, ""]
    else:
        parts += [STDIN_LINE, ""]
    parts += ["## Public examples", ""]
    if not examples:
        parts += ["(none)", ""]
    for number, example in enumerate(examples, 1):
        shown = example.get("raw_input", example["input"])
        label = "Input" if mode == "stdin" else "Input (one JSON argument per line)"
        for title, body in ((label, shown), ("Output", example["output"])):
            fence = _fence(body)
            parts += ["### Example %d %s" % (number, title), "", fence, body.rstrip("\n"), fence, ""]
    return "\n".join(parts)


def _slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _words(text):
    return frozenset(re.findall(r"\w+", text.lower()))


def _normalized(row):
    return " ".join((_text(row, "question_title") + " " + _text(row, "question_content")).split())


def _load_errata(path):
    with open(path, encoding="utf-8") as handle:
        return frozenset(json.load(handle)["ids"])


# ---------------------------------------------------------------- dataset files

def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_lock(path):
    try:
        with open(path, encoding="utf-8") as handle:
            lock = json.load(handle)
    except FileNotFoundError:
        return {"dataset": DATASET, "revisions": {}}
    if not isinstance(lock, dict) or not isinstance(lock.get("revisions"), dict):
        raise ValueError("%s is not a dataset lock" % path)
    return lock


def _write_json(path, data, indent=None):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=indent, sort_keys=indent is not None)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _recorded(lock, revision, name):
    entry = lock["revisions"].get(revision, {}).get("files", {}).get(name)
    return entry.get("sha256") if isinstance(entry, dict) else None


def _record(lock, lock_path, revision, name, sha256, size):
    files = lock["revisions"].setdefault(revision, {"files": {}}).setdefault("files", {})
    files[name] = {"sha256": sha256, "bytes": size}
    lock["dataset"] = DATASET
    _write_json(lock_path, lock, indent=2)


def _download(revision, name, destination, opener):
    request = urllib.request.Request(
        DATASET_URL.format(revision=revision, name=name),
        headers={"User-Agent": "harness-engineering-patterns-lcb-importer"})
    digest = hashlib.sha256()
    size = 0
    with opener(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, \
            open(destination, "wb") as handle:
        while True:
            chunk = response.read(CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise ValueError("%s is larger than %d bytes" % (name, MAX_DOWNLOAD_BYTES))
            digest.update(chunk)
            handle.write(chunk)
    return digest.hexdigest(), size


def resolve_file(revision, name, lock, lock_path, source_dir, cache_dir, opener):
    """A local path of the dataset file whose SHA-256 is the recorded one.

    A file from ``source_dir`` needs a recorded hash already (the lock is written by a
    real download). A downloaded file records its hash the first time and is checked
    against it afterwards.
    """
    expected = _recorded(lock, revision, name)
    if source_dir is not None:
        path = os.path.join(source_dir, name)
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        if expected is None:
            raise LockMismatch(
                "no SHA-256 is recorded for %s at %s; download it once without --source-dir"
                % (name, revision))
        actual = _sha256_file(path)
        if actual != expected:
            raise LockMismatch("%s has sha256 %s, the lock records %s" % (path, actual, expected))
        return path
    os.makedirs(cache_dir, exist_ok=True)
    cached = os.path.join(cache_dir, name)
    if os.path.isfile(cached) and expected is not None and _sha256_file(cached) == expected:
        return cached
    fd, partial = tempfile.mkstemp(prefix=".part-", dir=cache_dir)
    os.close(fd)
    try:
        actual, size = _download(revision, name, partial, opener)
        if expected is None:
            _record(lock, lock_path, revision, name, actual, size)
        elif actual != expected:
            raise LockMismatch(
                "downloaded %s has sha256 %s, the lock records %s" % (name, actual, expected))
        os.replace(partial, cached)
    finally:
        if os.path.exists(partial):
            os.unlink(partial)
    return cached


def _rows(path):
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                yield number, line


# ---------------------------------------------------------------- the import

class _Skip(Exception):
    def __init__(self, reason, detail=None, **extra):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.extra = extra


def _build(row, errata, max_bundle_bytes):
    """Validate one row and return what is needed to write it, or raise _Skip."""
    qid = _text(row, "question_id")
    if not ID_RE.match(qid):
        raise _Skip("unsafe_id")
    title = _text(row, "question_title")
    if qid in errata or _slug(title) in errata:
        raise _Skip("errata")
    try:
        mode, func_name = mode_of(row)
        description = description_for(row)
        starter = _text(row, "starter_code")
        platform, difficulty = _text(row, "platform"), _text(row, "difficulty")
        contest_date = _text(row, "contest_date")
        public = decode_private(_text(row, "public_test_cases"), MAX_DECODED_BYTES)
    except ValueError as exc:
        raise _Skip("malformed_row", str(exc)[:200])
    try:
        private = decode_private(_text(row, "private_test_cases"), MAX_DECODED_BYTES)
    except ValueError as exc:
        raise _Skip("private_tests_undecodable", str(exc)[:200])
    tests = public + private
    if not tests:
        raise _Skip("no_tests")
    if any(t.get("testtype", mode) != mode for t in tests):
        raise _Skip("test_type_mismatch", "metadata says %s" % mode)
    if sum(len(t["input"]) for t in tests) > max_bundle_bytes:
        raise _Skip("bundle_over_limit")
    inputs = []
    for test in tests:
        if mode == "functional":
            try:
                inputs.append(functional_args(test["input"]))
            except ValueError as exc:
                raise _Skip("bad_functional_input", str(exc)[:200])
        else:
            inputs.append(test["input"])
    hashes = []
    for test in tests:
        try:
            if mode == "functional":
                hashes.append(canonical.digest(canonical.canonical_value(json.loads(test["output"]))))
            else:
                hashes.append(canonical.digest(canonical.canonical_stdout(test["output"])))
        except Exception as exc:
            raise _Skip("no_canonical_expected_output",
                        "%s: %s" % (type(exc).__name__, str(exc)[:160]))
    tests_json = json.dumps({"mode": mode, "func_name": func_name, "inputs": inputs})
    bundle_bytes = len(tests_json.encode("utf-8")) + sum(
        os.path.getsize(os.path.join(HERE, name)) for name in BUNDLE_SOURCES)
    if bundle_bytes > max_bundle_bytes:
        raise _Skip("bundle_over_limit")
    examples = []
    for index, test in enumerate(public):
        example = {"input": inputs[index], "output": test["output"]}
        if mode == "functional":
            example["raw_input"] = test["input"]
        examples.append(example)
    return {
        "id": qid, "mode": mode, "func_name": func_name, "title": title,
        "platform": platform, "difficulty": difficulty, "contest_date": contest_date,
        "description": description, "starter": starter, "inputs": inputs, "hashes": hashes,
        "tests_json": tests_json, "bundle_bytes": bundle_bytes, "examples": examples,
        "statement_key": _normalized(row),
    }


def _write_problem(out, row, built):
    qid = built["id"]
    bundle = os.path.join(out, "graders", qid)
    fixture = os.path.join(out, "fixtures", qid)
    for directory in (bundle, fixture):
        shutil.rmtree(directory, ignore_errors=True)
        os.makedirs(directory)
    for name in BUNDLE_SOURCES:
        shutil.copyfile(os.path.join(HERE, name), os.path.join(bundle, name))
    with open(os.path.join(bundle, "tests.json"), "w", encoding="utf-8") as handle:
        handle.write(built["tests_json"])
    with open(os.path.join(fixture, "PROBLEM.md"), "w", encoding="utf-8") as handle:
        handle.write(problem_md(row, built["mode"], built["func_name"], built["examples"]))
    if built["mode"] == "functional":
        starter = built["starter"]
        if "from typing import *" not in starter:
            starter = TYPING_HEADER + starter
        with open(os.path.join(fixture, "solution.py"), "w", encoding="utf-8") as handle:
            handle.write(starter)


def _near_duplicates(descriptions):
    ids = list(descriptions)
    words = {qid: _words(text) for qid, text in descriptions.items()}
    pairs = []
    for index, first in enumerate(ids):
        for second in ids[index + 1:]:
            a, b = words[first], words[second]
            if a and b:
                score = len(a & b) / len(a | b)
                if score >= DUPLICATE_JACCARD:
                    pairs.append({"a": first, "b": second, "jaccard": round(score, 4)})
    return pairs


def run_import(revision, files, out, source_dir=None, cache_dir=None, lock_path=DEFAULT_LOCK,
               errata_path=DEFAULT_ERRATA, max_bundle_bytes=MAX_BUNDLE_BYTES,
               opener=urllib.request.urlopen):
    """Import ``files`` of ``revision`` into ``out``; return the report."""
    if not REVISION_RE.match(revision or ""):
        raise ValueError("--revision must be a full 40-character commit hash")
    for name in files:
        if not FILE_RE.match(name):
            raise ValueError("unsupported dataset file name %r" % (name,))
    out = os.path.abspath(out)
    cache_dir = cache_dir or os.path.join(out, "download-cache")
    errata = _load_errata(errata_path)
    lock = _read_lock(lock_path)
    paths = [resolve_file(revision, name, lock, lock_path, source_dir, cache_dir, opener)
             for name in files]
    os.makedirs(out, exist_ok=True)

    pool, exclusions = [], []
    counts = {}
    seen, statements, descriptions = set(), {}, {}
    rows_read = 0
    for name, path in zip(files, paths):
        for number, line in _rows(path):
            rows_read += 1
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("a row must be a JSON object")
            except ValueError as exc:
                exclusions.append({"id": "%s:%d" % (name, number), "reason": "malformed_row",
                                   "detail": str(exc)[:200]})
                continue
            raw_id = row.get("question_id")
            qid = raw_id if isinstance(raw_id, str) else repr(raw_id)
            try:
                if isinstance(raw_id, str) and ID_RE.match(raw_id):
                    if raw_id in seen:
                        raise _Skip("duplicate_id")
                    seen.add(raw_id)
                built = _build(row, errata, max_bundle_bytes)
                earlier = statements.get(built["statement_key"])
                if earlier is not None:
                    raise _Skip("duplicate_statement", duplicate_of=earlier)
            except _Skip as skip:
                item = {"id": qid[:100], "reason": skip.reason}
                if skip.detail:
                    item["detail"] = skip.detail
                item.update(skip.extra)
                exclusions.append(item)
                if isinstance(raw_id, str) and ID_RE.match(raw_id) and skip.reason != "duplicate_id":
                    for sub in ("graders", "fixtures"):
                        shutil.rmtree(os.path.join(out, sub, raw_id), ignore_errors=True)
                continue
            except ValueError as exc:
                exclusions.append({"id": qid[:100], "reason": "malformed_row",
                                   "detail": str(exc)[:200]})
                continue
            _write_problem(out, row, built)
            statements[built["statement_key"]] = built["id"]
            descriptions[built["id"]] = built["description"]
            counts.setdefault(built["platform"], {}).setdefault(built["difficulty"], 0)
            counts[built["platform"]][built["difficulty"]] += 1
            pool.append({
                "id": built["id"], "platform": built["platform"],
                "difficulty": built["difficulty"],
                "contest_date": built["contest_date"], "mode": built["mode"],
                "func_name": built["func_name"], "n_tests": len(built["inputs"]),
                "bundle_bytes": built["bundle_bytes"], "title": built["title"],
                "description": built["description"], "expected_hashes": built["hashes"],
                "public_examples": built["examples"],
            })

    reasons = {}
    for item in exclusions:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    report = {
        "revision": revision, "files": list(files), "rows_read": rows_read,
        "imported": len(pool), "counts": counts, "exclusions": exclusions,
        "exclusion_counts": reasons,
        "near_duplicates": _near_duplicates(descriptions),
        "duplicate_jaccard_threshold": DUPLICATE_JACCARD,
    }
    _write_json(os.path.join(out, "pool.json"), pool, indent=1)
    _write_json(os.path.join(out, "import_report.json"), report, indent=2)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--revision", required=True, help="full Hugging Face commit hash")
    parser.add_argument("--files", nargs="+", required=True, help="e.g. test5.jsonl test6.jsonl")
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-dir", help="local directory holding the jsonl files")
    parser.add_argument("--cache-dir", help="download cache (default <out>/download-cache)")
    parser.add_argument("--lock-file", default=DEFAULT_LOCK)
    parser.add_argument("--errata-file", default=DEFAULT_ERRATA)
    parser.add_argument("--max-bundle-bytes", type=int, default=MAX_BUNDLE_BYTES)
    args = parser.parse_args(argv)
    try:
        report = run_import(
            args.revision, args.files, args.out, source_dir=args.source_dir,
            cache_dir=args.cache_dir, lock_path=args.lock_file, errata_path=args.errata_file,
            max_bundle_bytes=args.max_bundle_bytes)
    except (LockMismatch, ValueError, OSError) as exc:
        sys.stderr.write("import failed: %s\n" % exc)
        return 2
    print("imported %d of %d rows" % (report["imported"], report["rows_read"]))
    for reason, count in sorted(report["exclusion_counts"].items()):
        print("  excluded %-30s %d" % (reason, count))
    for pair in report["near_duplicates"]:
        print("  near-duplicate %s %s (jaccard %.2f): review and list in duplicate_review"
              % (pair["a"], pair["b"], pair["jaccard"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
