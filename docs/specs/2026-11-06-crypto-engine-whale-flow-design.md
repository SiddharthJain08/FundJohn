# Crypto engine — whale/flow trackers, Crypto dashboard page, 24/7 crypto risk, pooled sizing (design, 2026-11-06)

**Operator brief (2026-11-05 23:52Z):** build live whale trackers for crypto markets to inform (a) a new crypto strategy
space, (b) the existing crypto regime monitor, and (c) emergency liquidation of crypto positions. Begin the crypto
engine here. Add a dashboard page "Crypto" to the right of Market holding the crypto regime structure plus the
parameters and signals that will drive all crypto investments (fold into Market only if there is not enough unique
content). Stay on Alpaca; target names tradable there. Sizing follows the standard rules: the benchmark ticker is
BTC/ETH (or other high-correlation coins); other names get alpha discounted by the benchmark's performance in the
current *crypto* regime; all positions, equity and crypto, pool into one sizing. Idea sources:
`kukapay/whale-tracker-mcp` (Whale Alert API) and `clawinfra/whalecli` (keyless on-chain + Hyperliquid, 0–100 whale score).

Status: **DESIGN — nothing built.** Every item marked *(my call)* is a default the operator can overturn; everything
else is the brief or a measured fact. Section 9 lists the decisions only the operator can make.

---

## 0. What exists today (measured 2026-11-05/06, read-only)

| Piece | State | Where |
|---|---|---|
| Crypto regime (SP-3.1) | LIVE. 4-state HMM on hourly BTC/ETH realized vol; same labels as equity (`LOW_VOL/TRANSITIONING/HIGH_VOL/CRISIS`). Hourly in-process cron `crypto-regime-tick` (UTC). Currently `CRISIS conf=1.0 streak=33`. | `src/regime/crypto_regime.py`, `scripts/run_crypto_market_state.py`, table `crypto_regime_states` (mig 118), `.agents/crypto-market-state/crypto_regime_latest.json`, `src/engine/cron-schedule.js:796` |
| Crypto bars | Daily `BTC-USD`-style rows in the main prices master via `fillPricesAlpacaCrypto`; hourly `BTC-USD`/`ETH-USD` only in `data/master/crypto_bars_1h.parquet` (whole-file rewrite, append-dedup). | `src/pipeline/collector.js:1221`, `src/ingestion/crypto_bars.py` |
| Crypto strategies | 3 live (`S_btc_momentum`, `S_btc_halving_clock_cycle_timing`, `S_kalman_laguerre_trend_crossover`), 4 candidates; all BTC/ETH only; signals minted only in the 10:00 ET daily run; gated on the crypto regime string. Backtest regime partition still uses the **equity** `historical_regimes.parquet`. | `src/strategies/implementations/`, `src/execution/engine.py:2274` |
| Crypto execution | Alpaca CLI only. Market `gtc` open sized by `pct_nav`, 45 s fill poll, resting `stop_limit` sell (limit = stop × 0.995). Close = cancel resting stop, then `position close`. No bracket. | `src/execution/alpaca_executor.py:1300-1500` |
| Crypto sizing | Separate lane: `crypto_redeploy_sizer` (λ=2.0 × NAV × crypto weight share), fired only on a confirmed crypto regime transition. **Not pooled**: the daily equity sizer drops `-USD` signals because `alpaca_tradable_universe` holds only `us_equity`. No benchmark rule C for crypto. | `src/execution/crypto_redeploy_sizer.py`, `regime_blended_sizer.py:889-918`, `src/maintenance/refresh_tradable_universe.py:60` |
| Breakers / flatten | `account_breaker`, `position_circuit_breaker`, `regime_liquidator` are **RTH weekdays, us_equity only**. Crypto has no night/weekend risk cover. | `src/execution/account_breaker.py`, `regime_liquidator.py:637-644`, `cron-schedule.js` `circuit-breaker-tick` |
| Dashboard | `:3000` inline SPA. Nav: Market, Portfolio, Strategies, Research, Pipeline Diagnostics. Crypto regime panel renders on the Market overview under the equity regime panel (`#crypto-regime-panel`, `_renderCryptoRegimeStructure`, `GET /api/regime/crypto`). | `src/channels/api/server.js:4826, :5867, :6109, :3346` |
| External streams pattern | `fetch → rows → append_dedup/append_insert_only` into a master parquet, one-line counters, systemd oneshot+timer (or `_sched` for sub-hourly/24/7), registered in `sync_data_ledger.PARQUET_MAP`, `master_freshness._CADENCES`, `scheduled_jobs`. | `src/ingestion/ingest_finra_short_interest.py` (template), `docs/systemd/README.md` |
| Alpaca crypto universe | 36 active `*/USD` pairs, all tradable + fractionable, none shortable: AAVE ADA ARB AVAX BAT BCH BONK BTC CRV DOGE DOT ETH FIL GRT HYPE LDO LINK LTC ONDO PAXG PEPE POL RENDER SHIB SKY SOL SUSHI TRUMP UNI USDC USDG USDT WIF XRP XTZ YFI. Zero crypto positions open. | `alpaca asset list --asset-class crypto --status active` |
| On-chain / flow keys | **None** (no Whale Alert, Etherscan, Glassnode, CryptoQuant, Coinglass). | `.env`, `.env.example` |

