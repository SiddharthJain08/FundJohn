# SP-3 — Asset-Class Expansion — Session Handoff

**Created:** 2026-05-25
**For:** a fresh Claude Code (BotJohn) session that will brainstorm → spec → plan → build SP-3.
**Status of predecessor work:** SP-1 (provider cutover) and **SP-2 (universe expansion, all four phases A–D)** are MERGED + DEPLOYED LIVE. SP-3 is the next sub-project in the data-provider-overhaul program.

> **Read order for the new session:**
> 1. This file (current state + SP-3 framing).
> 2. `docs/superpowers/specs/2026-05-21-data-provider-overhaul-handoff.md` **§3 (SP-3)** + §1 (anchored AAT Plus / FMP facts) — the original decomposition.
> 3. `/root/openclaw/CLAUDE.md` (architecture, pipeline, agents, the append-only master-DB invariant).
> 4. The auto-memory index `/root/.claude/projects/-root/memory/MEMORY.md`, then the specific memories listed at the bottom.
> 5. **Do NOT write code yet** — SP-3 starts with brainstorming + a grounded spec + plan (see "How to run SP-3").

---

## 1. Where production is RIGHT NOW (2026-05-25)

- **This machine IS the live VPS** (`srv1559223`). `johnbot.service` runs as `claudebot` (uid 1001). Heavy LLM cycles (saturday-brain, maintenance, subagent swarms) run as `claudebot` via systemd. `claude-bin` refuses to run as root — run LLM things via the systemd services or `sudo -u claudebot -H env ...`.
- **git:** `main` HEAD = `9c0bd0a`, and `main == origin/main` (fully pushed).
- **SP-2 is live.** Gates in `/root/openclaw/.env`: `OPENCLAW_UNIVERSE_RECS=1`, `OPENCLAW_PHASE_D_PREDICATE_AT_MINT=1`. The universe resolver (`OPENCLAW_UNIVERSE_RESOLVER`) is **default-on** (kill switch; absent = on). 129 strategies in `manifest.json` (42 live).
- **Ideator routing fix (2026-05-25) is deployed** — `saturday_brain._hunt` Population-1b routes pre-filled `kind='internal'` ideator drafts into coding. Validated end-to-end at the routing level on a live run (`saturday_run cf4d8656`): 25 internal drafts selected → bypassed PaperHunter → tiered A → dispatched to strategycoder. See `project_ideator_routing_fix.md`.

### ⚠️ Two live caveats the SP-3 session must know
1. **Account spend limit was hit TWICE today** (operator raised the monthly cap to $100). It halted (a) the saturday-brain ideator validation run mid-coding (`coded_synchronous=0`, every strategycoder/paperhunter exited code 1 with "You've hit your limit"), and (b) the Mon–Fri maintenance run. **Before triggering any heavy LLM cycle, confirm there is usage headroom** or it will fail the same way.
2. **25 ideator drafts are requeued but NOT yet minted.** They were reset to `data_tier=NULL` (Population-1b eligible) so the next saturday-brain run codes them — pending usage headroom. Trigger: `systemctl start openclaw-saturday-brain.service`. To requeue again after any failed run: `UPDATE research_candidates SET data_tier=NULL WHERE submitted_by='strategist-ideator' AND data_tier IS NOT NULL;`
3. **Minor inconsistency to reconcile (optional):** `S_growth_inflation_sector_timing` has a committed implementation file + `registry.py` `_IMPL_MAP` entry, but its `manifest.json` entry was pruned by the halted maintenance run (committed `9c0bd0a`). Either re-add it to the manifest or drop the registry/impl — operator's call.

---

## 2. SP-3 goal (asset-class expansion)

Broaden tradeable asset classes from **equities + options (current)** to include **crypto (24/7)** and **commodities/futures via Alpaca-tradeable ETPs** (and any direct futures products). Unlocks new archetypes: delta-hedged vol, crypto carry/momentum, commodity momentum, ETP arb, calendar spreads.

