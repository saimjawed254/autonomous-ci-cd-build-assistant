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

from src.ci_build_assistant import (
    analyze_build_log,
    read_build_log,
    run_agent_loop,
    load_settings,
    get_pr_comments,
    post_pr_comment,
    get_pr_branch,
    post_comment_reaction,
)


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
    
    print(f"\n🚀 Starting Autonomous CI/CD Build Assistant (Mode: {args.mode})")

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

    # Write transient status to file for retry workflow to consume
    try:
        transient_types = {"network_timeout", "oom_error", "disk_full", "permission_denied", "unknown"}
        is_transient = any(ft.value in transient_types for ft in analysis.diagnosis.failure_types)
        Path("transient_status.txt").write_text("true" if is_transient else "false", encoding="utf-8")
    except Exception as exc:
        print(f"Warning: Could not write transient status: {exc}", file=sys.stderr)

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

        category_names = []
        for ft in analysis.diagnosis.failure_types:
            ft_val = ft.value
            name = FAILURE_TYPE_NAMES.get(ft_val, ft_val.replace("_", " ").title())
            category_names.append(name)
        categories_str = ", ".join(category_names)

        print("=" * 80)
        print("🔍 CI/CD Build Diagnosis Report")
        print("=" * 80)
        print(f"📁 Log File: {build_log.path}")
        print(f"📊 Stats: {build_log.character_count} characters, {build_log.line_count} lines")
        print(f"🤖 Analysis Source: Gemini AI (Model: {settings.gemini_model})")
        print("-" * 80)
        print(f"🚨 Failure Categories: {categories_str}")
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

    # Post a 👀 reaction immediately to the triggering comment if GITHUB_COMMENT_ID is set
    comment_id_str = os.getenv("GITHUB_COMMENT_ID")
    if comment_id_str:
        try:
            comment_id = int(comment_id_str)
            print(f"Adding 👀 reaction to triggering comment #{comment_id}...")
            post_comment_reaction(repo, comment_id, token, "eyes")
        except Exception as exc:
            print(f"Warning: Failed to add start reaction: {exc}", file=sys.stderr)

    print("\n--- STEP 1: PARSING PR COMMENTS ---")
    print(f"🔧 Apply-fix mode: Fetching comments from {repo} PR #{pr_number}...")

    try:
        comments = get_pr_comments(repo, pr_number, token)
    except Exception as exc:
        msg = f"Failed to fetch PR comments: {exc}"
        print(f"Error: {msg}", file=sys.stderr)
        _post_failure_comment(repo, pr_number, token, msg)
        return 1

    # Determine what the user's most recent command is: /apply-fix or /deep-scan.
    # IMPORTANT: We use startswith() on the stripped body, NOT substring 'in',
    # because the bot's own diagnosis comments contain "/deep-scan" in their
    # instruction text and would cause false matches.
    user_command = None
    last_apply_fix_index = -1
    for idx, comment in enumerate(reversed(comments)):
        stripped = comment.strip()
        if stripped.startswith("/deep-scan"):
            user_command = "deep-scan"
            break
        elif stripped.startswith("/apply-fix"):
            user_command = "apply-fix"
            last_apply_fix_index = len(comments) - 1 - idx
            break

    if user_command == "deep-scan":
        print("🔍 Deep scan requested via PR comment. Triggering CI re-run...")
        try:
            branch = get_pr_branch(repo, pr_number, token)
            root = Path.cwd()
            env = {**os.environ, "GIT_AUTHOR_NAME": "AI Build Assistant", "GIT_COMMITTER_NAME": "AI Build Assistant",
                   "GIT_AUTHOR_EMAIL": "github-actions[bot]@users.noreply.github.com",
                   "GIT_COMMITTER_EMAIL": "github-actions[bot]@users.noreply.github.com"}
            
            def _run(cmd: list[str]) -> None:
                result = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, env=env)
                if result.returncode != 0:
                    raise RuntimeError(f"Command {' '.join(cmd)} failed: {result.stderr.strip()}")
                    
            _run(["git", "config", "user.name", "AI Build Assistant"])
            _run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"])
            _run(["git", "commit", "--allow-empty", "-m", "chore: trigger deep scan"])
            _run(["git", "push", "origin", f"HEAD:{branch}"])
            print("Successfully pushed trigger commit for deep scan.")
        except Exception as e:
            print(f"Error triggering deep scan: {e}")
            _post_failure_comment(repo, pr_number, token, f"Could not push trigger commit for deep scan: {e}")
            return 1
        return 0

    # Find the most recent Build Assistant metadata comment.
    # We search backwards from the comment immediately preceding the /apply-fix command.
    metadata = None
    search_comments = comments[:last_apply_fix_index] if last_apply_fix_index >= 0 else comments

    for comment in reversed(search_comments):
        stripped = comment.strip()
        # Loop prevention: If we hit an older /apply-fix command before finding a diagnosis,
        # it means the most recent diagnosis was already applied by that older command.
        # We stop searching to prevent re-applying the same old fixes.
        if stripped.startswith("/apply-fix"):
            break
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
        msg = "No Build Assistant diagnosis with file_changes found in PR comments. Make sure the AI Build Assistant has posted a diagnosis comment with code change suggestions first."
        print(f"Error: {msg}", file=sys.stderr)
        _post_failure_comment(repo, pr_number, token, msg)
        return 1

    file_changes = metadata.get("file_changes", [])
    if not file_changes:
        msg = "The most recent diagnosis comment has no file changes to apply. The AI may have been unable to determine exact code fixes for this error type."
        print(f"Error: {msg}", file=sys.stderr)
        _post_failure_comment(repo, pr_number, token, msg)
        return 1

    print(f"Found {len(file_changes)} file change(s) to apply.")

    print("\n--- STEP 2: APPLYING FILE CHANGES ---")
    # Apply each file change
    root = Path.cwd()
    applied_count = 0
    failure_details: list[str] = []
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
                detail = f"`{fc['path']}`: File not found, skipping delete."
                print(f"    ⚠️ {detail}")
                failure_details.append(detail)

        elif action == "modify":
            if not file_path.exists():
                detail = f"`{fc['path']}`: File not found. Cannot apply modification."
                print(f"    ❌ {detail}", file=sys.stderr)
                failure_details.append(detail)
                continue

            content = file_path.read_text(encoding="utf-8")
            new_content, strategy = _smart_replace(content, search, replace)
            if new_content is None:
                detail = f"`{fc['path']}`: Search block not found. The code may have changed since this fix was suggested."
                print(f"    ❌ {detail}", file=sys.stderr)
                failure_details.append(detail)
                continue

            print(f"    ✅ Attempting to modify block using strategy: {strategy}...")
            file_path.write_text(new_content, encoding="utf-8")
            applied_count += 1
        else:
            detail = f"`{fc['path']}`: Unknown action '{action}', skipping."
            print(f"    ⚠️ {detail}")
            failure_details.append(detail)

    if applied_count == 0:
        msg = "No file changes could be applied."
        if failure_details:
            msg += "\n\n**Details:**\n" + "\n".join(f"- {d}" for d in failure_details)
        msg += "\n\nThis usually means the codebase has changed since the fix was suggested. Please re-run the CI pipeline to get a fresh diagnosis."
        print(f"❌ {msg}", file=sys.stderr)
        _post_failure_comment(repo, pr_number, token, msg)
        return 1

    # Log partial failures but continue with what we have
    if failure_details:
        print(f"\n⚠️ {len(failure_details)} change(s) could not be applied (continuing with {applied_count} successful):")
        for d in failure_details:
            print(f"  - {d}")

    print(f"\n✅ Applied {applied_count}/{len(file_changes)} file change(s).")

    # Fetch PR branch name to push to the correct branch from detached HEAD
    try:
        branch = get_pr_branch(repo, pr_number, token)
    except Exception as exc:
        msg = f"Failed to fetch PR branch name: {exc}"
        print(f"Error: {msg}", file=sys.stderr)
        _post_failure_comment(repo, pr_number, token, msg)
        return 1

    # Commit and push
    try:
        _git_commit_and_push(root, repo, token, branch)
    except Exception as exc:
        msg = f"Git commit/push failed: {exc}"
        print(f"Error: {msg}", file=sys.stderr)
        _post_failure_comment(repo, pr_number, token, msg)
        return 1

    # Post a confirmation comment on the PR
    try:
        confirm_body = (
            f"### ✅ AI Build Assistant — Fix Applied\n\n"
            f"Applied **{applied_count}** code change(s) and pushed to this branch.\n"
            f"A re-run of the CI pipeline should start automatically.\n"
        )
        if failure_details:
            confirm_body += f"\n⚠️ **{len(failure_details)} change(s) could not be applied:**\n"
            for d in failure_details:
                confirm_body += f"- {d}\n"
        post_pr_comment(repo, pr_number, confirm_body, token)
    except Exception as exc:
        print(f"Warning: Could not post confirmation comment: {exc}", file=sys.stderr)

    print("🚀 Changes committed and pushed. CI will re-run automatically.")
    return 0


