"""Autonomous agent loop implementing the Observe-Reason-Act-Check cycle."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .config import load_settings
from .parser import BuildLog
from .memory import get_log_signature, get_past_attempts, record_attempt
from .analysis import analyze_build_log
from .schema import FileChange
from .tools import post_pr_comment, get_pr_comments


# ---------------------------------------------------------------------------
# Diff preview helper
# ---------------------------------------------------------------------------

def generate_diff_preview(file_changes: tuple[FileChange, ...]) -> str:
    """Render file changes as a GitHub-flavored markdown diff block."""

    if not file_changes:
        return ""

    parts: list[str] = []
    for fc in file_changes:
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
            # Modify: show removed lines then added lines
            for line in fc.search.splitlines():
                parts.append(f"- {line}")
            for line in fc.replace.splitlines():
                parts.append(f"+ {line}")

        parts.append("```")
        parts.append("")  # blank line between files

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

def run_agent_loop(build_log: BuildLog, project_root: Path | None = None) -> bool:
    """Execute the autonomous CI/CD recovery agent loop.

    1. Observe: Parse build log and generate SHA-256 error signature.
    2. Check Memory: Retrieve past failed attempts.
    3. Safety Guardrails: Halt if consecutive retries >= 3.
    4. Reason: Call Gemini to obtain a diagnosis, avoiding past failed steps.
    5. Act:
       - Post diagnosis details as a PR comment with visual diff preview.
       - Embed file_changes metadata for /apply-fix ChatOps trigger.
       - Save attempt status to local memory.
       - Re-run is handled by a separate retry workflow.
    """

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
                    # Try to parse as JSON metadata (new format)
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

    # Merge local history attempts with PR comments attempts, preserving order and uniqueness
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

    # 4. Reason: Run diagnosis with memory context
    try:
        analysis = analyze_build_log(build_log, past_attempts)
    except Exception as exc:
        print(f"Agent loop reasoning failed: {exc}", file=sys.stderr)
        return False

    # Write transient status to file for retry workflow to consume
    try:
        transient_types = {"network_timeout", "oom_error", "disk_full", "permission_denied", "unknown"}
        is_transient = analysis.diagnosis.failure_type.value in transient_types
        (root / "transient_status.txt").write_text("true" if is_transient else "false", encoding="utf-8")
    except Exception as exc:
        print(f"Warning: Could not write transient status: {exc}", file=sys.stderr)

    diagnosis = analysis.diagnosis
    suggested_fix = diagnosis.suggested_fix

    # Extra safety guardrail: if Gemini returns a fix we already attempted, override it
    if suggested_fix in past_attempts:
        print("Warning: LLM returned a suggestion that has already failed. Overriding.")
        alternate_fixes = [step for step in diagnosis.fix_steps if step not in past_attempts]
        if alternate_fixes:
            suggested_fix = alternate_fixes[0]
        else:
            print("Error: No new fix recommendations could be extracted. Halting agent loop.")
            return False

    print(f"Reasoned Failure Category: {diagnosis.failure_type.value}")
    print(f"Reasoned Fix Proposal: {suggested_fix}")

    if not token or not repo:
        print("\nWarning: GITHUB_TOKEN or GITHUB_REPOSITORY is not set in settings.")
        print("Saving attempt locally, but skipping GitHub API interactions.")
        record_attempt(history_file, signature, suggested_fix, "failed")
        return False

    # Act Part A: Post PR Comment with diff preview
    if pr_number:
        print(f"Posting diagnostic report comment to PR #{pr_number}...")
        comment_body = (
            f"### 🔍 AI Build Assistant Diagnosis\n\n"
            f"**Failure Category**: `{diagnosis.failure_type.value}`\n"
            f"**Confidence Level**: `{diagnosis.confidence}`\n"
            f"**Root Cause**: {diagnosis.root_cause}\n\n"
            f"#### 🛠️ Suggested Recovery Action:\n"
            f"> {suggested_fix}\n\n"
            f"#### 📋 Step-by-Step Fix Instructions:\n"
        )
        for idx, step in enumerate(diagnosis.fix_steps, 1):
            comment_body += f"{idx}. {step}\n"

        # Add diff preview if file changes are available
        if diagnosis.file_changes:
            diff_preview = generate_diff_preview(diagnosis.file_changes)
            comment_body += f"\n---\n\n#### 📝 Suggested Code Changes:\n\n{diff_preview}\n"
            comment_body += "\n💡 **To apply these changes automatically**, reply to this PR with:\n"
            comment_body += "```\n/apply-fix\n```\n"

        comment_body += f"\n*Attempt #{len(past_attempts) + 1}. A re-run will be triggered automatically.*"

        # Embed structured metadata for /apply-fix to consume
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
        print("No GITHUB_PR_NUMBER environment variable found. Skipping PR comment tool.")

    # Act Part B: Record attempt in memory
    record_attempt(history_file, signature, suggested_fix, "running")
    print("Diagnosis complete. Re-run will be triggered by the retry workflow.")
    return True

