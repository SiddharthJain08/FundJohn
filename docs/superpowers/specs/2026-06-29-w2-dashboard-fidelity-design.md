# W2 — Dashboard Fidelity Remediation (Design Spec)

- **Date:** 2026-06-29
- **Branch:** feat/intraday-regime-15min-prefetch
- **Status:** Approved (design) — pending spec review → implementation plan
- **Workstream:** W2 of the pre-live finalization program (W1 = reconcile+repair, DONE)
- **Audit source of record:** scratchpad `w2_findings.md` + `w2_ground_truth.md` (live diffs captured 2026-06-29)

## §0 Context & Goal

The user dashboard (`src/channels/api/server.js`, ~10,185 lines, served on :3000 → nginx :80;
NOT the :7870 control room) must be the **bidirectional source of truth**: (a) every metric
reflects reality exactly, and (b) every control change propagates to the live system immediately.

A read-only audit (4 parallel tracing agents + live diffs against the running endpoint, broker,
and Postgres) found the *write-propagation* requirement is fundamentally **met** — every real
sizing/risk knob (λ, asset-corr-thr, regime-sizing, regime-params eligible/size/stop/target,
proposal approve/modify, universe-threshold apply) writes to `pipeline_config` / `regime_sizer_params`,
which the sizer/engine re-read **every cycle as fresh subprocesses, no restart**. The failures are
(1) the strategy table misrepresents what actually trades, (2) several dead/mislabeled controls,
(3) stale/empty displays, (4) a missing realized-leverage metric, (5) a DB-integrity smell.

De-risk facts (live, 2026-06-29): :3000 is reachable internally without auth; the running bot
(PID 3274404, started Jun 28) is newer than server.js (mtime Jun 26) → static findings match
what users see.

## §1 Decisions Locked (operator)

1. **manifest↔registry** → flag drift (show manifest intent + flag registry divergence) + make the
   transition→registry sync fatal/retried + I generate a per-strategy sign-off sheet. **No automatic
   syncing of the live book.**
2. **W2 fix buckets** → all four: dead/mislabeled controls, stale displays, realized-leverage metric,
   pipeline_config dedup.
3. **Upstream-truth items** → deferred to separate workstreams (logged, not fixed in W2).
4. **Dead-control dispositions:** staging-Approve → wire to real promotion queue; trigger-news →
   relabel "Prune old news"; max_hold_days → remove from control surface; watchlist → relabel
   "broker watchlist (does not change traded universe)"; regime-priors → relabel "research/diagnostic
   input (not live sizing)".

## §2 Architecture

All new non-trivial logic goes into **small, independently testable helper modules** in
`src/channels/api/` (mirroring existing `positions_grouped.js`, `regime_active.js`, `alpaca_cli.js`),
NOT inline in the server.js monolith. New modules:

- **`strategy_drift.js`** — pure: `classifyDrift(manifestState, registryStatus) → 'none' | 'shown_live_not_trading' | 'trading_not_shown'`; plus a `summarizeDrift(rows)` count helper.
- **`leverage.js`** — pure: `realizedLeverage({long_market_value, short_market_value, equity}) → {gross, net}`.

Full server.js decomposition is explicitly **out of scope** (risky on a live file; unrelated refactor).

## §3 D1 — Strategy-state truth (P1)

Root cause (settled via live diff): `GET /api/strategies` renders manifest `.state`
(`strategy_row.js:16`), but the engine trades `strategy_registry.status='approved'` (`engine.py:324`);
the `/transition` registry sync is a non-fatal `try/catch` (`server.js:1738`). Live divergence:
62 manifest-live vs 76 registry-approved — `S_price_path_convexity` shown live but registry=deprecated;
15 manifest=candidate but registry=approved (engine trades them; ≥1 fired 2026-06-26 → HOT).

### (a) Display — show intent + flag drift
- `GET /api/strategies`: LEFT JOIN `strategy_registry.status` into each row's payload (add
  `registry_status` alongside the existing manifest `state`).
- `strategy_drift.js.classifyDrift` computes the per-row drift class.
- `strategy_row.js`: keep rendering the **manifest** state (intent) but add a ⚠️ drift badge that
  surfaces the registry truth when divergent; payload includes a header summary count
  (e.g. "15 trading, not shown live · 1 shown live, not trading").
- New (optional) `drift` field in the row object; no new route required.

