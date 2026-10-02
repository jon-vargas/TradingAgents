---
description: Initialize QA loop metadata and a unique session feedback file.
argument-hint: "@path/to/plan.md"
---

# /qa-start

Initialize a new QA loop with its **own** feedback file (no shared `FEEDBACK.md` writes).

## Required first step

Read and follow **`.cursor/skills/qa-start/SKILL.md`** (this repo only — not `~/.cursor/skills/`).

## Parse inputs

- First `@` file → `plan_path` (required)
- Optional explicit `@docs/qa/feedback/….md` or `feedback_path=…` → use that path instead of creating a new one

## Execute

Run the full skill workflow and echo recommended next command (`/qa-loop` or `/qa-review`).
