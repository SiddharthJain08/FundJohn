# Skill: fundjohn:portfolio-correlation
**Trigger**: `/portfolio-correlation` or `/correl`

## Purpose
Read the pre-computed pairwise 60-day return correlation matrix from the
handoff, identify clusters that would trip the concentration gate, and
propose rebalancing trims. MastermindJohn consumes this in strategy-stack
mode; BotJohn may invoke it ad-hoc for `@FundJohn correl`.

No live compute — the matrix is computed deterministically in
`trade_handoff_builder.py` so this skill stays tool-less.

## Input (from handoff)

`handoff.correlation_matrix`:

```json
{
  "as_of":         "2026-04-23",
  "window_days":   60,
  "tickers":       ["AAPL", "MSFT", "NVDA", "SPY", "XLK"],
  "strategy_map":  {"AAPL": "S9_dual_momentum", "MSFT": "S10_quality_value", ...},
  "matrix":        [[1.00, 0.82, 0.74, 0.61, 0.79],
                    [0.82, 1.00, 0.68, 0.58, 0.71],
                    ...]
}
```

`handoff.portfolio.positions[]` provides current position sizes (NAV%).

## Thresholds

| Correlation | Severity | Action |
|---|---|---|
| ≥ 0.90 | critical | Either flatten one leg or halve both |
| 0.70 – 0.89 | warn | Reduce smaller position by 50% (tie-break: smaller `kelly_net`) |
| 0.50 – 0.69 | note | Mention in memo; no action |
| < 0.50 | ignore | Diversified; no output |

## Output Format

```markdown
### Correlation review ({window_days}d, as of {as_of})

**Critical clusters (ρ ≥ 0.90)**:
- AAPL × MSFT (ρ = 0.93) — both LONG, combined 4.6% NAV → recommend flatten MSFT (smaller kelly_net)

**Warn clusters (0.70 ≤ ρ < 0.90)**:
- NVDA × AAPL (ρ = 0.81) — both LONG via different strategies → halve NVDA

**Notes (0.50 ≤ ρ < 0.70)**:
- SPY × XLK (ρ = 0.68) — SPY held via S23_regime_momentum; XLK via S24_52wk_high_proximity

**Concentration score**: {weighted_avg_pairwise_corr:.2f} — target < 0.40

**Recommended deltas**:
| Ticker | Current | New | Change |
|---|---|---|---|
| MSFT | 2.1% | 0.0% | flatten |
| NVDA | 1.8% | 0.9% | halve |
```

## Concentration Score

Weighted average pairwise correlation across current positions:

```
score = Σ_{i<j} (w_i * w_j * |ρ_ij|) / Σ_{i<j} (w_i * w_j)
```

Target < 0.40. Between 0.40 – 0.60 = acceptable. > 0.60 = escalate.

## Hard Rules

- Work only from the injected correlation_matrix + portfolio.positions.
  Never invoke a tool.
- Never propose ADDING a position — only trims / flattens.
- Tie-break between two correlated positions: smaller `kelly_net` goes
  first. If Kelly is unavailable, smaller NAV% goes first.
- If matrix is missing or has < 3 tickers, respond:
  `"correlation_matrix unavailable — < 3 positions, no action"` and stop.
