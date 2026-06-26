"""Autonomous agent loop and analysis orchestration for CI Build Assistant."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import (
    BuildLog,
    FailureDiagnosis,
    FailureType,
    FileChange,
    get_log_signature,
    get_past_attempts,
    get_pr_comments,
    load_settings,
    post_pr_comment,
    record_attempt,
)
from .llm_client import GeminiClient, SYSTEM_PROMPT, build_user_prompt
from .scanner import get_changed_files, get_full_file_contents, scan_files_for_errors


# ===========================================================================
# 1. High-Level Analysis Orchestration
# ===========================================================================

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


def analyze_build_log(build_log: BuildLog, past_attempts: list[str] | None = None, extra_context: str = "") -> AnalysisResult:
    """Analyze a build log using Gemini."""

    settings = load_settings()
    client = GeminiClient(settings)
    llm_enabled = client.is_configured()

    if not llm_enabled:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    try:
        user_prompt = build_user_prompt(build_log, past_attempts, extra_context)
        response = client.generate_json(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt)
        diagnosis = _parse_llm_response(response.text)
        return AnalysisResult(
            diagnosis=diagnosis,
            used_llm=True,
            llm_enabled=True,
        )
    except Exception as exc:
        raise RuntimeError(f"Gemini analysis failed: {exc}") from exc


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
    failure_types = _parse_failure_types(payload.get("failure_types"))
    confidence = _normalized_text(payload.get("confidence"), default="UNCERTAIN")
    root_cause = _normalized_text(payload.get("root_cause"), default="Gemini did not provide a root cause.")
    evidence = _normalized_text(payload.get("evidence"), default="Gemini did not provide evidence.")
    fix_steps = _parse_fix_steps(payload.get("fix_steps"))
    file_changes = _parse_file_changes(payload.get("file_changes"))

    return FailureDiagnosis(
        failure_types=failure_types,
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


def _parse_failure_types(value: Any) -> tuple[FailureType, ...]:
    types = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                normalized = item.strip().lower()
                for ft in FailureType:
                    if ft.value == normalized and ft not in types:
                        types.append(ft)
    if not types:
        types.append(FailureType.UNKNOWN)
    return tuple(types)


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
    """Parse the file_changes array from the Gemini JSON payload, enforcing safety limits."""

    if not isinstance(value, list):
        return ()
    changes = []
    total_lines = 0
    seen_paths = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        path = item.get("path", "").strip()
        if not path:
            continue

        seen_paths.add(path)
        # Enforce max 10 distinct files
        if len(seen_paths) > 10:
            print("Warning: Suggested file changes span more than 10 files, skipping auto-fix suggestion.", file=sys.stderr)
            return ()

        search = item.get("search", "")
        replace = item.get("replace", "")
        action = item.get("action", "modify").strip().lower()
        error_type = item.get("error_type", "unknown").strip().lower()

        # Count lines in search and replace blocks
        search_lines = search.count("\n") + 1 if search else 0
        replace_lines = replace.count("\n") + 1 if replace else 0

        # Enforce max 100 lines per block
        if search_lines > 100 or replace_lines > 100:
            print("Warning: Suggested file change block is too large (> 100 lines), skipping auto-fix suggestion.", file=sys.stderr)
            return ()

        total_lines += max(search_lines, replace_lines)
        # Enforce max 500 lines cumulative across all changes
        if total_lines > 500:
            print("Warning: Total suggested file changes are too large (> 500 lines), skipping auto-fix suggestion.", file=sys.stderr)
            return ()

        changes.append(FileChange(
            path=path,
            search=search,
            replace=replace,
            action=action,
            error_type=error_type,
        ))
    return tuple(changes)


def _suggest_fix(fix_steps: tuple[str, ...], root_cause: str) -> str:
    if fix_steps:
        return fix_steps[0]
    return root_cause


# ===========================================================================
# 2. Diff Preview Rendering
# ===========================================================================

def generate_diff_preview(file_changes: tuple[FileChange, ...]) -> str:
    """Render file changes as a GitHub-flavored markdown diff block, grouped by error type."""

    if not file_changes:
        return ""

    # Group by error_type
    groups: dict[str, list[FileChange]] = {}
    for fc in file_changes:
        groups.setdefault(fc.error_type, []).append(fc)

    parts: list[str] = []
    for error_type, changes in groups.items():
        if len(groups) > 1 or error_type != "unknown":
            parts.append(f"##### 🐛 Fixes for: `{error_type}`")
            parts.append("")

        for fc in changes:
            header = f"📄 `{fc.path}`"
            if fc.action == "create":
                header += " *(new file)*"
            elif fc.action == "delete":
                header += " *(delete file)*"

            parts.append(header)
            parts.append("```diff")

            if fc.action == "delete":
                for line in fc.search.splitlines():
                    parts.append(f"- {line}")
            elif fc.action == "create":
                for line in fc.replace.splitlines():
                    parts.append(f"+ {line}")
            else:
                for line in fc.search.splitlines():
                    parts.append(f"- {line}")
                for line in fc.replace.splitlines():
                    parts.append(f"+ {line}")

            parts.append("```")
            parts.append("")

    return "\n".join(parts)


# ===========================================================================
# 3. Main Agent Loop
# ===========================================================================

def run_agent_loop(build_log: BuildLog, project_root: Path | None = None) -> bool:
    """Execute the autonomous Observe-Reason-Act recovery cycle."""

    root = project_root or Path.cwd()
    settings = load_settings(root)

    # 1. Observe: Get log signature
    signature = get_log_signature(build_log.content)

    # 2. Check Memory
    history_file = root / ".build_assistant_history.json"
    past_attempts = get_past_attempts(history_file, signature)

    token = settings.github_token
    repo = settings.github_repository
    pr_number = settings.github_pr_number

    # Reconstruct past attempts from PR comments if available
    pr_attempts = []
    if token and repo and pr_number:
        print(f"Fetching PR comments from {repo} PR #{pr_number} to reconstruct history...")
        try:
            comments = get_pr_comments(repo, pr_number, token)
            for comment in comments:
                matches = re.findall(r"<!--\s*build_assistant_metadata:\s*(.*?)\s*-->", comment)
                for match in matches:
                    try:
                        meta = json.loads(match)
                        if isinstance(meta, dict):
                            match_sig = meta.get("signature") == signature
                            match_sha = False
                            current_sha = os.environ.get("GITHUB_SHA")
                            if current_sha and meta.get("commit_sha") == current_sha:
                                match_sha = True
                            
                            if match_sig or match_sha:
                                sf = meta.get("suggested_fix", "")
                                if sf:
                                    pr_attempts.append(sf)
                        continue
                    except (json.JSONDecodeError, ValueError):
                        pass
                    # Fall back to legacy format: signature:fix_text
                    parts = match.split(":", 1)
                    if len(parts) == 2 and len(parts[0]) == 64 and all(c in "0123456789abcdef" for c in parts[0]):
                        if parts[0] == signature:
                            pr_attempts.append(parts[1])
                    else:
                        pr_attempts.append(match)
            print(f"Extracted {len(pr_attempts)} historical attempts from PR comments.")
        except Exception as exc:
            print(f"Warning: Failed to fetch/parse PR comments for history: {exc}", file=sys.stderr)

    # Merge unique attempts
    unique_attempts = []
    for attempt in past_attempts + pr_attempts:
        if attempt not in unique_attempts:
            unique_attempts.append(attempt)
    past_attempts = unique_attempts

    # 3. Safety Guardrails (Max 3 attempts)
    if len(past_attempts) >= 3:
        print("=" * 80)
        print("⚠️  SAFETY HALT: Retry Guardrail Triggered")
        print("=" * 80)
        print(f"This error signature ({signature}) has already been attempted {len(past_attempts)} times.")
        print("Stopping autonomous fixes to avoid runaway actions cost and loops.")
        print("=" * 80)
        return False

    print(f"Starting CI recovery agent. Log signature: {signature}")
    print(f"Historical attempts detected: {len(past_attempts)}")

    # 4. Gather Extra Context based on Context Strategy
    extra_context = ""
    changed_files = get_changed_files(settings, root)
    if changed_files:
        if settings.context_strategy == "full":
            print(f"Gathering full context from {len(changed_files)} changed files...")
            extra_context = get_full_file_contents(changed_files, root)
        else:
            print(f"Running multi-layer pre-scan on {len(changed_files)} changed files...")
            extra_context = scan_files_for_errors(changed_files, root)

    # 5. Reason: Run diagnosis
    try:
        analysis = analyze_build_log(build_log, past_attempts, extra_context)
    except Exception as exc:
        print(f"Agent loop reasoning failed: {exc}", file=sys.stderr)
        return False

    # Write transient status to file for retry workflow
    try:
        transient_types = {"network_timeout", "oom_error", "disk_full", "permission_denied", "unknown"}
        is_transient = any(ft.value in transient_types for ft in analysis.diagnosis.failure_types)
        (root / "transient_status.txt").write_text("true" if is_transient else "false", encoding="utf-8")
    except Exception as exc:
        print(f"Warning: Could not write transient status: {exc}", file=sys.stderr)

    diagnosis = analysis.diagnosis
    suggested_fix = diagnosis.suggested_fix

    # Handle duplicate fix overriding
    if suggested_fix in past_attempts:
        print("Warning: LLM returned a suggestion that has already failed. Overriding.")
        alternate_fixes = [step for step in diagnosis.fix_steps if step not in past_attempts]
        if alternate_fixes:
            suggested_fix = alternate_fixes[0]
        else:
            print("Error: No new fix recommendations could be extracted. Halting agent loop.")
            return False

    categories = ", ".join(ft.value for ft in diagnosis.failure_types)
    print(f"Reasoned Failure Categories: {categories}")
    print(f"Reasoned Fix Proposal: {suggested_fix}")

    if not token or not repo:
        print("\nWarning: GITHUB_TOKEN or GITHUB_REPOSITORY is not set.")
        print("Saving attempt locally, but skipping GitHub API comments.")
        record_attempt(history_file, signature, suggested_fix, "failed")
        return False

    # 5. Act: Post PR Comment
    if pr_number:
        print(f"Posting diagnostic report comment to PR #{pr_number}...")
        comment_body = (
            f"### 🔍 AI Build Assistant Diagnosis\n\n"
            f"**Failure Categories**: `{categories}`\n"
            f"**Confidence Level**: `{diagnosis.confidence}`\n"
            f"**Root Cause**: {diagnosis.root_cause}\n\n"
            f"#### 🛠️ Suggested Recovery Action:\n"
            f"> {suggested_fix}\n\n"
            f"#### 📋 Step-by-Step Fix Instructions:\n"
        )
        for idx, step in enumerate(diagnosis.fix_steps, 1):
            comment_body += f"{idx}. {step}\n"

        if diagnosis.file_changes:
            diff_preview = generate_diff_preview(diagnosis.file_changes)
            comment_body += f"\n---\n\n#### 📝 Suggested Code Changes:\n\n{diff_preview}\n"
            comment_body += "\n💡 **To apply these changes automatically**, reply to this PR with:\n"
            comment_body += "```\n/apply-fix\n```\n"
        else:
            comment_body += "\n---\n\n#### ℹ️ No Automatic Code Changes Available\n\n"
            comment_body += "The assistant could not determine exact code changes for this error type. "
            comment_body += "This may happen for infrastructure issues (network, permissions, disk space) "
            comment_body += "or complex refactoring errors that require manual investigation.\n"

        comment_body += f"\n*Attempt #{len(past_attempts) + 1}. A re-run will be triggered automatically.*"

        metadata = {
            "signature": signature,
            "suggested_fix": suggested_fix,
            "commit_sha": os.environ.get("GITHUB_SHA", ""),
            "file_changes": [fc.to_dict() for fc in diagnosis.file_changes],
        }
        metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        comment_body += f"\n<!-- build_assistant_metadata: {metadata_json} -->"

        try:
            post_pr_comment(repo, pr_number, comment_body, token)
            print("GitHub PR comment posted successfully.")
        except Exception as exc:
            print(f"Warning: failed to post PR comment: {exc}", file=sys.stderr)
    else:
        print("No GITHUB_PR_NUMBER env found. Skipping PR comment.")

    # Record attempt in history database
    record_attempt(history_file, signature, suggested_fix, "running")
    return True
