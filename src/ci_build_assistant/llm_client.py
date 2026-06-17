"""Gemini API client for structured diagnosis generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import error, request

from .config import Settings


@dataclass(frozen=True, slots=True)
class GeminiResponse:
    """Raw response payload from Gemini."""

    text: str
    raw_json: dict


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
                    },
                    "required": [
                        "failure_type",
                        "root_cause",
                        "fix_steps",
                        "confidence",
                        "evidence",
                    ],
                    "propertyOrdering": [
                        "failure_type",
                        "root_cause",
                        "fix_steps",
                        "confidence",
                        "evidence",
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