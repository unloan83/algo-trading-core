# Mandatory Git Remote Synchronization Rules

## NON-NEGOTIABLE REQUIREMENTS FOR GIT OPERATIONS

1. **Empirical Verification of Git Status**:
   - NEVER state or claim that code is "in sync with git", "committed", or "updated" without executing:
     ```bash
     git status -uno
     git remote -v
     ```
   - Verify that `origin` remote exists and that `git push` has succeeded.

2. **Remote Origin Check**:
   - Every active repository MUST have `origin` configured to the target remote URL (e.g. GitHub `https://github.com/unloan83/...`).
   - Before confirming git sync to the user, inspect `git status` output to verify:
     ```text
     On branch master (or main)
     Your branch is up to date with 'origin/master' (or 'origin/main')
     ```

3. **No Unverified Claims**:
   - Stating that code is saved or synced to git when `git push` has not executed or remotes are missing is strictly prohibited.
