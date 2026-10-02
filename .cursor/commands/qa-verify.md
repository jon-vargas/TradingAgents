---
description: Post-implementation verification against plan acceptance criteria.
argument-hint: "@path/to/plan.md @docs/qa/feedback/<session>.md"
---

# /qa-verify

You are executing the **qa-verify** command.

## Required first step

1. Read **`.cursor/skills/qa-feedback-sessions.md`** and resolve `feedback_path`.
2. Read and follow **`.cursor/skills/qa-verify-implementation/SKILL.md`** (this repo only).

## Parse inputs

- First `@` → `plan_path`
- Second `@` → `feedback_path` (prefer explicit; else ACTIVE — same session as the loop)

## Execute

Build checklist from acceptance criteria, verify with code/tests/docs evidence, append verification table and verdict to the **session** feedback file.
