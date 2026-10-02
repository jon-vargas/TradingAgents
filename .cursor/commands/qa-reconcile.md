---
description: Stage 2 — reconcile QA findings and update plan.
argument-hint: "@path/to/plan.md @docs/qa/feedback/<session>.md"
---

# /qa-reconcile

You are executing the **qa-reconcile** command.

## Required first step

1. Read **`.cursor/skills/qa-feedback-sessions.md`** and resolve `feedback_path`.
2. Read and follow **`.cursor/skills/qa-reconcile/SKILL.md`** (this repo only).

## Parse inputs

- First `@` → `plan_path`
- Second `@` → `feedback_path` (prefer explicit; else ACTIVE — **not** legacy `docs/qa/FEEDBACK.md`)

## Execute

Run the full skill workflow: disposition matrix, plan updates for accepted/partial findings, append reconciliation to the **same** session feedback file.

## Next command

`/qa-gate @<plan_path> @<feedback_path>`
