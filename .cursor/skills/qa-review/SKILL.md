---
name: qa-review
description: Review implementation plans by verifying claims against code/config/docs and appending structured QA findings.
---

# QA Review

Use this skill when `/qa-review` is invoked or Stage 1 of `/qa-loop` runs.

**Project-local only:** `.cursor/skills/qa-review/SKILL.md` in this repo — do not use `~/.cursor/skills/`.

## Required inputs

- `plan_path` — file path to the plan being reviewed (often `~/.cursor/plans/*.plan.md` or `docs/qa/*.md`)
- `feedback_path` — resolve via **`../qa-feedback-sessions.md`** (session file under `docs/qa/feedback/`; never default-append to `docs/qa/FEEDBACK.md`)

## Workflow

1. Resolve and echo `feedback_path` per `qa-feedback-sessions.md`.
2. Read the plan and extract all explicit implementation claims.
3. For each claim, verify against current code, config, and docs:
   - Plan review mode: verify logic, architecture, scoping — flag vague acceptance criteria
   - Flag missing test strategy when behavior changes screening, ranking, or API contracts
   - Flag missing schema/migration notes when `research.db` tables or `database.py` persistence changes
   - Flag lens isolation issues (Opportunity vs Reversal vs Scan All must not cross-rank)
4. Classify findings:
   - **CRITICAL** — blocks implementation
   - **SIGNIFICANT** — risky or important gap
   - **ADVISORY** — non-blocking improvement
5. Append a `## QA Review Findings` section to the **session** feedback file (append-only).

Each finding must include:

```markdown
### Finding N: [SEVERITY] — [short title]

**Claim**: ...
**Evidence**: ...
**Risk**: ...
**Recommendation**: ...
```

6. Update loop metadata in the session file (`loop_status: qa_open` or `builder_response_pending`).
7. End with: `Reviewer summary: X CRITICAL, Y SIGNIFICANT, Z ADVISORY.` and the `feedback_path` used.

## Rules

- Do not rewrite the full plan in this stage.
- Do not remove prior feedback sections in the session file.
- If no findings, state explicitly: no blockers found.
- Recommend next step: `/qa-reconcile @<plan_path> @<feedback_path>`.
