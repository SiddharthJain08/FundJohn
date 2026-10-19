# Intraday session simulator — contract for single-session strategies

Status: Task 1 LANDED on main `a30458b6` (2026-10-19). Task 3 (contract hardening: helper module, lint rule, promotion
guard, StrategyCoder guidance) — see `.superpowers/sdd/2026-10-18-intraday-fill-model/brief.md`. Origin: the TR04/TR06
cohort strategies (changelog 2026-10-17/18 and 2026-10-19); this document is the durable contract for ANY future
strategy that enters and exits inside one regular-hours session.

## 1. What the daily backtester does without this
`backtest.unified_backtest._per_bar_simulate` fills every signal at the signal day's daily close (`fill_model`
`same_close`), re-anchors the bracket to that fill and walks DAILY bars for up to `max_hold_days`. A strategy that
means "enter 10:00 ET, flat at the close" is therefore measured as "buy at the close, hold overnight against the
bracket" — a different strategy. TR04 (11a2d63a) and TR06 (a19d85ee) were mis-measured this way for two weeks.

## 2. Opt-in contract (per signal)
A `Signal` opts in by carrying BOTH keys in `signal_params`:

| key | value | meaning |
|---|---|---|
| `exit_on` | `'session_close'` | this trade never survives the session; flat at the last RTH bar |
| `entry_bar_et` | `'HH:MM'` ET start of a 30-minute RTH bar (`09:30`, `10:00`, …, `15:30`) | the bar whose OPEN is the fill — the first bar AFTER the information the strategy used |

Rules:
- `entry_bar_et` must be strictly after the last bar the strategy read (TR04 reads the 09:30 bar ⇒ `10:00`; TR06 reads
  through the 15:00 bar ⇒ `15:30`). The simulator cannot check this; the strategy author and the lint rule own it.
- `entry_price` on the Signal is the strategy's DECISION price (e.g. the 09:30 bar close). Brackets are shaped around
  it and re-anchored (`_reanchor_bracket`) to the actual entry-bar open, exactly as for daily signals.
- A signal with `exit_on='session_close'` but no `entry_bar_et` is SKIPPED and counted (`intraday_missing_entry_bar`).
  Nothing is ever guessed and nothing ever falls back to the daily simulator.
- `exit_hook=True` strategies cannot opt in (`ValueError` at the first opted-in signal) — the open-book stepper does
  not model intraday bars.
- Any signal WITHOUT the key is byte-identical to the pre-2026-10-19 engine (pinned by
  `tests/backtest/test_intraday_exit.py::test_g_signal_without_exit_on_is_untouched`).

Use the helper (Task 3): `from strategies.intraday import session_close_params` →
`signal_params=session_close_params('10:00', strategy_id=self.id, ...)` validates the bar string at authoring time.

## 3. Simulator semantics (`simulate_intraday_trade`, `unified_backtest.py`)
- Input: ONE ticker's RTH bars for the signal's ET session, from `aux['prices_30m']` (the aux loader's point-in-time
  slice `[date−30 d, date]`, which includes the signal day), tagged by the shared `strategies.cohort_base.tag_rth_30m`
  (ET session from the bar TIMESTAMP — the stored `date` column is ignored; RTH = `09:30 <= ET start < 16:00`).
- Entry fill = `open` of the `entry_bar_et` bar, adverse slippage as `simulate_trade` (per-ticker `cost_bps` when the
  spread artifact is on, else the flat bps).
- Walk = that bar and every later bar of the same session through the shared `_bar_exit` (so `OPENCLAW_BT_DOUBLE_TOUCH`
  and `OPENCLAW_BT_GAP_FILL` apply unchanged). Bracket hit ⇒ `exit_reason` `stop`/`target` at the adverse fill.
- No bracket hit ⇒ exit at the close of the LAST RTH bar present (`session_close`). Half days end at 13:00 ET; the
  simulator never assumes a 15:30 bar exists.
- Returns the `simulate_trade` dict shape with `holding_days = 1`, `daily_marks = [(session, pnl_pct)]`. An intraday trade
  is ONE session for every downstream statistic: `avg_holding_days` reads 1.0; the MTM Sharpe sees one return per trade.
