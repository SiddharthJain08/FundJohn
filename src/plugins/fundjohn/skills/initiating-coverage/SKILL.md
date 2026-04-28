---
name: fundjohn:initiating-coverage
description: Produce an initiation-of-coverage memo conforming to the FundJohn template.
triggers:
  - /initiate <ticker>
  - new-name research kickoff
inputs:
  - ticker
  - thesis_summary
  - catalysts
outputs:
  - initiating_memo_md
keywords: [initiation, coverage, research-memo]
---
# Skill: fundjohn:initiating-coverage
**Trigger**: `/initiate` or `/coverage`

## Purpose
First memo on a new ticker. Compresses business model, thesis, valuation, and sizing recommendation into a single auditable document. Output goes to `workspaces/default/results/initiating-{ticker}.md` with frontmatter so MastermindJohn can vector-index it later.

## Mandatory Frontmatter
```yaml
---
type: initiating
ticker: AAPL
sector: Technology
thesis_class: quality_compounder | turnaround | event_driven | deep_value | growth
conviction: 0.72        # 0.0..1.0
horizon_days: 90
catalyst_date: 2026-05-15
risk_class: low | mid | high
tags: [#initiating, #ticker/AAPL, #sector/tech]
---
```

## Required Sections (in order, no others)
1. **Thesis (3 sentences max)** — what you believe, why now, what would prove you wrong
2. **Three drivers** — each ≤2 sentences, each falsifiable with a specific KPI
3. **Three risks** — each ≤2 sentences, each with a watch-trigger (e.g. "if Q3 GM < 41%, reduce")
4. **Valuation snapshot** — invoke `fundjohn:dcf-model` and `fundjohn:comps-analysis`; paste only fair value, upside %, binding assumption, peer-relative dislocation. Full models go in `work/{ticker}/`
5. **Catalyst** — one specific dated event that resolves directional uncertainty
6. **Sizing recommendation** — invoke `fundjohn:position-sizer`; paste `pct_nav_final` + `size_explanation`

## Word Cap
≤ 800 words total. If you can't say it in 800 words, you don't understand it yet.

## Quality Gates (auto-reject)
- Frontmatter complete and valid YAML
- Conviction is calibrated number, not "high/medium/low"
- Each risk has a watch-trigger
- Catalyst has a specific date (or "next earnings" with date)
- Valuation section has a binding assumption
