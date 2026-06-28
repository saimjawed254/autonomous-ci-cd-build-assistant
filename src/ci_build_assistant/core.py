"""Core structures, configurations, schemas, and API helpers for CI Build Assistant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib import error, request


# ===========================================================================
# 1. Log Parser Helpers
# ===========================================================================

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


# ===========================================================================
# 2. Configuration Settings
# ===========================================================================

@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration settings for Gemini and GitHub."""

    gemini_api_key: str | None
    gemini_model: str
    gemini_temperature: float
    gemini_timeout_seconds: float
    gemini_max_output_tokens: int

    github_token: str | None
    github_repository: str | None
    github_run_id: int | None
    github_pr_number: int | None
    context_strategy: str


def load_settings(project_root: Path | None = None) -> Settings:
    """Load settings from `.env` and the current process environment."""

    root = project_root or Path.cwd()
    dotenv_path = root / ".env"
    _load_dotenv_file(dotenv_path)

    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-1.5-flash"),
        gemini_temperature=_coerce_float(os.getenv("GEMINI_TEMPERATURE"), default=0.2),
        gemini_timeout_seconds=_coerce_float(os.getenv("GEMINI_TIMEOUT_SECONDS"), default=120.0),
        gemini_max_output_tokens=_coerce_int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS"), default=8192),
        github_token=os.getenv("GITHUB_TOKEN"),
        github_repository=os.getenv("GITHUB_REPOSITORY"),
        github_run_id=_coerce_int(os.getenv("GITHUB_RUN_ID"), default=None),
        github_pr_number=_coerce_int(os.getenv("GITHUB_PR_NUMBER"), default=None),
        context_strategy=os.getenv("CONTEXT_STRATEGY", "snippet"),
    )


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _coerce_float(value: str | None, *, default: float | None) -> float | None:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def _coerce_int(value: str | None, *, default: int | None) -> int | None:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


# ===========================================================================
# 3. Diagnosis & Change Schemas
# ===========================================================================

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
    """A single file edit suggested by the LLM."""

    path: str
    search: str
    replace: str
    action: str = "modify"
    error_type: str = "unknown"

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "search": self.search,
            "replace": self.replace,
            "action": self.action,
            "error_type": self.error_type,
        }


@dataclass(frozen=True, slots=True)
class FailureDiagnosis:
    """Structured diagnosis payload returned to the CLI."""

    failure_types: tuple[FailureType, ...]
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
            "failure_types": [ft.value for ft in self.failure_types],
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


# ===========================================================================
# 4. Attempt History & Memory Store
# ===========================================================================

def get_log_signature(content: str) -> str:
    """Generate a unique SHA-256 error signature from log contents."""

    # Normalize whitespaces to avoid minor formatting differences altering the hash
    normalized = " ".join(content.split())
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()


def load_history(history_path: Path) -> dict:
    """Load the JSON database dictionary, returning empty dict if missing."""

    if not history_path.exists():
        return {}
    try:
        return json.loads(history_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_history(history_path: Path, data: dict) -> None:
    """Write the dictionary history database to disk."""

    try:
        history_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"Warning: could not write history state: {exc}")


def get_past_attempts(history_path: Path, log_signature: str) -> list[str]:
    """Retrieve all fix suggestions previously attempted for this log signature."""

    history = load_history(history_path)
    entry = history.get(log_signature, {})
    attempts = entry.get("attempts", [])
    return [item["fix_suggestion"] for item in attempts if item.get("fix_suggestion")]


def record_attempt(history_path: Path, log_signature: str, fix_suggestion: str, status: str) -> None:
    """Record or update a fix suggestion attempt in history."""

    history = load_history(history_path)
    if log_signature not in history:
        history[log_signature] = {
            "first_seen": datetime.now().isoformat(timespec="seconds"),
            "attempts": []
        }

    attempts = history[log_signature]["attempts"]
    for attempt in attempts:
        if attempt.get("fix_suggestion") == fix_suggestion:
            attempt["status"] = status
            attempt["updated_at"] = datetime.now().isoformat(timespec="seconds")
            save_history(history_path, history)
            return

    attempts.append({
        "fix_suggestion": fix_suggestion,
        "status": status,
        "timestamp": datetime.now().isoformat(timespec="seconds")
    })
    save_history(history_path, history)


