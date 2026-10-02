# QA session feedback (local)

Append-only session files for `/qa-*` and `/feature-audit` loops. **Gitignored** — not committed.

| File | Purpose |
|------|---------|
| `<slug>-YYYY-MM-DD.md` | One plan or audit session |
| `ACTIVE.md` | Pointer to the current session (tracked in git is OK; content is local workflow state) |

Do **not** append new loop findings to `../FEEDBACK.md` (legacy index only).

Agents resolve paths per `.cursor/skills/qa-feedback-sessions.md`.
