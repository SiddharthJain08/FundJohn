---
type: strategy
name: <<name>>
state: candidate | paper | live | monitoring | deprecated
asset_class: equities | options | futures | fx | rates | crypto | multi
factor: momentum | value | quality | volatility | reversal | sentiment | macro | event | other
horizon_days: <<int>>
expected_sharpe: 0.00
turnover_annual: 0.00
capacity_usd: <<int>>
inception_date: YYYY-MM-DD
last_review: <<today>>
parent_papers: [[paper-name-1]]
tags: [#strategy, #factor/<<factor>>, #state/<<state>>]
---

## Hypothesis (≤3 sentences)

## Signal definition (pseudocode)
```
```

## Universe + frequency

## Entry / exit rules

## Sizing rule (link to fundjohn:position-sizer)

## Risk gates
- max_position_pct:
- max_correlation_with_book:
- regime_filter:

## Backtest summary
- Period:
- Sharpe:
- Max DD:
- Hit rate:
- Capacity:

## Live performance vs paper (if state >= paper)
| metric | paper | live | delta |
| Sharpe |       |      |       |
| DD     |       |      |       |
| Hit %  |       |      |       |

## Recent reviews
- [[YYYY-MM-DD-strategy-review-{{name}}]]
