---
name: docs-capture
description: >-
  Captures documentation updates from the current chat's work and git change
  set, then updates only the owning TradingAgents docs (SSOT). Use when the user
  runs /docs-capture, asks to update docs after a change, or wants post-update
  documentation capture without a full docs rewrite.
disable-model-invocation: true
---

# Docs capture (TradingAgents)

Use after meaningful code/process changes in this chat. Goal: record what just
changed into the **canonical** docs — not rewrite the whole spine.

**Project-local only:** `.cursor/skills/docs-capture/SKILL.md` in this repo — do not use `~/.cursor/skills/`.

## Required inputs

- `mode` — optional: `apply` (default) | `dry-run`
- Optional `@path` hints — treat as extra candidate docs to check

## Workflow

### 1. Gather evidence (this chat only)

Do not invent project-wide history. Use:

1. **This conversation** — features shipped, APIs/scripts added, screening/ranking behavior, new commands/skills.
2. **Git** — `git status -sb`, `git diff` (staged + unstaged), and if helpful `git log -5 --oneline`.
3. **Ownership map** — read `docs/README.md` and the table below.

Skip capture when the only work was:

- Pure data ops (one-off screen run, local `research.db` inspection) with **no** process/tooling/schema/UI contract change
- Typo-only or formatting-only edits with no operator-facing contract change
- Local-only artifacts (`docs/qa/feedback/*.md`, `docs/qa/FEEDBACK.md`, `docs/qa/SESSION.md`, `logs/`, cache dirs)

If nothing doc-worthy: say so and stop (no file edits).

### 2. Classify change → target docs

Map evidence to **one or more** classes. Prefer the SSOT; update secondary docs only if they duplicate stale facts (or replace duplication with a link).

| Change class | Primary doc(s) | Also consider |
|--------------|----------------|---------------|
| Architecture / runtime / module boundaries | `docs/ARCHITECTURE.md` | `README.md` system map if user-facing |
| Screening engine, signals, presets, Scan All | `docs/ARCHITECTURE.md` § screening | `USER_MANUAL.md` operator commands |
| Reversal buildup / phase gates / ranking | `docs/ARCHITECTURE.md` reversal section | `tradingagents/default_config.py` comments only if config keys are the contract |
| Webapp API routes / UI behavior | `USER_MANUAL.md`, `docs/ARCHITECTURE.md` § web | `webapp/templates/` only if no runbook exists |
| New/changed `make` targets or `scripts/` | `USER_MANUAL.md`, `Makefile` help text | `README.md` quickstart |
| Analysis / agent graph / reports | `docs/ARCHITECTURE.md` § analysis | `USER_MANUAL.md` analyze section |
| Database schema / persistence | `docs/ARCHITECTURE.md` § data model | migration notes in code comments if no schema doc |
| Config / env vars | `USER_MANUAL.md`, `.env.example` | `tradingagents/default_config.py` |
| Tests / QA gates | `docs/qa/README.md` if workflow changed | `Makefile` `stabilization-gate` mention in ARCHITECTURE |
| Agent/Cursor workflow (commands, skills, `/onboard`, QA loop) | `docs/qa/README.md`, `.cursor/README.md` | `docs/README.md` index link |
| User-visible milestone / shipped phase | `docs/CHANGELOG.md` | one-line in `README.md` if major |

**Do not edit** unless the chat explicitly changed them:

- Cursor plan files under `~/.cursor/plans/` unless user asked
- Append-only session feedback under `docs/qa/feedback/` (or legacy `docs/qa/FEEDBACK.md`)
- Golden HTML fixtures under `tests/goldens/` unless rendering contract intentionally changed (note in CHANGELOG instead)

### 3. Verify before writing

For each candidate edit:

1. Open the target doc section that should own the fact.
2. Confirm the new fact against **code or config** (`webapp/app.py`, `default_config.py`, `Makefile`) — not memory alone.
3. Prefer **minimal surgical edits**: update tables, bullets, command names, paths.
4. If unsure whether code or doc is intentional target-state, **do not guess** — list under **Needs human decision**.

### 4. Apply or dry-run

**`dry-run`:** list proposed edits (path + one-line why + snippet intent). No writes.

**`apply` (default):** make the edits. Keep tone/structure of each doc. Do not mass-reformat unrelated sections.

Changelog: add `docs/CHANGELOG.md` only when the change is user-visible or a meaningful shipped phase note — not for every skill tweak.

### 5. Report

Always end with:

```markdown
## Docs capture report
**Mode:** apply | dry-run
**Change summary:** <1–3 lines from this chat>
**Updated:**
- `path` — <what changed>
**Skipped:**
- <item> — <reason>
**Needs human decision:**
- <item or “none”>
**Suggested commit note:** <one sentence; do not commit unless asked>
```

## Rules

- Evidence from **this chat + git** beats prior assumptions.
- One fact, one SSOT; link elsewhere instead of duplicating long runbooks.
- Do not invent Makefile targets, env vars, or API routes — verify or skip.
- Do not commit unless the user explicitly asks.
- Do not treat `/docs-capture` as a full documentation rewrite.
