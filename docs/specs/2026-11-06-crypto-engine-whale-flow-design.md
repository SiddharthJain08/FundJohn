# Crypto engine — whale/flow trackers, Crypto dashboard page, 24/7 crypto risk, pooled sizing (design, 2026-11-06)

**Operator brief (2026-11-05 23:52Z):** build live whale trackers for crypto markets to inform (a) a new crypto strategy
space, (b) the existing crypto regime monitor, and (c) emergency liquidation of crypto positions. Begin the crypto
engine here. Add a dashboard page "Crypto" to the right of Market holding the crypto regime structure plus the
parameters and signals that will drive all crypto investments (fold into Market only if there is not enough unique
content). Stay on Alpaca; target names tradable there. Sizing follows the standard rules: the benchmark ticker is
BTC/ETH (or other high-correlation coins); other names get alpha discounted by the benchmark's performance in the
current *crypto* regime; all positions, equity and crypto, pool into one sizing. Idea sources:
`kukapay/whale-tracker-mcp` (Whale Alert API) and `clawinfra/whalecli` (keyless on-chain + Hyperliquid, 0–100 whale score).

Status: **DESIGN APPROVED WITH AMENDMENTS 2026-11-06 00:42Z — nothing built yet.** Every item marked *(my call)* is a
default the operator can still overturn; everything else is the brief, an operator decision, or a measured fact.

**Operator decisions (2026-11-06 00:42Z)** — these override anything below that says otherwise:
- **No paid subscriptions** for crypto data; maximise free sources. Whale Alert is **rejected**. Etherscan free key
  provided and stored (`ETHERSCAN_API_KEY` in `.env`, placeholder in `.env.example`); verified against the V2
  multi-chain endpoint (55 chains, one key). §10 is the free-source catalogue and how to grant access to the rest.
- **Benchmark = BTC only.** No ETH sleeve.
- **Caps**: per-position and correlated-cluster caps for crypto are the **same rule and the same levels as equities**
  (`0.10·(|S|+1)·λ·NAV` per ticker, same cluster cap). BTC sleeve cap **25 % NAV**. Crypto gross cap **50 % NAV**.
- **Gross**: crypto fills the book **by conviction, the same way equities do**, inside the one normaliser, and may take
  up **unused leverage/NAV on equity TRANSITIONING / HIGH_VOL / CRISIS days** (when equity floors shrink the equity
  book). §5.3b is rewritten accordingly; crypto is not a subordinate cash reservation.
- **Breaker: arm the drawdown triggers only** (`T4`). Crypto is already de-levered as a whole, so no flatten on
  regime entry (`T1` removed) or on flow shocks (`T2`/`T3` become dashboard/strategy signals, not triggers).
