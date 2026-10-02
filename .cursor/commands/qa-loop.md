---
description: Full QA loop — review, reconcile, gate (single turn).
argument-hint: "@path/to/plan.md [@docs/qa/feedback/<session>.md]"
---

# /qa-loop

You are executing the **qa-loop** orchestration command.

**Important:** Run all three stages in **this single turn**. Do not ask the user to run nested slash commands.

## Required first step

Read and follow **`.cursor/skills/qa-loop/SKILL.md`** (this repo only — not `~/.cursor/skills/`).

## Parse inputs

- First `@` file → `plan_path`
- Second `@` file (optional) → `feedback_path` if clearly a feedback/session file

## Execute

Run the full orchestrator skill: resolve one `feedback_path`, then review → reconcile → gate in this turn. Emit the output contract from the skill.
