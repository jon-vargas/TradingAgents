# QA feedback (legacy index)

**Do not append new `/qa-*` or `/feature-audit` findings here.**

Session files live under [`docs/qa/feedback/`](feedback/README.md):

```text
docs/qa/feedback/<slug>-YYYY-MM-DD.md
docs/qa/feedback/ACTIVE.md   → current session pointer
```

Start a loop with `/qa-start @path/to/plan.md` (creates a unique session file) or pass an explicit `@docs/qa/feedback/….md`.

See [`docs/qa/README.md`](README.md) and `.cursor/skills/qa-feedback-sessions.md`.

## QA skills (project-local)

All under `.cursor/skills/` in this repo:

| Skill | Command |
|-------|---------|
| `qa-start` | `/qa-start` |
| `qa-review` | `/qa-review` |
| `qa-reconcile` | `/qa-reconcile` |
| `qa-gate` | `/qa-gate` |
| `qa-loop` | `/qa-loop` |
| `qa-verify-implementation` | `/qa-verify` |
| `qa-status` | `/qa-status` |
| `feature-audit` | `/feature-audit` |
| `qa-feedback-sessions.md` | (shared session path resolution) |
