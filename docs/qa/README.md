# QA Loop Workflow (TradingAgents)

Command-driven QA in Cursor. Automation lives under **`.cursor/` in this repo** (tracked in git). Session findings are local-only under `docs/qa/feedback/`.

**Project-local only:** agents must use `.cursor/skills/` here — not `~/.cursor/skills/` — so CommonHour and TradingAgents do not cross-contaminate.

## Session feedback (no shared write target)

Each `/qa-*` or `/feature-audit` loop appends to its **own** file:

```text
docs/qa/feedback/<slug>-YYYY-MM-DD.md
docs/qa/feedback/ACTIVE.md   → pointer to the current session
```

- `/qa-start` creates the session file and updates `ACTIVE.md`.
- When no `@feedback` is passed, agents use ACTIVE or create a new session file.
- Do **not** append new findings to `docs/qa/FEEDBACK.md` (legacy/index only).
- Session files are gitignored (`docs/qa/feedback/**` except this `README.md`).
- Blocker scans cover **all** `docs/qa/feedback/*.md` (+ legacy FEEDBACK if present).

Procedure for agents: `.cursor/skills/qa-feedback-sessions.md`.

## QA skills (project-local)

| Command | Skill (`.cursor/skills/`) |
|---------|---------------------------|
| `/qa-start` | `qa-start/SKILL.md` |
| `/qa-review` | `qa-review/SKILL.md` |
| `/qa-reconcile` | `qa-reconcile/SKILL.md` |
| `/qa-gate` | `qa-gate/SKILL.md` |
| `/qa-loop` | `qa-loop/SKILL.md` (orchestrates the three stages) |
| `/qa-verify` | `qa-verify-implementation/SKILL.md` |
| `/qa-status` | `qa-status/SKILL.md` |
| `/feature-audit` | `feature-audit/SKILL.md` |

See also [`../agent-onboarding.md`](../agent-onboarding.md) and [`../../AGENTS.md`](../../AGENTS.md).

## Commands

| Command | Stage |
|---------|--------|
| `/qa-start @plan.md` | Create session feedback + metadata |
| `/qa-review @plan.md [@feedback]` | Stage 1 — review |
| `/qa-reconcile @plan.md @feedback` | Stage 2 — reconcile |
| `/qa-gate @plan.md @feedback` | Stage 3 — gate |
| `/qa-loop @plan.md [@feedback]` | Stages 1–3 in one turn (one session file) |
| `/qa-verify @plan.md @feedback` | Post-implementation |
| `/qa-status [@feedback]` | Loop status (+ other open sessions) |
| `/feature-audit <focus>` | Shipped-feature audit (not plan QA) |
| `/onboard [focus=…]` | Bootstrap agent with Working Brief |
| `/handoff` | Write `docs/qa/SESSION.md` for next chat |
| `/docs-capture [dry-run]` | Update SSOT docs from this chat’s changes |

Prefer explicit `@docs/qa/feedback/<session>.md` after `/qa-start` so parallel chats do not collide.

### Feature audit (shipped surfaces)

Use when validating live screening, Cross-Watchlist, Scan All, reversal classifier, reports, etc.:

```text
/feature-audit Cross-Watchlist board
/feature-audit @webapp/templates/screener.html depth=deep mode=audit+plan
/feature-audit reversal_buildup mode=audit+fix
```

| Arg | Values | Default |
|-----|--------|---------|
| focus | `@path`, route, or feature name | required |
| `depth=` | `quick` \| `standard` \| `deep` | `standard` |
| `mode=` | `audit` \| `audit+plan` \| `audit+fix` | `audit` |

Skill: `.cursor/skills/feature-audit/SKILL.md` (+ `reference.md` baseline packs). Does **not** replace `/qa-loop` for implementation plans.

## Typical flow

```text
/qa-start @~/.cursor/plans/my-feature.plan.md
/qa-loop @~/.cursor/plans/my-feature.plan.md
# … implement …
/qa-verify @~/.cursor/plans/my-feature.plan.md @docs/qa/feedback/my-feature-2026-08-24.md
```

## Existing session history

Prior audits and verifications live under `docs/qa/feedback/` (local). Examples from this repo:

- `cross-watchlist-latest-union-7be9be13-2026-08-19.md` — Verified
- `reversal-buildup-screener-2026-08-14.md`
- `scan-all-2026-08-17.md`

See `ACTIVE.md` for the current session pointer.

## Setup checklist

- [ ] `.cursor/commands/` and `.cursor/skills/` present (see `.cursor/README.md`)
- [ ] `.cursor/rules/qa-workflow.mdc` and `tradingagents-core.mdc` loaded (`alwaysApply: true`)
- [ ] Fully restart Cursor after first install (`Cmd+Q`)
- [ ] Command palette shows `/qa-loop`, `/onboard`, `/feature-audit`

If commands confuse the agent, confirm the workspace root is **TradingAgents** (not CommonHour) and rules are loaded.
