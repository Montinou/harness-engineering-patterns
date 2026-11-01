"""The canonicalizer must reach the same verdicts as LiveCodeBench's comparison."""

import hashlib
import random
import unittest

from benchmarks.livecodebench.canonical import (
    canonical_stdout,
    canonical_value,
    digest,
)
from tests.lcb.lcb_reference import (
    functional_outputs_match,
    stdio_outputs_match,
)

STDOUT_PAIRS = [
    # identical and whitespace at the edges
    ("1\n", "1\n"),
    ("1", "1\n"),
    ("1\n\n\n", "1"),
    ("  1  \n", "1\n"),
    ("1   \n2   \n", "1\n2\n"),
    ("\n\n1\n", "1\n"),
    ("1\r\n2\r\n", "1\n2\n"),
    ("\t7\t\n", "7"),
    # numeric spellings
    ("1.0\n", "1\n"),
    ("1e3\n", "1000\n"),
    ("1E3\n", "1000\n"),
    ("-0\n", "0\n"),
    ("-0.0\n", "0\n"),
    ("0.0\n", "0\n"),
    ("0e5\n", "0\n"),
    ("1.50\n", "1.5\n"),
    ("100\n", "1e2\n"),
    ("0.1\n", "0.10\n"),
    ("+5\n", "5\n"),
    ("1_0\n", "10\n"),
    ("0.3333333333\n", "0.3333333334\n"),
    ("50000000000000000000000000000000\n", "50000000000000000000000000000001\n"),
    ("5" + "0" * 40 + "\n", "5e40\n"),
    ("1e-30\n", "0.000000000000000000000000000001\n"),
    ("1e999999\n", "1e999999\n"),
    ("1e999999\n", "1e999998\n"),
    ("inf\n", "Infinity\n"),
    ("-inf\n", "-Infinity\n"),
    ("inf\n", "-inf\n"),
    ("nan\n", "nan\n"),
    ("nan\n", "NaN\n"),
    ("1 nan\n", "1 nan\n"),
    ("1 nan\n", "1.0 nan\n"),
    ("2\n", "3\n"),
    # token lists on one line
    ("1  2   3\n", "1 2 3\n"),
    ("1 2 3\n", "1.0 2.00 3e0\n"),
    ("1 2 3\n", "1 2\n"),
    ("1 2 3\n", "1 3 2\n"),
    ("1 2 3\n", "1 2 3 4\n"),
    ("1 -0 3\n", "1 0 3\n"),
    # text lines
    ("Yes\n", "Yes\n"),
    ("Yes\n", "yes\n"),
    ("Yes\n", "No\n"),
    ("a  b\n", "a b\n"),
    ("a b\n", "a  b\n"),
    ("hello world\n", "hello world   \n"),
    ("x y z\n", "x y z\n"),
    ("one\ttwo\n", "one two\n"),
    ("Case #1: 3\n", "Case #1: 3.0\n"),
    ("Case #1: 3\n", "Case #1: 4\n"),
    # mixed numeric and text lines
    ("3\nabc\n4.0\n", "3.0\nabc\n4\n"),
    ("3\nabc\n4.0\n", "3.0\nabd\n4\n"),
    ("3\nabc 4\n", "3\nabc 4.0\n"),
    ("3\nabc\n", "3\n1\n"),
    ("1 a\n", "1.0 a\n"),
    ("a 1\n", "a 1.0\n"),
    # empty output and blank lines
    ("", ""),
    ("", "\n"),
    ("\n\n", ""),
    ("   \n", ""),
    ("", "0\n"),
    ("1\n\n2\n", "1\n2\n"),
    ("1\n\n2\n", "1\n\n2\n"),
    ("1\n2\n\n", "1\n2\n"),
    ("1\n  \n2\n", "1\n\n2\n"),
    ("1\n2\n3\n", "1\n2\n"),
    ("1\n2\n", "1\n2\n\n\n3\n"),
]

VALUE_PAIRS = [
    # tuple vs list
    ((1, 2), [1, 2]),
    ((1, 2), [2, 1]),
    ((), []),
    ((1,), [1]),
    ([1, 2], [1, 2]),
    ([1, 2], [1, 2, 3]),
    # nested tuples: the reference only converts the outermost tuple
    ([(1, 2)], [[1, 2]]),
    (([1, 2], [3]), [[1, 2], [3]]),
    (((1, 2), (3,)), [[1, 2], [3]]),
    ([[1, 2], (3,)], [[1, 2], [3]]),
    ({"a": (1, 2)}, {"a": [1, 2]}),
    ({"a": [1, 2]}, {"a": [1, 2]}),
    # dict key order
    ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
    ({"a": 1, "b": 2}, {"a": 1, "b": 3}),
    ({"a": 1}, {"a": 1, "b": 2}),
    ({"a": {"x": 1, "y": 2}}, {"a": {"y": 2, "x": 1}}),
    ({"$t": [1]}, {"$t": [1]}),
    ({"$t": [1]}, {"$$t": [1]}),
    ({"$t": [1]}, [1]),
    # float vs int and bool vs int
    (1.0, 1),
    (1, 1.0),
    (2.5, 2.5),
    (2.5, 2),
    (0.1, 0.1),
    (0.1, 0.10000000000000002),
    (-0.0, 0),
    (-0.0, 0.0),
    (1e20, 10**20),
    (1e300, 10**300),
    (10**20 + 1, 10**20),
    (float("inf"), float("inf")),
    (float("inf"), float("-inf")),
    (True, 1),
    (False, 0),
    (True, 0),
    (True, True),
    ([True, False], [1, 0]),
    ([1.0, 2.0], [1, 2]),
    ({"a": True}, {"a": 1}),
    ({"a": 1.0}, {"a": 1}),
    # scalars and null
    (None, None),
    (None, 0),
    (None, False),
    ("a", "a"),
    ("a", "b"),
    ("1", 1),
    ("", ""),
    ("", None),
    ("é", "é"),
    # nesting
    ([[1, 2], [3, 4]], [[1, 2], [3, 4]]),
    ([[1, 2], [3, 4]], [[1, 2], [4, 3]]),
    ([[], []], [[], []]),
    ([[]], [[], []]),
    ([], {}),
    ([], None),
    ([1, [2, [3, 4.0]]], [1, [2, [3, 4]]]),
    ([{"k": [1.0]}], [{"k": [1]}]),
]


