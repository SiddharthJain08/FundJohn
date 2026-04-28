---
name: fundjohn:comps-analysis
description: Build a public-comps multiples table for relative-value analysis.
triggers:
  - /comps
  - comparable companies analysis request
inputs:
  - ticker
  - peer_set
  - multiples_to_compute
outputs:
  - comps_table
  - quality_gates
keywords: [comps, multiples, peer-analysis, EV/EBITDA]
---
# Skill: fundjohn:comps-analysis
**Trigger**: `/comps` or `/peers`

## Purpose
Quickly bench a target against 5–8 named peers on the multiples that matter for its asset class (equities → P/E, EV/EBITDA, P/S, EV/S, FCF yield). Output is one table + one paragraph naming the binding peer-relative dislocation.

## Peer Selection Rules
1. Same GICS sub-industry (FMP `profile.industry`)
2. Within 0.3× to 3× target market cap
3. Profitable in the trailing 12 months OR sole exception explicitly justified
4. Minimum 5, maximum 8 peers — fewer = noise; more = mush

## Multiples (compute for target + each peer)
- P/E (forward, Bloomberg-consensus or FMP `analyst_estimates`)
- EV/EBITDA (TTM)
- P/S, EV/S (TTM)
- FCF yield = TTM FCF / market cap
- Revenue growth (3y CAGR) — for context, not a multiple
- Gross margin — for quality control

## Output
```
| Ticker | P/E fwd | EV/EBITDA | P/S | FCF Yld | Rev CAGR 3y | GM |
| TGT    |  18.4×  |   12.1×   | 3.2 |  4.1%   |    8.2%     |42% |
| Peer1  |  21.0×  |   13.5×   | 4.0 |  3.3%   |    9.1%     |44% |
| ...    |   ...   |    ...    | ... |   ...   |    ...      |... |
| MEDIAN |  20.5×  |   13.0×   | 3.7 |  3.5%   |    8.8%     |43% |
```

Then one paragraph:
> "Target trades at 12.1× EV/EBITDA vs peer median 13.0× — 7% discount despite parity gross margin and rev-CAGR. Binding peer-relative dislocation: P/S (3.2 vs 3.7) suggests revenue is being penalized by [name the reason] relative to peers."

## Quality Gates
- All multiples positive (negative = data error or special situation; flag explicitly)
- Outliers >2σ from peer mean must be excluded with reason
- Paragraph names ONE binding multiple, not three
