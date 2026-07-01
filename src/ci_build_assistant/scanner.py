"""Multi-layer pre-scan orchestration for CI Build Assistant."""

import subprocess
import sys
from pathlib import Path

from .core import Settings, get_pr_changed_files


def get_changed_files(settings: Settings, root: Path) -> list[str]:
    """Retrieve changed files from PR or via local git diff."""
    print("Identifying modified files in the repository...")
    
    if settings.github_pr_number and settings.github_repository and settings.github_token:
        try:
            return get_pr_changed_files(settings.github_repository, settings.github_pr_number, settings.github_token)
        except Exception as exc:
            print(f"Warning: Could not fetch PR changed files: {exc}", file=sys.stderr)
            
    # Fallback to local git diff
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True
        )
        return [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except Exception as exc:
        print(f"Warning: Local git diff failed: {exc}", file=sys.stderr)
        return []


def get_full_file_contents(files: list[str], root: Path) -> str:
    """Read the full content of all valid changed text files."""
    print(f"Extracting full file contents for {len(files)} files (Deep Scan)...")
    
    parts = []
    for filepath in files:
        full_path = root / filepath
        if not full_path.is_file():
            continue
            
        try:
            content = full_path.read_text(encoding="utf-8")
            parts.append(f"=== FULL FILE CONTEXT: {filepath} ===\n{content}\n")
        except UnicodeDecodeError:
            pass  # skip binary files
            
    return "\n".join(parts)


def scan_files_for_errors(files: list[str], root: Path) -> str:
    """Run syntax and import scans on changed files to gather error snippets."""
    print(f"Running syntax and import scans on {len(files)} files...")
    
    errors = []
    
    for filepath in files:
        if not filepath.endswith(".py"):
            continue
            
        full_path = root / filepath
        if not full_path.is_file():
            continue

        # 1. Syntax Layer Scan
        try:
            # We use subprocess for py_compile to easily capture exact traceback formatting safely
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(full_path)],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                errors.append(f"Syntax Error in {filepath}:\n{result.stderr.strip()}")
                continue  # skip import scan if syntax is broken
        except Exception:
            pass

        # 2. Import / Dependency Layer Scan
        try:
            # Convert filepath to module name (e.g. src/utils/math.py -> src.utils.math)
            module_name = filepath[:-3].replace("/", ".").replace("\\", ".")
            result = subprocess.run(
                [sys.executable, "-c", f"import {module_name}"],
                cwd=str(root),
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                # Filter out the generic python-c traceback header to keep it concise
                err_lines = result.stderr.strip().splitlines()
                filtered_err = "\n".join(line for line in err_lines if 'File "<string>"' not in line)
                errors.append(f"Import/Runtime Error in {filepath}:\n{filtered_err}")
        except Exception:
            pass

    if not errors:
        return ""
        
    parts = ["=== PRE-SCAN SYNTAX/IMPORT ERRORS ==="]
    parts.extend(errors)
    return "\n\n".join(parts) + "\n"