### 0.1 Known defects this design must fix on its way
- **D-1 Position symbol mismatch.** Alpaca's REST position path names crypto `BTCUSD` (Task-0 snapshot: "CLI maps to
  `BTCUSD`"); the CLI accepts `BTC/USD` on orders; `crypto_redeploy_sizer._normalize_broker_symbol` documents
  positions as `BTC/USD`. Which form `position list` returns cannot be re-verified tonight (zero crypto positions), so
  the normaliser must accept **both**. If positions really come back as `BTCUSD`, today's sizer misses them; the equity sizer's
  `_load_broker_positions_usd` would treat `BTCUSD` as an equity holding with no target and emit an `orphan_close`.
  One normaliser (`BTCUSD` | `BTC/USD` → `BTC-USD`, with the 36-pair list as the authority) must be shared by every
  reader of broker positions before any pooled sizing or any breaker touches crypto.
- **D-2 Regime label aliasing.** Crypto strategy weights are looked up by crypto-regime label from a table partitioned
  on the equity regime. The pooled sizer (§5) needs a crypto-regime-partitioned Sharpe for crypto strategies.
- **D-3 Alpaca's own crypto tape is too thin for flow signals** (29 prints in 30 min on BTC/USD, max 0.001 BTC). Flow
  data must come from elsewhere (§2).

### 0.2 Data sources verified reachable from this box (US, Massachusetts; probed 2026-11-05 23:5xZ)

| Source | Key | What it gives | Notes |
|---|---|---|---|
| Whale Alert (`leviathan.whale-alert.io`) | **paid**: Alerts API $29.95/mo (websocket, min `min_value_usd` $100 000, 100 alerts/h, 14 chains, 2 ws conns); REST history = Enterprise $699/mo | Labelled transfers: `from.owner`/`to.owner`/`owner_type`/`address_type` (exchange hot/cold/deposit wallet), `transaction_type` transfer/mint/burn | `GET /status` keyless → 14 chains incl. bitcoin, ethereum, tron, solana, ripple, dogecoin, litecoin, bitcoin_cash, polygon, algorand. This is the only source with **entity attribution** at scale. 7-day trial. |
| mempool.space | none | BTC blocks, mempool, per-tx outputs | ~1 req/s polite |
| blockchain.info | none | `unconfirmed-transactions` (100 most recent, with values; top prints tonight 113 / 60 / 60 BTC), `rawblock` | ~1 req/s |
| Etherscan | **free key, registration required** | ETH + ERC-20 transfers per address, 5 req/s | needed only for ETH wallet-level tracking |
| Hyperliquid `api.hyperliquid.xyz/info` | none | 235 perps: OI, funding, premium, mark/oracle, 24h volume; per-wallet fills/positions | covers ~all 36 Alpaca names |
| Binance futures `fapi.binance.com` | none | OI, funding, premium index, global + top-trader long/short ratios, taker buy/sell ratio; spot `aggTrades` (large prints) | **Answers from a US IP today; could be blocked without notice** → fallbacks below |
| Bybit, OKX public | none | OI (USD), funding | fallbacks for Binance |
| Coinbase Exchange public | none | trades, 24h stats | US-native spot tape |
| DefiLlama `stablecoins.llama.fi` | none | stablecoin circulating supply (USDT $177.5 bn) | daily "dry powder" |
| CoinGecko `/global` | none, rate-limited | total mcap, BTC dominance | daily |
| alternative.me | none | Fear & Greed | daily |

---

## 1. Intent, goals, non-goals

**Why now.** The crypto lane trades 24/7 with regime gating but no flow information, no night/weekend risk cover, no
benchmark hurdle, and sizing that is not pooled with equities. The crypto regime is CRISIS today with nothing watching
the book between 16:00 and 09:30 ET. Whale/flow data is the first new information source for the crypto strategy
space the operator wants to open next.

**Goals**
1. A live, append-only **crypto flow data layer** (whale transfers, exchange flow, derivatives positioning, stablecoin
   supply, sentiment) for every Alpaca-tradable pair, on the same ingestion pattern as the other keyless streams.
2. A **signal layer** that turns raw flow into per-asset, per-hour features and a bounded 0–100 whale-pressure score
   with direction, consumable by strategies (aux kind), by the regime monitor, and by the breaker.
3. A **Crypto dashboard page** (right of Market) holding the crypto regime structure (moved), the flow board, market-wide
   tiles, the whale event feed, and the crypto sizing/risk parameters.
4. A **24/7 crypto breaker** that can liquidate crypto positions on defined emergencies, shadow first.
5. A **pooled sizing contract**: BTC (ETH optional) as the crypto benchmark sleeve; alts hurdled by the benchmark's
   Sharpe in the current crypto regime; one λ·NAV normaliser across equity and crypto.

