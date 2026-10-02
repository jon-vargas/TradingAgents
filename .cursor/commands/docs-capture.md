---
description: Capture this chat's changes into the owning TradingAgents docs (SSOT). Optional dry-run.
argument-hint: "[dry-run]"
---

# /docs-capture

You are executing the **docs-capture** command. Do not improvise a full docs rewrite.

## Parse inputs

- `dry-run` if present in the user message → `mode=dry-run`
- Otherwise `mode=apply`
- Optional `@path` mentions → candidate docs to check

## Actions

1. Read and follow **`.cursor/skills/docs-capture/SKILL.md`** (this repo only — do not use `~/.cursor/skills/`).
2. Gather evidence from this chat + git; classify → SSOT docs via the skill ownership map.
3. Apply or list edits per mode.
4. Emit the **Docs capture report** from the skill.
