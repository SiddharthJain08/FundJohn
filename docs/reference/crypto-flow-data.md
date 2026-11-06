# Crypto flow data layer (whale / exchange-flow / derivatives / sentiment)

Phase 1 of the crypto engine (spec `docs/specs/2026-11-06-crypto-engine-whale-flow-design.md`, as-built §7a;
plan `docs/superpowers/plans/2026-11-06-crypto-flow-phase1.md`). Live since 2026-11-06 23:40Z behind
`OPENCLAW_CRYPTO_FLOWS=1`. No paid data; every source is keyless or on a free key (`ETHERSCAN_API_KEY`,
`HELIUS_API_KEY`, `SANTIMENT_API_KEY`; optional `COINGECKO_API_KEY`, `BLOCKCYPHER_TOKEN`, `BLOCKCHAIR_API_KEY`).

## Runtime
- **Job**: in-process cron `crypto-flow-tick`, `*/15 * * * *` UTC (`src/engine/cron-schedule.js`), spawns
  `timeout --signal=TERM --kill-after=30 840 python3 scripts/run_crypto_flow_tick.py` inside a
  `systemd-run --scope -p MemoryMax=$OPENCLAW_CRYPTO_FLOW_MEMORY_MAX` (default `1G`); `os.nice(19)` inside.
  Log: `logs/crypto_flows_<YYYY-MM-DD>.log`. Summary line per tick:
  `[crypto-flow] ts=… providers_ok=… providers_failed=… providers_skipped=… events_new=… flows_rows=… snapshot_rows=… degraded=… skipped=… dt_s=…`
- **Skips** (`should_skip`): 15:00–15:50 ET Mon–Fri; shared Redis run lock held (`src/lib/run_lock`); a compute step
  script running (`engine.py`, `regime_blended_sizer_live.py`, `alpaca_executor`, `run_collector_once`,
  `pipeline_orchestrator.py`); `MemAvailable < OPENCLAW_CRYPTO_FLOW_MIN_MEM_MB` (1500). `--force` bypasses skips only.
- **Flags/env**: `OPENCLAW_CRYPTO_FLOWS` (gate), `OPENCLAW_CRYPTO_FLOW_MEMORY_MAX` (1G), `OPENCLAW_CRYPTO_FLOW_DEADLINE_S`
  (600; gates provider starts; long loops also honour the deadline), `OPENCLAW_CRYPTO_WHALE_MIN_USD` (1 000 000),
  `OPENCLAW_CRYPTO_LARGE_PRINT_USD` (250 000), `OPENCLAW_CRYPTO_DERIVS_BINANCE_TOP_N` / `…_LIQ_TOP_N` / `…_TAPE_TOP_N` (12),
  `OPENCLAW_CRYPTO_HL_WHALES_TOP_N` (200), `OPENCLAW_CRYPTO_SOL_MAX_CALLS` (600 with Helius, 60 public).
- **Manual**: `python3 scripts/run_crypto_flow_tick.py --dry-run --force [--providers a,b] [--since-hours N]`
  (no writes, no DB, Santiment skipped). Load `.env` in-process (`python-dotenv`), never `source` it.

## Providers (`src/ingestion/crypto_flow/providers/`, run sequentially in this order)
| name | cadence | source(s) | emits |
|---|---|---|---|
| btc_onchain | tick | blockchain.info unconfirmed + `rawblock` (whole block), mempool.space block list / 25-tx page fallback | BTC `WhaleEvent`s ≥ min USD |
| evm_onchain | tick | Etherscan V2 (ETH every tick + one of ARB/POL/AVAX/BASE/BNB round-robin), exchange-side `tokentx`/`txlist` | EVM `WhaleEvent`s |
| derivs | tick | Hyperliquid `metaAndAssetCtxs` (primary), Binance fapi (top-N; `1000PEPE/BONK/SHIB`), Bybit, OKX | `oi_usd, funding_8h, premium, mark_px, ls_ratio_*, taker_buy_sell, oi_usd_total, oi_venues` |
| liquidations | tick | OKX `liquidation-orders` | `liq_long_usd_1h, liq_short_usd_1h, liq_n_1h` |
| spot_tape | tick | Binance aggTrades → Coinbase → Kraken | `large_prints_*`, `large_print_imbalance_1h` (tape window) |
| deribit_vol | tick | Deribit DVOL + option book summary (BTC/ETH) | `dvol, atm_iv_30d, skew_25d, put_call_oi, options_degraded` |
| xrp_onchain | tick | XRPL `account_tx` on registry exchanges | XRP `WhaleEvent`s |
| sol_onchain | hourly | Helius / public RPC on registry exchanges (SOL, BONK, WIF, TRUMP) | SOL `WhaleEvent`s (budgeted) |
| utxo_alts | hourly | BlockCypher DOGE/LTC (≤ 2 blocks × 10 txs), Blockchair BCH | `WhaleEvent`s + `utxo_alts_coverage` |
| hl_whales | hourly | Hyperliquid leaderboard top-200 → `clearinghouseState` | `whale_perp_net_usd, whale_perp_long/short_usd, whale_perp_n_*` |
| cftc_cot | daily | CFTC TFF Socrata, `BITCOIN` (CME 5-BTC) | `cot_lev_net, cot_am_net, cot_dealer_net, cot_oi, cot_lev_net_chg_1w, cot_report_date` |
| stables_defi | hourly | DefiLlama stablecoins / chains / dexs | `stable_supply_usd (+ _missing), tvl_usd, dex_vol_24h_usd` |
| macro_sent | hourly | CoinGecko global + markets (CoinPaprika fallback), alternative.me | `btc_dominance, total_mcap_usd, fear_greed`; per asset `spot_px, spot_mcap_usd, spot_vol_24h_usd, cg_ret_1h, cg_ret_24h` |
| santiment | daily | SanAPI free tier (30-day lag) | writes `crypto_onchain_daily.parquet`; market `san_*_exchange_balance_30d_lag` |

