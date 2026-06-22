"""Gemini API client and prompt templates for structured build failure diagnosis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from textwrap import dedent
from urllib import error, request

from .core import Settings, BuildLog


@dataclass(frozen=True, slots=True)
class GeminiResponse:
    """Raw response payload from Gemini."""

    text: str
    raw_json: dict


# ===========================================================================
# 1. System & User Prompt Templates
# ===========================================================================

SYSTEM_PROMPT = dedent(
    """
    You are a CI/CD build failure assistant.
    Return only valid JSON.
    Your output must match this schema:
    {
      "failure_type": "dependency_error|test_failure|config_issue|oom_error|network_timeout|permission_denied|missing_secret|compile_error|disk_full|unknown",
      "root_cause": "short explanation",
      "fix_steps": ["step 1", "step 2"],
      "confidence": "HIGH|MEDIUM|UNCERTAIN",
      "evidence": "log line or pattern used",
      "file_changes": [
        {
          "path": "relative/path/to/file.py",
          "search": "exact code block to find",
          "replace": "exact code block to replace it with",
          "action": "modify"
        }
      ]
    }

    Rules for file_changes:
    - Prioritize providing exact code fixes in "file_changes" whenever possible, especially for test assertion mismatches, compilation errors, and simple code bugs that are visible in stack traces.
    - If the log shows a test assertion failing (e.g. expected value vs received value), prioritize suggesting the code fix that updates the assertion or test to pass in the CI environment.
    - For missing dependencies (e.g., module not found, failed to load url), suggest adding the missing package to package.json (or requirements.txt) under dependencies with a suitable version constraint (e.g., "^1.0.0"), or suggest removing the invalid import statement if it is not used.
    - The "search" field MUST exactly match existing code in the file, including all whitespace and indentation. Do NOT guess or paraphrase.
    - The "action" field must be "modify", "create", or "delete".
    - For "create": "search" should be empty, "replace" contains the full file content.
    - For "delete": "replace" should be empty, "search" should be empty.
    - Limit changes to at most 3 files and at most 50 lines per search/replace block.
    - If you cannot determine the exact code to change, return an empty file_changes array [].
    Keep fix steps short, direct, and actionable.
    """
).strip()


def build_user_prompt(build_log: BuildLog, past_attempts: list[str] | None = None) -> str:
    """Build the user prompt directly from the parsed log."""

    excerpt = _build_excerpt(build_log.content)
    
    attempts_context = ""
    if past_attempts:
        attempts_context = "\nIMPORTANT: The following fix suggestions were already tried and FAILED. DO NOT suggest these again. Find an alternative root cause or different fix steps:\n"
        for attempt in past_attempts:
            attempts_context += f"- {attempt}\n"

    return dedent(
        f"""
        Build log excerpt:
        {excerpt}
        {attempts_context}
        Diagnose the failure directly from the log.
        Return JSON only.
        """
    ).strip()


def _build_excerpt(content: str, limit: int = 4000) -> str:
    if len(content) <= limit:
        return content
    return "...[truncated]...\n" + content[-limit:]


# ===========================================================================
# 2. Gemini API Client
# ===========================================================================

class GeminiClient:
    """Minimal Gemini REST client built on the Python standard library."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def is_configured(self) -> bool:
        return bool(self._settings.gemini_api_key)

    def generate_json(self, *, system_prompt: str, user_prompt: str) -> GeminiResponse:
        if not self._settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._settings.gemini_model}:generateContent?key={self._settings.gemini_api_key}"
        )
        payload = {
            "systemInstruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt}],
                }
            ],
            "generationConfig": {
                "temperature": self._settings.gemini_temperature,
                "maxOutputTokens": self._settings.gemini_max_output_tokens,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "object",
                    "properties": {
                        "failure_type": {
                            "type": "string",
                            "enum": [
                                "dependency_error",
                                "test_failure",
                                "config_issue",
                                "oom_error",
                                "network_timeout",
                                "permission_denied",
                                "missing_secret",
                                "compile_error",
                                "disk_full",
                                "unknown"
                            ]
                        },
                        "root_cause": {"type": "string"},
                        "fix_steps": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "confidence": {"type": "string"},
                        "evidence": {"type": "string"},
                        "file_changes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "search": {"type": "string"},
                                    "replace": {"type": "string"},
                                    "action": {"type": "string"},
                                },
                                "required": ["path", "search", "replace", "action"],
                            },
                        },
                      },
                      "required": [
                          "failure_type",
                          "root_cause",
                          "fix_steps",
                          "confidence",
                          "evidence",
                          "file_changes",
                      ],
                      "propertyOrdering": [
                          "failure_type",
                          "root_cause",
                          "fix_steps",
                          "confidence",
                          "evidence",
                          "file_changes",
                      ],
                  },
              },
          }

        body = json.dumps(payload).encode("utf-8")
        http_request = request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(http_request, timeout=self._settings.gemini_timeout_seconds) as response:
                raw_bytes = response.read()
        except error.URLError as exc:
            raise RuntimeError(f"Gemini request failed: {exc}") from exc

        raw_json = json.loads(raw_bytes.decode("utf-8"))
        text = _extract_text(raw_json)
        return GeminiResponse(text=text, raw_json=raw_json)


def _extract_text(raw_json: dict) -> str:
    candidates = raw_json.get("candidates", [])
    for candidate in candidates:
        content = candidate.get("content", {})
        parts = content.get("parts", [])
        for part in parts:
            text = part.get("text")
            if text:
                return text
    raise RuntimeError("Gemini response did not include any text content")