**Non-goals (this design)**
- No new crypto strategies here. The strategy space is its own brainstorm once the flow features exist and have ≥ 30
  days of history.
- No on-chain node, no indexer, no address-level wallet tracking beyond a bundled exchange-address registry.
- No shorting (Alpaca crypto is long-only) and no perps trading (Alpaca `crypto-perp` exists but is out of scope).
- No change to equity sizing behaviour: rule C, caps, D-4′ clamp stay exactly as specified in the 2026-08-29/10-26 specs.

**Anti-goals (what would make this a failure even if it "works")**
- A flow tick that can OOM the 8 GB box during the 15:00 ET compute (every fetch is small, streamed, and `nice -n 19`;
  no whole-parquet loads).
- A breaker that fires on a data outage ("no data" ≠ "stress": fail-open to *no action*, loud alert).
- A dashboard page whose numbers come from a different store than the sizer reads (one DB snapshot table, both read it).
- Silent skips: every tick prints counters; the health checks read the counters.

---

## 2. Data layer — `crypto_flows`

### 2.1 Universe and symbol map
`src/ingestion/crypto_flow_universe.json` *(my call: a checked-in JSON, regenerated by a script, not hand-edited)*:
one row per Alpaca active `*/USD` pair → `{ticker: 'BTC-USD', alpaca: 'BTC/USD', position: 'BTCUSD', binance: 'BTCUSDT',
hyperliquid: 'BTC', bybit: 'BTCUSDT', okx: 'BTC-USDT-SWAP', coinbase: 'BTC-USD', chain: 'bitcoin', whale_alert_symbol:
'BTC', coingecko_id: 'bitcoin', role: 'benchmark'|'alt'|'stable'|'commodity'}`. Stables (`USDC/USDT/USDG`) carry
`role: stable` and are tracked for supply only, never sized. `PAXG` is `commodity` (gold-backed; excluded from the
crypto hurdle, *my call*). `scripts/refresh_crypto_flow_universe.py` diffs against `alpaca asset list` and reports
adds/drops; it never removes a row (append-only), it marks `active=false`.

### 2.2 Providers (one module each, `src/ingestion/crypto_flow/providers/*.py`)
Common contract: `fetch(universe, since_ts) -> list[Event|Observation]`, uses `_http_retry.fetch_with_retry`, hard
timeout 20 s, returns an empty list plus a counter on failure. Order of preference and fallbacks are declared in
`providers/__init__.py`; a provider that fails 3 ticks in a row is marked `degraded` in the snapshot (dashboard shows it).

| Provider | Produces | Cadence |
|---|---|---|
| `btc_onchain` (blockchain.info unconfirmed + mempool.space blocks) | `whale_event` rows for BTC transfers ≥ threshold (default **$1 M** *my call*), with input/output addresses; exchange attribution from the registry (§2.3) | every tick |
| `eth_onchain` (Etherscan, **only if `ETHERSCAN_API_KEY` set**) | `whale_event` rows for ETH + tracked ERC-20 (LINK, UNI, AAVE …) transfers ≥ threshold **from/to registry addresses only** (we cannot scan all of ETH keylessly) | every tick |
| `whale_alert` (**only if `WHALE_ALERT_API_KEY` set**) | websocket `subscribe_alerts` (min $1 M *my call*, all 14 chains, tx_types transfer/mint/burn) → `whale_event` rows with `owner`/`owner_type`; resume with `id` on drop | persistent ws inside the tick process **or** a small long-running unit *(builder's choice; see §8)* |
| `derivs` (Hyperliquid → Binance → Bybit → OKX) | per-asset `observation`: `oi_usd`, `funding_8h`, `premium`, `ls_ratio_global`, `ls_ratio_top`, `taker_buy_sell`, `vol_24h_usd`; aggregated `oi_usd_total` across venues when ≥ 2 answer | every tick |
| `spot_tape` (Binance `aggTrades` → Coinbase trades) | per-asset `observation`: `large_prints_n`, `large_prints_usd_buy`, `large_prints_usd_sell` for prints ≥ **$250 k** *my call*; 1 000 most recent per asset per tick (coverage %, recorded) | every tick |
| `stables` (DefiLlama) | `observation` for USDT/USDC/USDG: `circulating_usd` | hourly |
| `macro_sent` (CoinGecko global, alternative.me) | `observation`: `btc_dominance`, `total_mcap_usd`, `fear_greed` | hourly |

### 2.3 Exchange-address registry
`src/ingestion/crypto_flow/exchange_addresses.json`, seeded from whalecli's registry (ETH: Binance 8, Coinbase 5,
Kraken 4, OKX 3, Bybit 2, Huobi 3, Bitfinex 3, FTX 2; BTC: Binance 3, Coinbase 2, Kraken 2, Bitfinex 2) plus any
addresses Whale Alert labels over time (when the key exists, every `owner_type in {exchange}` address seen is appended
with `source='whale_alert', first_seen`). Append-only. Attribution without Whale Alert is therefore **thin by
construction**, and the dashboard says so (`attribution_coverage` = share of whale USD with a known owner on either side).

