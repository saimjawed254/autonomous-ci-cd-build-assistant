"""Rule-based failure classification for CI/CD build logs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .parser import BuildLog


class FailureType(str, Enum):
    """Supported failure categories for the Week 1 prototype."""

    DEPENDENCY = "dependency_error"
    TEST = "test_failure"
    CONFIG = "config_issue"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FailureDiagnosis:
    """Structured classification result for a build log."""

    failure_type: FailureType
    confidence: str
    matched_pattern: str
    evidence: str
    suggested_fix: str


def classify_failure(build_log: BuildLog | str | Path) -> FailureDiagnosis:
    """Classify a build log into a small set of failure buckets.

    The current flow is:
    raw log content -> normalized lowercase text -> keyword/regex checks ->
    the first matching bucket becomes the diagnosis.
    """

    text = _extract_text(build_log)
    lowered = text.lower()

    patterns: list[tuple[FailureType, str, str, tuple[str, ...]]] = [
        (
            FailureType.DEPENDENCY,
            "dependency keywords",
            "Installation or package resolution failure",
            (
                "could not find a version",
                "no matching distribution",
                "module not found",
                "cannot import name",
                "dependency resolution failed",
            ),
        ),
        (
            FailureType.TEST,
            "test failure keywords",
            "A unit or integration test failed",
            (
                "failed tests",
                "test session starts",
                "assertionerror",
                "failed",
                "collected",
                "traceback",
            ),
        ),
        (
            FailureType.CONFIG,
            "config keywords",
            "Workflow, YAML, or environment configuration issue",
            (
                "workflow is not valid",
                "unrecognized named-value",
                "invalid yaml",
                "the workflow is not valid",
                "missing required property",
                "environment variable",
            ),
        ),
    ]

    for failure_type, matched_pattern, evidence, keywords in patterns:
        if any(keyword in lowered for keyword in keywords):
            return FailureDiagnosis(
                failure_type=failure_type,
                confidence="HIGH",
                matched_pattern=matched_pattern,
                evidence=evidence,
                suggested_fix=_suggest_fix(failure_type),
            )

    return FailureDiagnosis(
        failure_type=FailureType.UNKNOWN,
        confidence="MEDIUM",
        matched_pattern="no known pattern",
        evidence="No rule matched the current log text",
        suggested_fix=_suggest_fix(FailureType.UNKNOWN),
    )


def _extract_text(build_log: BuildLog | str | Path) -> str:
    if isinstance(build_log, BuildLog):
        return build_log.content
    if isinstance(build_log, Path):
        return build_log.read_text(encoding="utf-8-sig", errors="replace")
    return str(build_log)


def _suggest_fix(failure_type: FailureType) -> str:
    suggestions: dict[FailureType, str] = {
        FailureType.DEPENDENCY: (
            "Review the package name and version, then reinstall dependencies "
            "locally or update the lockfile before rerunning the pipeline."
        ),
        FailureType.TEST: (
            "Open the failing test, reproduce it locally, fix the assertion or "
            "test data, and rerun the suite before merging."
        ),
        FailureType.CONFIG: (
            "Inspect the workflow YAML, environment variables, and required "
            "secrets, then correct the configuration and rerun the job."
        ),
        FailureType.UNKNOWN: (
            "Inspect the full log, identify the first error line, and narrow the "
            "issue by checking dependencies, tests, and workflow configuration."
        ),
    }
    return suggestions[failure_type]