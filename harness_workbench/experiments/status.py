"""Explicit statuses and measurement provenance.

Execution, evaluation and decision outcomes are separate enums so a crash, a
task failure and an evaluator failure can never be conflated. Members are
``(str, Enum)`` (Python >= 3.10 has no ``StrEnum``); serialize with ``.value``.
"""

from __future__ import annotations

from enum import Enum


class ExecutionStatus(str, Enum):
    COMPLETED = "completed"
    TASK_FAILURE = "task_failure"
    EXECUTION_ERROR = "execution_error"
    TIMEOUT = "timeout"
    RESOURCE_LIMIT = "resource_limit"
    CANCELLED = "cancelled"


class EvaluationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    EVALUATION_ERROR = "evaluation_error"
    NOT_EVALUATED = "not_evaluated"


class DecisionStatus(str, Enum):
    ELIGIBLE = "eligible"
    REJECTED = "rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVALID_EXPERIMENT = "invalid_experiment"
    EXPLORATORY_ONLY = "exploratory_only"


class Provenance(str, Enum):
    MEASURED = "measured"
    PROVIDER_REPORTED = "provider_reported"
    ADAPTER_REPORTED = "adapter_reported"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


USABLE_EXECUTION = {ExecutionStatus.COMPLETED, ExecutionStatus.TASK_FAILURE}
USABLE_EVALUATION = {EvaluationStatus.PASSED, EvaluationStatus.FAILED}
