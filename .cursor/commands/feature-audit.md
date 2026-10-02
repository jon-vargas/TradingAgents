---
description: Audit a focused screening/webapp/analysis feature for quality and lens correctness.
argument-hint: "<focus|@path> [depth=quick|standard|deep] [mode=audit|audit+plan|audit+fix]"
---

# /feature-audit

You are executing the **feature-audit** command. Do not improvise a different audit process.

## Required first step

1. Read **`.cursor/skills/qa-feedback-sessions.md`** (session feedback when recording findings).
2. Read and follow **`.cursor/skills/feature-audit/SKILL.md`** (this repo only — not `~/.cursor/skills/`).

## Parse inputs

- Feature **focus** — first `@` path, route, component, or plain-language feature name (required)
- `depth=quick|standard|deep` — default **`standard`**
- `mode=audit|audit+plan|audit+fix` — default **`audit`**
- Optional `@` → `feedback_path` if clearly a feedback/session file; else resolve/create `docs/qa/feedback/feature-<slug>-YYYY-MM-DD.md` when appending findings

## Execute

1. Run the skill workflow fully for the resolved focus/depth/mode (including **Honesty rules** and `reference.md` family packs).
2. Verify claims against code/config/docs/probes — no memory-only Pass; prefer Partial over fake Pass.
3. Append to the **session** feedback file per skill rules (append-only). Update `ACTIVE.md` when creating a new session file.
4. End with the **Feature audit summary** block from the skill (include `feedback_path`).

## Mode notes

- **`audit`** — findings only (no product code edits).
- **`audit+plan`** — findings + uplift plan under `docs/qa/`; do not implement; large plans → `/qa-loop`.
- **`audit+fix`** — fix CRITICAL/SIGNIFICANT only; re-probe; ask before product-policy changes.
