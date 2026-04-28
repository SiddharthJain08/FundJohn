---
name: fundjohn:paper-to-strategy
description: Translate a research paper into a strategy spec the StrategyCoder can implement.
triggers:
  - paper-hunter promotes a paper
  - /paper-to-strategy
inputs:
  - paper_metadata
  - extracted_methodology
outputs:
  - strategy_spec_md
keywords: [paper, strategy, extraction, methodology]
---
# Skill: fundjohn:paper-to-strategy
**Trigger**: `/paper-to-strategy` or `/p2s`

## Purpose
Extract a concrete, executable strategy specification from an academic alpha paper.
Output the full structured JSON that PaperHunter uses for 4-gate self-rejection and
that ResearchJohn uses to classify READY / BUILDABLE / BLOCKED.

## Output Format
Produce exactly this JSON block (all fields required unless marked optional):

```json
{
  "strategy_id": "S_{short_name}",
  "source_title": "Full paper title",
  "source_authors": ["Last, F.", "Other, G."],
  "source_year": 2024,
  "source_url": "https://doi.org/...",

  "hypothesis_one_liner": "One sentence: what edge does this paper claim and why it persists?",
  "signal_formula_pseudocode": "rank_by(momentum_12m_skip_1m, universe) → LONG top_decile, SHORT bottom_decile",
  "direction_vocab": ["LONG", "SHORT"],
  "regime_applicability": ["LOW_VOL", "NEUTRAL"],

  "minimum_universe_size": 100,
  "reported_metrics": {
    "sharpe": 1.2,
    "max_drawdown": 0.12,
    "backtest_period": "1990-2020",
    "out_of_sample": true
  },

  "data_requirements": {
    "required": [
      {
        "column": "prices",
        "provider": "polygon",
        "fallback": "yahoo",
        "refresh": "daily",
        "already_in_ledger": true
      }
    ],
    "optional": [
      {
        "column": "implied_volatility_skew_25delta",
        "provider": "yahoo",
        "fallback": null,
        "refresh": "daily",
        "already_in_ledger": false
      }
    ]
  },

  "similarity_fingerprint": {
    "regime_set_hash": "<sha256[:16] of sorted regime list joined by space>",
    "direction_hash": "<sha256[:16] of sorted direction_vocab joined by space>",
    "formula_tokens": ["momentum", "rank", "zscore"]
  },

  "self_reported_novelty": "Authors claim first OOS test on EU markets post-2010.",
  "overfitting_flags": [],

  "rejection_reason_if_any": null
}
```

## Field Rules

### strategy_id
Use `S_` + 2-4 word snake_case name derived from the paper's core signal.

### hypothesis_one_liner
One sentence only. State the anomaly and the economic mechanism that makes it persist.

### signal_formula_pseudocode
Be precise. Include: lookback window, ranking rule, directionality, position sizing hint.
Cite the specific paper section: `[§3.2]`.

### direction_vocab
Only values from: `LONG`, `SHORT`, `BUY_VOL`, `SELL_VOL`, `FLAT`, `HOLD`.

### regime_applicability
Only values from: `LOW_VOL`, `NEUTRAL`, `HIGH_VOL`, `TRANSITIONING`, `RISK_OFF`, `TREND`.
Empty array = all regimes.

### data_requirements.required[].column
Only columns from OpenClaw schema:
- `prices` — OHLCV + vwap, daily
- `financials` — ratios, margins, earnings, from FMP
- `options_eod` — full chain: IV, greeks, OI, bid/ask
- `insider` — Form 4 transactions
- `macro` — GDP, CPI, rates, VIX
- `earnings` — EPS actuals/estimates, revenue surprises
- Any other requirement: column = `"EXTERNAL: {description}"`, already_in_ledger = false

### already_in_ledger
Query `SELECT 1 FROM data_ledger WHERE column_name = $1` for each column.
Set `true` if row exists, `false` otherwise.
If data_ledger is unavailable, set to `false` for any non-standard column.

### similarity_fingerprint
Compute sha256 hashes as follows:
- `regime_set_hash`: sha256(sorted(regime_applicability).join(' '))[:16]
- `direction_hash`: sha256(sorted(direction_vocab).join(' '))[:16]
- `formula_tokens`: list of signal-relevant lowercase tokens from signal_formula_pseudocode
  (e.g. "momentum", "zscore", "iv_spread", "gamma", "rank", "reversion")

### overfitting_flags
List any of: `"in_sample_only"`, `"short_backtest"`, `"no_transaction_costs"`,
`"data_snooping_bias"`, `"survivorship_bias"`.

### rejection_reason_if_any
Set to null if passing. Set to one of:
- `"non_deterministic"` — thesis relies on qualitative judgment
- `"overfitting_risk"` — OOS Sharpe > 2.5 on monthly US equity
- `"duplicate_fingerprint"` — Jaccard(formula_tokens, existing) > 0.6 AND regime_set matches
- `"capability_gap"` — required column not in ledger AND no provider in servers.json covered_columns

## What NOT to Include
- Do not include implementation code
- Do not guess data availability — query data_ledger for each column
- Do not recommend lifecycle state — that is BotJohn's decision
- Do not fabricate reported_metrics — use null for missing fields
