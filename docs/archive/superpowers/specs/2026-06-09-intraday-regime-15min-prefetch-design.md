# Intraday Regime Monitor — 15-min cadence + tick-1 price prefetch / tick-3 data-ready gate

**Date:** 2026-06-09
**Status:** Design approved (operator), pending implementation
**Branch:** `feat/intraday-regime-15min-prefetch`

## Problem

On 2026-06-09 the intraday HMM monitor fired **two** full pipeline redeploys in 75 minutes
(LOW_VOL→TRANSITIONING @ 11:25 ET, TRANSITIONING→HIGH_VOL @ 12:40 ET; a third was
cooldown-blocked) as VIX chopped ~20→23. Two issues surfaced:

1. **Intraday churn.** 5-min ticks with *tiered* hysteresis (CRISIS=1 tick, HIGH_VOL=2,
   TRANSITIONING/LOW_VOL=3) let escalations confirm fast on a flappy tape, re-trading the
   whole book (~$385k gross across the two deltas).
2. **Stale price anchor.** The redeploy regenerates signals + bracket levels from
   `prices.parquet`, whose latest complete bar intraday is the *previous* day's EOD close.
   Fills landed up to **7.23%** away from that anchor (avg 0.65%) — worst exactly in HIGH_VOL,
   where the move from the prior close is largest and risk-control matters most.

## Goal

Make the monitor fluctuate far less, and ensure that when a transition *does* confirm,
signals are generated on **fresh** prices:

- 15-minute ticks; **uniform 3-tick confirmation for all transitions** (45-min persistence).
- On the **first** tick of a candidate transition, kick off a price refetch so data is fresh
  by the time the **third** tick confirms.
- If ingestion is not finished by the third tick, **wait** for it before generating signals /
  submitting orders; if it **fails**, abort (never trade on stale/partial data).

Because the process fluctuates much less, even the first tick is a high-confidence signal of a
real transition — strong enough to justify warming a full price-ingestion cycle.

## Locked decisions

| Decision | Choice |
|---|---|
| Tick cadence | **15 min** (`*/15 9-19 * * 1-5`) |
| Confirmation | **Uniform 3 ticks for ALL transitions** (flatten the tiers); keep **0.70 confidence floor** at the firing tick; **drop the 60-min redeploy cooldown** |
| Prefetch scope | **Prices only** (union-universe daily bars), fired on tick-1 of a candidate transition |
| Coordination | **Async prefetch + Redis sentinel poll-gate** |
| Tick-3 gate | **Bounded wait** for prefetch completion + freshness check; **abort + alert** on failure / timeout / stale |
| Model | **No retrain now**; ungated daily 18:00 ET refit keeps it current; flip feature timestamp floor 5min→15min |
| Rollout | Behind default-OFF flag `OPENCLAW_INTRADAY_15MIN_PREFETCH`; flip after a dry-run |

## Background — how it works today (verified 2026-06-09)

- **Tick scheduler** — `src/engine/cron-schedule.js`: `cron.schedule('*/5 9-19 * * 1-5', …)`
  spawns `scripts/run_intraday_market_state.py` detached each tick.
- **Detector** — `scripts/run_intraday_market_state.py`:
  - `HYSTERESIS_N = 3`, `CONFIDENCE_FLOOR = 0.70`.
  - `HYSTERESIS_TIERS` (per-state `(required_ticks, required_confidence)`) + `_DOWNWARD_TIER`
    give tiered confirmation (CRISIS=1/0.90, HIGH_VOL=2/0.80, TRANSITIONING & LOW_VOL=3/0.70).
  - `_required_ticks(new_state)` / `_confirmed_transition(...)` decide firing.
  - Model scored per tick via `model.predict_proba(x)[0]` (single feature vector → posterior;
    **cadence-independent** — uses startprob + Gaussian emissions, not the transition matrix).
  - On confirmed transition: syncs the canonical regime (`market_regime` row +
    `regime_latest.json`) then spawns `scripts/redeploy_pipeline.py` detached.
  - Cooldown: reads/sets `redeploy:cooldown:{date}`; also honors `liquidate:cooldown:{date}`.
  - Discord via `_post_to_discord(channel, msg)` → `_post_webhook` (reads
    `agent_registry.webhook_urls`). Channel key `intraday-regime` (registered 2026-06-09).
