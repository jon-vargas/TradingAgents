---
name: qa-start
description: Initialize a QA loop with a unique session feedback file and ACTIVE.md pointer.
disable-model-invocation: true
---

# QA Start

Use when `/qa-start` is invoked.

**Project-local only:** `.cursor/skills/qa-start/SKILL.md` in this repo — do not use `~/.cursor/skills/`.

## Required inputs

- `plan_path` — first `@` file (required)
- `feedback_path` — optional explicit `@docs/qa/feedback/….md` or `feedback_path=…`; else create per **`../qa-feedback-sessions.md`**

## Workflow

1. Read and follow **`../qa-feedback-sessions.md`** (resolve / create session path).
2. Confirm `plan_path` exists and is readable.
3. Resolve or **create** `feedback_path` under `docs/qa/feedback/<plan-slug>-YYYY-MM-DD.md`.
4. Write session header metadata (`plan_path`, `feedback_path`, `loop_status: qa_open`, `started_at`).
5. Update `docs/qa/feedback/ACTIVE.md` to point at this session.
6. Echo both paths and recommend next step:

```text
/qa-loop @<plan_path> @<feedback_path>
```

or `/qa-review @<plan_path> @<feedback_path>`.

## Rules

- Do **not** append to `docs/qa/FEEDBACK.md` (legacy index only).
- One loop → one session file.
