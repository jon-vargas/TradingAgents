# Agent and assistant conventions (TradingAgents)

## Start a new chat

Run **`/onboard`** (optional `focus=screening|webapp|analysis|pipeline|qa`) at the start of a new chat or when switching domains. End a chat with **`/handoff`** so the next onboard can resume from `docs/qa/SESSION.md`. Full procedure: [`docs/agent-onboarding.md`](docs/agent-onboarding.md).

After meaningful code or process changes in a chat, run **`/docs-capture`** (optional `dry-run`) so owning docs stay current. Pure ops (one-off screen run, local DB inspection) usually needs no doc update.

## Repo map

| Area | Path |
|------|------|
| Screening engine | `tradingagents/screening/` |
| Multi-agent analysis | `tradingagents/graph/`, `tradingagents/agents/`, `tradingagents/research.py` |
| Data providers + cache | `tradingagents/dataflows/` |
| Persistence + reports | `tradingagents/reporting/` (`research.db` locally) |
| Web UI + API | `webapp/app.py`, `webapp/templates/` |
| CLI / scripts | `cli/`, `scripts/`, `Makefile` |
| Tests | `tests/` |
| Canonical docs | `docs/README.md`, `docs/ARCHITECTURE.md`, `USER_MANUAL.md` |

## Plan → QA → build

For technical plans, use the command-driven QA loop (project-local: `.cursor/skills/`):

1. **Create/update plan** in `docs/qa/` or `~/.cursor/plans/*.plan.md`.
2. **Start a session** (unique feedback file under `docs/qa/feedback/`):
   - `/qa-start @path/to/plan.md`
3. **Run stage commands** with that session path (or rely on `ACTIVE.md`):
   - `/qa-review @path/to/plan.md @docs/qa/feedback/<session>.md`
   - `/qa-reconcile @path/to/plan.md @docs/qa/feedback/<session>.md`
   - `/qa-gate @path/to/plan.md @docs/qa/feedback/<session>.md`
4. **Optional orchestration shortcuts**:
   - `/qa-loop @path/to/plan.md` (creates/uses one session file for all three stages)
   - `/qa-verify @path/to/plan.md @docs/qa/feedback/<session>.md` (post-implementation)
   - `/qa-status` (loop status + cross-session blockers)

Do **not** append new loop findings to `docs/qa/FEEDBACK.md` (legacy/index). Treat any unresolved bare `open` CRITICAL/SIGNIFICANT finding across session files as a blocker to implementation.

For **shipped feature** audits (Cross-Watchlist, reversal screener, Scan All, report pipeline — not plan files), use:

```text
/feature-audit <focus> [depth=quick|standard|deep] [mode=audit|audit+plan|audit+fix]
```

See [`docs/qa/README.md`](docs/qa/README.md). Default `mode=audit` is read-only findings.

## Automation pointers

See `Makefile` (`make help`) and [`USER_MANUAL.md`](USER_MANUAL.md) for operator commands (`make ui`, `make screen-all`, `make test`, `make stabilization-gate`, etc.).
