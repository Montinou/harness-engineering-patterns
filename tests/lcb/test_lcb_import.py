"""The importer turns LiveCodeBench rows into tasks without trusting the data.

Every row here is synthetic. No real problem text or test is stored in the repo and
nothing touches the network: downloads go through an injected opener.
"""

import base64
import hashlib
import io
import json
import pickle
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from benchmarks.livecodebench import import_lcb
from benchmarks.livecodebench.canonical import (
    canonical_stdout,
    canonical_value,
    digest,
)

REVISION = "0123456789abcdef0123456789abcdef01234567"
GRADER_FILES = ("canonical.py", "grader.py", "call.py", "tests.json")

_EXECUTED = []


def _boom(*args):
    _EXECUTED.append(args)
    return "{}"


class _Evil:
    def __reduce__(self):
        return (_boom, ("pwned",))


def encode_private(tests):
    """The dataset's encoding: base64(zlib(pickle(json string)))."""
    blob = pickle.dumps(json.dumps(tests), protocol=4)
    return base64.b64encode(zlib.compress(blob)).decode("ascii")


def b64z(blob):
    return base64.b64encode(zlib.compress(blob)).decode("ascii")


def stdin_row(qid="abc900_a", title="Sum Two", content="Read two integers and print their sum.",
              public=None, private=None, difficulty="easy", platform="atcoder",
              date="2025-02-01T00:00:00"):
    public = public if public is not None else [
        {"input": "1 2\n", "output": "3\n", "testtype": "stdin"}]
    private = private if private is not None else [
        {"input": "10 20\n", "output": "30\n", "testtype": "stdin"}]
    return {
        "question_title": title,
        "question_content": content,
        "platform": platform,
        "question_id": qid,
        "contest_id": "abc900",
        "contest_date": date,
        "starter_code": "",
        "difficulty": difficulty,
        "public_test_cases": json.dumps(public),
        "private_test_cases": encode_private(private),
        "metadata": "{}",
    }


def functional_row(qid="3001", title="Double Them", content="Return each number doubled.",
                   func_name="doubleAll", public=None, private=None, difficulty="medium"):
    public = public if public is not None else [
        {"input": "[1, 2]\n3", "output": "[2, 4, 6]", "testtype": "functional"}]
    private = private if private is not None else [
        {"input": "[5]\n1", "output": "[10, 2]", "testtype": "functional"}]
    return {
        "question_title": title,
        "question_content": content,
        "platform": "leetcode",
        "question_id": qid,
        "contest_id": "weekly-contest-900",
        "contest_date": "2025-03-01T00:00:00",
        "starter_code": "class Solution:\n    def doubleAll(self, nums: List[int], k: int) -> List[int]:\n        ",
        "difficulty": difficulty,
        "public_test_cases": json.dumps(public),
        "private_test_cases": json.dumps(private),
        "metadata": json.dumps({"func_name": func_name}),
    }


