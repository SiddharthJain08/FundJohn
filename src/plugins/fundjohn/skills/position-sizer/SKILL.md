# Skill: fundjohn:position-sizer
**Trigger**: `/position-sizer` or `/size`

## Purpose
Compute Kelly-optimal position sizes for a set of signals, applying confluence bonuses, regime discounts, lifecycle discounts, and the hard NAV cap. This is the authoritative sizing logic for FundJohn — use it instead of hand-computing.

## Kelly Formula

```
f* = (p * R - (1 - p)) / R
```
Where:
- `p` = probability of reaching profit target (use signal `confidence`)
- `R` = reward-to-risk ratio = `(target - entry) / (entry - stop)`

**Half-Kelly**: Always multiply raw Kelly by `0.50` (system constant).

## Confluence Bonus

```
MIN_CONFLUENCE = 2
base_pct       = 0.01   # 1% NAV
bonus_per_conf = 0.005  # +0.5% NAV per confirming strategy beyond the first
confluence_cap = 0.03   # 3% NAV maximum
```

Formula:
```python
pct_nav = min(base_pct + max(0, confluence_count - 1) * bonus_per_conf, confluence_cap)
```

If `confluence_count < MIN_CONFLUENCE`, use `base_pct` only — no bonus.

## Regime Scale

```python
REGIME_POSITION_SCALE = {
    'LOW_VOL':       1.00,
    'TRANSITIONING': 0.70,
    'HIGH_VOL':      0.50,
    'CRISIS':        0.25,
}
```

Multiply `pct_nav` by the scale factor for the current regime.

## Lifecycle Discount

| Strategy state | Multiplier |
|----------------|-----------|
| `live`         | 1.00 |
| `paper`        | 0.50 |
| `monitoring`   | 0.50 |
| `candidate`    | 0.00 (no live sizing) |

## Hard Cap

`MAX_POSITION_PCT = 0.05` — no single signal may exceed 5% NAV regardless of Kelly or confluence.

## Computation Order

```
1. kelly_raw = half_kelly(f*)
2. pct_nav_kelly = kelly_raw * NAV
3. pct_nav_confluence = min(base + bonus * (confluence-1), cap)
4. pct_nav = min(pct_nav_kelly, pct_nav_confluence)  # use the lower bound
5. pct_nav = pct_nav * regime_scale[regime]
6. pct_nav = pct_nav * lifecycle_multiplier[state]
7. pct_nav = min(pct_nav, MAX_POSITION_PCT)
8. shares = floor(pct_nav * NAV / entry_price)
9. notional = shares * entry_price
```

## Output Per Signal

```json
{
  "ticker": "AAPL",
  "kelly_raw": 0.042,
  "kelly_adjusted": 0.021,
  "pct_nav_pre_regime": 0.015,
  "pct_nav_final": 0.0075,
  "shares": 12,
  "notional_usd": 2340.00,
  "size_explanation": "kelly=2.1% → confluence cap=1.5% → HIGH_VOL scale=0.50 → final=0.75%"
}
```

Always include `size_explanation` — it surfaces the binding constraint per signal.

## Portfolio Correlation Check

Before finalizing, check cross-signal correlation using price history:
- If two signals have `correlation(returns) > 0.7` over 60 days, reduce the smaller position by 50%
- Log: `"correlation_overlap: {ticker1} / {ticker2} = {corr:.2f} — {smaller} reduced 50%"`
- This reduction is logged as a `correlation_overlap` veto in `veto_log`
