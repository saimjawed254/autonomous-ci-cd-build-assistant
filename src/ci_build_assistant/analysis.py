"""High-level build log analysis with Gemini only."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

from .classifier import classify_failure
from .config import load_settings
from .llm_client import GeminiClient
from .parser import BuildLog
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .schema import FailureDiagnosis, FailureType, FileChange


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Final analysis payload returned to the CLI."""

    diagnosis: FailureDiagnosis
    used_llm: bool
    llm_enabled: bool
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = self.diagnosis.to_dict()
        data["used_llm"] = self.used_llm
        data["llm_enabled"] = self.llm_enabled
        data["error_message"] = self.error_message
        return data


def analyze_build_log(build_log: BuildLog, past_attempts: list[str] | None = None) -> AnalysisResult:
    """Analyze a build log using Gemini with a rule-based fallback."""

    settings = load_settings()
    client = GeminiClient(settings)

    llm_enabled = client.is_configured()

    if llm_enabled:
        try:
            user_prompt = build_user_prompt(build_log, past_attempts)
            response = client.generate_json(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt)
            diagnosis = _parse_llm_response(response.text)
            return AnalysisResult(
                diagnosis=diagnosis,
                used_llm=True,
                llm_enabled=True,
            )
        except Exception as exc:
            # Catch API errors, timeouts, or JSON parsing issues and degrade gracefully
            print(f"Warning: Gemini analysis failed ({exc}). Falling back to rule-based classifier.", file=sys.stderr)
            diagnosis = classify_failure(build_log)
            return AnalysisResult(
                diagnosis=diagnosis,
                used_llm=False,
                llm_enabled=True,
                error_message=str(exc),
            )
    else:
        # Fall back directly if API key is not configured
        print("Warning: GEMINI_API_KEY is not configured. Falling back to rule-based classifier.", file=sys.stderr)
        diagnosis = classify_failure(build_log)
        return AnalysisResult(
            diagnosis=diagnosis,
            used_llm=False,
            llm_enabled=False,
            error_message="GEMINI_API_KEY not set",
        )


def _parse_llm_response(text: str) -> FailureDiagnosis:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise RuntimeError("Gemini response did not contain valid JSON")

    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        raise RuntimeError("Gemini response JSON could not be parsed")

    return _diagnosis_from_payload(payload)


def _diagnosis_from_payload(payload: dict[str, Any]) -> FailureDiagnosis:
    failure_type = _parse_failure_type(payload.get("failure_type"))
    confidence = _normalized_text(payload.get("confidence"), default="UNCERTAIN")
    root_cause = _normalized_text(payload.get("root_cause"), default="Gemini did not provide a root cause.")
    evidence = _normalized_text(payload.get("evidence"), default="Gemini did not provide evidence.")
    fix_steps = _parse_fix_steps(payload.get("fix_steps"))
    file_changes = _parse_file_changes(payload.get("file_changes"))

    return FailureDiagnosis(
        failure_type=failure_type,
        confidence=confidence,
        matched_pattern="gemini-json",
        evidence=evidence,
        root_cause=root_cause,
        fix_steps=fix_steps,
        suggested_fix=_suggest_fix(fix_steps, root_cause),
        source="gemini",
        raw_model_output=json.dumps(payload, ensure_ascii=False),
        file_changes=file_changes,
    )


def _parse_failure_type(value: Any) -> FailureType:
    if isinstance(value, str):
        normalized = value.strip().lower()
        for failure_type in FailureType:
            if failure_type.value == normalized:
                return failure_type
    return FailureType.UNKNOWN


def _parse_fix_steps(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        steps = tuple(str(item).strip() for item in value if str(item).strip())
        if steps:
            return steps
    return (
        "Review the raw log and identify the first error line.",
        "Refine the prompt or fix the workflow/configuration based on the error.",
    )


def _normalized_text(value: Any, *, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _parse_file_changes(value: Any) -> tuple[FileChange, ...]:
    """Parse the file_changes array from the Gemini JSON payload."""

    if not isinstance(value, list):
        return ()
    changes = []
    for item in value:
        if not isinstance(item, dict):
            continue
        path = item.get("path", "").strip()
        if not path:
            continue
        changes.append(FileChange(
            path=path,
            search=item.get("search", ""),
            replace=item.get("replace", ""),
            action=item.get("action", "modify").strip().lower(),
        ))
    return tuple(changes)


def _suggest_fix(fix_steps: tuple[str, ...], root_cause: str) -> str:
    if fix_steps:
        return fix_steps[0]
    return root_cause