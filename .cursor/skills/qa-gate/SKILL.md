---
name: qa-gate
description: Apply final build-readiness gate from QA findings and reconciliation state.
---

# QA Gate

Use this skill when `/qa-gate` is invoked or Stage 3 of `/qa-loop` runs.

**Project-local only:** `.cursor/skills/qa-gate/SKILL.md` in this repo — do not use `~/.cursor/skills/`.

## Required inputs

- `plan_path`
- `feedback_path` — resolve via **`../qa-feedback-sessions.md`** (same session as review/reconcile)

## Workflow

1. Confirm `feedback_path` and echo it.
2. Read reconciliation matrix and unresolved findings in that session file.
3. Apply gate criteria — **Build Ready** only when ALL are true:
   - No unresolved CRITICAL observations (status ≠ bare `open`)
   - No unresolved SIGNIFICANT observations (status ≠ bare `open`)
   - Every ADVISORY has explicit disposition and rationale
   - Plan has measurable acceptance criteria (no `TBD`, vague owner, or untestable criteria)
   - NOTE: `open (pending implementation)` is NOT a blocker
4. Append `## QA Gate Decision` to the session feedback file.
5. Verdict: **Build Ready** or **Not Build Ready**.
6. If Not Build Ready: list explicit blockers and exact conditions to pass.
7. Set `loop_status: closed` when Build Ready.

## Rules

- Do not restate all notes; summarize gate-relevant evidence only.
- Do not mark build-ready if any bare `open` CRITICAL/SIGNIFICANT remains in **this** session file.
- Optionally note other session files under `docs/qa/feedback/` that still have bare `open` blockers (do not mix them into this gate verdict).
