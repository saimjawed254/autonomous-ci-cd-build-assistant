# AI-Powered CI/CD Build Assistant

A GitHub Action that uses Google Gemini to automatically diagnose build failures, post fix suggestions on your Pull Requests, and optionally retry failed builds.

When your CI pipeline fails, this action reads the build log, sends it to Gemini for analysis, and comments a structured diagnosis directly on your PR — including the root cause, confidence level, and step-by-step fix instructions.

---

## Setup Guide

### Prerequisites

You need one thing: a **Gemini API key** from [Google AI Studio](https://aistudio.google.com/apikey).

Add it as a repository secret:
1. Go to your repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
3. Name: `GEMINI_API_KEY`, Value: your key

---

### Step 1: Update Your CI Workflow

Open your existing CI workflow file (e.g. `.github/workflows/ci.yml`).

You need to do two things:

**A) Pipe your test output to a log file.** Modify your test step so the output is saved. For example:

```yaml
- name: Run Tests
  run: |
    pytest 2>&1 | tee build_failure.log
```

> The key part is `2>&1 | tee build_failure.log`. This captures both stdout and stderr into a file while still printing to the console. You can use this with any test command — `npm test`, `go test`, `cargo test`, etc.

**B) Add the assistant step after your tests.** Place this right after your test step:

```yaml
- name: AI Build Assistant
  if: failure()
  uses: saimjawed254/accenture-intern-assignment@main
  with:
    log-file: 'build_failure.log'
    gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
```

That's it. Your workflow will now automatically diagnose failures and post comments on PRs.

**Important:** Your job needs these permissions for PR commenting to work:

```yaml
permissions:
  contents: write
  pull-requests: write
```

---

### Step 2 (Optional): Enable Auto-Retry

By default, the assistant only diagnoses and comments. If you want it to also **automatically retry failed builds** (up to 3 attempts), add a second workflow file.

Create `.github/workflows/retry.yml` in your repo with this content:

```yaml
name: Auto-Retry Failed Builds

on:
  workflow_run:
    workflows: ["YOUR_CI_WORKFLOW_NAME"]  # Replace with your CI workflow's name
    types: [completed]

jobs:
  retry:
    runs-on: ubuntu-latest
    if: ${{ github.event.workflow_run.conclusion == 'failure' && github.event.workflow_run.run_attempt < 3 }}
    permissions:
      actions: write
    steps:
      - name: Rerun failed jobs
        run: gh run rerun ${{ github.event.workflow_run.id }} --failed --repo ${{ github.repository }}
        env:
          GH_TOKEN: ${{ secrets.PAT_TOKEN }}
```

**Replace `YOUR_CI_WORKFLOW_NAME`** with the exact `name:` from your CI workflow file (e.g. `"CI Check"` or `"Build and Test"`).

**Why a PAT?** GitHub blocks the default `GITHUB_TOKEN` from triggering other workflows (to prevent infinite loops). A Personal Access Token is needed so the retry chain works beyond a single rerun.

To create the PAT:
1. Go to GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)**
2. Generate a token with `repo` and `workflow` scopes
3. Add it as a repository secret named `PAT_TOKEN`

> Without a PAT, the retry will only fire once. With a PAT, it retries up to 3 times before the safety guardrail halts.

---

## Full Example

Here is a complete CI workflow for a Python project using pytest:

```yaml
name: CI Check

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run Tests
        run: pytest 2>&1 | tee build_failure.log

      - name: AI Build Assistant
        if: failure()
        uses: saimjawed254/accenture-intern-assignment@main
        with:
          log-file: 'build_failure.log'
          gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
```

---

## Action Inputs

| Input | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `log-file` | Yes | — | Path to the build log file |
| `gemini-api-key` | Yes | — | Your Gemini API key |
| `gemini-model` | No | `gemini-3.1-flash-lite` | Gemini model to use |
| `mode` | No | `agent` | `agent` (PR comments + retry) or `diagnose` (log output only) |
| `github-token` | No | `github.token` | Token for PR commenting (auto-provided by GitHub) |

---

## How It Works

1. **Your tests fail** → the build log is captured to a file.
2. **The assistant reads the log** and sends it to Gemini for analysis.
3. **Gemini returns a structured diagnosis**: failure category, root cause, confidence level, and fix steps.
4. **A comment is posted on your PR** with the diagnosis and suggested fix.
5. **If auto-retry is enabled**, the failed build is rerun automatically (up to 3 times).
6. **Safety guardrail**: After 3 failed attempts, the assistant stops retrying to prevent runaway costs. It tracks past attempts using hidden metadata in PR comments so it never suggests the same fix twice.

If Gemini is unavailable (API down, key missing, timeout), the assistant falls back to a built-in rule-based classifier that diagnoses common errors offline — your pipeline never crashes because of the assistant.

---

## Local CLI Usage

You can also run the assistant locally on any log file:

```bash
python assistant.py path/to/build.log
```

For JSON output (useful for scripting):

```bash
python assistant.py path/to/build.log --json
```

Requires a `.env` file at the repo root with `GEMINI_API_KEY=your_key`.