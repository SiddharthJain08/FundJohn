# FundJohn / OpenClaw v2.0

Autonomous quantitative hedge fund system built on Claude Code.

## Architecture

```
Operator
   │  (Discord)
BotJohn  [claude-opus-4-6]
   │  Orchestrates
    ├── DataPipeline [hardcoded]           — strategy execution, data collection, memo dispatch
   ├── ResearchJohn  [claude-sonnet-4-6]  — strategy memo synthesis, research report
   └── TradeJohn     [claude-sonnet-4-6]  — signal generation, position sizing
```

## Cycle Flow

```
DataPipeline (hardcoded via runner.js)
  → Collects market data (prices, fundamentals, options, macro, insider)
  → Deploys live/paper strategies
  → Publishes strategy memos to disk

ResearchJohn
  → Reads strategy memos
  → Produces consolidated research report (regime, performance, top signals)

TradeJohn
  → Reads research report + portfolio state
  → Generates trade signals with sizing

BotJohn
  → Reviews signals
  → Approves/vetoes based on EV and risk limits
  → Updates strategy lifecycle states as needed
  → Posts digest to Discord
```

## Strategy Lifecycle

```
candidate → paper → live → monitoring → deprecated → archived
```

Managed by `src/strategies/lifecycle.py` and `src/strategies/manifest.json`.
Promotion gate: paper→live requires Sharpe ≥ 0.5 AND max_drawdown ≤ 20%.

## Active Strategies

| ID | State | Description |
|---|---|---|
| S5_max_pain | live | Max-pain gravity — options-derived price attractor |
| S9_dual_momentum | live | Dual-momentum cross-asset rotation |
| S10_quality_value | live | Quality-value factor composite score |
| S12_insider | live | Insider cluster-buy signal (SEC Form 4) |
| S15_iv_rv_arb | live | IV/RV spread arbitrage |
| S_custom_jt_momentum_12mo | live | Jegadeesh-Titman 12-month momentum |
| S23_regime_momentum | paper | Regime-conditioned momentum |
| S24_52wk_high_proximity | paper | 52-week high proximity breakout |
| S25_dual_momentum_v2 | paper | Dual-momentum v2 — S9 successor candidate |
| S_custom_momentum_trend_v1 | deprecated | Orphaned hybrid — pending archive review |

## Stack
- **Language**: Python (strategies, lifecycle), Node.js (bot, orchestrator)
- **Data**: Parquet master datasets (prices, financials, options_eod, macro, insider)
- **Interface**: Discord via BotJohn
- **Infra**: Hostinger VPS (srv1559223), GitHub for version control
- **MCP**: Yahoo Finance, Polygon, FMP, SEC EDGAR, Alpha Vantage
