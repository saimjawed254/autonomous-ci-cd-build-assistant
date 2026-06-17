"""Rule-based failure classification engine for fallback operations."""

from __future__ import annotations

from pathlib import Path

from .parser import BuildLog
from .schema import FailureDiagnosis, FailureType


def classify_failure(build_log: BuildLog | str | Path) -> FailureDiagnosis:
    """Classify a build log using regex/keyword matching rules (Fallback Mode)."""

    text = _extract_text(build_log)
    lowered = text.lower()

    # Define keyword rules for all 10 failure categories in priority order
    rules = [
        (
            FailureType.OOM,
            ("out of memory", "exhausted", "exit code 137", "killed"),
            "OOM Error",
            "Build process exceeded memory limits.",
            (
                "Increase the runner or container memory limits.",
                "Optimize build processes or data loaders to use streaming.",
                "Check for memory leaks during compilation or runtime."
            )
        ),
        (
            FailureType.NETWORK,
            ("connection timed out", "timed out", "timeout", "could not connect", "network unreachable"),
            "Network Timeout",
            "A network timeout occurred while downloading resources.",
            (
                "Verify that external registries and endpoints are active.",
                "Check connection and firewall rules of the runner.",
                "Increase network timeout thresholds or add retry blocks."
            )
        ),
        (
            FailureType.PERMISSION,
            ("permission denied", "unauthorized", "forbidden", "access denied", "403 forbidden", "401 unauthorized"),
            "Permission Denied",
            "Authorization check failed or access was denied.",
            (
                "Verify API tokens and repository secrets are correct.",
                "Confirm write/read scopes on keys or access tokens.",
                "Check directory permissions on the runner."
            )
        ),
        (
            FailureType.SECRET,
            ("missing required environment variable", "configure secret", "secret is not set", "unconfigured secret"),
            "Missing Secret",
            "A required secret or environment variable is missing.",
            (
                "Navigate to Repository Settings > Secrets and variables > Actions.",
                "Ensure the secret name matches exactly what the code expects.",
                "Verify the secret value is correctly populated."
            )
        ),
        (
            FailureType.COMPILE,
            ("compile error", "compilation failed", "error: aborting due to", "rustc failed", "syntax error"),
            "Compile Error",
            "Source compilation failed due to compiler or syntax errors.",
            (
                "Locate the syntax or compile error line in logs.",
                "Reproduce the build compilation error locally.",
                "Fix formatting, missing imports, or type definitions in code."
            )
        ),
        (
            FailureType.DISK,
            ("no space left on device", "disk full", "out of disk space", "write error"),
            "Disk Full",
            "Runner ran out of disk space during build execution.",
            (
                "Clear build artifacts, temporary caches, and old images.",
                "Verify files generated dynamically don't bloat storage.",
                "Increase disk size for the runner environment."
            )
        ),
        (
            FailureType.DEPENDENCY,
            ("could not find a version", "no matching distribution", "module not found", "cannot import name", "dependency resolution failed"),
            "Dependency Error",
            "Installation or package resolution failure occurred.",
            (
                "Verify package name and version constraints in requirements.txt.",
                "Check if package exists in the PyPI registry or remote indexes.",
                "Reinstall dependencies locally to verify package compatibility."
            )
        ),
        (
            FailureType.TEST,
            ("failed tests", "test session starts", "assertionerror", "failed", "traceback"),
            "Test Failure",
            "One or more unit or integration test assertions failed.",
            (
                "Open the failing test file in your editor.",
                "Reproduce the test failure locally in your terminal.",
                "Update the test code or verify actual logic satisfies assertion."
            )
        ),
        (
            FailureType.CONFIG,
            ("workflow is not valid", "unrecognized named-value", "invalid yaml", "missing required property"),
            "Config Issue",
            "Workflow configuration file contains syntax errors or invalid formats.",
            (
                "Inspect the workflow YAML configuration configuration.",
                "Correct invalid syntax references or missing schema keys.",
                "Validate workflow settings against official documentation."
            )
        ),
    ]

    # Search for first matching rule
    for failure_type, keywords, matched_pattern, root_cause, fix_steps in rules:
        for keyword in keywords:
            if keyword in lowered:
                # Extract the line containing the keyword as evidence
                evidence = _find_matching_line(text, keyword)
                return FailureDiagnosis(
                    failure_type=failure_type,
                    confidence="MEDIUM",
                    matched_pattern=matched_pattern,
                    evidence=evidence,
                    root_cause=root_cause,
                    fix_steps=fix_steps,
                    suggested_fix=fix_steps[0],
                    source="rule-based"
                )

    # Fallback to UNKNOWN if no keywords matched
    return FailureDiagnosis(
        failure_type=FailureType.UNKNOWN,
        confidence="UNCERTAIN",
        matched_pattern="no matches",
        evidence="No rule keywords matched the logs.",
        root_cause="The log did not match any known failure categories.",
        fix_steps=(
            "Inspect full build output starting from first error line.",
            "Verify dependencies, local setup, and configs manually."
        ),
        suggested_fix="Inspect full build output starting from first error line.",
        source="rule-based"
    )


def _extract_text(build_log: BuildLog | str | Path) -> str:
    if isinstance(build_log, BuildLog):
        return build_log.content
    if isinstance(build_log, Path):
        return build_log.read_text(encoding="utf-8-sig", errors="replace")
    return str(build_log)


def _find_matching_line(text: str, keyword: str) -> str:
    """Find the first line containing the matching keyword."""
    for line in text.splitlines():
        if keyword in line.lower():
            return line.strip()
    return f"Logs contain matching pattern: {keyword}"
