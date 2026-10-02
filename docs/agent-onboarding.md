# Agent onboarding (TradingAgents)

**Audience:** Cursor agents (and humans bootstrapping a new chat).  
**Status:** Active — tracked reference. Commands/skills live under `.cursor/` in this repo (project-local only).

---

## How to use

In Cursor, at the start of a chat or when switching domains:

```text
/onboard
/onboard focus=screening
/onboard focus=webapp
/onboard focus=analysis
/onboard focus=pipeline
/onboard focus=qa
/handoff
/docs-capture
/docs-capture dry-run
```

The agent loads `.cursor/skills/project-onboard/SKILL.md`, reads the docs below, runs a **continuity scan**, and emits a **Working Brief**.

Always-on invariants: `.cursor/rules/tradingagents-core.mdc`, `.cursor/rules/qa-workflow.mdc`.  
Index for agents: [`AGENTS.md`](../AGENTS.md).

### Chat → chat continuity

| Step | Command | What happens |
|------|---------|--------------|
| After major changes | `/docs-capture` | Updates owning SSOT docs from this chat + git (optional `dry-run`) |
| End of chat | `/handoff` | Writes local `docs/qa/SESSION.md` (gitignored) with done / open / next step |
| Start of next chat | `/onboard` | Reads SESSION + session-feedback blockers + dirty tree + recent commits |

---

## QA command → skill map

All skills are under **`.cursor/skills/`** in this repo. Do **not** use `~/.cursor/skills/`.

| Command | Skill |
|---------|--------|
| `/qa-start` | `qa-start/SKILL.md` |
| `/qa-review` | `qa-review/SKILL.md` |
| `/qa-reconcile` | `qa-reconcile/SKILL.md` |
| `/qa-gate` | `qa-gate/SKILL.md` |
| `/qa-loop` | `qa-loop/SKILL.md` (orchestrates review → reconcile → gate) |
| `/qa-verify` | `qa-verify-implementation/SKILL.md` |
| `/qa-status` | `qa-status/SKILL.md` |
| `/feature-audit` | `feature-audit/SKILL.md` (+ `reference.md`) |
| Session paths | `qa-feedback-sessions.md` |

---

## Layers

| Layer | Path | When |
|-------|------|------|
| Tracked index | `AGENTS.md` | Every session that reads repo root conventions |
| Tracked reference | this file | Human + agent deep link |
| Always-on rules | `.cursor/rules/*.mdc` | Every chat |
| Slash commands | `.cursor/commands/*.md` | `/qa-*`, `/onboard`, etc. |
| Skills | `.cursor/skills/**/SKILL.md` | On-demand procedures |
| Local handoff | `docs/qa/SESSION.md` | Gitignored resume note |
| Session feedback | `docs/qa/feedback/` | Per-loop append-only files (+ `ACTIVE.md`) |

---

## Focus → docs

| Focus | Read after the core spine |
|-------|---------------------------|
| *(default / general)* | Core spine only |
| `screening` | `docs/ARCHITECTURE.md` § screening, `USER_MANUAL.md`, `tradingagents/screening/` |
| `webapp` | `webapp/app.py`, `webapp/templates/`, `Makefile` ui targets |
| `analysis` | `tradingagents/graph/`, `tradingagents/research.py`, report templates |
| `pipeline` | `scripts/`, `tradingagents/dataflows/`, `Makefile` data/screen targets |
| `qa` | `docs/qa/README.md`, `AGENTS.md` Plan → QA → build, `.cursor/skills/qa-feedback-sessions.md` |

### Core spine (always)

1. [`README.md`](../README.md) — quickstart  
2. [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — runtime model + pipelines  
3. [`AGENTS.md`](../AGENTS.md) — QA loop + repo map  

---

## Working Brief template

Agents must end `/onboard` with this shape (fill from evidence, not memory):

```markdown
## TradingAgents working brief
**Focus:** …
**Branch / dirty tree:** …
**Recent commits:** …
**Repo map:** …
**Relevant paths:** …
**Key commands:** …
**Resume / in progress:** …
**Open blockers:** …
**Suggested next step:** …
```

End with: **What are you trying to ship in this chat?**
