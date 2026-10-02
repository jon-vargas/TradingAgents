---
name: qa-reconcile
description: Reconcile QA findings with plan updates and disposition matrix.
---

# QA Reconciliation

Use this skill when `/qa-reconcile` is invoked or Stage 2 of `/qa-loop` runs.

**Project-local only:** `.cursor/skills/qa-reconcile/SKILL.md` in this repo — do not use `~/.cursor/skills/`.

## Required inputs

- `plan_path`
- `feedback_path` — resolve via **`../qa-feedback-sessions.md`** (same session file as review)

## Workflow

1. Confirm `feedback_path` (explicit `@` preferred; else ACTIVE). Echo it.
2. Read plan and session feedback; build a 1:1 observation map.
3. For each finding, assign disposition:
   - `accepted` — valid; plan addresses it
   - `partial` — valid; plan partially addresses it
   - `rejected` — invalid or misunderstood
   - `deferred` — valid but out-of-scope
   - `open` — not addressed (blocker if CRITICAL/SIGNIFICANT)
   - `open (pending implementation)` — scoped in plan; not a blocker
4. Update plan sections for accepted/partial findings (only when user/plan path is writable and in scope).
5. Append `## QA Reconciliation` with a matrix:

| OBS | Severity | Finding | Disposition | Plan Section | Residual Risk | Status |

6. Append `## Open Disagreements` for rejected/deferred items with rationale.
7. Set `loop_status: qa_gate_pending` when reconciliation is complete.

## Rules

- Do not remove existing QA notes; append only to the session file.
- Do not skip observations.
- Bare `open` on CRITICAL/SIGNIFICANT remains blocking.
- Recommend next step: `/qa-gate @<plan_path> @<feedback_path>`.
