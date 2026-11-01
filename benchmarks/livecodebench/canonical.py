"""Canonical forms of LiveCodeBench outputs, and their hashes.

The importer hashes the expected outputs on the host and the grader hashes what a
candidate produced; the supervisor compares the two lists of hashes. Two outputs
get the same canonical form exactly when LiveCodeBench's own comparison
(``lcb_runner/evaluation/testing_util.py``) accepts one for the other. The
equivalence test in ``tests/lcb`` runs the vendored comparison over a corpus.

Stdin mode (``grade_stdio``): the whole output is stripped, split on ``\\n`` and
each line stripped. Lines are equal when the text is equal or when both split on
whitespace into the same list of ``Decimal`` values. So a line whose tokens all
parse as ``Decimal`` is encoded as its normalized values and any other line as its
stripped text. Corners where the reference differs from a naive reading:

- ``Decimal.normalize()`` rounds to the context precision, which would make
  ``50000000000000000000000000000000`` equal ``...01``; the reference keeps full
  precision, so the normal form is built from the decimal tuple instead.
- Zero has one form, whatever its sign or exponent (``-0 == 0`` for ``Decimal``).
- ``inf`` and ``Infinity`` are equal ``Decimal`` values; NaN is never equal to
  anything, not even itself, so a line with a NaN token only matches by text.

Functional mode (``grade_call_based``): ``prediction == expected`` after the
outermost tuple, and only that one, becomes a list. Python equality makes ``True``
equal ``1`` and ``1.0`` equal ``1``, so those collapse to one form; nested tuples
stay distinct from lists, as they do in the reference.
"""

import hashlib
import json
import math
from decimal import Decimal
from typing import Optional

_NUMBER_PREFIX = "d:"
_TEXT_PREFIX = "t:"
_TUPLE_KEY = "$t"


def digest(canonical: str) -> str:
    """Lowercase SHA-256 hex of the canonical form."""
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()


def _decimal_token(value: Decimal) -> str:
    """One string per numeric value, with no rounding."""
    if value.is_infinite():
        return "-Infinity" if value.is_signed() else "Infinity"
    if value.is_zero():
        return "0"
    sign, digits, exponent = value.as_tuple()
    text = "".join(str(d) for d in digits)
    stripped = text.rstrip("0")
    exponent += len(text) - len(stripped)
    return "%s%se%d" % ("-" if sign else "", stripped, exponent)


def _numeric_line(line: str) -> Optional[str]:
    """The canonical form of ``line`` when the reference compares it as decimals."""
    try:
        values = [Decimal(token) for token in line.split()]
    except Exception:
        return None
    if any(value.is_nan() for value in values):
        # NaN != NaN: only the exact text can match, as in the reference's case 1
        return None
    return _NUMBER_PREFIX + " ".join(_decimal_token(value) for value in values)


def canonical_stdout(text: str) -> str:
    lines = [line.strip() for line in text.strip().split("\n")]
    encoded = []
    for line in lines:
        numeric = _numeric_line(line)
        encoded.append(numeric if numeric is not None else _TEXT_PREFIX + line)
    return "\n".join(encoded)


def _plain(obj: object, top: bool = False) -> object:
    """Map ``obj`` to JSON data that is equal exactly when Python's ``==`` is."""
    if obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, bool):
        return int(obj)
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj):
            raise ValueError("NaN is never equal to anything and has no canonical form")
        if obj.is_integer():
            return int(obj)
        return obj
    if isinstance(obj, list):
        return [_plain(item) for item in obj]
    if isinstance(obj, tuple):
        items = [_plain(item) for item in obj]
        return items if top else {_TUPLE_KEY: items}
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError("dict keys must be strings, got %s" % type(key).__name__)
            # escape keys that start with "$" so the tuple tag key cannot be forged
            out["$" + key if key.startswith("$") else key] = _plain(value)
        return out
    raise TypeError("no canonical form for %s" % type(obj).__name__)


def canonical_value(obj: object) -> str:
    return json.dumps(_plain(obj, top=True), sort_keys=True, separators=(",", ":"))
