---
name: project-onboard
description: >-
  Bootstraps a Cursor agent into the TradingAgents repo with a Working Brief,
  including continuity from recent git/QA/WIP activity. Use when the user runs
  /onboard or asks for project context / resume prior work.
disable-model-invocation: true
---

# Project onboard (TradingAgents)

Use this skill when `/onboard` is invoked or the user asks to onboard into the repo.

**Project-local only:** `.cursor/skills/project-onboard/SKILL.md` in this repo — do not use `~/.cursor/skills/`.

## Required inputs

- `focus` — optional: `screening` | `webapp` | `analysis` | `pipeline` | `qa` | `general` (default)

## Workflow

### 1. Read tracked docs (in order)

Always:

1. `docs/README.md`
2. `docs/ARCHITECTURE.md` — focus on **§1 Runtime Model** and relevant pipeline section
3. `README.md` (repo root quickstart)

Then by focus:

| Focus | Additional reads |
|-------|------------------|
| `general` | (none) |
| `screening` | `tradingagents/screening/engine.py` header, `USER_MANUAL.md` screening section, `docs/qa/README.md` |
| `webapp` | `webapp/app.py` route map (grep `@app`), `webapp/templates/`, `Makefile` ui targets |
| `analysis` | `tradingagents/graph/trading_graph.py`, `tradingagents/research.py`, report templates |
| `pipeline` | `scripts/`, `Makefile` help, `tradingagents/dataflows/` |
| `qa` | `docs/qa/README.md`, `.cursor/skills/qa-feedback-sessions.md`, `.cursor/skills/docs-capture/SKILL.md`, list `docs/qa/feedback/*.md` |

### 2. Inspect repo reality

- Brief `git status -sb` and current branch (dirty-tree summary only; **no commit**)
- Last ~5 commits: `git log -5 --oneline`
- Skim `Makefile` for focus-relevant targets (`make help` section)

### 3. Continuity scan

#### 3a. Session handoff

If `docs/qa/SESSION.md` exists, read it first. Strongest resume signal.

#### 3b. QA blockers

Scan **all** `docs/qa/feedback/*.md` (except `README.md` / `ACTIVE.md`) plus legacy `docs/qa/FEEDBACK.md`:

- Bare `open` **CRITICAL** / **SIGNIFICANT** → implementation blockers (summarize by file)
- `open (pending implementation)` — not a gate blocker
- Note `docs/qa/feedback/ACTIVE.md` session path and `loop_status` / verdict if present
- Procedure: `.cursor/skills/qa-feedback-sessions.md`

#### 3c. Working tree + WIP

- Summarize dirty paths by area (`tradingagents/`, `webapp/`, `tests/`, `scripts/`, `docs/`)
- Mention open Cursor plans under `~/.cursor/plans/` only if SESSION or user points at one

#### 3d. Prior chat titles (optional)

If agent transcripts exist for this workspace, list 2–4 recent titles only.

### 4. Emit Working Brief

```markdown
## TradingAgents working brief
**Focus:** …
**Branch / dirty tree:** …
**Recent commits:** … (≤5 one-liners)
**Repo map:** …
**Relevant paths:** …
**Key commands:** … (e.g. `make ui`, `python -m pytest tests/…`, `make screen-all`)
**Resume / in progress:** … (SESSION.md + dirty areas; or “nothing obvious”)
**Open blockers:** … (session feedback CRITICAL/SIGNIFICANT bare `open` by file, or “none”)
**Recent chats:** … (optional)
**Guardrails:** …
**Suggested next step:** …
```

Keep it concise (~one screen). Prefer 3–6 key commands for the focus.

### 5. One question

End with exactly: **What are you trying to ship in this chat?**

## End-of-chat handoff (when asked)

When the user invokes `/handoff` or asks to wrap up:

1. Write or replace `docs/qa/SESSION.md` (gitignored):

```markdown
# Session handoff
**Updated:** <ISO date>
**Focus:** <domain>
**Goal:** <one line>
**Done this chat:** <bullets>
**Still open:** <bullets>
**Key paths / artifacts:** <paths, plan paths, run ids>
**Suggested next step:** <one concrete action>
**Related chat:** <optional>
```

2. Keep it ≤40 lines. Do not commit unless asked.
3. Confirm path and remind user to start next chat with `/onboard`.

## Rules

- Do not invent Makefile targets — verify against `make help`.
- Do not paste entire feedback sessions into chat; summarize and link paths.
- Never append new loop findings to legacy `docs/qa/FEEDBACK.md`.
