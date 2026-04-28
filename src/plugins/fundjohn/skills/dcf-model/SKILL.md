---
name: fundjohn:dcf-model
description: Run a discounted-cash-flow valuation and emit a structured output schema.
triggers:
  - /dcf
  - fundamental valuation request
inputs:
  - ticker
  - wacc
  - terminal_growth
  - revenue_projections
outputs:
  - fair_value_per_share
  - sensitivity_table
keywords: [dcf, valuation, wacc, fundamental]
---
# Skill: fundjohn:dcf-model
**Trigger**: `/dcf` or `/valuation`

## Purpose
Compute a single-stage or two-stage DCF for a ticker and emit per-share fair value with a 5×5 sensitivity table over WACC and terminal growth. Use this rather than free-form valuation prose — it forces explicit assumption disclosure so MastermindJohn can audit.

## Required Inputs
- `ticker`
- 4–5y of historical FCF (from FMP `cashflow_statement` or computed `CFO − capex`)
- Net debt and shares outstanding (FMP `balance_sheet` + `enterprise_value`)
- Beta (FMP `profile`) → cost of equity via CAPM (rf=current 10y, MRP=5%)

## Assumption Block (must emit verbatim)
```yaml
revenue_growth: [y1..y5]   # explicit per year, not single rate
fcf_margin:    [y1..y5]
terminal_g:    0.025       # default 2.5%; cap at 10y treasury
wacc:          x.xx        # CAPM-derived, document each input
shares_out:    n
net_debt:      $n
```

## Output Schema
```json
{
  "ticker": "AAPL",
  "fair_value_per_share": 192.40,
  "current_price": 175.00,
  "implied_upside_pct": 9.94,
  "sensitivity": {
    "wacc_grid":   [0.07, 0.08, 0.09, 0.10, 0.11],
    "term_g_grid": [0.015, 0.020, 0.025, 0.030, 0.035],
    "fair_value_matrix": [[...], [...], [...], [...], [...]]
  },
  "binding_assumption": "wacc=9% / term_g=2.5% — fair value most sensitive to fcf_margin year 3"
}
```

`binding_assumption` is mandatory — surfaces the variable a +/-1σ move would invalidate the thesis on. If you can't state it, the model is over-fit.

## Quality Gates (auto-reject if any fail)
- Implied 5y revenue growth must not exceed 2× the trailing 5y CAGR
- Terminal `g` ≤ current 10y treasury yield
- WACC within [6%, 14%] for normal corporates; flag if outside
- Output `binding_assumption` non-empty
