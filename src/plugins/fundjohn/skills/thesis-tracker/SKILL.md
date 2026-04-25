# Skill: fundjohn:thesis-tracker
**Trigger**: `/thesis {ticker}` or `/track {ticker}`

## Purpose
Re-evaluate an open thesis against the most recent data. Output appends to `workspaces/default/results/thesis-{ticker}.md` (one section per check-in) so the timeline is traversable and Obsidian's graph view shows thesis evolution.

## Inputs
- The original initiating-coverage memo at `results/initiating-{ticker}.md` (read frontmatter for thesis_class, drivers, risks, catalyst_date)
- Latest fundamentals (FMP) and price (Polygon)
- Recent news (Tavily, last 7 days only)

## Required Output Block (append, do not rewrite)
```yaml
---
type: thesis_checkin
ticker: AAPL
date: 2026-04-25
prior_conviction: 0.72
new_conviction: 0.65
conviction_delta: -0.07
trigger: q2_earnings_miss
tags: [#thesis-checkin, #ticker/AAPL, #conviction/down]
---
```

Then four short sections:

1. **Driver status** — for each of the 3 drivers in the original memo: `confirmed | inconclusive | invalidated` + 1 line of evidence
2. **Risk triggers fired** — list any watch-triggers from the original memo that have hit; each ≤1 line
3. **Conviction delta justification** — one sentence per +/- 0.05 of change
4. **Action** — one of: `hold | trim 25% | trim 50% | exit | add 25%`. No prose; the bracket order JSON if an action is taken

## Decision Rules
- Conviction <0.40 + ≥1 risk trigger fired = `exit`
- Conviction <0.55 + thesis_class=`event_driven` and catalyst passed = `exit`
- Conviction increase >+0.10 + size <50% target = `add 25%`
- All other paths = `hold` or `trim 25%` based on regime scale

## Quality Gates
- Frontmatter `[[link]]` to original initiating note
- Conviction delta arithmetic correct
- Action falls within decision rules table
