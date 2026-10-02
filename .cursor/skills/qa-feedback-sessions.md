# QA feedback session files (TradingAgents)

Use for every `/qa-*` and `/feature-audit` write. **Do not** default all loops to a single shared `docs/qa/FEEDBACK.md`.

**Project-local only:** resolve procedures from this file in `.cursor/skills/` — not from `~/.cursor/skills/`.

## Paths

| Path | Role |
|------|------|
| `docs/qa/feedback/<slug>-YYYY-MM-DD.md` | **Session feedback** — append-only for one plan/audit loop |
| `docs/qa/feedback/ACTIVE.md` | Pointer to the current session `feedback_path` (overwrite on each `/qa-start` or new audit session) |
| `docs/qa/FEEDBACK.md` | Legacy / optional **index only** — do not append loop findings here |
| `docs/qa/SESSION.md` | Handoff note for `/handoff` → next `/onboard` (gitignored) |

Session files are gitignored (`docs/qa/feedback/**` except `README.md`).

## Resolve `feedback_path`

Apply in order:

1. **Explicit** — user `@` path that is clearly a feedback/session file (`FEEDBACK.md`, `docs/qa/feedback/*.md`, or `feedback_path=...`).
2. **Active pointer** — if `docs/qa/feedback/ACTIVE.md` exists, read `feedback_path:` and use it when it still exists on disk.
3. **Create session file** — when starting a loop (`/qa-start`, `/qa-loop` without feedback `@`, `/feature-audit` when recording findings):
   - Slug from plan basename or feature focus: lowercase, non-alphanumeric → `-`, collapse `-`, trim to 48 chars.
   - Path: `docs/qa/feedback/<slug>-YYYY-MM-DD.md` (local calendar date).
   - If that path exists, use `<slug>-YYYY-MM-DD-2.md`, then `-3`, etc.
   - Create the file with metadata header (below).
   - Overwrite `docs/qa/feedback/ACTIVE.md` with the new path.
4. **Never** silently append multi-loop findings to `docs/qa/FEEDBACK.md`.

Always echo the resolved `feedback_path` in the command summary so the user can `@` it in later turns.

## Session file header (create)

```markdown
# QA session — <slug> (<YYYY-MM-DD>)

## QA Loop Metadata
- plan_path: <path or n/a for feature-audit>
- focus: <feature focus or n/a>
- feedback_path: docs/qa/feedback/<file>.md
- loop_status: qa_open
- started_at: <ISO timestamp>
```

## ACTIVE.md format

```markdown
# Active QA feedback session
feedback_path: docs/qa/feedback/<file>.md
plan_path: <path or empty>
focus: <focus or empty>
updated_at: <ISO timestamp>
```

## Blocker scans (`/onboard`, `/qa-status`, gates)

Scan **all** `docs/qa/feedback/*.md` (except `README.md` / `ACTIVE.md`) for bare `open` CRITICAL/SIGNIFICANT. Summarize by file. Also note legacy `docs/qa/FEEDBACK.md` if it still contains bare `open` blockers.

`open (pending implementation)` is not a gate blocker.
