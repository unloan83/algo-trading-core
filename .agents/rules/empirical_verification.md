# Mandatory Empirical Verification Rules

## MANDATORY RULES BEFORE CLAIMING TASK COMPLETION OR STATUS

1. **Empirical Output Required**:
   - NEVER report status (tests passing, system active, credentials synced, git pushed) based on assumption or code edit alone.
   - Every status statement MUST be backed by actual CLI execution output run during the turn.

2. **No Speculation**:
   - If a background process, API call, or git push fails or is incomplete, report the exact failure output immediately.
   - Do not claim a process is working until execution returns clean status output (`exit code 0`).

3. **Zero Credential Exposure**:
   - Never print, ask for, display, or leak API keys, tokens, or secret credentials in chat or artifacts.
   - Sync credentials silently from local `.env` files.
