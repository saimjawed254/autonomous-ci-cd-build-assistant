"""Utilities for reading CI/CD build log files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BuildLog:
    """In-memory representation of a build log file."""

    path: Path
    content: str

    @property
    def line_count(self) -> int:
        """Return the number of lines in the log content."""

        if not self.content:
            return 0
        return self.content.count("\n") + (0 if self.content.endswith("\n") else 1)

    @property
    def character_count(self) -> int:
        """Return the number of characters in the log content."""

        return len(self.content)

    @property
    def is_empty(self) -> bool:
        """Return whether the log content is empty or whitespace only."""

        return not self.content.strip()


def read_build_log(log_path: str | Path) -> BuildLog:
    """Read a build log from disk using UTF-8 with replacement for bad bytes."""

    path = Path(log_path)
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    return BuildLog(path=path, content=content)