- **Separate Crypto page confirmed.**

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
| ~~Whale Alert~~ | **paid** ($29.95/mo alerts, $699/mo REST) | labelled transfers with `owner_type` | **REJECTED 2026-11-06 (no paid subscriptions).** Only source with entity attribution at scale; we accept thinner attribution. |
| mempool.space | none | BTC blocks, mempool, per-tx outputs, hashrate/difficulty/pools | ~1 req/s polite |
| blockchain.info | none | `unconfirmed-transactions` (100 most recent, with values; top prints tonight 113 / 60 / 60 BTC), `rawblock` | ~1 req/s; `charts` endpoint returned 0 points (degraded) |
| **Etherscan V2** `api.etherscan.io/v2/api?chainid=…` | **free key — STORED** | native + ERC-20 transfers per address, token holders, 5 req/s; **55 chains on one key** incl. Ethereum, Arbitrum, Polygon, Base, BNB, OP, Avalanche C-Chain | covers ETH ARB POL AVAX LINK UNI AAVE CRV LDO GRT SHIB PEPE SUSHI YFI BAT ONDO RENDER SKY USDC USDT USDG |
| Hyperliquid `api.hyperliquid.xyz/info` + `stats-data…/leaderboard` | none | 235 perps: OI, funding, premium, mark/oracle, 24h volume; per-wallet positions (`clearinghouseState`); **leaderboard of 48 k traders with account value** → top-N whale perp positioning | covers ~all 36 Alpaca names |
| **Deribit** `www.deribit.com/api/v2/public` | none | **DVOL** (BTC/ETH implied-vol index, hourly), 944 live BTC options with `mark_iv` and OI → ATM IV, 25Δ skew, put/call OI | the only implied-vol input available for crypto; feeds the regime monitor as an advisory column |
| **CFTC COT** `publicreporting.cftc.gov/resource/gpe5-46if.json` | none | weekly Traders-in-Financial-Futures for BITCOIN / MICRO BITCOIN / NANO BITCOIN: leveraged funds, asset managers, dealers long/short | Tuesday positioning, released Friday; the regulated-whale signal |
| Binance futures `fapi.binance.com` | none | OI, funding, premium index, global + top-trader long/short ratios, taker buy/sell ratio; spot `aggTrades` (large prints) | **Answers from a US IP today; could be blocked without notice** → fallbacks below. `allForceOrders` (liquidations) is retired. |
| Bybit public | none | OI, funding, `account-ratio` (long/short) | fallback for Binance ratios |
| OKX public | none | OI (USD), funding, **`liquidation-orders`** (filled liquidations by size/side) | primary liquidation feed |
| Coinbase Exchange public | none | trades, 24h stats, hourly candles (350 bars/call) | US-native spot tape; 24/7 bar fallback |
| Kraken public | none | 1 000 recent trades per call | second spot tape |
| XRPL `xrplcluster.com` (JSON-RPC) | none | ledger stream; `account_tx` / payments ≥ threshold | XRP whale transfers |
| Solana public RPC `api.mainnet-beta.solana.com` | none, rate-limited | slots, signatures/transactions for addresses | SOL BONK WIF TRUMP RENDER whale transfers (best-effort; a free Helius key lifts the limits, §10) |
| BlockCypher `api.blockcypher.com/v1/{doge,ltc}/main` | none (3 req/s; free key raises) | blocks, large txs | DOGE LTC |
| Blockchair `api.blockchair.com/{bitcoin-cash,…}` | none (low limit; free key raises) | stats, large txs | BCH, DOT fallback |
| Koios `api.koios.rest` | none | Cardano tip, address txs | ADA |
| DefiLlama `api.llama.fi`, `stablecoins.llama.fi` | none | stablecoin circulating supply (USDT $177.5 bn), chain TVL (421 chains), DEX 24h volume, fees | daily "dry powder" + on-chain activity |
| CoinGecko `/global`, `/coins/markets` | none (keyless tier ~10–30 req/min; free Demo key raises, §10) | total mcap, BTC dominance, per-coin mcap/volume | daily |
| CoinPaprika `api.coinpaprika.com/v1` | none | tickers, mcap | CoinGecko fallback |
| alternative.me | none | Fear & Greed | daily |
| Polymarket `gamma-api.polymarket.com` | none | crypto prediction markets (prices = crowd probabilities) | already have `polymarket_client.py` |

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
| `evm_onchain` (Etherscan V2, key stored) | `whale_event` rows for native + ERC-20 transfers ≥ threshold **touching registry exchange addresses** on Ethereum, Arbitrum, Polygon, Avalanche C-Chain, Base, BNB (we cannot scan whole chains; we watch the exchange side, which is what netflow needs). Token contracts for the ~20 EVM names live in the universe map. 5 req/s budget shared across chains | every tick, round-robin by chain |
| `sol_onchain` (public RPC; Helius key if provided) | `whale_event` rows for SOL/SPL transfers ≥ threshold touching registry exchange addresses (SOL BONK WIF TRUMP) | every tick, best-effort |
| `xrp_onchain` (XRPL JSON-RPC) | `whale_event` rows for Payments ≥ threshold (XRPL exposes full payment history keylessly) | every tick |
| `utxo_alts` (BlockCypher DOGE/LTC, Blockchair BCH) | `whale_event` rows for large transfers | hourly (rate limits) |
| `derivs` (Hyperliquid → Binance → Bybit → OKX) | per-asset `observation`: `oi_usd`, `funding_8h`, `premium`, `ls_ratio_global`, `ls_ratio_top`, `taker_buy_sell`, `vol_24h_usd`; aggregated `oi_usd_total` across venues when ≥ 2 answer | every tick |
| `liquidations` (OKX `liquidation-orders`; Bybit ws optional) | per-asset `observation`: `liq_long_usd_1h`, `liq_short_usd_1h` | every tick |
| `hl_whales` (Hyperliquid leaderboard → `clearinghouseState` for the top-N by account value, N=200 *my call*) | per-asset `observation`: `whale_perp_net_usd` (Σ signed position value of top-N), `whale_perp_n_long/short` | hourly |
| `deribit_vol` | BTC/ETH `observation`: `dvol`, `atm_iv_30d`, `skew_25d`, `put_call_oi`; **advisory to the regime monitor** | every tick |
| `spot_tape` (Binance `aggTrades` → Coinbase → Kraken trades) | per-asset `observation`: `large_prints_n`, `large_prints_usd_buy`, `large_prints_usd_sell` for prints ≥ **$250 k** *my call*; 1 000 most recent per asset per tick (coverage %, recorded) | every tick |
| `cftc_cot` (Socrata) | market `observation`: leveraged-funds / asset-manager net BTC futures position, weekly Δ | daily check, new row Fridays |
| `stables_defi` (DefiLlama) | `observation` for USDT/USDC/USDG `circulating_usd`; market `tvl_usd`, `dex_vol_24h_usd` | hourly |
| `macro_sent` (CoinGecko → CoinPaprika, alternative.me, Polymarket crypto markets) | `observation`: `btc_dominance`, `total_mcap_usd`, `fear_greed`, selected prediction-market probabilities | hourly |

