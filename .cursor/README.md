# Cursor project config (TradingAgents)

Command-driven QA loop and agent onboard. **Project-local only** — skills and commands live in this repo, not `~/.cursor/skills/`.

**Restart Cursor** (`Cmd+Q`) after changing commands, skills, or rules.

## Layout

| Path | Purpose |
|------|---------|
| `commands/qa-*.md` | Slash command entry points (`/qa-review`, `/qa-loop`, …) |
| `commands/feature-audit.md` | `/feature-audit` — shipped-surface audits |
| `commands/onboard.md` | `/onboard` — new-chat bootstrap + continuity scan |
| `commands/handoff.md` | `/handoff` — write `docs/qa/SESSION.md` for next chat |
| `commands/docs-capture.md` | `/docs-capture` — post-change docs update from this chat |
| `skills/docs-capture/SKILL.md` | SSOT doc capture procedure |
| `skills/qa-*/SKILL.md` | QA stage + orchestrator skills (**this repo only**) |
| `skills/feature-audit/` | Feature audit skill + `reference.md` baseline packs |
| `skills/project-onboard/SKILL.md` | Onboard + handoff → Working Brief |
| `skills/qa-feedback-sessions.md` | Session file resolution (no shared `FEEDBACK.md` writes) |
| `rules/qa-workflow.mdc` | Always-on: maps QA commands → project skills |
| `rules/tradingagents-core.mdc` | Always-on: repo map, guardrails, doc shortcuts |
| `../docs/qa/README.md` | Tracked QA workflow reference (in git) |

## Commands

### Onboard / handoff

- `/onboard` — general repo brief + continuity (SESSION, feedback sessions, WIP)
- `/onboard focus=screening`
- `/onboard focus=webapp`
- `/onboard focus=analysis`
- `/onboard focus=pipeline`
- `/onboard focus=qa`
- `/handoff` — write local session resume note for the next `/onboard`
- `/docs-capture` — update owning docs from this chat’s changes
- `/docs-capture dry-run` — propose doc edits without writing

## QA skills (commands + matching skills)

| Command | Skill folder |
|---------|----------------|
| `/qa-start` | `qa-start` |
| `/qa-review` | `qa-review` |
| `/qa-reconcile` | `qa-reconcile` |
| `/qa-gate` | `qa-gate` |
| `/qa-loop` | `qa-loop` (orchestrator) |
| `/qa-verify` | `qa-verify-implementation` |
| `/qa-status` | `qa-status` |
| `/feature-audit` | `feature-audit` |

Shared: `qa-feedback-sessions.md` (session path resolution).

### QA (plans) — slash commands

- `/qa-start @path/to/plan.md`
- `/qa-review @path/to/plan.md`
- `/qa-reconcile @path/to/plan.md @docs/qa/feedback/<session>.md`
- `/qa-gate @path/to/plan.md @docs/qa/feedback/<session>.md`
- `/qa-loop @path/to/plan.md` — review + reconcile + gate in one turn
- `/qa-verify @path/to/plan.md @docs/qa/feedback/<session>.md`
- `/qa-status @docs/qa/feedback/<session>.md`

### Feature audit (shipped surfaces)

- `/feature-audit Cross-Watchlist board`
- `/feature-audit @webapp/templates/screener.html depth=deep`
- `/feature-audit reversal_buildup mode=audit+fix`

## Project-only rule

Agents must read skills from **`.cursor/skills/` in this repository**. Do **not** fall back to `~/.cursor/skills/` — that avoids cross-project confusion with CommonHour or other repos.

## Verification checklist

- [ ] `.cursor/commands/` has 7 `qa-*.md` files + `feature-audit.md` + `onboard.md` + `handoff.md` + `docs-capture.md`
- [ ] `.cursor/skills/` has `qa-start`, `qa-loop`, `qa-status`, `qa-review`, `qa-reconcile`, `qa-gate`, `qa-verify-implementation`, `feature-audit`, `project-onboard`, `docs-capture`, `qa-feedback-sessions.md`
- [ ] `.cursor/rules/qa-workflow.mdc` and `tradingagents-core.mdc` exist (`alwaysApply: true`)
- [ ] Command palette shows `/qa-loop`, `/onboard`, `/feature-audit`, etc.
- [ ] `docs/qa/README.md` and `docs/qa/feedback/README.md` exist
- [ ] Session feedback under `docs/qa/feedback/` is gitignored (except `README.md`)
