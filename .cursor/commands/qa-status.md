---
description: Report QA loop status from session feedback file(s).
argument-hint: "[@docs/qa/feedback/<session>.md]"
---

# /qa-status

Report loop state from session feedback (not a single shared FEEDBACK dump).

## Required first step

Read and follow **`.cursor/skills/qa-status/SKILL.md`** (this repo only — not `~/.cursor/skills/`).

## Parse inputs

- Optional `@` → `feedback_path`
- Else use `docs/qa/feedback/ACTIVE.md` if present

## Execute

Run the full skill workflow and emit the status report.
