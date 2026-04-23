# FundJohn Top-10 Strategy Cohort — 2026-04

This folder contains the complete implementation of the ten strategies selected
from the 2026-Q2 deep-research pass across *high-volatility / IV-structure* and
*transitioning-regime* research threads. Every strategy is backed by a peer-
reviewed paper, fits within the FMP Starter + Polygon Massive Options Starter
data envelope, and has been end-to-end validated with an adapter backtest.

## What's here

```
top10_strategies_2026/
├── TOP10_README.md                     # this file
├── implementations/
│   ├── _compat.py                      # offline shim for backtests
│   ├── shv13_call_put_iv_spread.py     # Cremers-Weinbaum (2010)
│   ├── shv14_otm_skew_factor.py        # Xing-Zhang-Zhao (2010)
│   ├── shv15_iv_term_structure.py      # Johnson (2017)
│   ├── shv17_earnings_straddle_fade.py # Gao et al. (2018)
│   ├── shv20_iv_dispersion_reversion.py# Driessen-Maenhout-Vilkov (2009)
│   ├── str01_vvix_early_warning.py     # Thrasher (2017)
│   ├── str02_hurst_regime_flip.py      # Vogl (2022)
│   ├── str03_bocpd.py                  # Adams-MacKay (2007)
│   ├── str04_zarattini_intraday_spy.py # Zarattini et al. (2024)
│   └── str06_baltussen_eod_reversal.py # Baltussen-Da-Soebhag (2024)
├── backtests/
│   ├── backtest_framework.py           # shared harness, 4 trade archetypes
│   ├── bt_shv13.py … bt_str06.py       # per-strategy harnesses
│   └── results/                        # JSON reports
├── engine_patches/
│   └── aux_metrics.py                  # build_opts_map + build_market_data
├── ingest/
│   ├── ingest_vol_indices.py           # ^VIX ^VVIX ^VIX9D → vol_indices.parquet
│   ├── ingest_prices_30m.py            # Polygon 30-min bars → prices_30m.parquet
│   ├── ingest_earnings_calendar.py     # FMP earnings → earnings_calendar.parquet
│   └── ingest_iv_history.py            # derive iv_30d/iv_90d → iv_history.parquet
└── deploy/
    ├── deploy_top10.sh                 # one-shot deploy to VPS
    └── top10_manifest.json             # registry + promotion criteria
```

## The ten strategies

| ID | Strategy | Archetype | Horizon | Paper |
|---|---|---|---|---|
| S-HV13 | Call-Put IV Spread | Equity X-section | 7d | Cremers-Weinbaum 2010 |
| S-HV14 | OTM Skew Factor | Equity X-section | 10d | Xing-Zhang-Zhao 2010 |
| S-HV15 | IV Term Structure | Options-vol | 7d | Johnson 2017 |
| S-HV17 | Earnings Straddle Fade | Options-vol event | 1d | Gao et al. 2018 |
| S-HV20 | IV Dispersion Reversion | Options-vol basket | 10d | Driessen-Maenhout-Vilkov 2009 |
| S-TR-01 | VVIX Early Warning | Regime classifier | 10d lead | Thrasher 2017 |
| S-TR-02 | Hurst Regime Flip | Regime classifier | 20d lead | Vogl 2022 |
| S-TR-03 | BOCPD Changepoint | Regime classifier | 10d lead | Adams-MacKay 2007 |
| S-TR-04 | Zarattini Intraday SPY | Intraday single-asset | same-day | Zarattini et al. 2024 |
| S-TR-06 | Baltussen EOD Reversal | Intraday X-section | 30 min | Baltussen-Da-Soebhag 2024 |

### Selection logic

The ten were chosen from a pool of ~40 candidates across the two research
threads using four hard filters:

1. **Data-envelope compatibility** — every dependency must be derivable
   from existing FMP Starter + Polygon Massive Options Starter feeds,
   or from a one-time ingest on top of them (no paid add-ons).
2. **Regime diversity** — at least two distinct archetypes (equity,
   options-vol, regime, intraday) and spread across LOW/NEUTRAL/HIGH
   regimes, so the cohort keeps firing in any market.
