---
name: feature-audit
description: >-
  Audit a focused TradingAgents screening, webapp, or analysis feature for
  functionality, accuracy, and lens/ranking correctness. Use when the user
  invokes /feature-audit or asks to QA a named surface (Cross-Watchlist,
  reversal screener, Scan All, report rendering, etc.).
---

# Feature audit (TradingAgents)

Use when `/feature-audit` is invoked, or when the user asks for a structured QA/audit
of a **shipped feature** (not a plan file). For plan readiness use `/qa-review` /
`/qa-loop` instead.

**Project-local only:** `.cursor/skills/feature-audit/` in this repo — do not use `~/.cursor/skills/`.

Companion baselines: [`reference.md`](./reference.md) (feature-family expectation packs).

## Required inputs

| Input | Source | Default |
|-------|--------|---------|
| `focus` | First `@path`, route, component name, or plain feature name | **Required** — ask once if missing |
| `depth` | `depth=quick\|standard\|deep` | `standard` |
| `mode` | `mode=audit\|audit+plan\|audit+fix` | `audit` |
| `feedback_path` | `@` session path if clearly feedback | Resolve/create via **`../qa-feedback-sessions.md`** — never default-append to `docs/qa/FEEDBACK.md` |

Examples:

```text
/feature-audit Cross-Watchlist board
/feature-audit @webapp/templates/screener.html depth=deep mode=audit+plan
/feature-audit reversal_buildup mode=audit+fix
/feature-audit @tradingagents/screening/reversal_buildup.py depth=standard
```

## Honesty rules (non-negotiable)

1. **Evidence or it is not Pass.** Every Pass/Partial/Fail needs code path, test output, DB query, or runtime observation. Memory and “looks fine” are invalid.
2. **Prefer Partial over fake Pass** when `research.db` / live API / browser is unavailable.
3. **Do not soften severity** to make the feature look good. If rank isolation fails (Opportunity mixed into Reversal), that is SIGNIFICANT or CRITICAL.
4. **Do not invent product requirements.** Ground in `docs/ARCHITECTURE.md`, `USER_MANUAL.md`, session feedback, and in-repo tests.
5. **Separate observation from recommendation.** Findings state what is true now; fixes are recommendations or mode follow-through.
6. **No empty praise.** If verdict is Meets bar, say what was checked. If Below bar, lead with blockers.

## Modes

| Mode | Behavior |
|------|----------|
| `audit` | Map → baseline → probe → findings → verdict. **No product code changes.** Append-only feedback per rules below. |
| `audit+plan` | Same as audit, then write a focused uplift plan under `docs/qa/`. **Do not implement.** For large plans, recommend `/qa-loop` before build. |
| `audit+fix` | Same as audit; implement **CRITICAL/SIGNIFICANT** with clear evidence only. Re-probe after fixes. Ask once if a fix needs a product decision. |

`audit+fix` is blocked when **any** `docs/qa/feedback/*.md` session has unresolved bare `open` CRITICAL/SIGNIFICANT for **other** work — note the conflict and ask whether to proceed on this feature only.

## Depth

| Depth | Scope |
|-------|--------|
| `quick` | Map + baseline table + top issues; light probes; session feedback only if CRITICAL |
| `standard` | Full checklist; pytest/script probes; session feedback when SIGNIFICANT+ or user asked to record |
| `deep` | Standard + live `research.db` / web UI when env allows; related surfaces; regression tests |

## Workflow (do in order)

### 1. Resolve focus + family

Identify primary modules, API routes, templates, and docs. Classify **feature family** for baseline pack in `reference.md`:

`screening` | `cross-watchlist` | `scan-all` | `reversal` | `movers-long-horizon` | `webapp-api` | `analysis-report` | `other`

If focus is ambiguous, ask **one** question, then proceed.

### 2. Map the system

Short map (bullets or mermaid) with **real paths** verified by read/grep.

### 3. Modern baseline

Table: Expectation | TradingAgents today | Pass / Partial / Fail.

Load the matching pack from `reference.md`, then add surface-specific rows.

### 4. Probe with evidence

Minimum for `standard`/`deep`:

| Probe | When |
|-------|------|
| Hot-path read (engine, app route, template) | Always |
| Cross-surface drift (duplicate sort/filter logic) | When UI + API share a concept |
| `python -m pytest tests/test_<relevant>.py -q` | Core logic always |
| SQLite query on `research.db` | When validating live run ranks/phases |
| Browser / `make ui` | `deep`, or when the feature’s job requires it |

Record commands run and key numbers (run ids, counts, pass/fail).

### 5. Findings

Severity (same as plan QA):

- **CRITICAL** — broken primary path, wrong lens ranking, data corruption risk, silent no-op on main action
- **SIGNIFICANT** — logic drift, missing tests for core behavior, misleading UI labels vs backend
- **ADVISORY** — polish, copy, deferred niceties

```markdown
### Finding N: [SEVERITY] — [short title]

**Surface**: ...
**Evidence**: ...
**Risk**: ...
**Recommendation**: ...
**Disposition hint**: fix-now | plan | defer
```

### 6. Checklist (every row: Pass / Partial / Fail / N/A+reason)

| # | Check |
|---|--------|
| C1 | Functionality — primary happy path in code |
| C2 | Accuracy — ranks/phases/labels match engine + DB |
| C3 | Lens isolation — Opportunity vs Reversal vs Scan All not cross-contaminated |
| C4 | Streamlining — SSOT shared; no divergent duplicates (sort keys, pickers) |
| C5 | UI — filters, pagination, empty states honest |
| C6 | Config — `default_config.py` and module defaults aligned |
| C7 | Persistence — `research.db` / API embed shapes stable |
| C8 | Tests — core logic has runnable pytest entrypoint |
| C9 | Docs drift — ARCHITECTURE / USER_MANUAL vs code |
| C10 | API validation — bad ids rejected; enrich does not surprise-resort |
| C11 | Ops (deep) — live run probe or explicit Partial |

### 7. Verdict

- **Meets bar** — no CRITICAL; no SIGNIFICANT
- **Meets bar with issues** — no CRITICAL; SIGNIFICANT present but do not defeat the primary job
- **Below bar** — any CRITICAL, or SIGNIFICANT that defeats the feature’s primary job

### 8. Feedback append

Resolve `feedback_path` per `qa-feedback-sessions.md`. Append when `depth` ∈ {standard, deep} **and** (CRITICAL/SIGNIFICANT exist **or** user asked to record **or** mode ≠ `audit`).

### 9. Mode follow-through

- **`audit+plan`:** `docs/qa/<slug>-feature-audit-uplift-plan.md` with measurable ACs. Suggest `/qa-loop @plan` before large implementation.
- **`audit+fix`:** implement CRITICAL/SIGNIFICANT → add/adjust tests → re-run probes → append **Implementation note**.

## Output contract (chat)

```markdown
## Feature audit summary
**Focus:** ...
**Family / depth / mode:** ...
**Verdict:** Meets bar | Meets bar with issues | Below bar
**Findings:** X CRITICAL, Y SIGNIFICANT, Z ADVISORY
**Probes:** <commands or “code-only”>
**Top fixes:** <≤3 bullets>
**Feedback path:** <session path or none>
**Next:** <done | audit+plan | audit+fix | /qa-loop @plan>
```

## Rules

- Plan QA (`/qa-*`) ≠ feature audit (`/feature-audit`).
- Do not edit plan files the user did not name.
- Do not commit unless asked.
- Prefer linking SSOTs over pasting runbooks into findings.
