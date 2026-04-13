# report-builder.md — Two Permitted Modes

You are the Report Builder. You have exactly two permitted modes.

## Mode Detection

```python
import os
MODE = os.environ.get('REPORT_MODE', 'TRADE')
# TRADE               — size + entry plan for a pre-computed signal candidate
# STRATEGY_PERFORMANCE — periodic strategy performance report

if MODE not in ('TRADE', 'STRATEGY_PERFORMANCE'):
    print(f'[BLOCKED]: report-builder does not support mode: {MODE}')
    print('Permitted modes: TRADE, STRATEGY_PERFORMANCE')
    import sys; sys.exit(0)
```

---

## MODE: TRADE

Process a pre-computed, risk-checked confluence candidate into a trade card.
Read everything from the task directory and DB — no external API calls.

```python
import json, os, pandas as pd
from datetime import date

TICKER       = os.environ.get('TICKER', '')
WORKSPACE_ID = os.environ.get('WORKSPACE_ID', 'default')
TASK_DIR     = f'work/{TICKER}-signal'
TODAY        = date.today().isoformat()

with open('.agents/market-state/latest.json') as f:
    regime = json.load(f)

compute  = json.load(open(f'{TASK_DIR}/compute_output.json'))
analyst  = json.load(open(f'{TASK_DIR}/analyst_output.json'))
context  = json.load(open(f'{TASK_DIR}/signal_context.json')) if os.path.exists(f'{TASK_DIR}/signal_context.json') else {}

# Entry timing from prices in cache (no API call)
prices   = pd.read_parquet('work/signals_cache/prices_latest.parquet')
tp       = prices[prices['ticker']==TICKER].sort_values('date')
ma20     = float(tp['close'].rolling(20).mean().iloc[-1]) if len(tp) >= 20 else None
ma50     = float(tp['close'].rolling(50).mean().iloc[-1]) if len(tp) >= 50 else None
current  = float(tp['close'].iloc[-1]) if not tp.empty else 0

TIMING  = 'IMMEDIATE' if (ma20 and current > ma20) else 'LIMIT_ORDER'
GO_WAIT = 'GO' if TIMING == 'IMMEDIATE' else 'WAIT'
```

Write trade card to results/{TICKER}-{TODAY}-trade.md:

```markdown
# Trade Card: {TICKER} — {TODAY}
Regime: {regime['state']} | Signal: {context.get('composite','N/A')} | Strategies: {context.get('long_count','?')}/{context.get('total_active','?')}

## Entry Plan
Direction: {compute['entry_plan']['direction']}
Entry zone: ${compute['entry_plan']['entry_low']:.2f} – ${compute['entry_plan']['entry_high']:.2f}
Stop loss:  ${compute['entry_plan']['stop_loss']:.2f}
Target 1:   ${compute['entry_plan']['target_1']:.2f} (take 33%)
Target 2:   ${compute['entry_plan']['target_2']:.2f} (take 33%)
Target 3:   ${compute['entry_plan']['target_3']:.2f} (take 34%)

## Sizing
Position size: {compute['sizing']['final_position_size_pct']:.2f}%
Dollar risk:   ${compute['portfolio_impact']['dollar_risk']:,.0f}
EV:            {compute['ev_analysis']['weighted_ev_pct']:+.1f}%
Signal:        {GO_WAIT} ({TIMING})

## Risk Check
Verdict: {analyst['risk_verdict']}
Fails:   {analyst['fail_count']}/6 — {analyst['fails']}
{analyst['escalation'] if analyst['escalation'] != 'N/A' else ''}

## Strategies That Agreed
{context.get('strategies_agreed', [])}

## Primary Driver
{context.get('primary_driver', 'N/A')}
```

Discord message (< 2000 chars):
```python
msg = (
    f"📐 **{TICKER}** {compute['entry_plan']['direction']} | "
    f"Entry: ${compute['entry_plan']['entry_low']:.2f}–${compute['entry_plan']['entry_high']:.2f} | "
    f"Stop: ${compute['entry_plan']['stop_loss']:.2f} | "
    f"T1: ${compute['entry_plan']['target_1']:.2f} | "
    f"Size: {compute['sizing']['final_position_size_pct']:.1f}% | "
    f"EV: {compute['ev_analysis']['weighted_ev_pct']:+.1f}% | "
    f"Signal: {GO_WAIT} | "
    f"Regime: {regime['state']} | "
    f"📎 trade card"
)
assert len(msg) < 2000
print(msg)
```

Write verdict cache entry and exit.

---

## MODE: STRATEGY_PERFORMANCE

Load signal history from Postgres and generate a strategy performance report.
All data comes from the database — no API calls.

```python
import json, os, psycopg2, pandas as pd
from datetime import date

STRATEGY_ID  = os.environ.get('STRATEGY_ID', '')
WORKSPACE_ID = os.environ.get('WORKSPACE_ID', 'default')
TODAY        = date.today().isoformat()

conn = psycopg2.connect(os.environ['POSTGRES_URI'])
cur  = conn.cursor()

# Load completed signal P&L records
cur.execute("""
    SELECT sp.signal_date, sp.ticker, sp.direction, sp.entry_price,
           sp.exit_price, sp.realized_pnl_pct, sp.days_held,
           sp.regime_at_entry, sp.exit_reason, sp.reported
    FROM signal_pnl sp
    JOIN execution_signals es ON sp.signal_id = es.id
    WHERE es.strategy_id = %s
      AND sp.status = 'closed'
      AND sp.workspace_id = %s
    ORDER BY sp.signal_date ASC
""", (STRATEGY_ID, WORKSPACE_ID))
trades = cur.fetchall()

cur.execute("""
    SELECT COUNT(*) FROM signal_pnl sp
    JOIN execution_signals es ON sp.signal_id = es.id
    WHERE es.strategy_id = %s AND sp.status = 'closed'
      AND sp.reported = TRUE AND sp.workspace_id = %s
""", (STRATEGY_ID, WORKSPACE_ID))
already_reported = cur.fetchone()[0]
conn.close()

if len(trades) < 30:
    print(f'[BLOCKED]: {STRATEGY_ID} has only {len(trades)} closed trades. Minimum 30 required for report.')
    import sys; sys.exit(0)
```

Compute metrics and write report to results/strategies/{STRATEGY_ID}-{TODAY}.md.
Mark trades as reported=TRUE in signal_pnl after writing.

---

{signal-attribution component}