A provider whose PRIMARY source returns nothing raises `ProviderFetchError` → health row `ok=False`;
`/api/crypto/providers.degraded` lists names failing 3 ticks in a row. URL secrets are redacted in `_http_retry` logs.

## Storage (NEVER-DELETE, `CLAUDE.md` invariant)
- `data/master/crypto_whale_events.parquet` — raw events, key `(source, chain, tx_hash, out_index)`, insert-only.
- `data/master/crypto_flows_1h.parquet` — hourly feature rows per ticker + `__MARKET__`, key `(ticker, ts_utc)`, replace.
- `data/master/crypto_onchain_daily.parquet` — Santiment lagged daily, key `(ticker, date, metric)`, replace.
- Postgres (mig 168): `crypto_flow_snapshot` (15-min, append-only, JSONB `features`), `crypto_flow_provider_health`,
  `crypto_whale_events_live` (7-day CACHE — the only table that prunes).
- State: `data/master/.crypto_flow_state.json` (cursors, `last_run`, `slow_obs` raw carry with 3× cadence expiry),
  `.lock` beside it (flock).

## Features (`src/ingestion/crypto_flow/features.py`)
Per ticker: whale counts/USD (1h/24h), exchange in/out/netflow 24h + 7-day z, `attribution_coverage`, OI + 1h/24h
changes (from the 15-min snapshot history, venue-matched), funding (8h and annualised), L/S + taker ratios,
`oi_price_divergence`, large-print imbalance, liquidations, whale perp net, `price_usd` (= perp mark; `price_spot_usd`
kept), `ret_1h/ret_24h` (from snapshot prices), `delev_1h`, **whale_score 0–100** = netflow (0–40,
`(40/7)·log10(|netflow|)·cov_w`, saturates $10 M) + velocity (0–25) + cross-asset agreement (0–20) + derivatives
confirmation (0–15), `whale_direction` (accumulating / distributing / neutral). Market row: stablecoin supply,
dominance, F&G, aggregate OI, `delev_market`, COT, DVOL/IV/skew/PC (BTC), Santiment balances,
`providers_failed_this_tick`, `shock_lines` (printed as `[crypto-flow] shock …`). Thresholds: `DEFAULT_THRESHOLDS`.

## Consumers
- Dashboard **Crypto** page (`:3000`, nav right of Market): hero, crypto regime structure (+ `adv_*` advisory tiles),
  whale flow board, market tiles, live event feed (SSE `crypto_event`), risk/params panel. API `src/channels/api/routes_crypto.js`.
- Regime monitor: `scripts/run_crypto_market_state.py` adds `adv_btc_netflow_z_7d, adv_btc_funding_8h, adv_delev_market,
  adv_dvol_btc, adv_whale_score_btc, adv_flow_age_min` to `features_json` (advisory, not HMM inputs).
- Health: `scheduled_jobs` `('hours', 1)`; `master_freshness` for the three parquets (`('ts_utc',1)`, `('ts_utc',1)`, `('date',40)`).
- Strategies: deferred to Phase 4 (aux kind) once ≥ 30 days of history exist.

## Known limits / follow-ups
Exchange attribution is thin (validated registry: ETH 30, BTC 6, SOL 7, XRP 3 addresses; cold wallets rejected by the
activity rule); EVM watch = newest 100 rows per address per pass; SOL best-effort; OKX liquidation `limit=100` undercounts
cascades; netflow score component saturates for BTC/ETH (per-asset normalisation owed); Polymarket, Koios/ADA,
`refresh_exchange_addresses.py`, `ijson` streaming, snapshot prune migration all deferred.