- **Redeploy** — `scripts/redeploy_pipeline.py`: `REDEPLOY_STEPS = 'signals,handoff,trade,
  alpaca,reconcile'`; `_spawn_orchestrator` invokes `bin/run-graph.js daily-cycle` with those
  `requestedSteps`. RTH ship-safety gate; after-hours gated by `OPENCLAW_REDEPLOY_EXTENDED_HOURS`.
  Its own `_redis()` check also reads the cooldown before spawning.
- **Features** — `src/ingestion/intraday_features.py`: `collect_intraday_features()` returns a
  point-in-time dict (synthetic VIX 30d/90d, term slope, PCR, zero-DTE share; the realized-vol
  feature is dead/NaN since SP-1). `ts.floor('5min')` buckets the timestamp.
  `append_features_row()` appends to `data/master/intraday_features.parquet` (2622 rows today).
- **Refit** — `cron.schedule('0 18 * * *', train_intraday_hmm.py)` is **ungated**; bootstrap
  threshold `MIN_TRAINING_ROWS = 500`; writes `.agents/market-state/hmm_intraday_latest.pkl`
  (last regenerated 2026-06-08 22:00 UTC).
- **Collector** — `src/pipeline/collector.js`: rate-limited (~300 req/min) union-universe price
  fill into `prices.parquet` (append-dedup keep-last), updates `data_coverage`. Master data is
  **append-only** (CLAUDE.md invariant) — no deletes/overwrites except the audited backfill path.

## Design

