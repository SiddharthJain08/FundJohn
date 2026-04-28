---
name: fundjohn:veto-explainer
description: Explain why TradeJohn vetoed a signal in plain language using veto reason codes.
triggers:
  - /veto-explainer
  - investigating a vetoed signal
inputs:
  - signal_id
  - veto_reason_code
outputs:
  - plain_language_explanation
keywords: [veto, explanation, tradejohn, reason-codes]
---
# Skill: fundjohn:veto-explainer
**Trigger**: `/veto-explainer` or `/why-veto`

## Purpose
Surface actionable patterns from the historical `veto_log`. Convert raw cause-code counts into a ranked, readable explanation that lets ResearchJohn or BotJohn act — not re-read memos.

## Input Format

`VETO_HISTOGRAM_JSON` (injected into context at session start):
```json
{
  "S9_dual_momentum": {
    "negative_ev": 4,
    "missing_field": 0,
    "correlation_overlap": 1
  },
  "S15_iv_rv_arb": {
    "negative_ev": 2,
    "missing_field": 3
  }
}
```

If `VETO_HISTOGRAM_JSON` is absent or empty, output: `"No veto history in the last 30 days."`

## Veto Reason Codes

| Code | Meaning | Root cause hint |
|------|---------|----------------|
| `negative_ev` | TradeJohn vetoed because expected value ≤ 0 | Signal quality, regime mismatch, overfitting |
| `missing_field` | post_memos.py SO-6 lint failure | Data pipeline gap, engine output format drift |
| `correlation_overlap` | Two correlated signals — smaller reduced 50% | Portfolio concentration risk |
| `low_sharpe` | Backtest sharpe below promotion gate (0.5) | Strategy not ready for paper trading |
| `dd_breach` | Max drawdown exceeded 20% in live strategy | SO-5 escalation — strategy moved to monitoring |
| `regime_mismatch` | Signal regime_conditions filter dropped all output | Current regime not in strategy's allowed set |

## Pattern Detection Rules

Apply in order:

1. **Consecutive negative_ev ≥ 3** for any strategy → Warning: `"⚠️ {strategy_id}: {N} consecutive negative_ev vetoes — review signal thresholds or regime alignment"`

2. **missing_field ≥ 2 in last 5 cycles** for any strategy → Warning: `"⚠️ {strategy_id}: {N} memo lint failures — data pipeline review required"`

3. **correlation_overlap ≥ 2 across any pair** → Warning: `"⚠️ High concentration: {N} correlation_overlap vetoes — review universe diversification"`

## Output Format

Produce a ranked table (sorted by total veto count, descending):

```
**Veto Summary — last 30 days**

| Strategy | negative_ev | missing_field | correlation_overlap | Total | Action |
|----------|------------|---------------|---------------------|-------|--------|
| S9_dual_momentum | 4 | 0 | 1 | 5 | Review signal thresholds |
| S15_iv_rv_arb | 2 | 3 | 0 | 5 | Data pipeline review |

**Patterns detected:**
⚠️ S9_dual_momentum: 4 consecutive negative_ev vetoes — review signal thresholds
⚠️ S15_iv_rv_arb: 3 memo lint failures — data pipeline review required
```

## Hard Rules
- Surface data only — do not recommend lifecycle transitions (candidate/paper/live/monitoring)
- BotJohn has sole authority over lifecycle decisions
- Do not estimate expected fix time — that is the operator's call
