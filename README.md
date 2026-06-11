# AI-Powered CI/CD Build Assistant

Rule-based Week 1 CLI prototype for CI/CD build failure diagnosis.

## Assignment Status (Week 1)

- Completed: CLI accepts any log file path and reads build logs from disk.
- Completed: failure classification into `dependency_error`, `test_failure`, `config_issue`, and `unknown`.
- Completed: plain-text fix suggestion per detected failure type.
- Completed: clear console output with file stats, diagnosis details, and raw log body.

## How To Run

Use your Python environment and pass any log file path.

```powershell
python assistant.py "C:\path\to\your\build.log"
```

## Console Output You Should Expect

The CLI prints:

1. log metadata: file path, character count, line count
2. diagnosis block: failure type, confidence, matched pattern, evidence, suggested fix
3. original log content for manual verification

## Next Step (Week 2)

Replace rule-based diagnosis with LLM-based diagnosis while keeping this CLI contract stable.
