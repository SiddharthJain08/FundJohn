# Blueprint Fast Lane — design

**Date:** 2026-06-16
**Status:** DESIGN (awaiting operator review → implementation plan)
**Branch target:** `feat/intraday-regime-15min-prefetch` (live), additive only.

## Context

Our research pipeline originates strategies from academic papers (arXiv/OpenAlex → rate → PaperHunter extract → code → backtest → candidate). The hit-rate is low: most abstracts never become an implementable, backtestable strategy. Sites that publish **explicit, reproducible strategy blueprints** (entry/exit + params + the author's own backtest, often with code) convert far more reliably. We already seeded 3 blueprint RSS blogs (`blueprint_seed_sources.json`, committed `8539192`). This project goes further:

1. **Git ingester** — import already-coded strategies from `github.com/paperswithbacktest/awesome-systematic-trading` (~61 QuantConnect/LEAN `.py` files, each with a plain-English rule comment + a cited source URL).
2. **HTML clean-URL crawler** — crawl explicit-rule sites the current parser can't (TuringTrader, Quantpedia-free; their anchors lack the `.pdf`/`.htm` extension `RX_PAPER_LINK` requires).
3. **Blueprint priority** — blueprint/git candidates run BEFORE the academic lane, get the larger share of the weekly coding budget, and a lower promotion bar (the author already backtested).
4. **Coder-assist for git** — when porting an already-coded strategy, give StrategyCoder the reference source + a QuantConnect→BaseStrategy mapping guide so it TRANSLATES rather than invents.

**Operator decisions (2026-06-16):** fast lane (skip rate+hunt for git) · order+budget+lower-bar priority · LLM-assisted port + review · per-source link-pattern HTML crawl.

## Hard constraints

- **Clean-room / licensing.** `awesome-systematic-trading` has **no LICENSE file** (default: all rights reserved). We do NOT vendor or commit their `.py`, and we do NOT copy code verbatim into our repo. We extract the **strategy rules** (ideas/facts — not copyrightable; each file's comment block states the rule in plain English and cites a source, e.g. `https://quantpedia.com/strategies/asset-class-trend-following/`) and **clean-room re-implement** in BaseStrategy, informed by — not copied from — the reference. Provenance (repo URL + commit SHA + cited source) stored in candidate metadata.
- **Never execute cloned code.** Cloned files are read as text and fed to an LLM extractor only. No `import`, no `exec`, no running their backtests.
- **Append-only DB invariant.** New columns only (`ADD COLUMN IF NOT EXISTS`); no drops.
- **Candidates only.** Everything lands as `state=candidate` / `status=pending_approval`. Nothing auto-promotes to live; `_IMPL_MAP` untouched. Operator promotes.
- **Reuse, don't re-architect.** Pre-fill `research_candidates.hunter_result_json` and reuse Phases 5–8 (`data_tier_filter` → `_codeFromQueue` → register) unchanged. The data-fit tier gate (Phase 5) is the quality filter that stops us coding strategies we have no data for.

## Architecture

Two source families, one shared downstream:

```
GIT (pre-coded)         clone scratch → per-file git-extractor (LLM, reads comment+code+source)
                              → hunter_result_json {+ reference_impl, source_url, origin='git_blueprint'}
                                     │  (SKIPS PaperHunter — rules already explicit)
HTML/RSS blueprint      crawl → research_corpus → corpus-rate → PaperHunter extract
  (TuringTrader,              (origin='blog_blueprint', prioritized + lower bar)
   Quantpedia, blogs)               │
                                     ▼
                       research_candidates  (origin column distinguishes lane)
                                     ▼
            Phase 5 _tier (data_tier_filter)  ── unchanged, the data-fit gate
                                     ▼
            Phase 6 _code (_codeFromQueue → StrategyCoder)
              · blueprint coded FIRST, larger budget share
              · git candidates carry REFERENCE_IMPLEMENTATION + PORTING_GUIDE
                                     ▼
            Phase 7/8 register → manifest candidate + strategy_registry pending_approval
                                     ▼
                         Research Candidates dashboard page
```

Key insight: **git = skip-hunt** (the code IS the rule; a cheap git-extractor emits the spec directly). **HTML/blog = hunt** (the page must be read/extracted) but **prioritized**. Both share the tier→code→register tail.

## Components

### A. Schema (additive migration 136)
`research_candidates` gains:
- `origin TEXT DEFAULT 'paper'` — `paper | git_blueprint | blog_blueprint`. (Default keeps every existing row + the academic lane byte-identical.)
- `reference_url TEXT` — the cited source (paper/Quantpedia URL) for provenance.

`hunter_result_json` (JSONB, no migration) carries the new optional fields the git lane fills: `reference_impl` (the cloned source text, for porting only — never committed), `provenance` `{repo, commit, path, source}`.

### B. Git ingester — `src/ingestion/git_strategy_ingest.py`
- **Config** `src/ingestion/git_strategy_sources.json`: `[{repo, branch, strategies_glob, file_url_template, kind:'lean'}]`. Seed = awesome-systematic-trading `static/strategies/*.py`.
- Clone (shallow) to an **ephemeral scratch dir outside the repo** (`/tmp` or `workspaces/default/.git-ingest/`, git-ignored); never added to our tree.
- For each file: parse the leading comment block (rule + cited URL) + the code body. **Idempotency:** skip if `source_url` already in `research_candidates` or a strategy already cites it.
- **git-extractor** (one Sonnet call/file via `run-subagent-cli`, new lightweight prompt or a paperhunter variant): input = {comment, code, cited source}; output = the `hunter_result_json` shape (strategy_id `S_ast_<slug>`, hypothesis_one_liner, signal_logic, data_requirements, universe, inferred_instrument_class, inferred_universe_filter, stop/target/holding) + `reference_impl` + provenance. Cheaper/higher-fidelity than PaperHunter because rules are explicit.
- INSERT `research_candidates` (origin='git_blueprint', kind='git', source_url=file_url, reference_url=cited source).
- **Run modes:** `--bulk` (one-off backfill of the ~61; chunked + resumable + `nice`, mirroring the oxford build given the 2-core/8GB OOM history) and `--incremental` (weekly; only new/changed files — cheap).

### C. HTML clean-URL crawler — extend `src/ingestion/expanded_sources.py`
- Source spec gains optional `link_pattern` (regex) + `link_prefix`. New `_parse_html_index_pattern(body, base, pattern)` harvests `<a href>` matching the pattern regardless of extension, with an anchor-text-length filter (drops nav/footer).
- `fetch_source` dispatches to it when `link_pattern` present (else current behavior — byte-identical).
- Seed TuringTrader + Quantpedia-free in `blueprint_seed_sources.json` with verified index URL + pattern, `origin_hint:'blog_blueprint'`. These feed `research_corpus` → normal hunt, tagged for priority. (Verify exact index URLs + patterns during implementation; fall back to Firecrawl only if a target is JS-rendered — not a weekly-cron dep otherwise.)

### D. Priority — `src/agent/curators/saturday_brain.js`
- **Order:** run git ingest (and blog-blueprint corpus) before/ahead of the academic hunt; in Phase 6, sort blueprint-origin candidates first.
- **Budget split:** new env `OPENCLAW_BLUEPRINT_TIER_A_SHARE` (e.g. 0.5) reserves a share of `DEFAULT_TIER_A_CAP` (80) for blueprint candidates; papers take the rest. Code blueprint first up to its share, then papers up to theirs.
- **Lower bar:** blueprint-origin candidates use a lower auto-stage/surface threshold than `PROMOTION_THRESHOLDS` (configurable, e.g. min_sharpe 0.3). Candidates still all surface on the page (like the oxford set); the lower bar only affects any auto-staging decision. Conservative + env-gated.

### E. Coder-assist — porting guide + prompt hook
- `docs/strategy-coding/quantconnect-to-basestrategy.md` — the mapping reference (QC `Initialize/OnData/SetHoldings/SMA/AddEquity/Liquidate` → our `generate_signals(prices, regime, universe, aux_data)` / `Signal` / `compute_stops_and_targets` / house indicators; daily-bar fill model; `should_run(regime)`).
- `research-orchestrator.js:_codeStrategy` passes optional `REFERENCE_IMPLEMENTATION` + `PORTING_GUIDE` (path) into the StrategyCoder ctx when the candidate is git-origin.
- `strategycoder.md` gains a short section: "If REFERENCE_IMPLEMENTATION is present you are PORTING — translate the rule to our contract using the porting guide, do NOT copy verbatim, cite SOURCE_URL in the docstring." Absent → unchanged behavior.

## Data flow / contracts
- git-extractor output = the **exact** `hunter_result_json` shape Phase 5/6 already consume (mapped by the Explore: `strategy_id`, `hypothesis_one_liner`, `data_requirements{required,optional}`, `universe`, `inferred_universe_filter`, `inferred_instrument_class`). No downstream change.
- Registration reuses `lifecycle.register()` (state=candidate) + `upsertStrategyRegistry` (pending_approval) exactly as the paper lane and the oxford build did.

## Error handling
- Clone fail / network → log, skip that source, never block the weekly run (mirrors `expanded_sources.py` fail-soft).
- Extractor fail on a file → skip that file, continue.
- Idempotent inserts (`ON CONFLICT (source_url) DO NOTHING`); re-runs are safe.
- Data-tier C (no data) → not coded (existing gate); recorded, not an error.
- Kill-switches: `OPENCLAW_GIT_INGEST=0`, reuse `OPENCLAW_BLUEPRINT_SEEDS` for HTML seeds.

## Testing
- Unit: git-extractor output validates against the `hunter_result_json` schema; comment-block parser on real AST files; idempotency (re-run inserts 0); HTML link-pattern parser on a saved TuringTrader/Quantpedia fixture; origin column default = 'paper' (regression: academic lane unchanged).
- Integration: dry-run git ingest on 2–3 AST files → assert candidate rows with correct origin/provenance, no repo files written, no code executed.
- End-to-end (operator, bulk): chunked import of the 61, sample-verify a few coded strategies backtest + surface as candidates.
- Regression: paper lane byte-identical when all new gates off / origin defaults.

## Phasing (for the implementation plan)
1. Schema + origin plumbing + priority knobs (saturday_brain) + tests.
2. Git ingester + git-extractor + idempotency + bulk/incremental + tests.
3. Coder-assist (porting guide + prompt hook) + port-fidelity test.
4. HTML clean-URL crawler + TuringTrader/Quantpedia seeds + fixture tests.

## Open risks
- Extractor cost on bulk (~61 × Sonnet) — one-off, chunked; incremental after.
- Some AST strategies need data we lack (futures/intraday/specific universes) → tier-C filtered out; expected, logged (no silent cap).
- TuringTrader/Quantpedia may be JS-rendered → confirm static HTML at implementation; Firecrawl fallback only if needed.
- Lower promotion bar must not leak into live promotion — it only affects candidate surfacing/auto-stage; live promotion stays operator-gated.
