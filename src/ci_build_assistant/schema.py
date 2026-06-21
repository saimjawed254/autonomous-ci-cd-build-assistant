"""Shared diagnosis schema for the CI build assistant."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class FailureType(str, Enum):
    """Supported failure categories returned by the LLM."""

    DEPENDENCY = "dependency_error"
    TEST = "test_failure"
    CONFIG = "config_issue"
    OOM = "oom_error"
    NETWORK = "network_timeout"
    PERMISSION = "permission_denied"
    SECRET = "missing_secret"
    COMPILE = "compile_error"
    DISK = "disk_full"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FileChange:
    """A single file edit suggested by the LLM.

    Attributes:
        path: Relative file path (e.g. ``src/math_utils.py``).
        search: Exact code block to find in the file (empty for create/delete).
        replace: Code block to replace it with (empty for delete).
        action: One of ``"modify"``, ``"create"``, or ``"delete"``.
    """

    path: str
    search: str
    replace: str
    action: str = "modify"

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "search": self.search,
            "replace": self.replace,
            "action": self.action,
        }


@dataclass(frozen=True, slots=True)
class FailureDiagnosis:
    """Structured diagnosis payload returned to the CLI."""

    failure_type: FailureType
    confidence: str
    matched_pattern: str
    evidence: str
    root_cause: str
    fix_steps: tuple[str, ...]
    suggested_fix: str
    source: str = "gemini"
    raw_model_output: str | None = None
    file_changes: tuple[FileChange, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize the diagnosis to JSON-friendly data."""

        return {
            "failure_type": self.failure_type.value,
            "confidence": self.confidence,
            "matched_pattern": self.matched_pattern,
            "evidence": self.evidence,
            "root_cause": self.root_cause,
            "fix_steps": list(self.fix_steps),
            "suggested_fix": self.suggested_fix,
            "source": self.source,
            "raw_model_output": self.raw_model_output,
            "file_changes": [fc.to_dict() for fc in self.file_changes],
        }