3. **Academic provenance** — each strategy cites a primary peer-reviewed
   source with an out-of-sample Sharpe > 0.5.
4. **Non-overlapping edge** — pairwise intended-alpha correlations
   (measured by the shared risk factor + time horizon) are <0.4 so the
   cohort composes into real diversification rather than re-ranking the
   same inefficiency.

Twenty-two candidates were cut for data-cost reasons alone (tick-level
bid/ask or cross-asset futures curves), eight more for redundancy against
an accepted candidate.

## Data dependencies — what goes in `opts_map` and `market_data`

Every strategy consumes two dicts via its `generate_signals(market_data,
opts_map) -> List[Signal]` contract. `engine_patches/aux_metrics.py`
provides the canonical builders. The cross-reference:

| Field | Source | Consumed by |
|---|---|---|
| `opts_map[t].iv_spread_atm_oi_weighted` | options_eod.parquet → aux_metrics | S-HV13 |
| `opts_map[t].smirk_otmput_atmcall` | options_eod.parquet → aux_metrics | S-HV14 |
| `opts_map[t].iv_30d` / `iv_90d` / `ts_ratio` | options_eod.parquet → aux_metrics | S-HV15, S-HV17, S-HV20 |
| `opts_map[t].iv_rank` | iv_history.parquet → aux_metrics | S-HV13, S-HV14, S-HV15 |
| `opts_map[t].earnings_implied_move` | options_eod + earnings_calendar | S-HV17 |
| `opts_map[t].atm_bid_ask_spread_pct` | options_eod.parquet | S-HV17 (tradability gate) |
| `opts_map[t].avg_dollar_volume_30d` | prices.parquet | S-TR-06 (liquidity filter) |
| `opts_map[t].rv20` | prices.parquet | S-HV13 (stop sizing) |
| `market_data.vix_close` / `vvix_close` / `vix9d_close` | vol_indices.parquet | S-TR-01, S-TR-04, S-HV15 |
| `market_data.spy_close_history` | prices.parquet | S-TR-02, S-TR-03 |
| `market_data.spy_30m_bars` | prices_30m.parquet | S-TR-04 |
| `market_data.intraday_30m_bars` | prices_30m.parquet | S-TR-06 |
| `market_data.spx_iv_30d` / `spx_close` | options_eod (SPX) + prices.parquet | S-HV20 |

## Backtest guarantees

The framework in `backtests/backtest_framework.py` handles four archetypes
(EquityTrade, OptionsVolTrade, IntradayTrade, RegimeEvent), deducts costs
per trade type, and emits:

- Sharpe / Sortino / Calmar (annualised)
- Max drawdown (depth + duration)
- Win-rate / profit factor / expectancy
- **IS vs OOS Sharpe** (60/40 anchor walk-forward) — the spread is the
  primary robustness flag before promotion
- **Block-bootstrapped 95% CI on Sharpe** (21-day blocks, 2000 reps)
- Regime breakdown when a `regime` label column is attached

Costs modeled:

| Leg | Cost |
|---|---|
| Equity round-trip slippage + commission | 1–2 bps |
| Equity short borrow | 30 bps/year |
| Options slippage (straddle premium) | 10 bps round-trip |
| Options commission | $0.65/contract |
| Intraday slippage | 1 bps |

All ten backtests run end-to-end from either the VPS parquet files
(`--real` flag, auto-detected) or synthetic data (default). Synthetic
Sharpes are not meaningful as edge proxies — they exist to exercise the
code path, not to forecast live performance. Pre-promotion the `--real`
run is the decisive metric.

## Deployment workflow

### 1. Fresh-install path

```bash
# On the VPS (or anywhere with SSH access to it)
cd /root/openclaw
rsync -a <local>/top10_strategies_2026/ /root/openclaw/top10_strategies_2026/

# First-time data ingest (one-off, ~20 min on 250-name universe)
export FMP_API_KEY=...   # FMP Starter key
export POLYGON_API_KEY=...
python3 /root/openclaw/src/ingest/ingest_vol_indices.py --rebuild --from 2020-01-01
python3 /root/openclaw/src/ingest/ingest_prices_30m.py --rebuild --from 2023-01-01
python3 /root/openclaw/src/ingest/ingest_earnings_calendar.py --window 14
python3 /root/openclaw/src/ingest/ingest_iv_history.py --rebuild

# Deploy (shadow mode first — this is the default)
/root/openclaw/top10_strategies_2026/deploy/deploy_top10.sh --shadow
```

