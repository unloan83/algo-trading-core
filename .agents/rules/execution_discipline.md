# Execution Discipline Rules
### Extends `.agents/rules/empirical_verification.md` and `.agents/rules/git_remote_sync.md` — read both before this one.

Every rule below maps to a real incident from this project's history, noted in brackets. These are not style preferences — violating them is what caused every repeated-patching cycle so far.

---

## 1. No fabricated placeholder values, ever
If a value cannot be computed (missing data, failed API call, zero/negative equity), the function must return an explicit error/reason code carrying the **true** value — never a placeholder chosen to satisfy a type constraint or "look reasonable."
[Incident: `equity_now=-4500` was once returned as `equity_now=1.0` to satisfy a Pydantic `gt=0` constraint, hiding the real number from every downstream log and alert.]

**Rule:** before adding any default, ask "does this represent reality, or does it exist to make the code not crash?" If the latter — remove the default, raise/return an explicit failure instead.

## 2. No silent spec deviations
If an implementation differs from what was actually specified (e.g. scaling down a trade size instead of rejecting it outright), this must be surfaced explicitly as a flagged decision requiring sign-off — never shipped silently under the same function signature.
[Incident: cash/open-risk breaches were silently changed from "hard reject" to "scale down qty" without flagging it; this went unnoticed for a full review cycle.]

**Rule:** any deviation from an explicit prior instruction gets a one-line callout: "Note: implemented as X instead of Y because Z — confirm before proceeding," not a description that reads as if it matches the original ask.

## 3. Full test suite, real output, every time
"Tests passed" is only a valid claim when the **entire** suite was executed in that turn and the raw output is pasted — not a subset, not a manually re-typed summary, not a description of what the tests check.
[Incident: multiple rounds reported "16/16 passed" or "11/11 passed" based on partial runs or hand-written summaries; the first full `pytest tests/ -v` actually run in this project surfaced 3 previously-unreported failures.]

**Rule:** `pytest tests/ -v` (full path, no filters) runs at the end of every change, and its literal stdout is what gets reported — not a paraphrase, not a subset re-run of "the tests I touched."

## 4. Dependency injection over hidden globals — especially near risk logic
Any function feeding into risk, circuit-breaker, or position-sizing decisions must receive its inputs as explicit parameters, never by reaching into `os.environ` or another global directly. If it does, it can't be tested in isolation and a config-loading failure in production silently misreports as an unrelated error.
[Incident: `check_system_blockers()` called `paper_starting_capital()`, which read `PAPER_STARTING_CAPITAL` directly from the OS environment, bypassing every injected mock and causing real circuit-breach conditions to be masked by an unrelated "capital missing" error.]

**Rule:** if a function's correctness depends on environment state the caller didn't explicitly pass in, that's a defect to fix, not a detail to work around in the test.

## 5. Dead code does not stay in the repo "just in case"
A script not wired into any scheduler (systemd timer, cron, explicit call site) is not neutral — it's a loaded gun. Either delete it, or move it to a clearly-labeled `scripts/dev_only/` or `scripts/deprecated/` path with a docstring stating it is not part of the runtime path and why it still exists.
[Incident: `regime_check.py` generated fake regime data via `hash(str(date)) % 100` and sat unscheduled in `scripts/` for multiple review cycles before being caught; `eod_reconcile.py` always returned a trivial true-by-construction pass the same way.]

**Rule:** before ending a task, list every file touched this session and state explicitly, for each one, whether it is wired into the runtime path or not. If "not," justify why it still exists.

## 6. Docs and config must be re-verified whenever behavior changes
Changing an implementation detail (e.g. switching Telegram from webhook to long-polling) requires grepping the **entire** repo — including `.md` files and `config/*.yaml` — for references to the old behavior, and updating them in the same change.
[Incident: `security_rules.md` documented an inbound port-443 webhook firewall rule for a bot that had already been implemented as long-polling; the doc and the code disagreed for an unknown number of review cycles.]

**Rule:** `grep -ri` the changed concept across the whole repo before declaring a change complete, not just in the file you edited.

## 7. Read existing rules/spec files completely before writing new ones
Before creating any new governance doc, config file, or module, check whether an equivalent already exists (`.agents/rules/`, `config/`, `core/`) and extend it rather than duplicating or silently contradicting it.
[Incident: this very file only avoided duplicating `empirical_verification.md` and `git_remote_sync.md` because they were read first — that check should be automatic, not something a reviewer has to prompt for.]

## 8. External facts get verified in-session, not recalled from training
Any specific claim about a third-party API's behavior, a tax/regulatory rate, or a library's current limits must be checked against current documentation or actual live behavior in the same session — never stated confidently from memory alone.
[Incident: brokerage-rate assumptions in `cost_model.py` were never checked against Upstox's actual current published plan; Upstox's real access-token/analytics-token behavior was initially guessed at before being verified against docs.]

**Rule:** if a fact could have changed since the agent's training data was last updated, or is specific to a named third party, verify it live before relying on it in code or in a claim of correctness.

## 9. One fix, one verification, before moving to the next
When a punch list has multiple items, each one is verified individually (targeted test or command) before starting the next — not batched into a pile of changes verified once at the end via a single broad claim.
[Incident: rounds of "N fixes applied" were reported together with a single aggregate "all tests passed" line, which turned out to still contain unfixed or newly-broken items once actually checked one at a time.]

## 10. Diff-based reporting for changes to existing files
When modifying a file that already exists (not creating it fresh), report what specifically changed relative to the prior version — a diff, or an explicit before/after — not just "Updated X.py" with no comparison to what was there before.
[Incident: the same files were repeatedly reported as "Created X.py" across many turns with no way to tell what had actually changed between versions, making regressions invisible until an external review caught them.]

---

## Pre-execution checklist (run through this before starting any task)
- [ ] Have I read every existing `.agents/rules/*.md` file relevant to this task?
- [ ] Am I about to introduce any default/placeholder value? If yes — does it represent a real state, or does it exist to avoid a crash?
- [ ] Does my planned implementation match what was actually asked, exactly? If not, will I flag the deviation explicitly?
- [ ] Will I run the full test suite (not a subset) and report its literal output when I'm done?
- [ ] Am I creating any file that duplicates something that already exists?
- [ ] Am I leaving any script un-wired into the runtime path without explicitly labeling it as such?
- [ ] Does anything I'm touching reference an external API, tax rule, or library behavior I should verify live rather than assume?