### 2.3 Exchange-address registry
`src/ingestion/crypto_flow/exchange_addresses.json`, seeded from whalecli's registry (ETH: Binance 8, Coinbase 5,
Kraken 4, OKX 3, Bybit 2, Huobi 3, Bitfinex 3, FTX 2; BTC: Binance 3, Coinbase 2, Kraken 2, Bitfinex 2), extended
with Etherscan's public **name tags** for the major exchanges' hot/cold wallets on every V2 chain (the `txlist`
response for a tagged address is the cheap way to confirm a label), the exchanges' own published deposit addresses
for XRP/SOL, and a weekly `scripts/refresh_exchange_addresses.py` that proposes new candidates (addresses receiving ≥
50 large transfers/week from ≥ 20 distinct senders — the deposit-wallet fingerprint) for operator review in
#trade-reports. Append-only; `source`, `first_seen`, `confidence` per row. Attribution without a paid labeller is
**thin by construction**, and the dashboard says so (`attribution_coverage` = share of whale USD with a known owner
on either side).

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

**Triggers** — **operator decision 2026-11-06: drawdown triggers only.** Crypto is already de-levered as a whole; the
regime redeploy already de-weights on transition. Each trigger has a Redis cooldown
`crypto_breaker:fired:{date}:{reason}`, 6 h *(my call)*.

| id | condition | source | status |
|---|---|---|---|
| `T4a crypto_sleeve_drawdown` | crypto sleeve Σ unrealized_pl / Σ cost_basis ≤ **−8 %** *(my call)* | broker positions (normalised, D-1) | **ARMED** when `OPENCLAW_CRYPTO_BREAKER=1` |
| `T4b crypto_position_drawdown` | any single crypto position `unrealized_plpc` ≤ **−15 %** *(my call)* → close **that position** only | broker positions | **ARMED** when `=1` |
| `T5 data_blackout` | regime **and** flow snapshots both > 3 h stale while holding crypto | freshness | alert only, never orders |
| ~~`T1 regime_crisis_entry`~~ | — | — | **removed** (operator) |
| `delev_market`, `exch_netflow_z_7d`, `whale_score` shocks | the former `T2`/`T3` conditions | snapshot | **signals only**: shown on the Crypto page, available to strategies as aux features, logged as `[crypto_flow] shock` lines; they place no orders |

**Actions**: `T4a` → `flatten_crypto(reason, dry_run)`; `T4b` → `close_crypto_position(ticker, reason, dry_run)`. Both =
`_cancel_open_crypto_stops`, then `_route_crypto_close(100 %)` via `execute_single(sess=None, …)` (the
`redeploy_crypto.py` pattern), poll to terminal, audit row in `alpaca_liquidations` with `asset_class='crypto'`, Discord
post `[crypto_breaker] FIRED reason=… n=… notional=…`. `T5` → **no orders**, loud alert only (anti-goal: never
liquidate on missing data). After a `T4a` fire the crypto lane is held flat (§5.3c) until the operator re-arms
(`OPENCLAW_CRYPTO_BREAKER_REARM=<iso>`, mirroring the account breaker) **or** 24 h have passed *(my call)*; after a
`T4b` fire only that ticker is held flat for 7 days (mirrors the equity stop-out cooldown).

