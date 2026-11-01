"""Vendored LiveCodeBench output comparison, used only as a test oracle.

Source: https://github.com/LiveCodeBench/LiveCodeBench
        lcb_runner/evaluation/testing_util.py (``grade_stdio`` and ``grade_call_based``)

MIT License

Copyright (c) 2024 LiveCodeBench

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Changes from upstream: only the comparison logic is kept. ``get_stripped_lines``
and ``convert_line_to_decimals`` are verbatim. The per-test comparison inside
``grade_stdio`` (after the candidate ran) is lifted into ``stdio_outputs_match``
and the one in ``grade_call_based`` into ``functional_outputs_match``; the code
execution, signal, capture and mock machinery is not vendored. Upstream imports
numpy at module level but never uses it in the comparison, so nothing replaces
it: this module is stdlib only.
"""

from decimal import Decimal


def convert_line_to_decimals(line: str) -> tuple[bool, list[Decimal]]:
    try:
        decimal_line = [Decimal(elem) for elem in line.split()]
    except:
        return False, []
    return True, decimal_line


def get_stripped_lines(val: str):
    ## you don't want empty lines to add empty list after splitlines!
    val = val.strip()

    return [val_line.strip() for val_line in val.split("\n")]


def stdio_outputs_match(prediction: str, gt_out: str) -> bool:
    """The verdict ``grade_stdio`` reaches for one test: True when it keeps going
    (accepted), False when it records a wrong answer (-2)."""
    stripped_prediction_lines = get_stripped_lines(prediction)
    stripped_gt_out_lines = get_stripped_lines(gt_out)

    if len(stripped_prediction_lines) != len(stripped_gt_out_lines):
        return False

    for output_line_idx, (
        stripped_prediction_line,
        stripped_gt_out_line,
    ) in enumerate(zip(stripped_prediction_lines, stripped_gt_out_lines)):
        ## CASE 1: exact match
        if stripped_prediction_line == stripped_gt_out_line:
            continue

        ## CASE 2: element-wise comparision
        success, decimal_prediction_line = convert_line_to_decimals(
            stripped_prediction_line
        )
        if not success:
            return False
        success, decimal_gtout_line = convert_line_to_decimals(stripped_gt_out_line)
        if not success:
            return False

        if decimal_prediction_line == decimal_gtout_line:
            continue

        return False
    return True


def functional_outputs_match(prediction, gt_out) -> bool:
    """The verdict ``grade_call_based`` reaches for one test."""
    # don't penalize model if it produces tuples instead of lists
    # ground truth sequences are not tuples
    if isinstance(prediction, tuple):
        prediction = list(prediction)

    return prediction == gt_out
