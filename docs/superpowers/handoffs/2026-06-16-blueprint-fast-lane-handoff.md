# Blueprint Fast Lane — Operator Handoff

**Date:** 2026-06-16
**Branch:** `feat/intraday-regime-15min-prefetch` (live; commits `62e779b`..`b298279`, NOT pushed)
**Spec:** `docs/superpowers/specs/2026-06-16-blueprint-fast-lane-design.md` · **Plan:** `docs/superpowers/plans/2026-06-16-blueprint-fast-lane.md`
**Status:** BUILT + reviewed (final verdict SHIP_WITH_FOLLOWUPS). Git lane GATED-OFF/inert; blog lane wired (activates on new seed posts). Nothing auto-promotes to live.

## What it does
Two "blueprint" lanes feed `research_candidates` and reuse the existing Sunday 8AM-ingest (`saturday_brain.js`) / 2PM-code (`saturday_brain_finisher.js`) tier→code→register tail. Blueprint candidates are coded **first** and get a reserved share of the weekly Tier-A coding budget.

- **GIT lane** — clean-room imports already-coded strategies from `github.com/paperswithbacktest/awesome-systematic-trading` (~61 QuantConnect/LEAN files). A cheap Sonnet extractor reads each rule-comment+code → pre-filled `hunter_result_json` (origin=`git_blueprint`, kind=`git`), skipping PaperHunter. The reference source + a QC→BaseStrategy porting guide are handed to StrategyCoder so it **ports** rather than reinvents. **No LICENSE on the repo → clean-room: rules extracted, re-implemented, never vendored or executed.**
- **BLOG lane** — TuringTrader + Quantpedia-free (new clean-URL HTML crawler) + the 3 RSS blogs (Quantified Strategies / Robot Wealth / Alvarez) are tagged `origin_hint='blog_blueprint'` → corpus → rated → candidates with origin=`blog_blueprint`. **Already live** (seeds ingest every Sunday); now prioritized.

## Activation

### Migrations — DONE
- **136** (`origin`/`reference_url` on `research_candidates`) and **137** (allow `kind='git'`) are applied to the live DB. (Re-apply is safe/idempotent. If the filename-tracking migrate runner re-runs them, harmless.)

### Blog lane — already active, no action
The blueprint seed feeds ingest every Sunday. New posts now flow as `blog_blueprint` candidates and code **ungated, with priority** (the gate scopes only the heavy git lane). Pre-deploy corpus rows stay `origin='paper'` (`ON CONFLICT DO NOTHING`) — the lane activates on *new* posts, not history.

### Git lane — operator-triggered (gated)
1. **Bulk import** (one-off, ~$30 of Sonnet, off-hours on the 2-core box): `bash scripts/bulk_git_ingest.sh` — chunked, resumable (idempotent on `source_url`), `nice`. Creates ~61 `research_candidates` rows (origin=`git_blueprint`, pending). NOTE: `--dry-run` still runs the extractor (not a free preview).
2. **Soak / review** the created candidates.
3. **Enable weekly coding** — set `OPENCLAW_GIT_INGEST=1` in **BOTH** service envs (the 8AM `openclaw-sunday-research-ingest` *and* the 2PM `openclaw-sunday-research-code`). The gate is read by both; setting one is a silent no-op. Once on, the 2PM finisher codes git candidates (blueprint-first, budget-split) and re-imports incrementally each week. Kill-switch: unset it.

## Realized vs. not
- **Realized priority levers:** (1) blueprint-first ordering in Phase 6, (2) reserved Tier-A budget share (`OPENCLAW_BLUEPRINT_TIER_A_SHARE`, default 0.5; symmetric pass so an all-blueprint backlog uses the full cap).
- **NOT delivered — "lower promotion bar" for blueprints.** No safe/meaningful insertion point: every strategy that codes becomes a *candidate* regardless of Sharpe (no surfacing gate to lower), and the only real Sharpe gate (candidate→live in `lifecycle.py`) must stay uniform + operator-gated for safety. Dead helper removed (commit `b298279`). If you want a per-origin live-promotion bar, that's a separate, deliberate, safety-reviewed `lifecycle.py` change.

## Safety (verified in final review)
- **No ungated path codes a git candidate** when `OPENCLAW_GIT_INGEST` is off (all 8 consumers of pending candidates traced; git rows excluded by an independent predicate in each: finisher gate `origin <> 'git_blueprint'`, `processQueue` `kind <> 'git'`, `_hunt` populations by submitted_by/kind/rejection, recovery/retry by their own predicates, staging_approver is operator-keyed).
- **Gate-off is a true no-op today** (279/279 candidate rows `origin='paper'`; the new filter selects the identical set).
- **Candidates only** — `_IMPL_MAP` untouched; nothing auto-promotes to live.
- **Clean-room** — cloned files read as text → LLM only; never imported/exec'd; scratch clone under gitignored `workspaces/default/.git-ingest/`.

## Minor follow-ups (non-blocking)
- Quantpedia index has ~82 strategy links but `--max-per-source 50` truncates ~32/run (raise the cap for that seed if full coverage wanted).
- Porting hook is skipped if a `.py` already exists at the canonical path (re-attempt after a partial prior run would skip the port) — low likelihood given idempotency + 30d ageout.
- Un-codeable blueprints age out of the finisher queue after 30 days (logged) rather than retrying forever.

## Tests
- Node: `test_blueprint_priority`, `test_blueprint_code_budget_split`, `test_git_lean_parser`, `test_git_extractor`, `test_git_ingest_run`, `test_git_ingest_cli`, `test_coder_porting_ctx` — all green.
- Python: `test_migration_136_origin`, `test_html_pattern_parser`, `test_blueprint_seed_sources` (+`test_paper_fingerprint_wiring` regression) — green.
- Regression: `test_hunt_internal_pop` (5/5), `test_research_parsejson` (12/12).
