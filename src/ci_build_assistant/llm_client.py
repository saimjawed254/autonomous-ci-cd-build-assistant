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
      "failure_types": ["dependency_error", "test_failure"],
      "root_cause": "short explanation",
      "fix_steps": ["step 1", "step 2"],
      "confidence": "HIGH|MEDIUM|UNCERTAIN",
      "evidence": "log line or pattern used",
      "file_changes": [
        {
          "path": "relative/path/to/file.py",
          "search": "exact code block to find",
          "replace": "exact code block to replace it with",
          "action": "modify",
          "error_type": "dependency_error"
        }
      ]
    }

    CRITICAL RULES for file_changes:
    - Identify ALL error categories present in the log and list them in "failure_types".
    - You MUST ALWAYS provide file_changes with exact search/replace blocks for any error that involves code.
    - NEVER return an empty file_changes array [] when the failure is related to code (syntax errors, compile errors, test failures, import errors, dependency errors, missing functions, etc.).
    - For each file_change, assign its "error_type" to one of the values listed in your "failure_types" array.
    - Only return an empty file_changes array [] for truly non-code failures like network timeouts, OOM, disk full, or missing secrets/environment variables that cannot be fixed by changing source files.
    - The "replace" field MUST contain syntactically valid, complete code. Before returning, mentally verify: are all colons present? All parentheses balanced? All brackets closed? All semicolons included? Double-check for common omissions like missing colons after function definitions, missing closing parentheses, and missing commas.
    - The "search" field MUST exactly match existing code in the file, including all whitespace and indentation. Do NOT guess or paraphrase. Copy the exact lines from the error log or stack trace context.
    - The "fix_steps" field should describe WHAT code changes you are making (e.g., "Add missing colon to function definition on line 5 of discount_service.py"), NOT instructions for a human to manually investigate (e.g., do NOT say "Verify if function was renamed" or "Check the import statement").
    - Prioritize providing exact code fixes in "file_changes" whenever possible, especially for test assertion mismatches, compilation errors, and simple code bugs that are visible in stack traces.
    - If the log shows a test assertion failing (e.g. expected value vs received value), prioritize suggesting the code fix that updates the assertion or test to pass in the CI environment.
    - For missing dependencies (e.g., module not found, failed to load url), suggest adding the missing package to package.json (or requirements.txt) under dependencies with a suitable version constraint (e.g., "^1.0.0"), or suggest removing the invalid import statement if it is not used.
    - For import errors where a function/class is missing from a module, provide the actual code to add or fix the import and/or add the missing function/class definition.
    - The "action" field must be "modify", "create", or "delete".
    - For "create": "search" should be empty, "replace" contains the full file content.
    - For "delete": "replace" should be empty, "search" should be empty.
    - Limit changes to at most 10 files. If you are provided with FULL FILE CONTEXT, you MUST replace the ENTIRE file by making the "search" block the exact full file content, and the "replace" block the full corrected file content. Otherwise, limit to at most 100 lines per block.
    - Include enough surrounding context lines in "search" to make the match unambiguous.
    Keep fix steps short, direct, and actionable.
    """
).strip()


def build_user_prompt(build_log: BuildLog, past_attempts: list[str] | None = None, extra_context: str = "") -> str:
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
        {extra_context}
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
                        "failure_types": {
                            "type": "array",
                            "items": {
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
                            }
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
                                    "error_type": {"type": "string"},
                                },
                                "required": ["path", "search", "replace", "action", "error_type"],
                            },
                        },
                      },
                      "required": [
                          "failure_types",
                          "root_cause",
                          "fix_steps",
                          "confidence",
                          "evidence",
                          "file_changes",
                      ],
                      "propertyOrdering": [
                          "failure_types",
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