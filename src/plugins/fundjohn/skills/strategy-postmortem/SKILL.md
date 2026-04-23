# Skill: fundjohn:strategy-postmortem
**Trigger**: `/strategy-postmortem` or `/postmortem`

## Purpose
Standardize the per-strategy note inside MastermindJohn's Friday
strategy-stack memo. Every live + monitoring strategy gets one section
using this schema so the weekly memo is diffable week over week.

## Required Inputs (already in MastermindJohn's injected context)

| Key | Source |
|---|---|
| `stack_row`       | row from `strategy_stats` view (migration 042) |
| `alpaca_rows`     | `alpaca_submissions` last 30d, grouped by status |
| `veto_rows`       | `veto_log` last 30d for this strategy |
| `recommendations` | last `position_recommendations` row for this strategy |
| `daily_summary`   | `daily_signal_summary` last 30d |
| `regime_attribution` | per-regime realized Sharpe (pre-computed by strategy_stack.js) |

## Output Format (strict)

```markdown
### {strategy_id} — {class_name}

**State**: {state} (since {state_since})
**Period**: trailing 30 trading days, {n_signals} signals → {n_orders} orders → {n_fills} fills → {n_closed} closed

**Realized vs predicted**
| Metric | Predicted | Realized | Δ |
|---|---|---|---|
| Sharpe   | {pred_sharpe}   | {real_sharpe}   | {delta} |
| Hit rate | {pred_hit_rate} | {real_hit_rate} | {delta} |
| Avg EV   | {pred_avg_ev}   | {real_avg_pnl}  | {delta} |

**Regime attribution**
- LOW_VOL:       {n} trades, Sharpe {sharpe}
- TRANSITIONING: {n} trades, Sharpe {sharpe}
- HIGH_VOL:      {n} trades, Sharpe {sharpe}
- CRISIS:        {n} trades, Sharpe {sharpe}

**Veto mix** ({n_vetoes} total / {pct}% veto rate):
- {top_reason_1}: {n} ({pct}%)
- {top_reason_2}: {n} ({pct}%)

**Sizing signal**: {maintain | reduce_half | pause | promote_request | demote_request}
**Why**: {one sentence citing the numbers above}

**What I want to see next week**: {one concrete observable, e.g. "hit rate above 55% in LOW_VOL"}
```

## Decision Rules for `sizing signal`

Apply in order, first match wins:

1. `real_sharpe < 0` AND `n_closed ≥ 10` → `pause` (escalate to BotJohn).
2. `max_drawdown > 0.20` → `demote_request` (invoke SO-5).
3. `real_sharpe < pred_sharpe * 0.5` AND `n_closed ≥ 10` → `reduce_half`.
4. `real_sharpe ≥ 0.75` AND `state == "paper"` AND `max_drawdown ≤ 0.20` → `promote_request`.
5. Otherwise → `maintain`.

## Hard Rules

- One section per strategy. Do not merge. Do not skip.
- Numbers must come from `stack_row` / `daily_summary` / `regime_attribution`
  — never estimate, never round to more precision than the source.
- Never recommend a lifecycle transition directly — that is operator +
  BotJohn's call. Use `promote_request` / `demote_request` as the signal.
- If `n_closed < 5`, use `sizing signal = maintain` and note "insufficient
  data for decision" in **Why**.
