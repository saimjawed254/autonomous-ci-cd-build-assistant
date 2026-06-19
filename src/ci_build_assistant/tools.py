"""GitHub API network tools for PR commenting and comment retrieval."""

from __future__ import annotations

import json
from urllib import error, request


def post_pr_comment(repo: str, pr_number: int, comment: str, token: str) -> None:
    """Post a diagnostic report comment onto a GitHub Pull Request."""

    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    payload = {"body": comment}
    body = json.dumps(payload).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "CI-Build-Assistant",
        "Content-Type": "application/json",
    }

    http_request = request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=15) as response:
            response.read()
    except error.URLError as exc:
        raise RuntimeError(f"Failed to post PR comment: {exc}") from exc


def get_pr_comments(repo: str, pr_number: int, token: str) -> list[str]:
    """Retrieve all comments posted on a Pull Request issues thread."""

    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "CI-Build-Assistant",
    }

    http_request = request.Request(
        url,
        headers=headers,
        method="GET",
    )

    try:
        with request.urlopen(http_request, timeout=15) as response:
            raw_bytes = response.read()
    except error.URLError as exc:
        raise RuntimeError(f"Failed to fetch PR comments: {exc}") from exc

    try:
        comments_list = json.loads(raw_bytes.decode("utf-8"))
        if isinstance(comments_list, list):
            return [str(comment.get("body", "")) for comment in comments_list if comment.get("body")]
    except (json.JSONDecodeError, ValueError):
        pass

    return []