class StdoutEquivalence(unittest.TestCase):
    def test_corpus_is_large_and_has_both_verdicts(self):
        self.assertGreaterEqual(len(STDOUT_PAIRS), 40)
        verdicts = {stdio_outputs_match(a, b) for a, b in STDOUT_PAIRS}
        self.assertEqual(verdicts, {True, False})

    def test_stdout_equivalence_matches_reference(self):
        for actual, expected in STDOUT_PAIRS:
            with self.subTest(actual=actual, expected=expected):
                want = stdio_outputs_match(actual, expected)
                got = digest(canonical_stdout(actual)) == digest(
                    canonical_stdout(expected)
                )
                self.assertEqual(got, want)
                # the reference verdict is symmetric, so the hashes must be too
                self.assertEqual(
                    digest(canonical_stdout(expected))
                    == digest(canonical_stdout(actual)),
                    want,
                )

    def test_random_numeric_and_text_lines_match_reference(self):
        rng = random.Random(20261101)
        tokens = [
            "0", "-0", "0.0", "1", "1.0", "1.00", "10", "1e1", "1E1", "0.5", ".5",
            "5e-1", "-1", "+1", "a", "A", "x1", "1x", "inf", "-Infinity", "nan",
            "1_0", "007",
        ]
        seps = [" ", "  ", "\t", " \t "]
        ends = ["", " ", "\n", "  \n", "\n\n"]

        def make():
            lines = []
            for _ in range(rng.randint(0, 3)):
                n = rng.randint(0, 3)
                lines.append(rng.choice(seps).join(rng.choice(tokens) for _ in range(n)))
            return "\n".join(lines) + rng.choice(ends)

        checked = 0
        for _ in range(4000):
            a = make()
            # half the time derive b from a so equal verdicts are well represented
            b = make() if rng.random() < 0.5 else a.replace("1.0", "1").replace(" ", "  ")
            with self.subTest(a=a, b=b):
                want = stdio_outputs_match(a, b)
                got = canonical_stdout(a) == canonical_stdout(b)
                self.assertEqual(got, want)
            checked += 1
        self.assertEqual(checked, 4000)

    def test_large_integers_keep_full_precision(self):
        a = "50000000000000000000000000000000"
        b = "50000000000000000000000000000001"
        self.assertNotEqual(canonical_stdout(a), canonical_stdout(b))

    def test_text_line_cannot_collide_with_numeric_encoding(self):
        # whatever the numeric encoding looks like, a text line spelled like it differs
        numeric = canonical_stdout("1000")
        self.assertNotEqual(numeric, canonical_stdout(numeric))
        self.assertNotEqual(canonical_stdout("1e3"), canonical_stdout("1e3x"))


class ValueEquivalence(unittest.TestCase):
    def test_corpus_has_both_verdicts(self):
        verdicts = {functional_outputs_match(a, b) for a, b in VALUE_PAIRS}
        self.assertEqual(verdicts, {True, False})

    def test_value_equivalence_matches_reference(self):
        for actual, expected in VALUE_PAIRS:
            with self.subTest(actual=actual, expected=expected):
                want = functional_outputs_match(actual, expected)
                got = digest(canonical_value(actual)) == digest(canonical_value(expected))
                self.assertEqual(got, want)

    def test_unrepresentable_values_raise(self):
        # NaN never equals anything in the reference, so it has no hash to match
        with self.assertRaises(ValueError):
            canonical_value(float("nan"))
        with self.assertRaises(TypeError):
            canonical_value({1: "a"})
        with self.assertRaises(TypeError):
            canonical_value({1, 2})

    def test_canonical_value_is_compact_sorted_json_for_plain_data(self):
        self.assertEqual(canonical_value({"b": [1, 2], "a": None}), '{"a":null,"b":[1,2]}')
        self.assertEqual(canonical_value((1, 2)), "[1,2]")


class Digest(unittest.TestCase):
    def test_digest_is_lowercase_sha256_hex_of_utf8(self):
        self.assertEqual(digest("abc"), hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(digest("é"), hashlib.sha256("é".encode("utf-8")).hexdigest())
        d = digest("")
        self.assertEqual(len(d), 64)
        self.assertEqual(d, d.lower())

    def test_digest_accepts_lone_surrogates(self):
        self.assertEqual(len(digest("\ud800")), 64)
        self.assertNotEqual(digest("\ud800"), digest("\ud801"))


if __name__ == "__main__":
    unittest.main()
