# AI-Powered CI/CD Build Assistant

Just Testing 

Week 2 of the CI/CD build assistant is now wired for Gemini-backed structured diagnosis only.

## Current Status

- CLI accepts any log file path and reads build logs from disk.
- Gemini integration is the only analysis path when `GEMINI_API_KEY` is available.
- The CLI prints structured JSON diagnosis output followed by the raw log body.
- If the Gemini request fails or the JSON is invalid, the CLI prints the reason to `stderr` and exits non-zero.

## Runtime Files

The pushable runtime surface is intentionally small:

- `assistant.py` - CLI entrypoint
- `src/ci_build_assistant/parser.py` - log parsing
- `src/ci_build_assistant/schema.py` - shared diagnosis types
- `src/ci_build_assistant/config.py` - `.env` and environment loading
- `src/ci_build_assistant/prompts.py` - Gemini prompt templates
- `src/ci_build_assistant/llm_client.py` - Gemini REST client
- `src/ci_build_assistant/analysis.py` - orchestration and JSON parsing
- `src/ci_build_assistant/__init__.py` - package exports

The archived Week 1 classifier is stored under `unwanted/` for reference and is not part of the active runtime path.

Non-essential artifacts were moved into the ignored `unwanted/` folder.

## Environment Variables

Add these to your `.env` file at the repo root:

```env
GEMINI_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-1.5-flash
GEMINI_TEMPERATURE=0.2
GEMINI_TIMEOUT_SECONDS=30
GEMINI_MAX_OUTPUT_TOKENS=1024
```

If `GEMINI_API_KEY` is missing, the assistant exits with a clear configuration error.

## How To Run

```powershell
python assistant.py "C:\path\to\your\build.log"
```

## Output Shape

By default, the CLI prints a beautiful, emoji-enriched terminal report dashboard featuring:
- Log stats (character count, line count) and model name.
- Status badges for confidence level (🟢 HIGH, 🟡 MEDIUM, 🔴 UNCERTAIN).
- Sectioned details for **Failure Category**, **Root Cause**, **Evidence**, **Suggested Fix**, and numbered **Step-by-Step Recovery Actions**.
- The raw build log contents at the end.

If you specify the `--json` option, the CLI outputs a structured JSON diagnosis object (ideal for automation/integrations):
```powershell
python assistant.py "C:\path\to\your\build.log" --json
```

## GitHub Action Usage

The assistant is packaged as a reusable composite GitHub Action. To integrate it into any repository workflow on failure, add a step like this:

```yaml
- name: Run AI Build Assistant
  if: failure()
  uses: saimjawed254/accenture-intern-assignment@main
  with:
    log-file: 'build_failure.log'
    gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
    mode: 'agent'
```

### Action Inputs

| Input | Description | Required | Default |
| :--- | :--- | :--- | :--- |
| `log-file` | Path to the build log file to analyze | **Yes** | N/A |
| `gemini-api-key` | API Key for Gemini (Google AI Studio) | **Yes** | N/A |
| `gemini-model` | Gemini model identifier | No | `gemini-3.1-flash-lite` |
| `mode` | Execution mode: `diagnose` (prints report) or `agent` (PR comments + re-run) | No | `agent` |

### Robust Fallback Mode

If Gemini is unreachable, the API key is not configured, or network timeouts occur, the assistant automatically falls back to a local **Rule-Based Classifier** that processes log keywords offline to diagnose issues, ensuring the pipeline never crashes.

## Project Status

- **Completed (Week 1)**: Core parser and regex-based classifier.
- **Completed (Week 2)**: Gemini client, structured JSON diagnostics, and 10 categories.
- **Completed (Week 3)**: Autonomous Agentic Loop (PR commenting, workflow re-runs, state memory, and safety retry caps).
- **Completed (Week 4)**: Packaged composite Action, defensive fallback logic, and demo configuration.