class DecodePrivateTests(unittest.TestCase):
    def test_decode_private_plain_json(self):
        tests = [{"input": "1\n", "output": "2\n", "testtype": "stdin"}]
        self.assertEqual(import_lcb.decode_private(json.dumps(tests), 1 << 20), tests)

    def test_decode_private_pickled_string(self):
        tests = [{"input": "1\n", "output": "2\n", "testtype": "stdin"},
                 {"input": "é\n", "output": "x\n", "testtype": "stdin"}]
        self.assertEqual(import_lcb.decode_private(encode_private(tests), 1 << 20), tests)

    def test_decode_private_rejects_global_opcode(self):
        del _EXECUTED[:]
        payload = b64z(pickle.dumps(_Evil(), protocol=4))
        with self.assertRaises(ValueError):
            import_lcb.decode_private(payload, 1 << 20)
        self.assertEqual(_EXECUTED, [])

    def test_decode_private_rejects_global_opcode_protocol_0(self):
        del _EXECUTED[:]
        payload = b64z(pickle.dumps(_Evil(), protocol=0))
        with self.assertRaises(ValueError):
            import_lcb.decode_private(payload, 1 << 20)
        self.assertEqual(_EXECUTED, [])

    def test_decode_private_rejects_other_shapes(self):
        two_strings = pickle.dumps(("[]", "[]"), protocol=4)
        no_string = pickle.dumps(None, protocol=4)
        trailing = pickle.dumps("[]", protocol=4) + b"junk"
        a_list = pickle.dumps(["[]"], protocol=4)
        for blob in (two_strings, no_string, trailing, a_list):
            with self.assertRaises(ValueError, msg=repr(blob)):
                import_lcb.decode_private(b64z(blob), 1 << 20)

    def test_decode_private_requires_exactly_one_string(self):
        # built from allowed opcodes only, so just the string count can reject it
        body = b"\x8c\x02[]\x94\x8c\x02[]\x94."
        two = b"\x80\x04\x95" + len(body).to_bytes(8, "little") + body
        one = b"\x80\x04\x95" + (len(body) // 2).to_bytes(8, "little") + body[:len(body) // 2] + b"."
        self.assertEqual(import_lcb.decode_private(b64z(one), 1 << 20), [])
        with self.assertRaises(ValueError):
            import_lcb.decode_private(b64z(two), 1 << 20)
        with self.assertRaises(ValueError):
            import_lcb.decode_private(b64z(b"\x80\x04\x95" + (1).to_bytes(8, "little") + b"."), 1 << 20)

    def test_decode_private_rejects_malformed_input(self):
        cases = [
            "!!!not base64!!!",
            base64.b64encode(b"not zlib at all").decode(),
            b64z(pickle.dumps("not json", protocol=4)),
            b64z(pickle.dumps('{"a": 1}', protocol=4)),  # not a list
            b64z(pickle.dumps('[{"input": "1"}]', protocol=4)),  # no output
            b64z(pickle.dumps('[{"input": 1, "output": "2"}]', protocol=4)),
            "",
        ]
        for text in cases:
            with self.assertRaises(ValueError, msg=text[:40]):
                import_lcb.decode_private(text, 1 << 20)

    def test_decode_private_rejects_oversize(self):
        bomb = zlib.compress(pickle.dumps("[" + " " * 200000 + "]", protocol=4))
        self.assertLess(len(bomb), 1000)
        with self.assertRaises(ValueError):
            import_lcb.decode_private(base64.b64encode(bomb).decode(), 10000)
        # the same payload is fine under a larger cap
        self.assertEqual(
            import_lcb.decode_private(base64.b64encode(bomb).decode(), 1 << 20), [])
        # plain JSON obeys the cap too
        with self.assertRaises(ValueError):
            import_lcb.decode_private("[" + " " * 20000 + "]", 10000)

    def test_cap_is_enforced_while_decompressing(self):
        # decompression must stop at the cap, not after inflating everything
        payload = base64.b64encode(zlib.compress(b"\0" * (64 << 20))).decode()
        with self.assertRaises(ValueError):
            import_lcb.decode_private(payload, 1 << 16)


    def test_decompression_is_bounded_by_the_cap(self):
        calls = []
        real = zlib.decompressobj

        class Spy:
            def __init__(self):
                self.inner = real()

            def decompress(self, data, max_length=0):
                calls.append(max_length)
                return self.inner.decompress(data, max_length)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        payload = base64.b64encode(zlib.compress(b"\0" * (8 << 20))).decode()
        original = zlib.decompressobj
        zlib.decompressobj = Spy
        try:
            with self.assertRaises(ValueError):
                import_lcb.decode_private(payload, 1 << 16)
        finally:
            zlib.decompressobj = original
        self.assertTrue(calls)
        self.assertTrue(all(0 < n <= (1 << 16) + 1 for n in calls), calls)


class ImportTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lcb-import-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.source = self.root / "source"
        self.out = self.root / "out"
        self.lock = self.root / "dataset_lock.json"
        self.errata = self.root / "errata.json"
        self.source.mkdir()
        self.errata.write_text(json.dumps({"ids": ["abc311_c", "find-the-peaks"]}))

    def write_source(self, name, rows):
        path = self.source / name
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        return path

    def write_lock(self, name, path):
        sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self.lock.write_text(json.dumps({
            "dataset": "livecodebench/code_generation_lite",
            "revisions": {REVISION: {"files": {name: {"sha256": sha}}}},
        }))
        return sha

    def run_import(self, rows, **overrides):
        path = self.write_source("test6.jsonl", rows)
        self.write_lock("test6.jsonl", path)
        options = dict(revision=REVISION, files=["test6.jsonl"], out=self.out,
                       source_dir=self.source, lock_path=self.lock, errata_path=self.errata)
        options.update(overrides)
        return import_lcb.run_import(**options)

    def pool(self):
        return json.loads((self.out / "pool.json").read_text())

    def report(self):
        return json.loads((self.out / "import_report.json").read_text())


class ImportTests(ImportTestCase):
    def test_errata_and_oversize_bundles_excluded(self):
        big = "x" * (9 * 1024 * 1024)
        rows = [
            stdin_row("abc900_a"),
            stdin_row("abc311_c", title="Erratum"),
            functional_row("3002", title="Peaks Found Here", content="Statement of the peaks."),
            stdin_row("abc900_b", title="Big", content="A huge one.",
                      private=[{"input": big, "output": "1\n", "testtype": "stdin"}]),
        ]
        rows[2]["question_title"] = "Find the Peaks"
        self.run_import(rows)
        ids = [entry["id"] for entry in self.pool()]
        self.assertEqual(ids, ["abc900_a"])
        reasons = {item["id"]: item["reason"] for item in self.report()["exclusions"]}
        self.assertEqual(reasons["abc311_c"], "errata")
        self.assertEqual(reasons["3002"], "errata")  # LeetCode errata entries are title slugs
        self.assertEqual(reasons["abc900_b"], "bundle_over_limit")
        self.assertFalse((self.out / "graders" / "abc900_b").exists())
        self.assertFalse((self.out / "graders" / "abc311_c").exists())

    def test_graders_dir_contains_no_expected_outputs(self):
        stdin = stdin_row(
            "abc900_a",
            public=[{"input": "1 2\n", "output": "EXPECTED-PUBLIC-7731\n", "testtype": "stdin"}],
            private=[{"input": "3 4\n", "output": "EXPECTED-PRIVATE-5519\n", "testtype": "stdin"}])
        functional = functional_row(
            "3001",
            public=[{"input": "[1]\n2", "output": "[424242, 8]", "testtype": "functional"}],
            private=[{"input": "[2]\n2", "output": "[919191, 6]", "testtype": "functional"}])
        self.run_import([stdin, functional])
        pool = self.pool()
        self.assertEqual(len(pool), 2)
        secrets = ["EXPECTED-PUBLIC-7731", "EXPECTED-PRIVATE-5519", "424242", "919191"]
        for entry in pool:
            secrets.extend(entry["expected_hashes"])
        self.assertTrue(all(len(h) == 64 for h in pool[0]["expected_hashes"]))
        files = [p for p in (self.out / "graders").rglob("*") if p.is_file()]
        self.assertGreaterEqual(len(files), 8)
        for path in files:
            data = path.read_bytes().decode("utf-8", "replace")
            for secret in secrets:
                self.assertNotIn(secret, data, "%s leaks %s" % (path, secret))

    def test_bundle_layout_and_tests_json_shape(self):
        self.run_import([stdin_row("abc900_a"), functional_row("3001")])
        for qid in ("abc900_a", "3001"):
            bundle = self.out / "graders" / qid
            self.assertEqual(sorted(p.name for p in bundle.iterdir()), sorted(GRADER_FILES))
            for name in ("canonical.py", "grader.py", "call.py"):
                source = Path(import_lcb.__file__).parent / name
                self.assertEqual((bundle / name).read_bytes(), source.read_bytes())
        stdin_tests = json.loads((self.out / "graders/abc900_a/tests.json").read_text())
        self.assertEqual(sorted(stdin_tests), ["func_name", "inputs", "mode"])
        self.assertEqual(stdin_tests["mode"], "stdin")
        self.assertIsNone(stdin_tests["func_name"])
        self.assertEqual(stdin_tests["inputs"], ["1 2\n", "10 20\n"])
        func_tests = json.loads((self.out / "graders/3001/tests.json").read_text())
        self.assertEqual(func_tests["mode"], "functional")
        self.assertEqual(func_tests["func_name"], "doubleAll")
        # one JSON argument list per input, as call.py reads it
        self.assertEqual(func_tests["inputs"], ["[[1, 2], 3]", "[[5], 1]"])

    def test_pool_entries_and_hashes(self):
        self.run_import([stdin_row("abc900_a"), functional_row("3001")])
        by_id = {entry["id"]: entry for entry in self.pool()}
        stdin = by_id["abc900_a"]
        self.assertEqual(
            sorted(stdin),
            sorted(["id", "platform", "difficulty", "contest_date", "mode", "func_name", "n_tests",
                    "bundle_bytes", "title", "description", "expected_hashes", "public_examples"]))
        self.assertEqual(stdin["mode"], "stdin")
        self.assertEqual(stdin["n_tests"], 2)
        self.assertEqual(stdin["expected_hashes"],
                         [digest(canonical_stdout("3\n")), digest(canonical_stdout("30\n"))])
        self.assertEqual(stdin["public_examples"], [{"input": "1 2\n", "output": "3\n"}])
        self.assertGreater(stdin["bundle_bytes"], 0)
        func = by_id["3001"]
        self.assertEqual(func["func_name"], "doubleAll")
        self.assertEqual(func["expected_hashes"],
                         [digest(canonical_value([2, 4, 6])), digest(canonical_value([10, 2]))])
        self.assertEqual(func["public_examples"][0]["input"], "[[1, 2], 3]")
        self.assertEqual(func["public_examples"][0]["raw_input"], "[1, 2]\n3")
        self.assertEqual(func["public_examples"][0]["output"], "[2, 4, 6]")

    def test_fixtures_hold_problem_and_functional_starter(self):
        self.run_import([stdin_row("abc900_a"), functional_row("3001")])
        problem = (self.out / "fixtures/abc900_a/PROBLEM.md").read_text()
        self.assertIn("Read two integers and print their sum.", problem)
        self.assertIn("1 2", problem)  # the public example input
        self.assertFalse((self.out / "fixtures/abc900_a/solution.py").exists())
        starter = (self.out / "fixtures/3001/solution.py").read_text()
        self.assertIn("def doubleAll(self, nums: List[int], k: int) -> List[int]:", starter)
        self.assertIn("from typing import *", starter)  # LiveCodeBench preloads it, call.py does not
        # the private example is not in the statement fixture
        self.assertNotIn("[10, 2]", (self.out / "fixtures/3001/PROBLEM.md").read_text())

    def test_description_contains_statement_and_mode(self):
        stdin = import_lcb.description_for(stdin_row())
        self.assertIn("Sum Two", stdin)
        self.assertIn("Read two integers and print their sum.", stdin)
        self.assertIn("read standard input", stdin)
        self.assertIn("write standard output", stdin)
        self.assertTrue(stdin.rstrip().endswith("Write your solution to solution.py."))
        functional = import_lcb.description_for(functional_row())
        self.assertIn("Return each number doubled.", functional)
        self.assertIn("Implement `Solution.doubleAll`", functional)
        self.assertIn("typing", functional)
        self.assertTrue(functional.rstrip().endswith("Write your solution to solution.py."))
        self.assertNotIn("read standard input", functional)

    def test_unusable_tests_are_excluded_with_a_reason(self):
        bad_hash = functional_row("3002", private=[
            {"input": "[1]\n1", "output": "NaN", "testtype": "functional"}])
        bad_args = functional_row("3003", private=[
            {"input": "[1]\nnot json", "output": "[1]", "testtype": "functional"}])
        bad_private = stdin_row("abc900_c")
        bad_private["private_test_cases"] = b64z(pickle.dumps(_Evil(), protocol=4))
        unsafe = stdin_row("../escape")
        mismatch = stdin_row("abc900_d", private=[
            {"input": "1\n", "output": "1\n", "testtype": "functional"}])
        good = stdin_row("abc900_a")
        del _EXECUTED[:]
        self.run_import([good, bad_hash, bad_args, bad_private, unsafe, mismatch, good])
        self.assertEqual(_EXECUTED, [])
        self.assertEqual([e["id"] for e in self.pool()], ["abc900_a"])
        reasons = {}
        for item in self.report()["exclusions"]:
            reasons.setdefault(item["reason"], []).append(item["id"])
        self.assertEqual(reasons["no_canonical_expected_output"], ["3002"])
        self.assertEqual(reasons["bad_functional_input"], ["3003"])
        self.assertEqual(reasons["private_tests_undecodable"], ["abc900_c"])
        self.assertEqual(reasons["unsafe_id"], ["../escape"])
        self.assertEqual(reasons["test_type_mismatch"], ["abc900_d"])
        self.assertEqual(reasons["duplicate_id"], ["abc900_a"])
        self.assertFalse((self.root / "escape").exists())
        self.assertFalse((self.out / "graders" / "3002").exists())

    def test_report_counts_and_near_duplicates(self):
        statement = " ".join("word%d" % i for i in range(40))
        near = statement + " extra"
        rows = [
            stdin_row("abc900_a", title="A", content=statement, difficulty="easy"),
            stdin_row("abc900_b", title="B", content=near, difficulty="easy"),
            stdin_row("abc900_c", title="C", content=statement, difficulty="easy"),  # same as A
            stdin_row("abc900_d", title="D", content="Completely different text here.",
                      difficulty="hard"),
            functional_row("3001", difficulty="medium"),
        ]
        rows[2]["question_title"] = "A"
        self.run_import(rows)
        report = self.report()
        self.assertEqual(report["counts"], {
            "atcoder": {"easy": 2, "hard": 1}, "leetcode": {"medium": 1}})
        self.assertEqual(report["imported"], 4)
        self.assertEqual(report["rows_read"], 5)
        reasons = {item["id"]: item for item in report["exclusions"]}
        self.assertEqual(reasons["abc900_c"]["reason"], "duplicate_statement")
        self.assertEqual(reasons["abc900_c"]["duplicate_of"], "abc900_a")
        pairs = report["near_duplicates"]
        self.assertEqual(len(pairs), 1)
        self.assertEqual(sorted((pairs[0]["a"], pairs[0]["b"])), ["abc900_a", "abc900_b"])
        self.assertGreaterEqual(pairs[0]["jaccard"], 0.8)

    def test_bundle_grades_a_correct_solution_end_to_end(self):
        self.run_import([stdin_row("abc900_a"), functional_row("3001")])
        by_id = {entry["id"]: entry for entry in self.pool()}
        cases = {
            "abc900_a": "a, b = map(int, input().split())\nprint(a + b)\n",
            "3001": ("class Solution:\n"
                     "    def doubleAll(self, nums: List[int], k: int) -> List[int]:\n"
                     "        return [2 * x for x in nums] + [2 * k]\n"),
        }
        for qid, source in cases.items():
            work = self.root / ("work-" + qid)
            work.mkdir()
            solution = work / "solution.py"
            prefix = "from typing import *\n" if qid == "3001" else ""
            solution.write_text(prefix + source)
            out = self.root / ("observed-" + qid)
            bundle = self.out / "graders" / qid
            subprocess.run(
                [sys.executable, "-I", "grader.py", str(solution), str(out)],
                cwd=str(bundle), check=True, timeout=120)
            observed = json.loads((out / "observed.json").read_text())
            self.assertEqual(observed["hashes"], by_id[qid]["expected_hashes"], qid)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class DownloadTests(ImportTestCase):
    def test_download_checks_sha256(self):
        path = self.write_source("test6.jsonl", [stdin_row("abc900_a")])
        self.write_lock("test6.jsonl", path)
        # the file changes after the hash was recorded
        self.write_source("test6.jsonl", [stdin_row("abc900_a", content="Tampered statement.")])
        with self.assertRaises(import_lcb.LockMismatch):
            import_lcb.run_import(
                revision=REVISION, files=["test6.jsonl"], out=self.out,
                source_dir=self.source, lock_path=self.lock, errata_path=self.errata)
        self.assertFalse((self.out / "pool.json").exists())

    def test_source_dir_file_without_recorded_hash_is_refused(self):
        self.write_source("test6.jsonl", [stdin_row("abc900_a")])
        with self.assertRaises(import_lcb.LockMismatch):
            import_lcb.run_import(
                revision=REVISION, files=["test6.jsonl"], out=self.out,
                source_dir=self.source, lock_path=self.lock, errata_path=self.errata)
        self.assertFalse(self.lock.exists())

    def test_download_records_lock_then_verifies(self):
        body = (json.dumps(stdin_row("abc900_a")) + "\n").encode()
        urls = []

        def opener(request, timeout=None):
            urls.append(request.full_url if hasattr(request, "full_url") else request)
            return FakeResponse(body)

        cache = self.root / "cache"
        options = dict(revision=REVISION, files=["test6.jsonl"], out=self.out,
                       cache_dir=cache, lock_path=self.lock, errata_path=self.errata,
                       opener=opener)
        import_lcb.run_import(**options)
        self.assertEqual(urls, [
            "https://huggingface.co/datasets/livecodebench/code_generation_lite/resolve/%s/test6.jsonl"
            % REVISION])
        lock = json.loads(self.lock.read_text())
        recorded = lock["revisions"][REVISION]["files"]["test6.jsonl"]["sha256"]
        self.assertEqual(recorded, hashlib.sha256(body).hexdigest())
        self.assertEqual([e["id"] for e in self.pool()], ["abc900_a"])
        # a second run reuses the verified cache and does not download again
        import_lcb.run_import(**options)
        self.assertEqual(len(urls), 1)
        # a changed upstream file is refused
        shutil.rmtree(cache)
        other = body.replace(b"Sum Two", b"Sum Too")

        def changed(request, timeout=None):
            return FakeResponse(other)

        options["opener"] = changed
        with self.assertRaises(import_lcb.LockMismatch):
            import_lcb.run_import(**options)
        self.assertEqual(json.loads(self.lock.read_text()), lock)

    def test_revision_and_file_names_are_validated(self):
        for revision in ("main", "../x", REVISION[:-1]):
            with self.assertRaises(ValueError, msg=revision):
                import_lcb.run_import(
                    revision=revision, files=["test6.jsonl"], out=self.out,
                    source_dir=self.source, lock_path=self.lock, errata_path=self.errata)
        for name in ("../test6.jsonl", "a/b.jsonl", "test6.txt"):
            with self.assertRaises(ValueError, msg=name):
                import_lcb.run_import(
                    revision=REVISION, files=[name], out=self.out,
                    source_dir=self.source, lock_path=self.lock, errata_path=self.errata)


class CommandLineTests(ImportTestCase):
    def test_script_runs_from_the_checkout(self):
        path = self.write_source("test6.jsonl", [stdin_row("abc900_a")])
        self.write_lock("test6.jsonl", path)
        script = Path(import_lcb.__file__)
        done = subprocess.run(
            [sys.executable, str(script), "--revision", REVISION, "--files", "test6.jsonl",
             "--out", str(self.out), "--source-dir", str(self.source),
             "--lock-file", str(self.lock), "--errata-file", str(self.errata)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([e["id"] for e in self.pool()], ["abc900_a"])


if __name__ == "__main__":
    unittest.main()
