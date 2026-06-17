"""Local JSON-file history store to avoid duplicate fix attempts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


def get_log_signature(content: str) -> str:
    """Generate a unique SHA-256 error signature from log contents."""

    # Normalize whitespaces to avoid minor formatting differences altering the hash
    normalized = " ".join(content.split())
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()


def load_history(history_path: Path) -> dict:
    """Load the JSON database dictionary, returning empty dict if missing."""

    if not history_path.exists():
        return {}
    try:
        return json.loads(history_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_history(history_path: Path, data: dict) -> None:
    """Write the dictionary history database to disk."""

    try:
        history_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        # Avoid crashing the build if file operations fail on the runner
        print(f"Warning: could not write history state: {exc}")


def get_past_attempts(history_path: Path, log_signature: str) -> list[str]:
    """Retrieve all fix suggestions previously attempted for this log signature."""

    history = load_history(history_path)
    entry = history.get(log_signature, {})
    attempts = entry.get("attempts", [])
    
    # We want to retrieve all suggestions that were attempted (both failed and active/running)
    return [item["fix_suggestion"] for item in attempts if item.get("fix_suggestion")]


def record_attempt(history_path: Path, log_signature: str, fix_suggestion: str, status: str) -> None:
    """Record or update a fix suggestion attempt in history."""

    history = load_history(history_path)
    if log_signature not in history:
        history[log_signature] = {
            "first_seen": datetime.now().isoformat(timespec="seconds"),
            "attempts": []
        }

    attempts = history[log_signature]["attempts"]
    
    # Check if this exact fix was already recorded; if so, update its status/timestamp
    for attempt in attempts:
        if attempt.get("fix_suggestion") == fix_suggestion:
            attempt["status"] = status
            attempt["updated_at"] = datetime.now().isoformat(timespec="seconds")
            save_history(history_path, history)
            return

    # Otherwise, add a new attempt item
    attempts.append({
        "fix_suggestion": fix_suggestion,
        "status": status,
        "timestamp": datetime.now().isoformat(timespec="seconds")
    })
    save_history(history_path, history)