- Returns `None` (⇒ skipped + `intraday_no_bars`) when the entry bar is missing or the session has < 2 RTH bars.
- Point-in-time: the simulator reads only bars at/after the entry bar; strategies must read only bars at/before their
  decision bar (cohort_base hands them TODAY's full RTH session — selecting by `time_et` is the discipline).

## 4. Flags and provenance
- `OPENCLAW_BT_INTRADAY_EXIT` — default ON. `0` forces the legacy daily path for every signal (comparison runs).
- `config_json` on `strategy_backtest_runs`: `intraday_exit` (`on`/`off`; `off` also for options runs), `intraday_trades`,
  `intraday_no_bars`, `intraday_missing_entry_bar`; `exit_reasons` gains `session_close`.
- A run with `intraday_trades > 0` LEANED on this simulator. The promotion gate must refuse it for live until the live
  mirror exists (§6).

## 5. Data routes for a new strategy
1. **Cohort route** (subclass `CohortBaseStrategy`): `market_data['spy_30m_bars']`, `market_data['intraday_30m_bars'][t]`
   (today's RTH bars with `session`/`time_et`), `market_data['intraday_30m_session']`, `market_data['spy_10d_volume_avg']`
   (prior sessions only), `opts_map[t]['avg_dollar_volume_30d']` (prior sessions only). Select bars by `time_et`, never
   by index (half days).
2. **Raw route** (any `BaseStrategy`): `aux_data.get('prices_30m')` — long frame `date datetime ticker open high low close
   volume vwap transactions`; call `strategies.cohort_base.tag_rth_30m(df)` to get `session`/`time_et` and RTH rows,
   then take `session == max(session)` for today. Declare `prices_30m` in the manifest `data_dependencies`.
- Coverage: SPY from 2024-04-18; ~115 other liquid names from 2026-03-02 (`prices_30m.parquet`, 32 extended-hours bars/day
  through 2026-04-22, 13 RTH bars/day since 2026-04-30). Trades before coverage are skipped and counted, never faked.

## 6. Live execution — NOT available (known gap, tracked in the handoff backlog)
The live chain collects + signals at ~15:00 ET and submits bracket orders before the close; the engine, handoff builder
and sizer ignore `exit_on` and `entry_bar_et`. A session_close strategy promoted today would be executed as an overnight
bracket trade — the exact mis-measurement §1 describes, but with real money. Guards:
- Promotion gate (`src/lib/promotion_service.js`, Task 3): a primary run with `intraday_trades > 0` fails
  `intraday_exit_live_disabled` unless `OPENCLAW_INTRADAY_EXIT_LIVE=1` — the twin of the exit-hook guard.
- Lint (`src/strategies/strategy_lint.py`, Task 3, kind `intraday`): `exit_on: 'session_close'` without a valid
  `entry_bar_et` literal (or without `session_close_params(...)`) is a violation at validate/promotion time.
Live support (an intraday scheduler for the entry bar + a flatten-at-close order path + sizer awareness) is a separate
SDD item; nobody should set `OPENCLAW_INTRADAY_EXIT_LIVE=1` before it lands.

## 7. Reference results (first strategies measured honestly)
See changelog 2026-10-19: TR04 and TR06 canonicals on `a30458b6`. Probe (same windows as 10-17): TR04 475 trades,
mean −12 bps/trade with the session_close exits averaging −13 bps against a 5.5 bps modelled SPY round trip — i.e.
"long SPY 10:00→close almost every day" has negative drift in this window (NOPE's gap scale makes it fire daily); TR06
1,193 trades, mean −16 bps/trade ≈ the modelled round trip for liquid names, gross edge ≈ 0–4 bps (paper: ~3 bps/leg),
0.3 %/0.5 % bracket lopsided inside a 30-minute hold (stops −57 bps, targets +24 bps, 50/50). Operator decision
2026-10-19: research question CLOSED — no further TR04/TR06 tuning; the path is kept for future strategies.