### 2.4 Storage (NEVER-DELETE invariant, both files; add to `CLAUDE.md` invariant list)
- `data/master/crypto_whale_events.parquet` — raw events. Key `(source, chain, tx_hash, out_index)`, `append_insert_only`.
  Columns: `ts_utc, source, chain, symbol, ticker, amount, amount_usd, from_addr, from_owner, from_owner_type, to_addr,
  to_owner, to_owner_type, tx_type, direction` (`exchange_in` | `exchange_out` | `exchange_to_exchange` | `unknown`).
- `data/master/crypto_flows_1h.parquet` — hourly features per ticker (§3). Key `(ticker, ts_utc)`, `append_dedup`
  `mode='replace'` (a later tick within the hour overwrites the same hour).
- DB `crypto_flow_snapshot` (mig **168**): one row per `(ticker, ts_utc)` tick, JSONB `features`, plus the market-wide
  row `ticker='__MARKET__'`. Dashboard and breaker read the **latest** row per ticker; retention 90 days *(my call;
  parquet is the archive)*. Mig 168 also creates `crypto_flow_provider_health (provider, ts_utc, ok, rows, latency_ms, err)`.

### 2.5 Scheduler
`_sched('crypto-flow-tick', '*/15 * * * *', …, {timezone:'UTC'})` next to `crypto-regime-tick` in
`src/engine/cron-schedule.js`, spawning `scripts/run_crypto_flow_tick.py` detached with `nice -n 19` and
`MemoryMax` via `src/lib/capped_spawn.js` (cap **600 M** *my call*; the tick is HTTP + small frames). Gate:
`OPENCLAW_CRYPTO_FLOWS=1` (new; default unset = inert). The tick **skips** (with a counter) during the 15:00–15:50 ET
compute window on NYSE trading days, or whenever `pgrep` finds the pipeline orchestrator running (there is no lock file
today: `src/lib/process_guard.js` is an uncaught-exception guard, not a mutex), so it never co-tenants the compute. Log `logs/crypto_flows_<date>.log`. Registered in
`scheduled_jobs` (`('minutes', 15)`), `master_freshness._CADENCES` (both parquets, 1-day lag), `sync_data_ledger`.

---

## 3. Signal layer — features and the whale-pressure score

Computed in `src/ingestion/crypto_flow/features.py` (pure functions, unit-tested) at each tick over 1h/24h/7d windows
from the two parquets (windowed reads only, never the whole file).

Per ticker, per hour:
- **Transfer flow**: `whale_n_1h/24h`, `whale_usd_1h/24h`, `exch_in_usd_24h`, `exch_out_usd_24h`,
  `exch_netflow_usd_24h` (= in − out; positive = coins moving **onto** exchanges = sell-side supply),
  `exch_netflow_z_7d` (z-score vs the trailing 7-day hourly distribution), `attribution_coverage`.
- **Derivatives**: `oi_usd`, `oi_chg_24h_pct`, `funding_8h`, `funding_ann`, `premium`, `ls_ratio_global`,
  `ls_ratio_top`, `taker_buy_sell`, `oi_price_divergence` (OI up while price down over 24h → crowded short build;
  OI down with price down → deleveraging).
- **Tape**: `large_print_imbalance_1h` = (buy − sell)/(buy + sell) over large prints, `large_print_usd_1h`.
- **Deleveraging flag** `delev_1h`: `oi_chg_1h_pct ≤ −5 %` **and** `ret_1h ≤ −3 %` *(my call; thresholds live in
  `pipeline_config.crypto_flow_thresholds`, dashboard-visible)*.
- **Whale-pressure score** 0–100 with `direction ∈ {accumulating, distributing, neutral}` — adapted from whalecli's
  four components, re-weighted for market-level (not wallet-level) use *(my call)*:
  - exchange netflow magnitude & sign (0–40): `min(40, 3.5·log10(|exch_netflow_usd_24h|)·age_weight)` where
    `age_weight` is replaced by the attribution-coverage weight (score is damped when coverage < 25 %);
  - velocity (0–25): `whale_usd_24h` vs its 30-day mean, `log2` scaled, capped;
  - cross-asset agreement (0–20): share of alts whose 24h netflow sign matches BTC's (coordination);
  - derivatives confirmation (0–15): positive when funding, L/S and taker ratios lean the same way as the flow.
  Direction: `distributing` if netflow z ≥ +1 (onto exchanges) and tape imbalance ≤ 0; `accumulating` if z ≤ −1 and
  imbalance ≥ 0; else `neutral`. Score ≥ 70 is "alert" severity (same convention as whalecli).
- Market-wide row (`__MARKET__`): `stable_supply_usd`, `stable_supply_chg_7d_pct`, `btc_dominance`, `fear_greed`,
  `oi_usd_total`, `delev_market` (BTC or ETH `delev_1h`), `whale_score_btc`, `whale_score_eth`.