### (b) Fatal + retried sync on `/transition`
- `POST /api/strategies/:id/transition` (server.js ~1722): replace the `console.warn`-only catch with
  a retry loop (3 attempts, short backoff). On persistent failure, **revert the manifest `.state`
  write** (restore the prior value under the same file lock used at ~1586-1609) and return HTTP 500
  with an explicit "drift prevented — registry sync failed, manifest reverted" error.
- Invariant: after this route returns, manifest trade-intent and registry trade-reality are
  consistent OR the call failed loudly with no state change. They can never silently diverge again.
- This changes behavior on a live promotion control → **full TDD** (success path, registry-fail→revert
  path, retry-then-succeed path).

### (c) Sign-off sheet (read-only; no auto-sync)
- Generate `docs/w2-strategy-drift-signoff.md`: one row per divergent strategy with
  `id · manifest_state · registry_status · last_signal_date (execution_signals) · real broker holdings ·
  backtest_sharpe · recommended action`. Operator decides per-row keep-trading (promote manifest) vs
  stop (deprecate registry). **The live book is mutated only after explicit per-row operator sign-off**
  (same migrate-then-approve posture as W1). This artifact is produced during implementation; it is
  not itself a code change.

## §4 D2/D7 — Control dispositions

- **Staging "Approve"** (`routes_research.js` POST `/staging/:id/decision`): on `approve`, in addition to
  the status write, enqueue the existing `research_candidates → strategy_approval_jobs` promotion job
  (the safe multi-gate coder→registry path). Requires confirming the staging-row→candidate linkage key;
  if no linkage exists, the design falls back to relabel + surface (flagged during planning). Does NOT
  shortcut a draft to the live book.
- **"Trigger news"** (`server.js:210` + client button): relabel button text and the JSON response to
  "Prune old news (>30d)" to match actual behavior (`collector.runNewsCollection` is prune-only).
- **`max_hold_days`**: remove the input from the regime-params UI; the POST handler
  (`routes_regime_params.js`) stops accepting/persisting it. DB column retained (data), no control writes it.
- **Watchlist** (server.js ~2368-2420): relabel the panel/buttons "broker watchlist — does not change the
  traded universe". No behavior change.
- **regime-priors** (`routes_regime_drift.js` POST `/regime-priors/...` + UI): relabel "research/diagnostic
  input — not live sizing".
- **`/api/verdicts`** (server.js:184, `verdict_cache` always-empty legacy): remove the panel + its client
  fetch. (Route may remain; the dead UI surface is removed.)
- **`/api/db/cycles`** (server.js:227): drop `polygon_calls`/`yfinance_calls` (hardwired-0 since SP-1
  purge) from the display payload/render. Writer left untouched (vestigial columns, not master data).

## §5 D3/D4/D5 — Stale displays

- **D3 Pipelines tile** (`routes_pipelines.js:91`): backfill `active`/`today`/`failures_24h`/`graphs`
  from durable `langgraph.checkpoints` (the source `/runs` already merges), so the tile isn't empty after
  a bot restart wipes the in-memory traceBus. Preserve live traceBus values when present.
- **D4 Crypto badge** (server.js:1296-1307): branch the badge loop on `instrument_class`; crypto strategies
  resolve eligibility against the crypto regime (`crypto_regime_latest.json` / `/api/regime/crypto`), not
  the equity regime. Equity strategies unchanged.
- **D5 Regime freshness** (`/api/regime`, server.js:2440 + client gauge ~5055): add `daily_stale`
  (boolean) + `daily_date` + age-vs-`ENGINE_REGIME_FAIL_HOURS`(80) to the payload. The UI flags/greys the
  `stress_score`/`roro_score`/`date` as "as-of <date> (stale)" when the daily block is stale, instead of
  presenting the frozen Jun-8 values (stress 82 / roro -41.4) as current. The upstream Jun-8 freeze itself
  is deferred (U2); this is a display-honesty fix only.

## §6 D6 — Realized leverage

- `leverage.js.realizedLeverage` computes `gross = (LMV + |SMV|)/equity`, `net = (LMV + SMV)/equity` from
  the broker account fields already fetched by `/api/portfolio/account`.
- Surface in the account/exposure panel as **"actual N.NN×"** beside the existing config-intent λ×liq
  ("target"), clearly labeled. Live reference: gross 1.70× at audit time. Read-only; no behavior change.