### What already exists that SP-3 builds on (grounding — verify before relying)
- **Crypto price plumbing exists** — SP-1 Task 15 added `fillPricesAlpacaCrypto` (`BTC-USD → BTC/USD` via `alpaca data crypto bars`) in the collector's non-equity dispatcher (`runMarketPricesNonEquity`). So the crypto-bars fetch primitive is already wired; SP-3 extends storage + strategy/backtest/executor support around it.
- **Alpaca CLI is the sole broker+data interface** (`/root/go/bin/alpaca`, alpha v0.0.9). Subcommands incl. `crypto`, `crypto-orderbook`, `option`, `screener`, `corporate-actions`, `asset`. Auth via `ALPACA_API_KEY`/`ALPACA_API_SECRET` env (the CLI needs them exported — do NOT `source .env`, unquoted parens break bash; export just those two vars for ad-hoc probes). See `reference_alpaca_cli.md`.
- **Universe resolver + 5y backfill (SP-2)** give per-strategy point-in-time universe slicing and the backfill driver pattern SP-3 can reuse for crypto history.
- **No WebSocket in the CLI** — crypto streaming would need an `alpaca-py` client (an SP-5 concern, flag it; not SP-3 MVP).

### Likely scope (from the original §3 — treat as a starting hypothesis, not gospel)
1. `asset_class` field on `StrategyRecord` (`equity`/`option`/`crypto`/`etp`/`futures`) threaded through manifest, lifecycle, sizer, executor. **Heed `feedback_lifecycle_silent_strip.md`** — a new top-level manifest field that isn't added to `StrategyRecord` + `from_manifest` + `to_dict` gets silently stripped on every lifecycle promotion.
2. Per-asset-class sizing in `regime_blended_sizer_live.py` (crypto 24/7 vol ≠ equity; greeks-aware delta-equivalent sizing for options).
3. Executor per-asset routing in `alpaca_executor.py` (equity+option exist; crypto spot/perp; commodities likely ETPs in the `equity` class).
4. New data tables/ingestion: `crypto_prices.parquet`, `crypto_orderbook.parquet`, `src/ingestion/alpaca_crypto.py` (append-only — see master-DB invariant).
5. Multi-asset backtest engine (`unified_backtest.py`): 24/7 vs RTH time alignment, wider crypto spreads, vol-regime model (separate HMM or shared HMM + asset-class feature).
6. Per-asset-class lifecycle promotion thresholds (Sharpe/MaxDD differ for crypto).
7. Doctor/system_checks asset-class awareness ("is market open" is false for 24/7 crypto).

### Decisions to brainstorm with the operator first
- Asset-class taxonomy: enum vs freeform?
- Per-asset-class sizer: plug-in pattern vs single sizer with branches?
- Crypto in MVP or deferred to SP-3.1?
- Backtest engine: refactor in place vs fork per asset class?
- Lifecycle promotion guards: per-asset-class thresholds in YAML/config?
- Commodities scope: ETPs-only (treat as equities) vs pursue direct futures (Alpaca futures are limited)?

### Files to read first (SP-3 surface)
- `src/strategies/lifecycle.py` — `StrategyRecord` field threading (the silent-strip trap)
- `src/execution/regime_blended_sizer_live.py` — per-asset-class sizing
- `src/execution/alpaca_executor.py` — executor routing + session awareness (`_alpaca_session_kind`)
- `src/backtest/unified_backtest.py` — multi-asset engine
- `src/pipeline/*` + `src/ingestion/*` — collector/non-equity dispatcher (`fillPricesAlpacaCrypto` lives here)
- Probes (export `ALPACA_API_KEY`/`ALPACA_API_SECRET` first): `alpaca asset list --asset-class crypto --status active`; `alpaca data crypto bars --symbols BTC/USD --timeframe 1Day --start 2026-05-01`

---

## 3. How to run SP-3 (the working conventions used through SP-1/SP-2 — keep them)

1. **Brainstorm before building.** Invoke `superpowers:brainstorming` to align with the operator on the SP-3 design (taxonomy, MVP scope, sizer/backtest approach) BEFORE writing a spec.
2. **Spec, then plan — and GROUND them against source.** Per `feedback_spec_plan_codebase_grounding.md`: grep-verify every named convention (env var, table, function signature, CLI flag, file path) against the actual code before committing a spec/plan. SP-2's plans referenced fictional candidate lists, nonexistent `lifecycle.stage()`, and wrong table/column names — all caught by pre-flight `\d`/grep checks. Patch a "corrections" table into the plan before dispatching.
3. **Execute with `superpowers:subagent-driven-development`:** one fresh implementer subagent per task + **two-stage review** (spec-compliance reviewer, THEN code-quality reviewer); loop fixes until both pass. Use git worktrees under `.claude/worktrees/`.
4. **Use `superpowers:systematic-debugging` for any bug** — root-cause before fixing (this is how the ideator dead-path was found).
5. **Verify before claiming done** — run the real command/probe; for live-pipeline changes, prefer read-only verification against live data, and surface before minting/deploying.

