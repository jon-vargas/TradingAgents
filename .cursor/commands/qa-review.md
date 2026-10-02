---
description: Stage 1 — QA review (claims vs code/config/docs).
argument-hint: "@path/to/plan.md [@docs/qa/feedback/<session>.md]"
---

# /qa-review

You are executing the **qa-review** command.

## Required first step

1. Read **`.cursor/skills/qa-feedback-sessions.md`** and resolve `feedback_path` (never default-append to `docs/qa/FEEDBACK.md`).
2. Read and follow **`.cursor/skills/qa-review/SKILL.md`** (this repo only — do not use `~/.cursor/skills/`).

## Parse inputs

- First `@` markdown file → `plan_path` (unless clearly a feedback session file)
- Second `@` or `feedback_path=` → session feedback; else ACTIVE / create per `qa-feedback-sessions.md`
- Echo resolved `feedback_path` before writing

## Execute

Run the full skill workflow: extract claims, verify evidence, classify findings, **append** to the session feedback file, print severity summary.

## Next command

`/qa-reconcile @<plan_path> @<feedback_path>`