# ===========================================================================
# 5. GitHub API Integrations
# ===========================================================================

def post_pr_comment(repo: str, pr_number: int, comment: str, token: str) -> None:
    """Post a diagnostic report comment onto a GitHub Pull Request."""

    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    payload = {"body": comment}
    body = json.dumps(payload).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "CI-Build-Assistant",
        "Content-Type": "application/json",
    }

    http_request = request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=15) as response:
            response.read()
    except error.URLError as exc:
        raise RuntimeError(f"Failed to post PR comment: {exc}") from exc


def get_pr_comments(repo: str, pr_number: int, token: str) -> list[str]:
    """Retrieve all comments posted on a Pull Request issues thread."""

    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "CI-Build-Assistant",
    }

    http_request = request.Request(
        url,
        headers=headers,
        method="GET",
    )

    try:
        with request.urlopen(http_request, timeout=15) as response:
            raw_bytes = response.read()
    except error.URLError as exc:
        raise RuntimeError(f"Failed to fetch PR comments: {exc}") from exc

    try:
        comments_list = json.loads(raw_bytes.decode("utf-8"))
        if isinstance(comments_list, list):
            return [str(comment.get("body", "")) for comment in comments_list if comment.get("body")]
    except (json.JSONDecodeError, ValueError):
        pass

    return []


def get_pr_branch(repo: str, pr_number: int, token: str) -> str:
    """Retrieve the branch name (head.ref) for a given Pull Request."""

    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "CI-Build-Assistant",
    }

    http_request = request.Request(
        url,
        headers=headers,
        method="GET",
    )

    try:
        with request.urlopen(http_request, timeout=15) as response:
            raw_bytes = response.read()
    except error.URLError as exc:
        raise RuntimeError(f"Failed to fetch PR branch details: {exc}") from exc

    try:
        data = json.loads(raw_bytes.decode("utf-8"))
        if isinstance(data, dict) and "head" in data and "ref" in data["head"]:
            return str(data["head"]["ref"])
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        raise RuntimeError(f"Failed to parse PR branch details response: {exc}") from exc

    raise RuntimeError("PR branch name not found in GitHub response.")


def post_comment_reaction(repo: str, comment_id: int, token: str, content: str = "eyes") -> None:
    """Post an emoji reaction to a GitHub comment to show that processing has started."""

    url = f"https://api.github.com/repos/{repo}/issues/comments/{comment_id}/reactions"
    payload = {"content": content}
    body = json.dumps(payload).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.add-reactions+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "CI-Build-Assistant",
        "Content-Type": "application/json",
    }

    http_request = request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=10) as response:
            response.read()
    except Exception as exc:
        print(f"Warning: Failed to post comment reaction: {exc}", file=sys.stderr)


def get_pr_changed_files(repo: str, pr_number: int, token: str) -> list[str]:
    """Retrieve the list of file paths changed in a Pull Request."""

    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files?per_page=100"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "CI-Build-Assistant",
    }

    http_request = request.Request(
        url,
        headers=headers,
        method="GET",
    )

    try:
        with request.urlopen(http_request, timeout=15) as response:
            raw_bytes = response.read()
    except error.URLError as exc:
        raise RuntimeError(f"Failed to fetch PR changed files: {exc}") from exc

    try:
        files_list = json.loads(raw_bytes.decode("utf-8"))
        if isinstance(files_list, list):
            return [str(f.get("filename", "")) for f in files_list if f.get("filename")]
    except (json.JSONDecodeError, ValueError):
        pass

    return []
