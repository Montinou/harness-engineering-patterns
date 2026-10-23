"""Strict numeric and JSON validation.

Every number that enters a manifest or leaves in a report passes through this
module. It rejects ``bool`` (a subclass of ``int``), NaN, and +/-Infinity, and
it refuses to read or write the non-standard JSON constants ``NaN``,
``Infinity`` and ``-Infinity``.
"""

from __future__ import annotations

import json
import math
from typing import Any


class ValidationError(ValueError):
    """Raised when a value is not a strictly valid finite number or JSON document."""


def require_finite(
    name: str,
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    """Return ``value`` as a finite float, or raise ``ValidationError``.

    Rejects ``bool``, non-numbers (including numeric strings), NaN, +/-Infinity
    and values outside ``minimum``/``maximum``. With ``exclusive_minimum`` the
    value must be strictly greater than ``minimum``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be a finite number, got {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError:
        raise ValidationError(f"{name} is too large to be a finite number") from None
    if not math.isfinite(number):
        raise ValidationError(f"{name} must be finite, got {number!r}")
    if minimum is not None:
        if exclusive_minimum:
            if not number > minimum:
                raise ValidationError(f"{name} must be > {minimum}, got {number!r}")
        elif number < minimum:
            raise ValidationError(f"{name} must be >= {minimum}, got {number!r}")
    if maximum is not None and number > maximum:
        raise ValidationError(f"{name} must be <= {maximum}, got {number!r}")
    return number


def require_int(
    name: str,
    value: Any,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Return ``value`` as an int, or raise ``ValidationError``.

    Rejects ``bool`` and floats (even ``5.0``) and strings.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer, got {type(value).__name__}")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{name} must be >= {minimum}, got {value!r}")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{name} must be <= {maximum}, got {value!r}")
    return value


def assert_finite_tree(value: Any, path: str = "$") -> None:
    """Walk dicts, lists and tuples; raise naming the path of the first non-finite float."""
    _walk(value, path, set())


def _walk(value: Any, path: str, active: set[int]) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"non-finite number {value!r} at {path}")
        return
    if isinstance(value, dict):
        marker = id(value)
        if marker in active:
            raise ValidationError(f"circular reference at {path}")
        active.add(marker)
        try:
            for key, item in value.items():
                _walk(item, f"{path}.{key}", active)
        finally:
            active.discard(marker)
    elif isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in active:
            raise ValidationError(f"circular reference at {path}")
        active.add(marker)
        try:
            for index, item in enumerate(value):
                _walk(item, f"{path}[{index}]", active)
        finally:
            active.discard(marker)


def _reject_constant(token: str) -> Any:
    raise ValidationError(f"non-finite JSON constant {token} is not allowed")


def json_loads_strict(text: str) -> Any:
    """``json.loads`` that raises ``ValidationError`` on NaN, Infinity and -Infinity."""
    return json.loads(text, parse_constant=_reject_constant)


def json_dumps_strict(value: Any, **kwargs: Any) -> str:
    """``json.dumps`` that refuses non-finite numbers (checked first, then ``allow_nan=False``)."""
    assert_finite_tree(value)
    kwargs["allow_nan"] = False
    return json.dumps(value, **kwargs)
