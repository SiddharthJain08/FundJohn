# Skill: fundjohn:memo-schema
**Trigger**: `/memo-schema`

## Purpose
Encode the SO-6 memo format as a single authoritative skill. Every agent that writes or reads a strategy memo loads this skill. Schema drift becomes impossible.

## SO-6 Required Fields

| Field | Type | Constraint |
|-------|------|-----------|
| `strategy_id` | string | Must match an entry in `src/strategies/manifest.json` |
| `run_timestamp` | string | ISO 8601 (e.g. `2026-04-17T21:30:00+00:00`) |
| `cycle_date` | string | YYYY-MM-DD |
| `sharpe` | float | Numeric; may be negative |
| `max_drawdown` | float | Positive fraction (0.12 = 12%) |
| `signal_count` | int | Non-negative integer |
| `top_signals` | array | Non-empty array of signal objects (see below) |

## Signal Object Schema (inside `top_signals`)

```json
{
  "signal_id": "uuid-string",
  "ticker": "AAPL",
  "direction": "LONG | SHORT",
  "ev": 0.12,
  "kelly": 0.021,
  "entry": 185.50,
  "stop": 171.20,
  "target": 204.10
}
```

## Programmatic Validator

Use this validator before emitting any memo:

```python
from execution.lint_memo import lint_memo
ok, missing = lint_memo(memo_dict)
if not ok:
    # DO NOT post — flag violation and alert #ops
    raise MemoValidationError(f"SO-6 violation: missing {missing}")
```

## Rejection Protocol

If a received memo is missing any required field:
1. **Reject** — do not use its data in the research report or position sizing
2. **Add to Warnings section** — name the strategy and the missing field(s)
3. **Post alert to #ops** — "SO-6 VIOLATION: {strategy_id} missing {fields}"
4. **Never infer** missing values — a missing `sharpe` is not "0.0", it is unknown

## Example Valid Memo

```json
{
  "strategy_id": "S9_dual_momentum",
  "run_timestamp": "2026-04-17T21:30:00+00:00",
  "cycle_date": "2026-04-17",
  "sharpe": 0.82,
  "max_drawdown": 0.14,
  "signal_count": 3,
  "top_signals": [
    {"signal_id": "abc123", "ticker": "NVDA", "direction": "LONG",
     "ev": 0.18, "kelly": 0.025, "entry": 850.0, "stop": 782.0, "target": 935.0}
  ]
}
```
