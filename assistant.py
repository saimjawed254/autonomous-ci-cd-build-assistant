"""Command-line entry point for the CI build assistant prototype."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.ci_build_assistant import classify_failure, read_build_log


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
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and print diagnosis output."""

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

    print(f"Log file: {build_log.path}")
    print(f"Characters: {build_log.character_count}")
    print(f"Lines: {build_log.line_count}")

    diagnosis = classify_failure(build_log)
    print(f"Failure type: {diagnosis.failure_type.value}")
    print(f"Confidence: {diagnosis.confidence}")
    print(f"Matched pattern: {diagnosis.matched_pattern}")
    print(f"Evidence: {diagnosis.evidence}")
    print(f"Suggested fix: {diagnosis.suggested_fix}")
    print("-" * 72)
    if build_log.is_empty:
        print("[empty log file]")
    else:
        print(build_log.content, end="" if build_log.content.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())