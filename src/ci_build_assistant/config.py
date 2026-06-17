"""Configuration loading for the Gemini-backed Week 2 flow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


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


def load_settings(project_root: Path | None = None) -> Settings:
    """Load settings from `.env` and the current process environment."""

    root = project_root or Path.cwd()
    dotenv_path = root / ".env"
    _load_dotenv_file(dotenv_path)

    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-1.5-flash"),
        gemini_temperature=_coerce_float(os.getenv("GEMINI_TEMPERATURE"), default=0.2),
        gemini_timeout_seconds=_coerce_float(os.getenv("GEMINI_TIMEOUT_SECONDS"), default=30.0),
        gemini_max_output_tokens=_coerce_int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS"), default=1024),
        github_token=os.getenv("GITHUB_TOKEN"),
        github_repository=os.getenv("GITHUB_REPOSITORY"),
        github_run_id=_coerce_int(os.getenv("GITHUB_RUN_ID"), default=None),
        github_pr_number=_coerce_int(os.getenv("GITHUB_PR_NUMBER"), default=None),
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