### Component 1 — Cadence (`src/engine/cron-schedule.js`)
Register the intraday tick schedule **conditionally on the flag** at startup:
`OPENCLAW_INTRADAY_15MIN_PREFETCH === '1'` → `'*/15 9-19 * * 1-5'` (ticks at :00/:15/:30/:45),
else the legacy `'*/5 9-19 * * 1-5'`. Cron is registered once at boot, and flipping the flag
already requires a johnbot restart — so this keeps the flag a single deploy-safe switch
(flag OFF + restart = today's exact behavior). Update the comment block. The 18:00 ET refit
cron is unchanged.

### Component 2 — Feature cadence (`src/ingestion/intraday_features.py`)
Floor the timestamp to **15min when the flag is ON, else 5min** (`ts.floor('15min')` vs
`'5min'`). Update the module docstring. **Keep appending a row every tick** regardless (the
"store data regardless" requirement). No other feature change. The mixed-cadence parquet is
fine for the refit (point-in-time emissions; transition matrix unused at scoring).

### Component 3 — Uniform confirmation + cooldown removal (`scripts/run_intraday_market_state.py`)
- Make `_required_ticks(state)` return **3 for every state** (flatten `HYSTERESIS_TIERS` and
  `_DOWNWARD_TIER` to `(3, 0.70)`), so all transitions need 3 consecutive ticks. Keep
  `CONFIDENCE_FLOOR = 0.70` enforced at the firing tick (unchanged `_confirmed_transition`
  confidence check).
- **Drop the redeploy cooldown**: remove the `redeploy:cooldown:{date}` *set* and its *read*
  here and in `redeploy_pipeline.py`. **Retain** the `liquidate:cooldown:{date}` read so an
  operator forced-liquidation still suppresses an immediate auto-redeploy.
- **Single-in-flight lock** replaces the cooldown's anti-overlap role: before spawning a
  redeploy, set Redis `intraday:redeploy:inflight` (TTL ~10 min); skip the spawn if already set;
  clear it when the redeploy completes (and let the TTL expire it as a backstop). Confirmations
  are naturally ≥~30–45 min apart, so this is belt-and-suspenders against pathological overlap.

### Component 4 — Tick-1 prefetch trigger (`scripts/run_intraday_market_state.py`)
- **Candidate transition** predicate: `state != settled_state` AND `streak == 1` AND market
  open (RTH; never on carry-forward `quality_flag == 3` ticks) AND `confidence >= 0.70`.
- On candidate: write/refresh sentinel Redis key `intraday:prefetch:{date}` =
  `{status:'running', target_state, episode, started_at}` and spawn the prices-only refetch
  (Component 6) **detached** (fire-and-forget; never blocks the tick).
- **Debounce (episode):** `episode = f"{date}:{target_state}:{streak_start_ts}"`. If a sentinel
  for the same episode is already `running` or `done`-and-fresh, do **not** re-spawn. A genuinely
  new candidate (different target or stale sentinel) spawns afresh.
- Gated by `OPENCLAW_INTRADAY_15MIN_PREFETCH` (when OFF, no prefetch is spawned).

### Component 5 — Tick-3 data-ready gate (`scripts/redeploy_pipeline.py`)
On confirmed transition the detector syncs the regime (unchanged) and spawns the redeploy as
today. The redeploy, **before** `_spawn_orchestrator`, runs the gate:

1. Read sentinel `intraday:prefetch:{date}`.
2. `status == done` AND **freshness OK** → proceed.
3. `status == running` → poll every ~30 s up to **`GATE_TIMEOUT = 20 min`**:
   - completes `done` + fresh → proceed; `failed` → abort; timeout → abort.
4. **No sentinel** (sudden jump straight to a confirmed state, no tick-1 prefetch) → start a
   prices-only refetch **synchronously**, wait up to `GATE_TIMEOUT`, same proceed/abort logic.
5. **Abort path:** do NOT run `signals/handoff/trade/alpaca/reconcile`. The `market_regime`
   row + `regime_latest.json` stay updated (the regime *is* what it is); only execution is
   withheld. Post `⛔ redeploy aborted — price ingestion failed/timeout; no signals/orders`.

**Freshness check:** prices considered fresh when the prefetch sentinel reports `done` with a
recent `finished_at` AND `data_coverage` (or `prices.parquet` last-bar) shows the union
universe updated to `today` (guards the "done but actually stale/partial" case — the
connection-loss silent-gap failure mode). Define "recent" = within the current tick window.

On proceed: run the unchanged `REDEPLOY_STEPS`.

### Component 6 — Prices-only refetch entrypoint (`scripts/refetch_prices.py`)
New script (or a `--prices-only` mode on the collector). Responsibilities:
- Resolve the union universe for `today` (same resolver the collector uses).
- Refetch daily bars (today's live/partial bar) and `append_dedup` to `prices.parquet`
  (keep-last on `(ticker, date)` — **append-only**, no deletes). Update `data_coverage`.
- Write the sentinel completion: `{status:'done', finished_at, n_tickers}` on success;
  `{status:'failed', error}` on failure (partial/connection-loss → failed, never silently
  "done"). Reuses the collector's existing rate-limit / quarantine handling.
- The 16:30 ET EOD collect later overwrites today's row with the final close (dedup keep-last).

### Component 7 — Discord (`#intraday-regime`, registered 2026-06-09)
- Tick-1 candidate: `🔄 candidate {S0}→{S1} (tick 1/3) — prefetching prices`.
- Tick-3 proceed: existing `✅ confirmed {S0}→{S1} — redeploy spawned`.
- Tick-3 abort: `⛔ redeploy aborted — ingestion {failed|timeout}; regime updated, no orders`.

## Data flow (happy path)

```
T+0   tick-1: settled=LOW_VOL, state=TRANSITIONING, streak=1, conf≥0.70
            → sentinel{running}; spawn refetch_prices (detached); post "candidate 1/3"
T+0..  refetch_prices runs (~few min) → append_dedup prices.parquet → sentinel{done, fresh}
T+15  tick-2: TRANSITIONING, streak=2  (no-op)
T+30  tick-3: TRANSITIONING, streak=3, conf≥0.70 → CONFIRMED
            → sync market_regime + regime_latest.json
            → spawn redeploy_pipeline → gate reads sentinel{done, fresh} → PROCEED
            → signals(fresh prices) → handoff → trade → alpaca → reconcile
            → post "confirmed → redeploy spawned"
```

## Edge cases / error handling

- **Flip-flop before tick-3:** prefetch ran at tick-1; state reverts → no confirmation, no
  redeploy. Prefetched data is simply fresh (harmless); next candidate reuses it (debounce) or
  re-prefetches if stale.
- **Collect slow (> tick-3):** gate polls up to `GATE_TIMEOUT`, then proceeds (if done) or
  aborts (timeout). Never trades half-ingested data.
- **Collect failed / partial (connection-loss silent-gap):** freshness check fails → abort +
  alert. No signals/orders.
- **No tick-1 prefetch (sudden jump):** gate runs a synchronous refetch + wait.
- **Concurrent redeploys:** `intraday:redeploy:inflight` lock blocks overlap.
- **Manual operator flatten:** `liquidate:cooldown` still honored → no instant auto-redeploy.
- **Carry-forward / market closed:** no prefetch, no confirmation (existing `quality_flag==3`
  carry logic unchanged). After-hours redeploys still gated by `OPENCLAW_REDEPLOY_EXTENDED_HOURS`.
- **Cold start / post-restart:** settled-state bootstrap unchanged; a candidate requires a real
  `settled→new` with `streak==1`, so a cold start does not spuriously prefetch.
- **Flag OFF:** byte-identical-ish legacy behavior except cadence — see Rollout.

## Rollout / safety

- A single new flag **`OPENCLAW_INTRADAY_15MIN_PREFETCH`** (default OFF) gates **everything**:
  cadence (Component 1, read at cron registration), feature floor (Component 2), uniform-tier +
  cooldown-drop + in-flight lock (Component 3), and the prefetch + data-ready gate (Components
  4–6). **Flag OFF + restart = today's exact behavior** (5-min ticks, tiered hysteresis,
  cooldown, no prefetch). One flip + restart switches the whole feature on.
- **Activation:** dry-run `redeploy_pipeline.py --dry-run` exercising the gate against a mocked
  sentinel; verify the prices-only refetch on a live tick; review one full tick1→tick3 lifecycle
  in dry-run; THEN flip the flag and restart johnbot (operator-approved; johnbot is a systemd
  **user** service — `systemctl --user`).
- **Constraints:** VPS is 2-core — implementation/test subagents run **sequentially**, no
  parallel pytest/backtest. Master parquets are **append-only** (refetch uses dedup keep-last).
  Do not `git reset --hard`.

## Testing

**Unit (`tests/`):**
- Confirmation: all 4 states require 3 ticks; 2-tick streak does NOT fire; confidence floor
  enforced at firing tick; two confirmations 30–45 min apart both fire (no cooldown lockout).
- In-flight lock blocks a second overlapping redeploy spawn; `liquidate:cooldown` still blocks.
- Candidate detection: `streak==1` + new state + market-open + conf≥floor → spawn + sentinel;
  carry-forward tick does NOT; debounce skips same-episode re-spawn.
- Gate: `done`+fresh→proceed; `running`→wait→proceed; `failed`→abort (no steps run);
  timeout→abort; no-sentinel→synchronous refetch path; `done`-but-stale→abort.
- Feature floor: rows align to 15-min buckets.

**Integration / smoke:**
- Dry-run a full tick1→tick3 lifecycle with a mocked prices-only refetch (no live orders).
- `refetch_prices.py` live smoke: writes fresh today rows + sentinel `done`, dedup keep-last,
  master parquet not shrunk.

## Addendum 2026-06-09 — Prefetch fetches today's INTRADAY snapshot (Option B, all-asset)

**Finding (verified live, 15:16 ET):** re-running the daily `collect` intraday does NOT
produce today's bar — Alpaca delivers a *complete* daily bar only post-close, and the
collector's `updateCoverage` advances `date_to` only when `rowsAdded > 0`, so an intraday
daily-bar fetch writes 0 rows for today (`prices.parquet`/`data_coverage` topped out at the
prior session while the market was open). A daily-bar prefetch would therefore only heal gaps
in the *prior* close, leaving signals anchored on stale data (the 7% drift stays).

**Operator decision:** the prefetch must fetch **today's intraday snapshot** so signals +
brackets compute on the live price, **for all asset classes**.

**Mechanism (supersedes Component 6's daily `--prices-only` path):**
- New collector mode `runIntradaySnapshotPrices(universe)`:
  - **Equity + ETF:** `alpaca data multi-snapshots --symbols <chunk>` (batched ~100–200/call);
    take each symbol's `dailyBar` (today's partial OHLCV; `c` = current price). Verified
    available on this tier (SPY dailyBar returned live; the "Options Starter only" collector
    comment is stale for stocks).
  - **Crypto:** Alpaca crypto snapshot/latest bars (mirror `fillPricesAlpacaCrypto`).
  - **Indices / forex:** FMP real-time quote (mirror `fillPricesFmpHistorical` /
    `runMarketPricesNonEquity`); if a class's intraday source is unavailable, fall back to its
    last close for that class and log (non-fatal — graceful degradation).
  - Write each as a **partial today row** `{ticker, date=today_ET, o/h/l/c/v}` via the existing
    append-dedup (keep-last on `ticker,date`) + flush; advance `data_coverage.date_to=today`.
- `scripts/run_collector_once.js --intraday-snapshot` invokes it; `refetch_prices.py`
  `_run_price_fill` calls `--intraday-snapshot` (NOT `--prices-only`).
- **Freshness check tightens** (Component 5 / `refetch_prices._freshness_ok`): require
  `data_coverage.date_to >= today` (now achievable — the snapshot writes today's row). A refetch
  that fails to advance to today → sentinel `failed` → tick-3 gate aborts.
- **EOD finalization:** the 16:30 collect's `runHistoricalPrices` overwrites today's partial
  with the FINAL daily bar via the same dedup keep-last — the master series self-corrects post-close.
- **Accepted risk:** today's `dailyBar` is in-progress — `close` = current price (what signals/
  brackets need), but `high/low/volume` are partial; strategies keying off intraday high/low get
  partial mid-day values. Operator accepted this (Option B).

**Plan delta:** insert Task 3.5 (intraday-snapshot collector mode + repoint refetch + tighten
freshness) after Task 3; Task 7's gate consumes the tightened freshness automatically.

## OWED before activation (flag-ON only — does NOT affect the live flag-off system)

Final holistic review (2026-06-09) found one **activation-blocker** in the tick-3 freshness
check. `refetch_prices._freshness_ok` was implemented as `data_coverage.date_to >= today`
only — the spec's `finished_at`-recency half (Component 5: "done with a recent finished_at AND
today-coverage") was dropped. Because `date_to` is monotonic and the gate keys the sentinel by
**date only** (not the confirmed transition's episode) and never clears it, a **stale `done`
from a prior intraday episode can let the gate proceed on intra-day-stale prices** — reachable
only when (a) the confirmed target is TRANSITIONING (sub-floor ticks fall back to TRANSITIONING
so no `streak==1` candidate prefetch fires for that episode) AND (b) a prior episode's `done` is
still within the 6h sentinel TTL. Impact is bounded: same-day-but-1–2h-stale prices on a
TRANSITIONING redeploy (NOT prior-day stale — `date_to>=today` still holds), paper-only,
self-corrected by the 16:30 EOD collect.

**A naive `finished_at`-recency window does NOT work** — the legitimate tick-1 prefetch finishes
~30 min before the tick-3 gate (at 15-min cadence), so any recency window tight enough to reject
a stale prior-episode `done` would also reject the legitimate prefetch. **Robust fix = episode-bind
the gate:** the detector passes the confirmed transition's episode (`{date}:{state}:{streak_start_floor}`)
to `redeploy_pipeline.py`; the gate proceeds only when `sentinel.status=='done' AND
sentinel.episode == expected_episode AND date_to>=today`, else runs `_sync_refetch`. (Alternative:
clear the sentinel after the gate consumes it — but that leaves a fired-but-unconfirmed episode's
`done` lingering, so episode-binding is preferred.)

Two test gaps to close alongside: `done`-but-stale→abort/refetch, and `running`→poll-wait→proceed.

## Already-live caveat (rides this branch, NOT part of this feature)

`cron-schedule.js:416` OPG open-reconcile retime `28 9 → 25 9` (operator's pre-existing
uncommitted change, swept into the first feature commit `c0d2bca`). It is **not** gated by
`OPENCLAW_INTRADAY_15MIN_PREFETCH` — it's gated by `OPENCLAW_EOD_RECONCILE` (=1 in prod), so it
is **active on the live system now**. The change is correct (Alpaca OPG window closes 9:28;
firing at 9:28 lands at the deadline and is rejected). Operator is aware (flagged in-session).

## Activation runbook (operator-gated)

1. (Optional but recommended) land the episode-binding freshness fix above.
2. Dry-run validate the tick1→tick3 lifecycle (`redeploy_pipeline.py --dry-run`) and a live
   `refetch_prices.py --date <ET-today>` smoke (confirm it writes today's rows + advances
   `data_coverage`, and `prices.parquet` row count does not shrink).
3. NOTE: `OPENCLAW_INTRADAY_HMM_LIVE=1` is already set in prod → flipping
   `OPENCLAW_INTRADAY_15MIN_PREFETCH=1` makes redeploys spawn LIVE (not dry-run). Do step 2 first.
4. Flip `OPENCLAW_INTRADAY_15MIN_PREFETCH=1` in prod `.env` and restart johnbot
   (`systemctl --user` — operator-approved). The cron re-registers at `*/15`.
5. Watch the first 15-min ticks + first candidate→confirm lifecycle in `#intraday-regime`.

## Files touched

- `src/engine/cron-schedule.js` — tick cadence.
- `src/ingestion/intraday_features.py` — 15-min floor + docstring.
- `scripts/run_intraday_market_state.py` — uniform tiers, cooldown drop, in-flight lock,
  tick-1 candidate prefetch trigger + sentinel, Discord posts.
- `scripts/redeploy_pipeline.py` — data-ready gate (poll/wait/abort), cooldown-read removal,
  synchronous-refetch fallback.
- `scripts/refetch_prices.py` — NEW prices-only refetch + sentinel writer.
- `tests/test_intraday_15min_prefetch.py` (+ extend `tests/test_intraday_hmm*` as needed).