**Shadow exit criterion**: two weeks of shadow with every shadow fire reviewed and either justified or its threshold
retuned, before `=1`.

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
- **BTC only** (operator 2026-11-06). No ETH sleeve; ETH is hurdled like every other alt.

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
the **same** λ·NAV normaliser, **the same per-ticker cap `0.10·(|S|+1)·λ·NAV` and the same cluster cap at the same
levels as equities** (operator 2026-11-06; crypto clusters come from the existing asset-correlation module run over the
pooled return panel, so BTC/ETH/SOL-correlated alts share a cluster with each other, not with equities), D9 similarity,
with benchmark exemptions as today. `benchmark_max_nav_frac` becomes per class:
`pipeline_config.benchmark_max_nav_frac_crypto = 0.25` (**BTC sleeve cap 25 % NAV**, operator); the D-4′ clamp applies
per class. `pipeline_config.crypto_gross_max_nav_frac = 0.50` (**crypto gross cap 50 % NAV**, operator).

### 5.3a Regime-of-record per class inside the mixed book *(my call)*
`_sharpe_cadence_path` today takes one `regime_state`. Under pooling each ticker's strategy contributors carry a class,
and **every regime-keyed input is read from that class's regime**: strategy weights (`load_current(crypto_regime)` for
crypto, as `crypto_redeploy_sizer` already does), eligibility, conviction floor, acting-strategy gate, cadence-reset
bypass. The two sub-books are each evaluated against their own regime's parameters and then enter the one normaliser.
The zero-conviction flatten is evaluated **per class** (crypto conviction cannot keep the equity flatten from firing,
nor vice versa). A confirmed crypto regime flip stamps `cadence_reset` for crypto strategies only. Equity semantics are
therefore unchanged by construction (non-goal honoured); the only equity-visible effect is the shared normaliser and
the funding pass in 5.3b.

### 5.3b Funding and gross *(operator decision 2026-11-06)*
Crypto **fills the book by conviction exactly as equities do**: both classes enter the one λ·NAV normaliser, each
ticker's weight is its hurdled conviction, and the caps in §5.3 bound the result. Crypto is explicitly allowed to
**take up unused leverage/NAV on equity TRANSITIONING / HIGH_VOL / CRISIS days**: on those days the equity conviction
floors and regime weights shrink the equity gross, the normaliser's remaining budget is free, and crypto conviction
may absorb it up to the 50 % gross cap. Crypto is **not** a subordinate cash reservation.

The one physical constraint is that Alpaca crypto is non-marginable: crypto notional must be ≤ non-marginable buying
power after the equity targets' Reg-T requirement (equity_gross / 2 under 2× λ). A **feasibility pass** after the
normaliser checks this; if it binds (expected rarely — it needs equity gross near 2× NAV *and* crypto conviction near
its cap on the same day) crypto is scaled pro-rata to the available cash, the shortfall is logged
(`crypto_sizing.funding_bind=…`) and the Crypto page shows it. The executor gets a crypto buying-power guard (the
DTBP guard is equity-only). The `crypto_sizing.shadow[…]` line prints the unconstrained and the feasible crypto
targets so the shadow review can see how often funding binds.

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
   `CRYPTO_POOLED_SIZING`), λ, `benchmark_max_nav_frac_crypto`, `crypto_gross_max_nav_frac`, breaker thresholds,
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
| **P2 Crypto breaker (shadow → armed, `T4` only)** | §4 | `OPENCLAW_CRYPTO_BREAKER` unset → `1` | two weeks of shadow, every shadow fire reviewed |
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

## 7a. As built — Phase 1 (2026-11-06, branch `crypto-flow-p1`, 32 commits, 222 Python + 30 Node offline tests)

Where the build deviated from §2–§6 above, the code is authoritative and this section records it (every item is a
controller ruling in the SDD ledger, surfaced to the operator at finish):

- **Whale-score netflow component** is `min(40, (40/7)·log10(max(|netflow|,1))·cov_w)` — saturates at $10 M/24 h — not
  the `3.5·log10` written in §3 (that constant could not reach the spec's own score bound). REVISIT in the shadow weeks:
  BTC/ETH netflow routinely exceeds $10 M, so the component pins at 40 for the majors; a per-asset normalisation (30-day
  median |netflow| or the z-score) is the likely replacement.
- **Snapshot cadence**: `crypto_flow_snapshot.ts_utc` is floored to **15 min** (the tick), not the hour — the PK +
  `ON CONFLICT DO NOTHING` would otherwise keep only the :00 tick. The flows parquet stays hour-floored (replace).
- **Snapshot retention**: append-only (no prune), overriding §2.4's "90 days". Growth ≈ 3.4 k rows/day (~2.5 GB/yr
  JSONB). Revisit at 90 days with a prune migration if disk matters; the NEVER-DELETE spirit wins until then.