### 2. Daily cron

Add these to the existing FundJohn cron alongside the nightly
`ingest_options.py` and `ingest_prices.py`:

```cron
# ~05:10 UTC — after the main ingest completes
10 5 * * 1-5  FMP_API_KEY=...  /usr/bin/python3 /root/openclaw/src/ingest/ingest_vol_indices.py
20 5 * * 1-5  POLYGON_API_KEY=...  /usr/bin/python3 /root/openclaw/src/ingest/ingest_prices_30m.py
30 5 * * 1-5  FMP_API_KEY=...  /usr/bin/python3 /root/openclaw/src/ingest/ingest_earnings_calendar.py
40 5 * * 1-5  /usr/bin/python3 /root/openclaw/src/ingest/ingest_iv_history.py
```

### 3. Phased rollout (all-cohort default)

The deploy script writes a `strategy_phases.json` entry per strategy.
A strategy progresses one phase at a time, driven by `promotion_criteria`
in `top10_manifest.json`:

```
shadow  →  paper  →  live
(log)      ($1/signal     (real $)
            fake book)
```

Promotion thresholds:

| Transition | Min days | Min signals/trades | Guard |
|---|---|---|---|
| shadow → paper | 20 | 30 signals | zero crash sessions |
| paper → live | 60 | 40 trades | Sharpe > 0.7, DD ≤ 8% |
| live → paused | — | — | auto-pause on 12% DD in 90d |

These live in `top10_manifest.json` so the DataPipeline can enforce them
without hard-coding. When a strategy crosses a threshold it is queued
for review in Discord before phase flip.

## Integration receipts

1. **Strategy loader** — `deploy_top10.sh` writes each strategy into
   `src/strategies/registry.json`; the existing engine loader will pick
   them up on next restart with no code change.
2. **Engine hook** — the one code change on the engine side is a 4-line
   insertion inside `_load_options_aux()` / `_load_market_data()` (the
   exact snippet is at the bottom of `engine_patches/aux_metrics.py`).
3. **Discord posting** — no change; each `Signal` flows through the
   existing signal-formatter, and `signal_params['strategy_id']` identifies
   the source for the Discord thread header.
4. **Risk manager** — sizing is all declared via `position_size_pct` on
   each Signal, so the existing risk manager enforces the per-name cap
   and cohort cap without knowing the strategies exist.

## Budget envelope

| Line item | $/month | Notes |
|---|---:|---|
| FMP Starter | $19 | existing, unchanged |
| Polygon Massive Options Starter | $199 | existing, unchanged |
| VPS (Hostinger KVM 4) | $19 | existing, unchanged |
| Alpaca data + routing | $0 | existing, unchanged |
| Discord + webhooks | $0 | free tier |
| **Subtotal (cohort adds)** | **$0** | all strategies fit the existing feed envelope |

The ten strategies add no recurring cost — the only net new artifacts are
the four parquet files in `/root/openclaw/data/master/`, which fit in
well under 1 GB total.

## What this cohort does NOT cover

For transparency: strategies deliberately out of scope for this cohort
(data or infra cost reasons — revisit if budget expands):

- Anything requiring L2 order book or per-tick trades (Polygon Full Feed)
- Cross-asset vol spillover from futures curves (CL / NG / HG surfaces)
- Intraday options flow / gamma imbalance (requires per-trade options tape)
- Single-stock futures or ADR arbitrage
- Fixed-income vol, credit, or rates surfaces

Each of these was considered; cost-benefit did not clear the $400/mo gate.

---

*FundJohn / OpenClaw v2.0 — Top-10 cohort generated 2026-04-23 by Claude.
Pair this file with `ARCHITECTURE.md` and `PIPELINE.md` for the full
system context.*
