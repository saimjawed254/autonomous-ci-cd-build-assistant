"""Command-line entry point for the CI build assistant."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime

from src.ci_build_assistant import analyze_build_log, read_build_log, run_agent_loop
from src.ci_build_assistant.config import load_settings
from src.ci_build_assistant.tools import get_pr_comments, post_pr_comment, get_pr_branch


FAILURE_TYPE_NAMES = {
    "dependency_error": "Dependency / Package Error",
    "test_failure": "Test Suite Failure",
    "config_issue": "Workflow Configuration Issue",
    "oom_error": "Out of Memory (OOM) Error",
    "network_timeout": "Network Timeout / Connectivity Error",
    "permission_denied": "Permission Denied / Authorization Error",
    "missing_secret": "Missing Env Variable or Secret",
    "compile_error": "Code Compile / Build Error",
    "disk_full": "Disk Space Exhausted",
    "unknown": "Unknown / Unclassified Failure"
}


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""

    parser = argparse.ArgumentParser(
        description="Read and analyze a CI/CD build log file.",
    )
    parser.add_argument(
        "log_file",
        nargs="?",
        type=Path,
        default=None,
        help="Path to the build log text file (not required for apply-fix mode).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the analysis in raw JSON format instead of the human-readable dashboard.",
    )
    parser.add_argument(
        "--mode",
        choices=["diagnose", "agent", "apply-fix"],
        default="diagnose",
        help="Execution mode: 'diagnose' (default), 'agent' (PR comments + retry), or 'apply-fix' (apply suggested code changes).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and print structured diagnosis output."""

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)

    # apply-fix mode does not need a log file
    if args.mode == "apply-fix":
        return _run_apply_fix()

    if args.log_file is None:
        print("Error: log_file is required for diagnose/agent modes.", file=sys.stderr)
        return 1

    try:
        build_log = read_build_log(args.log_file)
    except FileNotFoundError:
        print(f"Error: log file not found: {args.log_file}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: could not read {args.log_file}: {exc}", file=sys.stderr)
        return 1

    if args.mode == "agent":
        success = run_agent_loop(build_log)
        return 0 if success else 1

    try:
        analysis = analyze_build_log(build_log)
    except RuntimeError as exc:
        _log_llm_error(exc)
        print(f"LLM analysis error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        # Match previous output shape for JSON integrations
        print(f"Log file: {build_log.path}")
        print(f"Characters: {build_log.character_count}")
        print(f"Lines: {build_log.line_count}")
        print("Diagnosis JSON:")
        print(json.dumps(analysis.to_dict(), indent=2, ensure_ascii=False))
        print("-" * 72)
    else:
        # Print a beautiful human-readable report dashboard
        settings = load_settings()
        confidence = analysis.diagnosis.confidence.upper()
        if "HIGH" in confidence:
            confidence_badge = "🟢 HIGH"
        elif "MEDIUM" in confidence:
            confidence_badge = "🟡 MEDIUM"
        else:
            confidence_badge = "🔴 UNCERTAIN"

        failure_type_val = analysis.diagnosis.failure_type.value
        category_name = FAILURE_TYPE_NAMES.get(failure_type_val, failure_type_val.replace("_", " ").title())

        print("=" * 80)
        print("🔍 CI/CD Build Diagnosis Report")
        print("=" * 80)
        print(f"📁 Log File: {build_log.path}")
        print(f"📊 Stats: {build_log.character_count} characters, {build_log.line_count} lines")
        print(f"🤖 Analysis Source: Gemini AI (Model: {settings.gemini_model})")
        print("-" * 80)
        print(f"🚨 Failure Category: {category_name}")
        print(f"🎯 Confidence Level: {confidence_badge}")
        print("-" * 80)
        print("\n📝 Root Cause:")
        print(f"   {analysis.diagnosis.root_cause}")
        print("\n🔍 Evidence:")
        print(f"   {analysis.diagnosis.evidence}")
        print("\n🛠️ Suggested Fix:")
        print(f"   {analysis.diagnosis.suggested_fix}")
        
        print("\n📋 Step-by-Step Recovery Actions:")
        for idx, step in enumerate(analysis.diagnosis.fix_steps, 1):
            print(f"   {idx}. {step}")
            
        print("\n" + "=" * 80)
        print("📄 Raw Build Log Content")
        print("=" * 80)

    if build_log.is_empty:
        print("[empty log file]")
    else:
        print(build_log.content, end="" if build_log.content.endswith("\n") else "\n")

    if not args.json:
        print("=" * 80)

    return 0


# ---------------------------------------------------------------------------
# Apply-fix mode
# ---------------------------------------------------------------------------

def _run_apply_fix() -> int:
    """Fetch the most recent Build Assistant diagnosis from PR comments,
    apply file changes, commit, and push back to the PR branch."""

    settings = load_settings()
    token = settings.github_token
    repo = settings.github_repository
    pr_number = settings.github_pr_number

    if not token or not repo or not pr_number:
        print("Error: GITHUB_TOKEN, GITHUB_REPOSITORY, and GITHUB_PR_NUMBER are all required for apply-fix mode.", file=sys.stderr)
        return 1

    print(f"🔧 Apply-fix mode: Fetching comments from {repo} PR #{pr_number}...")

    try:
        comments = get_pr_comments(repo, pr_number, token)
    except Exception as exc:
        print(f"Error: Failed to fetch PR comments: {exc}", file=sys.stderr)
        return 1

    # Find the most recent Build Assistant metadata comment (search from newest to oldest)
    metadata = None
    for comment in reversed(comments):
        matches = re.findall(r"<!--\s*build_assistant_metadata:\s*(.*?)\s*-->", comment)
        for match in matches:
            try:
                parsed = json.loads(match)
                if isinstance(parsed, dict) and "file_changes" in parsed:
                    metadata = parsed
                    break
            except (json.JSONDecodeError, ValueError):
                continue
        if metadata:
            break

    if not metadata:
        print("Error: No Build Assistant diagnosis with file_changes found in PR comments.", file=sys.stderr)
        print("Make sure the AI Build Assistant has posted a diagnosis comment with code change suggestions first.")
        return 1

    file_changes = metadata.get("file_changes", [])
    if not file_changes:
        print("Error: The most recent diagnosis comment has no file changes to apply.", file=sys.stderr)
        return 1

    print(f"Found {len(file_changes)} file change(s) to apply.")

    # Apply each file change
    root = Path.cwd()
    applied_count = 0
    for fc in file_changes:
        file_path = root / fc["path"]
        action = fc.get("action", "modify")
        search = fc.get("search", "")
        replace = fc.get("replace", "")

        print(f"  📄 {action.upper()}: {fc['path']}")

        if action == "create":
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(replace, encoding="utf-8")
            applied_count += 1

        elif action == "delete":
            if file_path.exists():
                file_path.unlink()
                applied_count += 1
            else:
                print(f"    ⚠️ File not found, skipping delete: {fc['path']}")

        elif action == "modify":
            if not file_path.exists():
                print(f"    ❌ File not found: {fc['path']}. Cannot apply modification.", file=sys.stderr)
                continue

            content = file_path.read_text(encoding="utf-8")
            if search not in content:
                print(f"    ❌ Search block not found in {fc['path']}.", file=sys.stderr)
                print("    The code has changed since this fix was suggested. Please re-run the tests.", file=sys.stderr)
                continue

            new_content = content.replace(search, replace, 1)
            file_path.write_text(new_content, encoding="utf-8")
            applied_count += 1
        else:
            print(f"    ⚠️ Unknown action '{action}', skipping.")

    if applied_count == 0:
        print("❌ No file changes could be applied. Aborting.", file=sys.stderr)
        return 1

    print(f"\n✅ Applied {applied_count}/{len(file_changes)} file change(s).")

    # Fetch PR branch name to push to the correct branch from detached HEAD
    try:
        branch = get_pr_branch(repo, pr_number, token)
    except Exception as exc:
        print(f"Error: Failed to fetch PR branch name: {exc}", file=sys.stderr)
        return 1

    # Commit and push
    try:
        _git_commit_and_push(root, repo, token, branch)
    except Exception as exc:
        print(f"Error: Git commit/push failed: {exc}", file=sys.stderr)
        return 1

    # Post a confirmation comment on the PR
    try:
        confirm_body = (
            f"### ✅ AI Build Assistant — Fix Applied\n\n"
            f"Applied **{applied_count}** code change(s) and pushed to this branch.\n"
            f"A re-run of the CI pipeline should start automatically.\n"
        )
        post_pr_comment(repo, pr_number, confirm_body, token)
    except Exception as exc:
        print(f"Warning: Could not post confirmation comment: {exc}", file=sys.stderr)

    print("🚀 Changes committed and pushed. CI will re-run automatically.")
    return 0


def _git_commit_and_push(root: Path, repo: str, token: str, branch: str) -> None:
    """Configure git identity and push applied changes."""

    env = {**os.environ, "GIT_AUTHOR_NAME": "AI Build Assistant", "GIT_COMMITTER_NAME": "AI Build Assistant",
           "GIT_AUTHOR_EMAIL": "github-actions[bot]@users.noreply.github.com",
           "GIT_COMMITTER_EMAIL": "github-actions[bot]@users.noreply.github.com"}

    def _run(cmd: list[str]) -> None:
        result = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, env=env)
        if result.returncode != 0:
            raise RuntimeError(f"Command {' '.join(cmd)} failed: {result.stderr.strip()}")

    _run(["git", "config", "user.name", "AI Build Assistant"])
    _run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"])
    _run(["git", "add", "-A"])

    # Check if there are staged changes
    status = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(root), capture_output=True)
    if status.returncode == 0:
        print("No changes detected after applying fixes (files may already be correct).")
        return

    _run(["git", "commit", "-m", "fix: apply AI Build Assistant suggested changes"])
    _run(["git", "push", "origin", f"HEAD:{branch}"])
    print("Git push completed successfully.")


def _log_llm_error(exc: Exception) -> None:
    """Write Gemini failures to an ignored local log for debugging."""

    log_dir = Path(".build_assistant_logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "llm-errors.log"
    timestamp = datetime.now().isoformat(timespec="seconds")
    message = f"[{timestamp}] {exc}\n"
    with log_path.open("a", encoding="utf-8", errors="ignore") as handle:
        handle.write(message)


if __name__ == "__main__":
    raise SystemExit(main())