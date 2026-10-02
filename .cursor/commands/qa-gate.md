---
description: Stage 3 — build-readiness gate verdict.
argument-hint: "@path/to/plan.md @docs/qa/feedback/<session>.md"
---

# /qa-gate

You are executing the **qa-gate** command.

## Required first step

1. Read **`.cursor/skills/qa-feedback-sessions.md`** and resolve `feedback_path`.
2. Read and follow **`.cursor/skills/qa-gate/SKILL.md`** (this repo only).

## Parse inputs

- First `@` → `plan_path`
- Second `@` → `feedback_path` (prefer explicit; else ACTIVE)

## Execute

Run the full skill workflow and append `## QA Gate Decision` with **Build Ready** or **Not Build Ready** to the session feedback file.

If Build Ready, user may proceed to implementation. If not, list exact blockers.