def _post_failure_comment(repo: str, pr_number: int, token: str, reason: str) -> None:
    """Post a transparent failure comment to the PR so the user knows why apply-fix failed."""

    body = (
        f"### ⚠️ AI Build Assistant — Apply-Fix Failed\n\n"
        f"**Reason:** {reason}\n\n"
        f"💡 You can try again by commenting `/apply-fix` after the issue is resolved, "
        f"or wait for the next CI run to get a fresh diagnosis with updated code changes.\n"
    )
    try:
        post_pr_comment(repo, pr_number, body, token)
        print("Posted failure notification comment to PR.")
    except Exception as exc:
        print(f"Warning: Could not post failure comment to PR: {exc}", file=sys.stderr)


def _smart_replace(content: str, search: str, replace: str) -> tuple[str | None, str]:
    """Safely replace search block with replace block in content,
    resilient to line-ending mismatches and minor indentation differences.
    
    Returns a tuple of (new_content_or_None, strategy_name).
    """

    # 1. Try exact match first
    if search in content:
        return content.replace(search, replace, 1), "exact-match"

    # 2. Try normalizing line endings (Windows CRLF vs Linux LF)
    search_lf = search.replace("\r\n", "\n")
    content_lf = content.replace("\r\n", "\n")
    if search_lf in content_lf:
        has_crlf = "\r\n" in content
        ending = "\r\n" if has_crlf else "\n"
        replace_normalized = replace.replace("\r\n", "\n").replace("\n", ending)
        search_normalized = search.replace("\r\n", "\n").replace("\n", ending)
        if search_normalized in content:
            return content.replace(search_normalized, replace_normalized, 1), "line-ending-normalization"
        replaced_lf = content_lf.replace(search_lf, replace.replace("\r\n", "\n"), 1)
        result = replaced_lf.replace("\n", ending) if has_crlf else replaced_lf
        return result, "line-ending-normalization"

    # 3. Try matching with fuzzy indentation / stripped lines
    search_lines = [line.strip() for line in search.splitlines() if line.strip()]
    if not search_lines:
        return None, "no-match"

    content_lines = content.splitlines()
    n_search = len(search_lines)
    n_content = len(content_lines)

    for i in range(n_content - n_search + 1):
        match = True
        for j in range(n_search):
            if content_lines[i + j].strip() != search_lines[j]:
                match = False
                break

        if match:
            # Reconstruct the replacement with the matched indentation of the file
            first_line = content_lines[i]
            indentation = first_line[:len(first_line) - len(first_line.lstrip())]

            replace_lines = replace.splitlines()
            new_replace_lines = []
            for r_line in replace_lines:
                if not r_line.strip():
                    new_replace_lines.append("")
                else:
                    new_replace_lines.append(indentation + r_line.lstrip())

            has_crlf = "\r\n" in content
            ending = "\r\n" if has_crlf else "\n"

            before = ending.join(content_lines[:i])
            middle = ending.join(new_replace_lines)
            after = ending.join(content_lines[i + n_search:])

            parts = []
            if before:
                parts.append(before)
            parts.append(middle)
            if after:
                parts.append(after)

            return ending.join(parts) + (ending if content.endswith(ending) else ""), "fuzzy-indentation"

    # 4. Fallback: character-level similarity matching using difflib
    #    Find the best matching region in the content that is similar to the search block
    from difflib import SequenceMatcher

    search_normalized = "\n".join(search_lines)  # stripped, non-empty lines
    best_ratio = 0.0
    best_start = -1
    best_end = -1
    # The minimum similarity threshold to accept a match
    SIMILARITY_THRESHOLD = 0.75

    # Slide a window of similar size across the content
    window_sizes = [n_search, n_search + 1, n_search - 1] if n_search > 1 else [n_search, n_search + 1]
    for window_size in window_sizes:
        if window_size < 1 or window_size > n_content:
            continue
        for i in range(n_content - window_size + 1):
            candidate_lines = [content_lines[i + k].strip() for k in range(window_size) if content_lines[i + k].strip()]
            if not candidate_lines:
                continue
            candidate = "\n".join(candidate_lines)
            ratio = SequenceMatcher(None, search_normalized, candidate).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = i
                best_end = i + window_size

    if best_ratio >= SIMILARITY_THRESHOLD and best_start >= 0:
        first_line = content_lines[best_start]
        indentation = first_line[:len(first_line) - len(first_line.lstrip())]

        replace_lines_list = replace.splitlines()
        new_replace_lines = []
        for r_line in replace_lines_list:
            if not r_line.strip():
                new_replace_lines.append("")
            else:
                new_replace_lines.append(indentation + r_line.lstrip())

        has_crlf = "\r\n" in content
        ending = "\r\n" if has_crlf else "\n"

        before = ending.join(content_lines[:best_start])
        middle = ending.join(new_replace_lines)
        after = ending.join(content_lines[best_end:])

        parts = []
        if before:
            parts.append(before)
        parts.append(middle)
        if after:
            parts.append(after)

        return ending.join(parts) + (ending if content.endswith(ending) else ""), f"difflib-similarity({best_ratio:.0%})"

    return None, "no-match"


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