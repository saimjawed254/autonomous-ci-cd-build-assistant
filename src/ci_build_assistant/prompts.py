"""Prompt templates for the Week 2 Gemini workflow."""

from __future__ import annotations

from textwrap import dedent

from .parser import BuildLog


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
      "evidence": "log line or pattern used"
    }
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
    return content[:limit] + "\n...[truncated]..."