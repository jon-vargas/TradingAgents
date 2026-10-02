---
name: qa-status
description: Report QA loop status from session feedback files and open blockers across sessions.
disable-model-invocation: true
---

# QA Status

Use when `/qa-status` is invoked.

**Project-local only:** `.cursor/skills/qa-status/SKILL.md` in this repo — do not use `~/.cursor/skills/`.

## Required inputs

- `feedback_path` — optional `@` session file; else `docs/qa/feedback/ACTIVE.md`

## Workflow

1. Read **`../qa-feedback-sessions.md`**.
2. Resolve `feedback_path` (explicit `@` preferred; else ACTIVE).
3. Scan **all** `docs/qa/feedback/*.md` (except `README.md` / `ACTIVE.md`) for bare `open` CRITICAL/SIGNIFICANT blockers. Also note legacy `docs/qa/FEEDBACK.md` if present.

## Report (active/requested session)

- `plan_path` / `focus` from metadata
- `loop_status` / verdict if present
- Finding counts by severity
- Open blockers (bare `open` on CRITICAL/SIGNIFICANT)
- Last completed stage (review / reconcile / gate / verify)
- **Recommended next command** with exact `@plan` and `@feedback_path`

Then list **other** session files with bare `open` CRITICAL/SIGNIFICANT (path + title only).

## Rules

- Do not paste entire session files; summarize counts and titles.
- `open (pending implementation)` is not a gate blocker.