- **Intra-hour features** (`oi_chg_1h`, `oi_chg_24h`, `ret_1h`, `ret_24h`) are computed from the 15-min snapshot history
  (`store.read_snapshot_history`), not from the hour-floored flows rows; `price_usd` is the perp **mark** (Hyperliquid,
  per tick) with the CoinGecko spot kept as `price_spot_usd`; macro_sent's raw fields are named `spot_px, spot_mcap_usd,
  spot_vol_24h_usd, cg_ret_1h, cg_ret_24h` so they never collide with feature outputs.
- **Slow-provider carry**: hourly/daily observations are carried between ticks from a raw `slow_obs` store in the state
  file (with per-field timestamp and cadence), never from snapshot features; a carried field expires after 3× its
  provider's cadence (3 h / 3 d).
- **Tick guards** (`should_skip`): 15:00–15:50 ET Mon–Fri clock window; the shared Redis run lock
  (`src/lib/run_lock`, checked for the ET and UTC dates); pgrep of the compute step scripts (`engine.py`,
  `regime_blended_sizer_live.py`, `alpaca_executor`, `run_collector_once`, `pipeline_orchestrator.py`); and a
  `MemAvailable` floor (`OPENCLAW_CRYPTO_FLOW_MIN_MEM_MB`, default 1500). `--force` bypasses all four, never the gate.
- **Hard bound**: the spawn is wrapped in `timeout --signal=TERM --kill-after=30 840`; the driver's SIGTERM handler
  releases the flock and prints the summary with `skipped=sigterm`; providers with long loops honour `deadline_at`
  (clamped to the next 15:00 ET) and return partial results; an exclusive `flock` on `<state>.lock` serialises ticks and
  a `skipped=locked` tick writes a `__tick__` provider-health row. Tick-level deadline `OPENCLAW_CRYPTO_FLOW_DEADLINE_S`
  (600 s) gates provider starts.
- **Memory cap** `OPENCLAW_CRYPTO_FLOW_MEMORY_MAX` default **1G** (not 600M): the Hyperliquid leaderboard is a 40 MB JSON
  (48 k rows) and the measured tick peak was ≈457 MB RSS before the other reads.
- **Provider order** (fallback preference under the deadline): btc_onchain, evm_onchain, derivs, liquidations,
  spot_tape, deribit_vol, xrp_onchain, sol_onchain, utxo_alts, **hl_whales**, cftc_cot, stables_defi, macro_sent,
  santiment. `sol_onchain` and `utxo_alts` are **hourly** (RPC budgets), not every tick.
- **Exchange-address registry** is validated on-chain (format + activity: Etherscan ≥ 1000 txs, mempool.space
  `tx_count ≥ 1000`, XRPL balance ≥ 1 M XRP, Solana ≥ 1000 signatures); rejects live in `_rejected` with reason/date.
  Upstream whalecli held fabricated entries (`…Kzobserved`). EVM EOAs are chain-portable, so the ETH registry is watched
  on every tracked EVM chain. The weekly `refresh_exchange_addresses.py` candidate-proposer is **deferred**.
- **BTC block coverage** uses blockchain.info `rawblock` (whole block, ~8 MB) with the mempool.space 25-tx page as the
  fallback; Binance FUTURES symbols for PEPE/BONK/SHIB are `1000…USDT` (spot keeps the plain symbol); Hyperliquid
  `kBONK/kPEPE/kSHIB` are ×1000 units.
- **Failure signalling**: a provider whose primary source returns None raises `ProviderFetchError` (health `ok=False`);
  `providers_failed_this_tick` on the market row lists them; `/api/crypto/providers.degraded` is the 3-in-a-row rule.
  URL-borne secrets (`api-key`, `apikey`, `key`, `token`, `access_token`) are redacted in `_http_retry` failure logs.
- **Health policy** for `crypto-flow-tick` is `('hours', 1)` (the `intraday_min` kind is judged only in RTH).
- **Regime advisory** uses `adv_btc_funding_8h` (per-8 h fraction), shown ×100 on the dashboard; the DVOL tile reads
  `dvol_btc` derived from the BTC-USD Deribit observation.
- **Thresholds** live in `features.DEFAULT_THRESHOLDS` and are mirrored in `/api/crypto/risk` (JS); the
  `pipeline_config.crypto_flow_thresholds` indirection is deferred until the shadow review tunes them.
- **Dashboard**: `large_print_imbalance_1h` spans the returned tape window (1 000 prints), not literally 1 h; the mobile
  board keeps ticker/price/24 h/score/netflow/coverage; degraded provider dots are yellow; `S_m crypto by regime` cells
  read `pending` until Phase 3.
- **Soak (2026-11-06 23:45Z → 11-10 20:10Z, 3.85 days)**: 363 ticks, 0 errors, 0 OOM; 96/96 ticks on 11-07 and 11-08,
  90/96 on 11-09 and 80/80 on 11-10 to 19:45Z (the 7 skips were `compute_window` ×5 and `run_lock_held` ×2 — the guards
  working as designed); every provider ≥ 99.2 % ok (Deribit DVOL None ×3, XRPL ×1, Etherscan unsupported-chain ×2);
  4 925 whale events, 3 128 hourly flow rows, 180 Santiment rows; snapshot table 15 MB. Calibrations landed from the soak:
  Etherscan free tier serves ETH/ARB/POL only (`OPENCLAW_CRYPTO_EVM_CHAINS`, default `ETH,ARB,POL`; AVAX/BASE/BNB
  transfers are therefore not watched) and the throttle is 2 req/s (`OPENCLAW_CRYPTO_ETHERSCAN_RPS`; 3/s still tripped).
  First signals: XRP-USD whale score 74 accumulating (shock line fired), ETH-USD 62 distributing, BTC-USD 42
  accumulating with −$41.7 M exchange netflow/24 h at 0.26 % attribution coverage (thin by construction).
- **Deferred to a follow-up**: Polymarket crypto probabilities, ADA via Koios, `refresh_exchange_addresses.py`, strategy
  aux-kind wiring (Phase 4), `ijson` streaming of the leaderboard, a prune migration for the snapshot table.

## 7b. As built — Phase 2 breaker (2026-11-10, branch `crypto-breaker-p2`, merged `28323490`, 145 Python + 36 Node offline tests)

- **Live since 2026-11-10 21:31Z in SHADOW**: `OPENCLAW_CRYPTO_BREAKER_TICK=1` (5-min `crypto-breaker-tick` in johnbot `_sched`,
  `timeout 240`, MemoryMax 400M, `os.nice(10)`); the arm flag `OPENCLAW_CRYPTO_BREAKER` stays unset until Phase 3 sizes crypto
  positions and two weeks of shadow evals are clean. First tick 21:35Z: `no crypto positions`; `/api/crypto/risk.breaker.status = shadow`;
  dry run with a fake-positions file (dd −11.5 %) wrote one `mode='dry_run'` eval row and posted the shadow line.
- **Triggers as decided in §9**: T4a sleeve drawdown ≤ −8 % (flatten every crypto position; subsumes T4b), T4b position ≤ −15 %
  (close that ticker), T5 regime+flow both > 3 h stale = alert only. Thresholds env-overridable, clamped to (−1, 0).
- **Orders** go through `alpaca_executor.execute_single(... close_only=True, strategy_id='__crypto_breaker__')`, polled to terminal,
  audited in `alpaca_liquidations` (+ `asset_class`, mig 169) and `crypto_breaker_evals` (mig 169). Evidence trail precedes the first
  order: eval row `flatten:started`/`close_started:<T>` → hold + cooldown → mirror → FIRING post → orders → FIRED (with `remain_open=`).
- **Hold contract** (deviation from the plan's bare-sleeve mirror): Redis `crypto_breaker:hold_flat` (24 h) and
  `crypto_breaker:hold_ticker:{T}` (7 d) are the source; the `pipeline_config.crypto_breaker_hold_flat` mirror is `{sleeve, tickers}` JSON
  (or `''`), never overwritten while Redis is unreachable, and read back (sleeve AND tickers) when Redis is down or its scan fails.
  Consumers: `crypto_hold.hold_state()` → redeploy sizer zeroes targets, executor refuses crypto BUYs. Re-arm:
  `OPENCLAW_CRYPTO_BREAKER_REARM=<since iso>` (sleeve) / `OPENCLAW_CRYPTO_BREAKER_REARM_TICKER=<T>` (one ticker; unset after one tick).
- **Cooldowns**: `crypto_breaker:fired:{date}:{reason}` 6 h; shadow/dry-run scoped `{mode}:` so a shadow would-fire never suppresses
  the first armed fire; armed cooldown falls back to `crypto_breaker_evals` when Redis is down. A hold does not gate a re-fire — the
  cooldown does; a position still past the threshold re-fires after 6 h (intended retry).
- **Blindness is loud**: `positions_unavailable` posts DEGRADED (6 h cooldown; every tick when Redis is down) and exits 1 when armed;
  system check `crypto_breaker_evaluating` (tag `broker`) WARNs when the newest eval is > 30 min old while crypto is held.
- **Known limits**: mirror-sourced holds carry no TTL during a long Redis outage; the system check fails open when it cannot reach
  the DB; duplicate broker rows where only one carries a cost basis merge to `plpc=None` (counted, T4a still covers them).

## 8. Left to the builder
- Exact provider fallback ordering per field; which of Binance's spot `aggTrades` vs Coinbase trades is primary for
  large prints (coverage decides).
- JSONB shape of `features`; whether `crypto_flow_snapshot` keeps 90 or 30 days.
- Chart.js vs inline-span sparklines on the board.
- Threshold defaults in `pipeline_config.crypto_flow_thresholds` beyond those named above.
- Order of migrations/units in the plan; test fixtures.

---

## 9. Operator decisions — RESOLVED 2026-11-06 00:42Z

| # | Question | Decision |
|---|---|---|
| 1 | Whale Alert subscription | **No paid subscriptions.** Maximise free sources (§10). |
| 2 | Etherscan key | **Provided and stored** (`ETHERSCAN_API_KEY`). V2 multi-chain verified. |
| 3 | Binance.com public data from a US IP | Not objected to under "maximise free data"; kept **with fallbacks** (Hyperliquid primary for OI/funding; Bybit/OKX ratios; Coinbase/Kraken tape). Flagged as fragile. |
| 4 | Benchmark | **BTC only.** |
| 5 | Caps and funding | Per-position and cluster caps **same rule, same levels as equities**; **BTC 25 % NAV**; **crypto gross 50 % NAV**; crypto fills the book by conviction like equities and may take **unused NAV on equity TRANS/HIGH_VOL/CRISIS days**; feasibility pass only for the non-marginable constraint (§5.3b). |
| 6 | Breaker scope | **Drawdown triggers only** (`T4a` sleeve, `T4b` position). `T1` removed; flow shocks are signals, not triggers. |
| 7 | Page | **Separate Crypto page confirmed.** |

Still open: none that block Phase 1. (The threshold placeholders marked *(my call)* get reviewed in the shadow weeks.)

## 10. Free data catalogue and how to grant access

Every row below was probed from this box on 2026-11-05/06. "Operator action" is what you do to turn on the extra
tier; everything marked *none* works today with no action. Keys go into `/root/openclaw/.env` as `NAME=value` (the
file is root-only and git-ignored) — paste them in Discord and I add them, or add the line yourself; `.env.example`
gets the empty placeholder. Nothing here costs money.

### 10.1 Working now, no key (built into Phase 1)
| Source | Signal it feeds | Limits |
|---|---|---|
| mempool.space, blockchain.info | BTC whale transfers; hashrate/difficulty | ~1 req/s |
| Etherscan V2 (**your key**) | EVM whale transfers on 6 chains for ~20 Alpaca names; exchange netflow; token holders | 5 req/s, 100 k calls/day |
| Hyperliquid info + leaderboard | perp OI/funding/premium for ~all names; **top-200 whale perp positioning** | unmetered public |
| Deribit public | BTC/ETH implied vol (DVOL), ATM IV, skew, put/call OI → regime advisory | unmetered public |
| CFTC COT (Socrata) | weekly leveraged-funds / asset-manager BTC futures positioning | unmetered |
| Binance futures + spot | OI, funding, global & top-trader L/S, taker ratio, large prints | US-IP fragile |
| Bybit, OKX public | OI, funding, L/S ratio, **liquidations** (OKX) | generous |
| Coinbase, Kraken public | spot tape, 24/7 hourly candles | generous |
| XRPL, Solana RPC, Koios, BlockCypher, Blockchair | XRP / SOL-ecosystem / ADA / DOGE / LTC / BCH whale transfers | Solana RPC and Blockchair are tight; see 10.2 |
| DefiLlama | stablecoin supply, chain TVL, DEX volume, fees | unmetered |
| CoinGecko (keyless), CoinPaprika | mcap, dominance, per-coin volume | CoinGecko ~10–30 req/min keyless |
| alternative.me | Fear & Greed | daily |
| Polymarket gamma | crypto prediction-market probabilities | existing client |

### 10.2 Free registrations (operator checked 2026-11-06 21:08Z: **Helius and Santiment keys PROVIDED and stored**; CoinGecko Demo, Dune, Glassnode were not free or trial-only → dropped; BlockCypher, Blockchair, Subscan, Messari, CryptoCompare not yet checked → later if needed)
| Service | Env var | What it adds | Why it matters |
|---|---|---|---|
| **CoinGecko Demo API** (coingecko.com → Developer Dashboard) | `COINGECKO_API_KEY` | 30 req/min, 10 k calls/month, stable auth | lifts the keyless throttle so per-coin mcap/volume for all 36 names fits in one tick |
| **Helius** — **KEY STORED** | `HELIUS_API_KEY` | Solana RPC with 1 M credits/month, enhanced parsed transfers, address webhooks | the public Solana RPC is the weakest link for SOL/BONK/WIF/TRUMP whale transfers |
| **Dune Analytics** (dune.com, free plan: 2 500 credits/month API) | `DUNE_API_KEY` | run community SQL dashboards (exchange netflows, ETF holdings, stablecoin mints) via API | the closest free substitute for Whale Alert's labelled exchange flows; 2 500 credits ≈ one daily refresh of 3–4 queries |
| **Glassnode** (studio.glassnode.com, free "Standard" account) | `GLASSNODE_API_KEY` | API access to Tier-1 metrics at 24 h resolution (active addresses, exchange balance for BTC/ETH on the free tier, SOPR basics) | daily exchange-balance series = the netflow ground truth we otherwise approximate; the endpoint returned 401 keyless, so the key is required |
| **Santiment SanAPI** — **KEY STORED** | `SANTIMENT_API_KEY` | verified: `exchange_balance`, `exchange_inflow/outflow`, `whale_transaction_count_1m_usd_to_inf`, `daily_active_addresses` for BTC/ETH, **30-day lag**, 1-year window | lagged daily history → `crypto_onchain_daily.parquet` (backtest ground truth for our netflow proxy; never a live signal) |
| **BlockCypher** (blockcypher.com, free token) | `BLOCKCYPHER_TOKEN` | 3 → 200 req/s for DOGE/LTC | removes the hourly-only cadence for those two names |
| **Blockchair** (blockchair.com/api, free key) | `BLOCKCHAIR_API_KEY` | higher limits for BCH and fallback chains | same |
| **Subscan** (subscan.io, free key) | `SUBSCAN_API_KEY` | Polkadot transfers | the only DOT source; low priority |
| **Messari** (messari.io, free) | `MESSARI_API_KEY` | asset metrics, supply schedules, unlock calendars | token-unlock dates for the alt names (supply shocks) |
| **CryptoCompare / CoinDesk Data** (free key) | `CRYPTOCOMPARE_API_KEY` | exchange-level volumes, order-book snapshots, social stats | third-source fallback for prices and volume |

### 10.3 Looked at and not usable for free
- **Whale Alert** — paid only (rejected).
- **CryptoQuant, Coinglass, Nansen, Arkham, Kaiko** — paid API tiers only (web UIs are free but not scrapeable in good faith).
- **Spot BTC ETF flows** — Farside returns 403 to non-browser clients; iShares' holdings download returns the HTML
  shell without a browser session. Substitute: Dune community queries on ETF holdings (10.2), or FMP ETF holdings via
  the key we already have (checked in the plan; FMP `/stable/etf/holdings` coverage for IBIT/FBTC to be verified).
- **Binance `allForceOrders`** (liquidations) — retired by Binance; OKX covers it.
- **blockchain.com `charts`** — returned zero points; mempool.space covers hashrate/difficulty.
- **Bitnodes** — non-JSON response from this box; node count is low value anyway.
