"""Autonomous agent loop implementing the Observe-Reason-Act-Check cycle."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .config import load_settings
from .parser import BuildLog
from .memory import get_log_signature, get_past_attempts, record_attempt
from .analysis import analyze_build_log
from .tools import post_pr_comment, trigger_workflow_rerun, get_pr_comments


def run_agent_loop(build_log: BuildLog, project_root: Path | None = None) -> bool:
    """Execute the autonomous CI/CD recovery agent loop.

    1. Observe: Parse build log and generate SHA-256 error signature.
    2. Check Memory: Retrieve past failed attempts.
    3. Safety Guardrails: Halt if consecutive retries >= 3.
    4. Reason: Call Gemini to obtain a diagnosis, avoiding past failed steps.
    5. Act:
       - Post diagnosis details as a PR comment.
       - Save attempt status to local memory.
       - Re-trigger the failed GitHub Action workflow run.
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
    run_id = settings.github_run_id
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

    # Act Part A: Post PR Comment
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
            
        comment_body += f"\n*Attempt #{len(past_attempts) + 1} started. Automatically triggering pipeline re-run...*"
        comment_body += f"\n<!-- build_assistant_metadata: {signature}:{suggested_fix} -->"

        try:
            post_pr_comment(repo, pr_number, comment_body, token)
            print("GitHub PR comment posted successfully.")
        except Exception as exc:
            print(f"Warning: failed to post PR comment: {exc}", file=sys.stderr)
    else:
        print("No GITHUB_PR_NUMBER environment variable found. Skipping PR comment tool.")

    # Act Part B: Record attempt as 'running' in memory
    record_attempt(history_file, signature, suggested_fix, "running")

    # Act Part C: Trigger Workflow Rerun
    if run_id:
        print(f"Triggering GitHub Actions workflow re-run for Run #{run_id}...")
        try:
            trigger_workflow_rerun(repo, run_id, token)
            print("Workflow re-run triggered successfully.")
            return True
        except Exception as exc:
            print(f"Error: failed to trigger workflow re-run: {exc}", file=sys.stderr)
            # Re-record status as failed since the rerun trigger failed
            record_attempt(history_file, signature, suggested_fix, "failed")
            return False
    else:
        print("Error: GITHUB_RUN_ID is missing. Cannot trigger workflow re-run.")
        record_attempt(history_file, signature, suggested_fix, "failed")
        return False