## §7 D8 — pipeline_config dedup

- Inspect the duplicate `collection_enabled` and `collect_technicals` rows; if the duplicate values
  **differ**, that is a real bug — surface it and keep the operationally-correct value.
- Delete the redundant row(s) keeping the correct value (pipeline_config is a config table, NOT master
  data — dedup is permitted; the NEVER-DELETE invariant does not apply).
- Add a `UNIQUE(key)` constraint (via a numbered migration) and confirm all writers use
  `ON CONFLICT(key) DO UPDATE`, so duplicates cannot recur. Verify no writer relies on multi-row keys.

## §8 Testing strategy

TDD (RED→GREEN) per bucket. Pure helpers (`strategy_drift.js`, `leverage.js`) get unit tests first.
The fatal-sync (D1b) gets the most coverage: success, registry-fail→manifest-revert, retry-then-succeed.
Display/relabel fixes get a focused assertion (payload shape / label text). DB dedup (D8) gets a
round-trip migration test (apply on a seeded dup → one row + constraint; idempotent re-run). Respect the
VPS 2-core constraint (serialize, `nice` heavy steps). Pre-existing stale tests (the 7 from W1) are NOT in
scope.

## §9 Sequencing & commit plan

Independent, path-scoped commits (W1-style), low-risk first:

1. C1 — `leverage.js` + D6 surface (pure helper + panel) [TDD]
2. C2 — D5 regime freshness flag [TDD]
3. C3 — D3 pipelines-tile durable backfill [TDD]
4. C4 — D4 crypto badge branch [TDD]
5. C5 — D7 relabels + dead-panel removal (trigger-news, watchlist, regime-priors, verdicts, db/cycles cols, max_hold_days UI removal)
6. C6 — `strategy_drift.js` + D1a display drift-flag [TDD]
7. C7 — D1b fatal+retried sync (highest risk) [full TDD]
8. C8 — D2 staging-Approve → real promotion queue [TDD]
9. C9 — D8 pipeline_config dedup + UNIQUE migration [round-trip test]
10. (parallel, read-only) D1c sign-off sheet generated for operator review

Each commit staged by explicit path to avoid staging the live finisher's WIP
(`manifest.json`, `registry.py`, untracked `implementations/S_*` — the mastermind is actively coding).

## §10 Deferred (logged, NOT W2)

- **U1** realized P&L modeled off parquet, not reconciled to broker fills (`alpaca_submissions`) → folds into W7 (Alpaca).
- **U2** frozen Jun-8 regime daily block (daily-authority bug; `project_daily_crisis_artifact_vs_intraday_lowvol`). Fixing the source also resolves D5's stale values at the root.
- **U3** `signal_pnl` has 76,020 status='open' rows vs 109 real broker positions — bloated open-book accounting; pollutes DB-sourced position/P&L metrics.
- **U4** the manifest↔registry **trading** decision (which of the 15 + `S_price_path_convexity` keep trading) — gated on the D1c sign-off sheet; per-strategy operator sign-off; NO auto-sync.

## §11 Risk controls

- **No live-book mutation without per-strategy operator sign-off** (D1c gate).
- **Path-scoped commits only** — never `git add -A`/`.`; the live tree carries uncommitted WIP that is
  unrecoverable if clobbered. Never `git reset --hard` / `git clean` / blind `git checkout`.
- **D1b/D8 touch live behavior/DB** — TDD + careful; D8 migration verified idempotent before apply.
- **No master-data deletion** — only `pipeline_config` (config) is de-duplicated.
- Bot restart (to pick up server.js changes) is a discrete, operator-acknowledged step at the end —
  the user-scope `johnbot.service` (NEVER the disabled system unit; EADDRINUSE on :3000).

## §12 Definition of done

- Every D1–D8 bucket implemented + tested + committed (path-scoped) on feat/intraday-regime-15min-prefetch.
- Dashboard strategy table shows manifest intent + drift flags; `/transition` can no longer silently
  diverge manifest/registry; sign-off sheet delivered for operator review.
- No dead/mislabeled control remains (wired, removed, or honestly relabeled per §4).
- Stale displays flag/refresh correctly; realized-leverage surfaced; pipeline_config deduped + constrained.
- Bot restarted on the user scope; :3000 healthy; changes pushed to origin.
- Deferred items (U1–U4) logged to memory/learnings for their own workstreams.
