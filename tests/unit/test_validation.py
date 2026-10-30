import unittest

from harness_workbench.experiments.validation import (
    ValidationError,
    assert_finite_tree,
    json_dumps_strict,
    json_loads_strict,
    require_finite,
    require_int,
)


class RequireFiniteTests(unittest.TestCase):
    def test_require_finite_rejects_nan_inf_bool_and_strings(self):
        for bad in (float("nan"), float("inf"), float("-inf"), True, False, "1.5", None, [1], {}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    require_finite("x", bad)

    def test_require_finite_accepts_int_and_float_and_returns_float(self):
        self.assertEqual(require_finite("x", 3), 3.0)
        self.assertIsInstance(require_finite("x", 3), float)
        self.assertEqual(require_finite("x", -2.5), -2.5)

    def test_require_finite_rejects_huge_int_that_overflows_float(self):
        with self.assertRaises(ValidationError):
            require_finite("x", 10**400)

    def test_require_finite_range_and_exclusive_minimum(self):
        self.assertEqual(require_finite("x", 0.5, minimum=0, maximum=1), 0.5)
        with self.assertRaises(ValidationError):
            require_finite("x", 0, minimum=0, exclusive_minimum=True)
        self.assertEqual(require_finite("x", 0, minimum=0), 0.0)
        with self.assertRaises(ValidationError):
            require_finite("x", -0.1, minimum=0)
        with self.assertRaises(ValidationError):
            require_finite("x", 1.1, minimum=0, maximum=1)
        self.assertEqual(require_finite("x", 1, maximum=1), 1.0)

    def test_require_finite_allows_negative_without_minimum(self):
        self.assertEqual(require_finite("x", -1e9), -1e9)

    def test_error_names_the_field(self):
        with self.assertRaisesRegex(ValidationError, "my_field"):
            require_finite("my_field", float("nan"))


class RequireIntTests(unittest.TestCase):
    def test_require_int_rejects_bool_and_float(self):
        for bad in (True, False, 5.0, 5.5, "5", None, float("nan")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    require_int("n", bad)

    def test_require_int_accepts_int_and_range(self):
        self.assertEqual(require_int("n", 5), 5)
        self.assertEqual(require_int("n", -5), -5)
        self.assertEqual(require_int("n", 3, minimum=3, maximum=3), 3)
        with self.assertRaises(ValidationError):
            require_int("n", 2, minimum=3)
        with self.assertRaises(ValidationError):
            require_int("n", 4, maximum=3)


class AssertFiniteTreeTests(unittest.TestCase):
    def test_assert_finite_tree_names_path(self):
        with self.assertRaisesRegex(ValidationError, r"\$\.a\[1\]\.b"):
            assert_finite_tree({"a": [1, {"b": float("nan")}]})

    def test_assert_finite_tree_handles_tuples_and_infinity(self):
        with self.assertRaisesRegex(ValidationError, r"\$\.t\[2\]"):
            assert_finite_tree({"t": (1, 2, float("-inf"))})
        with self.assertRaisesRegex(ValidationError, r"^.*\$.*$"):
            assert_finite_tree(float("inf"))

    def test_assert_finite_tree_accepts_finite_and_non_numeric(self):
        assert_finite_tree({"a": [1, 2.5, "x", None, True, {"b": (0.0, -1)}]})

    def test_assert_finite_tree_custom_root_path(self):
        with self.assertRaisesRegex(ValidationError, r"root\.k"):
            assert_finite_tree({"k": float("nan")}, "root")

    def test_assert_finite_tree_reports_first_in_order(self):
        with self.assertRaisesRegex(ValidationError, r"\$\.a\b"):
            assert_finite_tree({"a": float("nan"), "b": float("inf")})


class JsonStrictTests(unittest.TestCase):
    def test_json_strict_roundtrip(self):
        with self.assertRaises(ValidationError):
            json_loads_strict('{"x": NaN}')
        with self.assertRaises(ValidationError):
            json_dumps_strict({"x": float("inf")})

    def test_json_loads_rejects_all_non_finite_constants(self):
        for text in ("NaN", "Infinity", "-Infinity", '[1, Infinity]'):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    json_loads_strict(text)

    def test_json_loads_accepts_valid_and_passes_through_syntax_errors(self):
        self.assertEqual(json_loads_strict('{"x": [1, 2.5, null]}'), {"x": [1, 2.5, None]})
        with self.assertRaises(ValueError):
            json_loads_strict("{not json")

    def test_json_dumps_forwards_kwargs_and_never_emits_nan(self):
        self.assertEqual(json_dumps_strict({"b": 1, "a": 2}, sort_keys=True), '{"a": 2, "b": 1}')
        with self.assertRaises(ValidationError):
            json_dumps_strict([float("nan")])
        with self.assertRaises(ValidationError):
            json_dumps_strict({"x": (1.0, float("nan"))}, indent=2)


if __name__ == "__main__":
    unittest.main()
