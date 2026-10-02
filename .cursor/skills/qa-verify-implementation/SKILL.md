---
name: qa-verify-implementation
description: Verify delivered implementation against plan acceptance criteria.
---

# QA Verify Implementation

Use this skill when `/qa-verify` is invoked.

**Project-local only:** `.cursor/skills/qa-verify-implementation/SKILL.md` in this repo — do not use `~/.cursor/skills/`.

## Required inputs

- `plan_path`
- `feedback_path` — resolve via **`../qa-feedback-sessions.md`** (prefer the same session as the loop)

## Workflow

1. Resolve and echo `feedback_path`.
2. Build a verification checklist from plan acceptance criteria and explicit claims.
3. Verify behavior against code, config, docs, and available runtime evidence.
4. Run relevant tests where applicable, e.g.:
   - `python -m pytest tests/test_<area>.py -q`
   - `make qa` or targeted `make test` when appropriate
   - Do not require live Yahoo/API keys for unit-test verification
5. Mark each criterion `pass`, `partial`, or `fail` with evidence.
6. Append `## Post-Implementation QA Verification` to the session feedback file with a table and verdict:
   - `Verified`
   - `Verified with Issues`
   - `Not Verified`

## Rules

- Do not infer correctness from labels alone; require evidence.
- Unresolved critical/significant mismatches → `Not Verified`.
- For UI changes, cite template/JS paths and API routes probed; browser optional unless AC requires it.
