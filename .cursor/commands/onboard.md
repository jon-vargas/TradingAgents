---
description: Bootstrap a new agent chat with TradingAgents repo context, continuity scan, and a Working Brief.
argument-hint: "[focus=screening|webapp|analysis|pipeline|qa]"
---

# /onboard

You are executing the **onboard** command. Do not improvise a different bootstrap.

## Parse inputs

- Optional `focus=` from the user message: `screening` | `webapp` | `analysis` | `pipeline` | `qa`
- Default focus: `general` (core spine only)

## Actions

1. Read and follow **`.cursor/skills/project-onboard/SKILL.md`** (this repo only — do not use `~/.cursor/skills/`).
2. Run the full workflow including the **continuity scan** (SESSION.md, feedback blockers, dirty tree, recent commits).
3. Emit the Working Brief template from that skill.
4. Ask once: **What are you trying to ship in this chat?**