**Consumers**
- Strategies: new aux kind `crypto_flows` (hourly frame by ticker) via `requirements.json` → `engine.py` aux loading
  and the backtest mirror in `aux_data_loader.load_aux_data`. Backtests see the same parquet (point-in-time by
  `ts_utc`; the parquet is append-only so no look-ahead by construction — *the lesson from the options-aux staleness
  episode: the backtest loader must refuse a frame whose latest row is > 3 days stale rather than fall back*).
- Regime monitor: `run_crypto_market_state.py` **reads** `delev_market`, `exch_netflow_z_7d` (BTC) and
  `funding_ann` (BTC) and writes them into `features_json` as *advisory* columns. **Not** HMM inputs in this design
  (changing the HMM feature set means a refit and a new epoch; a follow-on if the shadow shows value).
- Breaker: §4.
- Dashboard: §6.

---

## 4. 24/7 crypto breaker — `src/execution/crypto_breaker.py`

Runs from `_sched('crypto-breaker-tick', '*/5 * * * *', UTC)`, gate `OPENCLAW_CRYPTO_BREAKER` (unset/0 = **shadow**:
evaluates, logs, posts a `[crypto_breaker] shadow` line to #trade-reports only when a trigger is hit; `1` = armed).
Skips (counter) when there are no crypto positions.

**Triggers** (any one, each with its own Redis cooldown `crypto_breaker:fired:{date}:{reason}`, 6 h *(my call)*):
| id | condition | source |
|---|---|---|
| `T1 regime_crisis_entry` | crypto regime **confirmed** transition into `CRISIS` within the last tick window | `crypto_regime_states` |
| `T2 deleveraging` | `delev_market=true` **and** BTC `ret_1h ≤ −5 %` | `crypto_flow_snapshot` |
| `T3 exchange_inflow_shock` | BTC or ETH `exch_netflow_z_7d ≥ 3` with `attribution_coverage ≥ 0.25` **and** `whale_score ≥ 80 distributing` | snapshot |
| `T4 crypto_book_drawdown` | crypto sleeve P&L from the sizer's last target ≤ **−8 %** *(my call)* or any single crypto position ≤ −15 % from entry | broker positions (normalised, D-1) + `alpaca_submissions` |
| `T5 data_blackout` | regime **and** flow snapshots both > 3 h stale while holding crypto | freshness |

**Actions**: `T1–T4` → `flatten_crypto(reason, dry_run)` = for each crypto position: `_cancel_open_crypto_stops`, then
`_route_crypto_close(100 %)` via `execute_single(sess=None, …)` (the `redeploy_crypto.py` pattern), poll to terminal,
audit row in `alpaca_liquidations` with `asset_class='crypto'`, Discord post `[crypto_breaker] FIRED reason=… n=…
notional=…`. `T5` → **no orders**, loud alert only (anti-goal: never liquidate on missing data). After a fire, the
crypto lane is held flat until the operator re-arms (`OPENCLAW_CRYPTO_BREAKER_REARM=<iso>`, mirroring the account
breaker) **or** the crypto regime confirms a non-CRISIS state for ≥ 24 h *(my call)*.

**Shadow exit criterion**: two weeks of shadow with every shadow fire reviewed and either justified or its threshold
retuned, before `=1`. Rollout: shadow → armed for `T4` only → armed for all.

**Reuse**: `regime_liquidator._load_broker_positions` (returns `asset_class`), `_poll_to_terminal`,
`_write_liquidation_audit`, `_post_to_discord`; `account_breaker`'s re-arm/escalation shape. The RTH gate and
`us_equity` filters are **not** reused.

---

## 5. Pooled sizing contract (equity + crypto, one λ·NAV)

The operator's rule, restated for crypto: BTC/ETH are the benchmark tickers; every other crypto name's conviction is
discounted by the benchmark's Sharpe in the current **crypto** regime; equity and crypto positions pool into one book.

### 5.1 Crypto benchmark sleeve
- New strategy `S_beta_btc` (class `crypto`, `benchmark_sleeve=True`, long `BTC-USD`, eligible in **all** crypto
  regimes, `benchmark_sleeve_always_on`, hold 1 d re-minted daily, exit hook `regime_exit` on crypto CRISIS entry
  *(my call — mirrors `S_beta_spy`)*. Weight := `S_m_crypto` for the current crypto regime (Amendment-1 semantics).
- `S_beta_eth` is **optional, off by default** *(my call)*; if the operator wants an ETH leg, the alt hurdle uses
  `S_m` of whichever benchmark the alt has the higher 90-day return correlation with (precomputed in the universe map).

### 5.2 `S_m_crypto`
`benchmark_baseline.regime_benchmark_sharpe_by_horizon(benchmark='BTC-USD', regime_source='crypto', calendar=365,
horizon=pipeline_config.benchmark_horizon_days)` — buy-and-hold BTC daily excess returns (rf = `OPENCLAW_RF_SOURCE`
DGS3MO) **partitioned by the crypto regime label history**.

**Crypto regime history (fixes D-2)** *(my call, mirrors the equity precedent)*: the equity backtest partition is
**not** the live HMM — `src/strategies/historical_regimes.py` is a deterministic 5-day-median VIX tier table, chosen for
reproducibility, and the live intraday HMM is regime-of-record for execution. Crypto follows the same split:
`src/strategies/crypto_historical_regimes.py` builds `data/master/crypto_historical_regimes.parquet` (append-only) as a
deterministic tier table on BTC-USD **daily** closes from the prices master (earliest BTC-USD row available; Alpaca
history starts ~2021, earlier rows come from the prior provider already in the master): 5-day median of 30-day
annualised realized vol → `LOW_VOL / TRANSITIONING / HIGH_VOL / CRISIS`, with the three thresholds **calibrated once**
so that tier frequencies over the overlap window (2025-11 → now) match the live HMM's state frequencies in
`crypto_regime_states`, then frozen in the module as constants (like the VIX 15/22/30 cut-points). The live hourly HMM
stays regime-of-record for the sizer and the breaker; the tier table is the backtest/`S_m` partition. The two will
disagree on some days exactly as VIX tiers and the intraday HMM do for equities today; that is accepted by the
"backtest side authoritative" ruling (2026-08-07). Hourly BTC/ETH bars are **not** backfilled to 2021 and the live HMM
training window is **not** extended (that would be a new crypto regime epoch, out of scope here).

**Negative `S_m_crypto`** *(my call)*: the SPY hurdle never met a negative S_m (0.81/0.42/0.49/1.54). BTC
buy-and-hold in CRISIS plausibly is negative. For the **crypto class only**: the hurdle uses `max(S_m_crypto, 0)` (alts
are never *boosted* by a losing benchmark) and the sleeve weight is `max(S_m_crypto, 0)` (long-only: the sleeve goes
flat in a regime where holding BTC loses). SPY semantics untouched. Mirrored in `unified_backtest` for crypto classes.

The S_m cache in `pipeline_config.benchmark_regime_sharpe` becomes a dict keyed by benchmark
(`{'SPY': …, 'BTC-USD': …}`) — fixes the single-payload invalidation noted in review.

### 5.3 Rule C for crypto alts, inside one normaliser
Inside `_sharpe_cadence_path`: each ticker carries `instrument_class`; the hurdle picks `S_m` by class
(`equity/etp → SPY`, `crypto → BTC-USD`); benchmark tickers of either class keep their own S_adj. Everything then enters
the **same** λ·NAV normaliser, per-ticker cap `0.10·(|S|+1)·λ·NAV`, cluster cap, D9 similarity, with benchmark
exemptions as today. `benchmark_max_nav_frac` becomes per class (`pipeline_config.benchmark_max_nav_frac_crypto`,
default **0.25** *(my call: BTC alone must not take a quarter of NAV without a conversation)*); the D-4′ clamp applies
per class. A new `pipeline_config.crypto_sleeve_max_nav_frac` (default **0.30** *(my call)*) caps gross crypto.

### 5.3a Regime-of-record per class inside the mixed book *(my call)*
`_sharpe_cadence_path` today takes one `regime_state`. Under pooling each ticker's strategy contributors carry a class,
and **every regime-keyed input is read from that class's regime**: strategy weights (`load_current(crypto_regime)` for
crypto, as `crypto_redeploy_sizer` already does), eligibility, conviction floor, acting-strategy gate, cadence-reset
bypass. The two sub-books are each evaluated against their own regime's parameters and then enter the one normaliser.
The zero-conviction flatten is evaluated **per class** (crypto conviction cannot keep the equity flatten from firing,
nor vice versa). A confirmed crypto regime flip stamps `cadence_reset` for crypto strategies only. Equity semantics are
therefore unchanged by construction (non-goal honoured); the only equity-visible effect is the shared normaliser and
the funding pass in 5.3b.

### 5.3b Funding: crypto is cash-only *(operator decision 9.5; default below)*
Alpaca crypto is non-marginable: it draws on non-marginable buying power, and every crypto buy also reduces equity
buying power 1:1, while the equity book is sized to λ=2.0·NAV on margin. One normaliser can therefore emit crypto
targets the account cannot fill. Default *(my call, pending 9.5)*: **crypto is a cash reservation**. The pooled sizer
sizes both classes in the one normaliser, clamps crypto gross to `crypto_sleeve_max_nav_frac`, then reserves that
notional from cash and re-normalises the equity book to `λ·NAV − crypto_notional` so the equity book yields, not
crypto. The executor gets a crypto buying-power guard (the DTBP guard is equity-only). The `crypto_sizing.shadow[…]`
line prints both the unconstrained and the funded crypto targets so the shadow review can see how often funding binds.

### 5.3c Breaker hold-flat contract *(my call)*
`crypto_breaker` writes `crypto_breaker:hold_flat = {since, reason}` (Redis, mirrored into `pipeline_config`). While
set: the pooled sizer and the transition re-run set **every** crypto target to 0, sleeve included; the executor refuses
crypto buys (`refused:hold_flat` counter); the Crypto page shows the chip in red. Cleared by `OPENCLAW_CRYPTO_BREAKER_REARM`
or by 24 h of confirmed non-CRISIS. `T4`'s reference: broker `unrealized_plpc` on the normalised crypto positions
(cost basis = fills), sleeve-level = Σ unrealized_pl / Σ cost_basis.

### 5.4 Plumbing this requires (the real work)
1. `refresh_tradable_universe.py` adds `asset_class='crypto'` rows from `alpaca asset list --asset-class crypto`.
   **Guard first**: `alpaca_tradable_universe` already has `asset_class`, but `regime_blended_sizer.py:895` and
   `trade_handoff_builder.load_tradable_universe` filter only on `status/tradable`, so adding crypto rows would make
   the *current* un-pooled lane stop dropping `-USD` signals. Both consumers get an explicit `asset_class='us_equity'`
   filter until `OPENCLAW_CRYPTO_POOLED_SIZING=1`.
2. D-1 normaliser used by `_load_broker_positions_usd`, `crypto_redeploy_sizer`, `account_breaker`,
   `position_ownership`, `crypto_breaker`.
3. Crypto strategy backtests partition on `crypto_historical_regimes.parquet` (fixes D-2); fleet re-gate of the 7
   crypto strategies on the new partition (small: 7 jobs).
4. The daily 15:55 ET execution lane places crypto orders through the existing `-USD` interception; `pct_nav` is
   supplied by the pooled sizer. `crypto_redeploy_sizer` becomes a **thin re-run of the pooled sizer for the crypto
   subset** on regime transition (same code path, crypto-only filter), not a second sizing model.
5. Shadow first: `OPENCLAW_CRYPTO_POOLED_SIZING` unset → the pooled path prints a `crypto_sizing.shadow[…]` line
   (targets it would have set) while the current lane keeps acting; `=1` flips after two clean shadow cycles, the same
   protocol used for `bench_sizing.shadow`.
6. Backtest ≡ live parity: the backtest side is authoritative; the pooled hurdle is mirrored in
   `unified_backtest` for crypto classes before the live flip.

---

## 6. Dashboard — "Crypto" page

Decision: a **separate page**, not a Market sub-section — there is enough unique content (regime structure, flow board
for 36 names, event feed, sizing/risk parameters, breaker state). Nav button `nav-crypto` inserted immediately after
`nav-market` (reads: Market · Crypto · Portfolio · Strategies · Research · Pipeline Diagnostics). New router
`src/channels/api/routes_crypto.js` mounted at `/api/crypto`; the existing `GET /api/regime/crypto` stays (the panel
moves, its endpoint does not).

Layout, top to bottom *(my call, GitHub-dark tokens, Chart.js 4 already loaded; one mockup round with the operator
before build if they want it)*:
1. **Hero strip**: BTC and ETH price / 24h, crypto regime badge (state, conf, streak, age), breaker status chip
   (`shadow | armed | FIRED <reason> <age>`), flows freshness chip, provider health dots (one per provider; grey =
   degraded).
2. **Crypto Regime Structure** — the existing `_renderCryptoRegimeStructure` panel, moved verbatim, with three advisory
   tiles appended (BTC netflow z, funding ann., deleveraging flag).
3. **Whale Flow Board** — table of the non-stable pairs: price, 24h, whale score (coloured 0–100 bar) + direction,
   exch netflow 24h (USD, signed) + z, OI (USD) + 24h Δ, funding (ann.), L/S global, taker ratio, large-print
   imbalance 1h, attribution coverage. Sortable; benchmark rows pinned on top. Click a row → right-side detail with 7-day
   sparklines of netflow, OI, funding, score.
4. **Market tiles**: stablecoin supply + 7d Δ, BTC dominance, Fear & Greed, aggregate OI, deleveraging flag, `S_m_crypto`
   by regime (4 values) with the current regime highlighted.
5. **Whale Event Feed** — last 50 `crypto_whale_events` rows (time, chain, symbol, USD, from→to owners or "unknown
   wallet", direction tag), SSE-pushed (`crypto_event` on `/events`) so it ticks live.
6. **Sizing & Risk Parameters** (read-only until the sizing phase lands): flags (`CRYPTO_FLOWS`, `CRYPTO_BREAKER`,
   `CRYPTO_POOLED_SIZING`), λ, `benchmark_max_nav_frac_crypto`, `crypto_sleeve_max_nav_frac`, breaker thresholds,
   last 10 breaker evaluations (shadow or armed), current crypto positions and open crypto signals.

Endpoints: `GET /api/crypto/snapshot` (latest per ticker + market row), `GET /api/crypto/history/:ticker?days=7`,
`GET /api/crypto/events?limit=50`, `GET /api/crypto/providers`, `GET /api/crypto/risk` (flags, params, breaker log),
`GET /api/crypto/positions`. Page poll 60 s while visible (pattern of `showPipelines`); `loadMarket`'s "onMarket"
check learns about the new page so the 5-min Market poll does not repaint over it; the Market overview drops its
`cryptoRegimePlaceholder`.

Mobile: the board collapses to score / netflow / OI Δ / funding columns under 768 px.

---

## 7. Rollout and safety

| Phase | Ships | Flags | Done when |
|---|---|---|---|
| **P1 Flow layer + Crypto page** | §2, §3, §6 (board, tiles, feed, regime move; §6.6 shows "pending" for sizing params) | `OPENCLAW_CRYPTO_FLOWS=1` | 7 consecutive days of ticks with every provider ≥ 95 % ok, freshness green, page live; ≥ 30 days of history before any strategy uses the aux kind |
| **P2 Crypto breaker (shadow)** | §4 | `OPENCLAW_CRYPTO_BREAKER` unset | two weeks of shadow, every shadow fire reviewed |
| **P3 Pooled sizing (shadow → live)** | §5 | `OPENCLAW_CRYPTO_POOLED_SIZING` | two clean shadow cycles; backtest mirror landed; fleet re-gate of the 7 crypto strategies |
| **P4 Crypto strategy space** | separate brainstorm + spec | — | — |

Safety invariants: all three new flags default inert; both parquets NEVER-DELETE (tmp + `os.replace`); the flow tick
and breaker tick refuse to run while the 15:00 ET compute guard is active; nothing here starts the system-scope
johnbot (user-scope only); a reboot requires re-checking the three `_sched` entries came back (they live in the
johnbot process, so they do).

Tests (per component, `pytest -k crypto_flow` — never the full suite while the fleet runs): universe map round-trip;
each provider parses a recorded fixture and returns counters on HTTP failure; feature functions on synthetic frames
(score bounds, direction rules, deleveraging flag); parquet writers idempotent on replay; breaker trigger table with a
fixture snapshot (each trigger fires once, cooldown holds, `T5` places no orders, dry-run places no orders); symbol
normaliser over `BTCUSD`/`BTC/USD`/`BTC-USD`/`SPY`/OCC; API handlers return `{available:false}` when tables are empty.

---

## 8. Left to the builder
- Whale Alert websocket hosting: inside the 15-min tick with a 14-min lifetime and `id`-resume, or a tiny long-running
  user-scope unit. Prefer the former unless alert loss > 1 % in the first week.
- Exact provider fallback ordering per field; which of Binance's spot `aggTrades` vs Coinbase trades is primary for
  large prints (coverage decides).
- JSONB shape of `features`; whether `crypto_flow_snapshot` keeps 90 or 30 days.
- Chart.js vs inline-span sparklines on the board.
- Threshold defaults in `pipeline_config.crypto_flow_thresholds` beyond those named above.
- Order of migrations/units in the plan; test fixtures.

---

## 9. Decisions only the operator can make (answer changes what gets built)

1. **Whale Alert subscription ($29.95/mo Alerts API, 7-day trial)** — yes now, later, or never? Without it we have
   BTC large transfers and derivatives/tape/stablecoin data, but **almost no exchange attribution** (the registry is
   ~40 addresses). The "exchange netflow" and `T3` trigger are weak without it. *Recommendation: start the trial in P1
   week 2 once the keyless layer is stable, decide on the data.*
2. **Etherscan free key** (registration, no cost) — register, or skip ETH wallet-level tracking? *Recommendation:
   register; it is the only keyless-cost way to see ETH/ERC-20 exchange flows.*
3. **Binance.com public data from a US IP** — accept it as the primary derivatives/tape source with Bybit/OKX/
   Hyperliquid/Coinbase fallbacks, or exclude it on policy grounds? *Recommendation: accept with fallbacks; Hyperliquid
   is primary for OI/funding anyway.*
4. **Benchmark**: BTC only, or BTC + ETH basket (alt hurdled by its higher-correlation benchmark)? *Recommendation:
   BTC only for P3; ETH sleeve as an amendment once the hurdle has shadow data.*
5. **Crypto NAV caps and funding**: `benchmark_max_nav_frac_crypto` 0.25 and `crypto_sleeve_max_nav_frac` 0.30 are my
   placeholders. What gross crypto exposure is acceptable at this stage of the paper account? And because Alpaca
   crypto is cash-only while equities run at λ=2.0 on margin, who yields when cash is short: the equity book (my
   default in §5.3b: crypto is a cash reservation up to its cap), or crypto scaled down to whatever cash is left after
   equities?
6. **Breaker arming scope**: is a flatten on confirmed CRISIS entry (`T1`) wanted at all, given the regime is CRISIS
   today and the redeploy sizer already de-weights on transition? Or breaker on `T2–T4` only?
7. **Crypto page vs Market section**: this design says separate page. Confirm, or fold into Market.
