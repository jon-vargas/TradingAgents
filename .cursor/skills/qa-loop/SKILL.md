---
name: qa-loop
description: Full QA loop orchestration — review, reconcile, and gate in a single turn.
disable-model-invocation: true
---

# QA Loop (orchestrator)

Use when `/qa-loop` is invoked.

**Project-local only:** `.cursor/skills/qa-loop/SKILL.md` in this repo — do not use `~/.cursor/skills/`.

**Important:** Run all three stages in **this single turn**. Do not ask the user to run nested slash commands.

## Required inputs

- `plan_path` — first `@` file
- `feedback_path` — optional second `@` if clearly a feedback/session file; else resolve/create per **`../qa-feedback-sessions.md`**

## Workflow

1. Read **`../qa-feedback-sessions.md`**. Resolve **one** `feedback_path` for the entire loop (create session file + `ACTIVE.md` if needed). Echo it before Stage 1.
2. Do **not** default writes to `docs/qa/FEEDBACK.md`.

### Stage 1 — Review

1. Read and follow **`../qa-review/SKILL.md`**.
2. Append findings to the resolved `feedback_path`.

### Stage 2 — Reconcile

1. Read and follow **`../qa-reconcile/SKILL.md`**.
2. Update `plan_path` and append reconciliation matrix to the **same** `feedback_path`.

### Stage 3 — Gate

1. Read and follow **`../qa-gate/SKILL.md`**.
2. Append gate verdict to the **same** `feedback_path`.

## Output contract

End with: findings count, reconciliation status, **Build Ready** / **Not Build Ready**, and the `feedback_path` used.

If Build Ready, user may proceed to implementation; then `/qa-verify @<plan_path> @<feedback_path>`.
