# Skill: fundjohn:backtest-plumb
**Trigger**: `/backtest-plumb` or `/plumb`

## Purpose
Diagnose a strategy that is failing to produce signals or failing the promotion gate. Produce a structured diagnosis and either a pass memo (candidate → paper) or an SO-6 failure memo.

## Promotion Gate
To promote a strategy from `candidate` to `paper`, both conditions must pass:
- **Sharpe ≥ 0.5** (annualized, out-of-sample)
- **Max drawdown ≤ 0.20** (20%)

If both pass → emit a pass memo, recommend `state: paper`.
If either fails → emit an SO-6 failure memo with `gate_result: fail`.

## Diagnostic Checklist (run in order)

1. **Manifest state check** — `src/strategies/manifest.json`: is `state` one of `{candidate, paper, live}`? If `deprecated` or `archived`, stop — do not proceed.

2. **Registry entry** — `src/strategies/registry.py`: does `_IMPL_MAP` contain the strategy_id key? If missing, that is the root cause.

3. **Parquet column availability** — does `data/master/prices.parquet` contain every column referenced in `generate_signals()`? Run `pd.read_parquet('data/master/prices.parquet', columns=[...]).columns` to verify. Same check for `financials.parquet`, `options_eod.parquet` as needed.

4. **Signal dataclass field types** — are all `Signal` fields the correct Python type for psycopg2 insert? `entry_price`, `stop_loss`, `profit_target` must be `float` not `numpy.float64`. `signal_date` must be `str` in `YYYY-MM-DD` format, not a `datetime` object. `confidence` must be `float` between 0 and 1, not `None`.

5. **Regime filter coverage** — if `regime_conditions` is non-empty in the strategy_spec, does the current regime (`src/data/regime.json`) match? If the filter drops all signals, that is expected behavior not a bug.

6. **Empty DataFrame handling** — call `generate_signals(pd.DataFrame())` mentally or actually. It must return `[]` without raising.

## Common Root Causes

| Symptom | Root Cause |
|---------|-----------|
| `KeyError: 'some_column'` | Column not in parquet — either missing from data pipeline or wrong column name |
| `psycopg2.errors.NotNullViolation` on `confidence` | `confidence` is `None` — must be set to a float |
| `0 signals generated` in LOW_VOL regime | Regime filter in `generate_signals()` is too restrictive |
| `AttributeError: 'NoneType' has no attribute 'compute_stops_and_targets'` | Forgot to call `super().__init__()` |
| Wrong stop/target geometry | Not using `self.compute_stops_and_targets()` — ATR math hand-rolled incorrectly |

## Output Format

```json
{
  "strategy_id": "...",
  "gate_result": "pass | fail",
  "sharpe": 0.0,
  "max_drawdown": 0.0,
  "root_cause": "One sentence — what is wrong",
  "affected_file": "src/strategies/implementations/...",
  "proposed_fix": "Exact code change needed",
  "verification_command": "python3 -c \"...\" command to confirm fix"
}
```

If `gate_result = fail`, also emit this SO-6 failure memo block:
```
strategy_id: {id}
run_timestamp: {ISO 8601}
cycle_date: {date}
sharpe: {value}
max_drawdown: {value}
signal_count: 0
top_signals: []
failure_reason: {sharpe below 0.5 | drawdown above 20%}
```
