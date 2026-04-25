# Skill: fundjohn:morning-note
**Trigger**: `/morning` or `/morning-note`

## Purpose
A 7-minute pre-market briefing. Replaces ad-hoc Discord pings with a single structured artifact. Token budget: ≤2,500 output tokens.

## Frontmatter
```yaml
---
type: morning_note
date: 2026-04-25
regime: LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
overnight_es_pct: -0.42
overnight_vix: 14.8
tags: [#morning-note, #regime/{regime}]
---
```

## Required Sections (max one paragraph each)
1. **Regime read** — current label + what changed since yesterday's close (1 line). Pull from `regime_context.md`; if stale >24h, recompute via Polygon vol surface.
2. **Overnight tape** — ES, NQ, RTY, VIX, DXY, US10Y. One line: `ES -0.4 / NQ -0.6 / RTY +0.1 / VIX 14.8 / DXY 104.2 / US10Y 4.21%`
3. **Today's catalysts** — earnings before/after-the-bell, scheduled FOMC/BLS prints, expected speakers. Pull from FMP `earnings_calendar` + AlphaVantage `economic_calendar`. Cap at 8 items.
4. **Signal queue** — latest TradeJohn output filtered to `status: pending_approval`. Show ticker, side, size %, EV. Maximum 10 rows.
5. **Watchlist deltas** — anything from `active_tasks.md` with `status: watching` that triggered overnight (price through stop, news hit, IV spike). Cap at 5.
6. **Decision needed** — one bullet per question requiring operator input today. Empty section allowed.

## Quality Gates
- All numeric fields filled (no `n/a` for prices)
- Catalyst section has actual dates, not "this week"
- Decision needed section ends the note (or is explicitly empty)
- Total ≤ 2,500 output tokens
