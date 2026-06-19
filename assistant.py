"""Command-line entry point for the CI build assistant."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime

from src.ci_build_assistant import analyze_build_log, read_build_log, run_agent_loop
from src.ci_build_assistant.config import load_settings


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
        type=Path,
        help="Path to the build log text file.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the analysis in raw JSON format instead of the human-readable dashboard.",
    )
    parser.add_argument(
        "--mode",
        choices=["diagnose", "agent"],
        default="diagnose",
        help="Execution mode: 'diagnose' (default - print report) or 'agent' (observe and trigger active fixes).",
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