### Standing operating constraints (non-negotiable)
- **NEVER `git add -A`/`git add .`** — stage files by explicit name. **Never commit secrets** — `.env*` is now gitignored (`.env.*` + `!.env.example`), but stay vigilant; `.mcp.json` too.
- **Don't touch operator in-flight files** unless told; if asked to "commit all", exclude secrets + plugin caches (`.superpowers/`) and surface exactly what you're committing.
- **Surface before any merge/deploy** that touches the live minting/trading pipeline. This machine is the live VPS — `git push` and service restarts are real.
- **NEVER delete from the master DB** — `data/master/*.parquet` + canonical Postgres tables are append-only (columns/tickers/dates only grow). New asset-class tables must follow this.
- **`migrate()` runner wart:** non-idempotent migrations (008/027/069 …) can poison the transaction on re-run ("current transaction is aborted"). After deploying a new migration, **verify the table actually exists** (`\d <table>`), don't trust the warning-laden log.
- **Operator decisions via `AskUserQuestion`.** Auto Mode is active: bias toward acting, ask only for decisions only the operator can make (scope, spend, irreversible/visible actions).
- **Heavy LLM runs:** as `claudebot` via systemd; `Type=oneshot` services block on `systemctl start` → use `--no-block` (or run blocking in the background and await completion). Mind the spend limit (see §1).
- **Log gaps/learnings** to `/root/.learnings/` (ERRORS/LEARNINGS/FEATURE_REQUESTS) and update auto-memory (`/root/.claude/projects/-root/memory/`).

### Memories to read (auto-memory)
- `project_regime_blended_sizer.md` — sizer architecture (for the per-asset / delta-aware sizing extension)
- `feedback_lifecycle_silent_strip.md` — `StrategyRecord` new-field strip trap (directly relevant to the `asset_class` field)
- `feedback_silent_failure_pattern.md` — shared HMM/regime across asset classes: if the shared model breaks, all classes are affected; check which store the consumer reads
- `reference_alpaca_cli.md` — CLI auth + flags + `alpaca doctor`
- `project_sp2_phase_*` + `feedback_universe_predicate_contract.md` + `feedback_spec_plan_codebase_grounding.md` — what SP-2 shipped + the grounding discipline
- `project_ideator_routing_fix.md` — the most recent change + the pending 25-draft mint

---

## 4. Initialization prompt (paste into the fresh session)

```
We're starting SP-3 — Asset-Class Expansion (equities + options → + crypto + commodities/ETPs) for the FundJohn/OpenClaw quant system. You are BotJohn, running as Claude Code on the live VPS.

Before doing anything else, read, in order:
1. docs/superpowers/specs/2026-05-25-sp3-asset-class-expansion-handoff.md  (current state + SP-3 framing + conventions)
2. docs/superpowers/specs/2026-05-21-data-provider-overhaul-handoff.md  §3 (SP-3) and §1 (AAT Plus / FMP facts)
3. /root/openclaw/CLAUDE.md
4. The auto-memory files the handoff lists (MEMORY.md index first).

Then: do NOT write code yet. Use the brainstorming skill to align with me on SP-3 scope — at minimum: asset-class taxonomy (enum vs freeform), whether crypto is in the MVP or deferred to SP-3.1, per-asset-class sizing approach, and backtest-engine strategy. Once we agree, write a spec and a plan, GROUNDED against the actual source (grep-verify every named env var / table / function / CLI flag before committing — SP-2 plans had fictional names that broke subagents). Execute with subagent-driven-development (one implementer per task + two-stage review), in a git worktree.

Operating constraints: never `git add -A` or commit secrets (.env* is gitignored); never delete from the master DB (append-only); surface before any merge/deploy (this machine is the live VPS); confirm LLM-usage headroom before triggering heavy cycles (the monthly limit was hit twice on 2026-05-25). Two open items from the prior session: (a) 25 ideator drafts are requeued (data_tier=NULL) awaiting a saturday-brain run with usage headroom to mint; (b) S_growth_inflation_sector_timing has a committed impl + registry entry but no manifest entry — reconcile if convenient.

Start by confirming you've read the handoff, then ask me your opening brainstorming questions for SP-3.
```
