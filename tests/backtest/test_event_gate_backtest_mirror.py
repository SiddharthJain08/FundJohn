"""C3 backtest mirror: with OPENCLAW_BT_EVENT_GATE=1 the per-bar loop takes no
ENTRY on a T-1..T macro-event session. Exits are untouched. The gate is
per-signal and equity-only (fix round 1, T11 item 1): crypto (dash-form
tickers) and OCC option symbols are never blocked, mirroring the live
per-ticker gate.

Self-contained: synthetic bars, a synthetic trading calendar and a synthetic
macro_events master in tmp_path. aux_data is stubbed so nothing reaches the DB
or data/.
"""
from __future__ import annotations

import ast
import datetime as dt
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub   # noqa: E402
from lib import macro_events as me            # noqa: E402
from lib import trading_calendar as tc        # noqa: E402

DATES = pd.date_range('2026-09-01', periods=20, freq='B')


@pytest.fixture(autouse=True)
def _no_aux(monkeypatch):
    """load_aux_data is imported INSIDE _per_bar_simulate, so patching the
    source module is what takes effect."""
    import strategies.aux_data_loader as adl
    monkeypatch.setattr(adl, 'load_aux_data',
                        lambda *a, **k: {'options': {}}, raising=False)


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in DATES]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    monkeypatch.setattr(tc, '_alpaca_sessions',
                        lambda a, b: (_ for _ in ()).throw(AssertionError('no alpaca probe')))
    yield
    tc.clear_cache()


def _events(tmp_path, monkeypatch, sessions):
    rows = [{'event': 'CPI',
             'scheduled_at': pd.Timestamp(dt.datetime(d.year, d.month, d.day, 12), tz='UTC'),
             'session_date': d, 'source': 'test',
             'ingested_at': pd.Timestamp('2026-09-01T00:00:00Z')}
            for d in sessions]
    p = tmp_path / 'macro_events.parquet'
    pd.DataFrame(rows, columns=me.COLUMNS).to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))


def _events_with_active(tmp_path, monkeypatch, rows_spec):
    """rows_spec: [(session_date: date, active: bool|None), ...]. active=None
    omits the column value (NaN) — 'missing/NULL counts as active' per the
    macro_events module docstring."""
    rows = []
    for d, active in rows_spec:
        row = {'event': 'CPI',
               'scheduled_at': pd.Timestamp(dt.datetime(d.year, d.month, d.day, 12), tz='UTC'),
               'session_date': d, 'source': 'test',
               'ingested_at': pd.Timestamp('2026-09-01T00:00:00Z')}
        if active is not None:
            row['active'] = active
        rows.append(row)
    p = tmp_path / 'macro_events.parquet'
    pd.DataFrame(rows, columns=me.COLUMNS).to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))


def _dataset():
    closes = [100.0 + i for i in range(len(DATES))]
    close_wide = pd.DataFrame({'AAA': closes}, index=DATES)
    close_wide.index.name = 'date'
    bars = {'AAA': pd.DataFrame(
        {'open': [c - 0.5 for c in closes], 'high': [c + 0.2 for c in closes],
         'low': [c - 0.2 for c in closes], 'close': closes},
        index=pd.DatetimeIndex(DATES, name='date'))}
    regimes = pd.Series(['LOW_VOL'] * len(DATES), index=DATES)
    return close_wide, bars, regimes


def _instance():
    from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

    class Stub(BaseStrategy):
        id = 'stub_event_gate'
        min_lookback = 1
        MAX_SIGNALS = 5
        active_in_regimes = list(CANONICAL_REGIMES)

        def generate_signals(self, prices, regime, universe, aux_data=None):
            close = float(prices['AAA'].iloc[-1])
            return [Signal(ticker='AAA', direction='LONG', entry_price=close,
                           stop_loss=close * 0.5, target_1=close * 1.5,
                           target_2=0.0, target_3=0.0, position_size_pct=0.0,
                           confidence='MED')]

    return Stub()


def _sim(max_hold_days=None):
    close_wide, bars, regimes = _dataset()
    kwargs = dict(strategy_id='stub_event_gate', fill_model='same_close')
    if max_hold_days is not None:
        kwargs['max_hold_days'] = max_hold_days
    return ub._per_bar_simulate(_instance(), close_wide, bars, regimes,
                                DATES[0], DATES[-1], **kwargs)


def _dataset_multi(tickers):
    """Like _dataset() but for >1 ticker, each with its own realistic price
    level, so a signal priced off ITS OWN close never trips the
    stop/target-vs-entry sanity check or needs re-anchoring."""
    base = {'AAA': 100.0, 'BTC-USD': 50000.0}
    close_wide = pd.DataFrame(
        {t: [base[t] + i * (1.0 if t == 'AAA' else 10.0) for i in range(len(DATES))]
         for t in tickers},
        index=DATES)
    close_wide.index.name = 'date'
    bars = {}
    for t in tickers:
        closes = close_wide[t].tolist()
        bars[t] = pd.DataFrame(
            {'open': [c - 0.5 for c in closes], 'high': [c + 0.2 for c in closes],
             'low': [c - 0.2 for c in closes], 'close': closes},
            index=pd.DatetimeIndex(DATES, name='date'))
    regimes = pd.Series(['LOW_VOL'] * len(DATES), index=DATES)
    return close_wide, bars, regimes


def _instance_multi(tickers):
    from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

    class Stub(BaseStrategy):
        id = 'stub_event_gate_multi'
        min_lookback = 1
        MAX_SIGNALS = 5
        active_in_regimes = list(CANONICAL_REGIMES)

        def generate_signals(self, prices, regime, universe, aux_data=None):
            out = []
            for t in tickers:
                close = float(prices[t].iloc[-1])
                out.append(Signal(ticker=t, direction='LONG', entry_price=close,
                                  stop_loss=close * 0.5, target_1=close * 1.5,
                                  target_2=0.0, target_3=0.0, position_size_pct=0.0,
                                  confidence='MED'))
            return out

    return Stub()


def _sim_multi(tickers, max_hold_days=None):
    close_wide, bars, regimes = _dataset_multi(tickers)
    kwargs = dict(strategy_id='stub_event_gate_multi', fill_model='same_close')
    if max_hold_days is not None:
        kwargs['max_hold_days'] = max_hold_days
    return ub._per_bar_simulate(_instance_multi(tickers), close_wide, bars, regimes,
                                DATES[0], DATES[-1], **kwargs)


def test_gate_skips_entries_on_t_minus_one_and_t(tmp_path, monkeypatch, calendar):
    gated = DATES[10].date()
    _events(tmp_path, monkeypatch, [gated])
    monkeypatch.delenv('OPENCLAW_BT_EVENT_GATE', raising=False)
    base = _sim()
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    out = _sim()
    assert out['entries_event_gated'] == 2          # T-1 and T, one signal each
    assert len(out['trades']) == len(base['trades']) - 2
    entered = {t['entry_date'] for t in out['trades']}
    assert gated not in entered
    assert DATES[9].date() not in entered


def test_a_calendar_with_no_events_gates_nothing(tmp_path, monkeypatch, calendar):
    _events(tmp_path, monkeypatch, [])
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    assert _sim()['entries_event_gated'] == 0


def test_missing_master_is_inert(tmp_path, monkeypatch, calendar):
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(tmp_path / 'nope.parquet'))
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    assert _sim()['entries_event_gated'] == 0


def test_the_live_flag_alone_does_not_arm_the_backtest(tmp_path, monkeypatch, calendar):
    """Spec §0: never stack epochs. Arming C3 live must not silently change
    every fleet run."""
    _events(tmp_path, monkeypatch, [DATES[10].date()])
    monkeypatch.delenv('OPENCLAW_BT_EVENT_GATE', raising=False)
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert _sim()['entries_event_gated'] == 0


def test_low_importance_event_does_not_gate(tmp_path, monkeypatch, calendar):
    """gated_sessions() defaults to HIGH_IMPORTANCE = (FOMC_DECISION, CPI, NFP)
    per ruling R3; a PCE release (not in that set) must not gate entries."""
    d = DATES[10].date()
    rows = [{'event': 'PCE',
             'scheduled_at': pd.Timestamp(dt.datetime(d.year, d.month, d.day, 12), tz='UTC'),
             'session_date': d, 'source': 'test',
             'ingested_at': pd.Timestamp('2026-09-01T00:00:00Z')}]
    p = tmp_path / 'macro_events.parquet'
    pd.DataFrame(rows, columns=me.COLUMNS).to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    assert _sim()['entries_event_gated'] == 0


def test_config_json_literal_carries_the_event_gate_keys():
    src = (ROOT / 'src' / 'backtest' / 'unified_backtest.py').read_text()
    assert "'event_gate':" in src
    assert "'entries_event_gated':" in src


def test_flag_unset_identity_with_full_event_coverage(tmp_path, monkeypatch, calendar):
    """Fix round 1 item 3 — the REAL unset-identity proof (supersedes the
    predecessor's `test_flag_unset_is_byte_identical`, which only checked
    `entries_event_gated == 0` and a non-empty trade list, not an actual
    diff). Even when EVERY session in the panel carries a gating event, an
    unset flag must produce the exact same trade list as no events at all:
    `_event_gate_sessions` stays {} in that case, so `if _event_gate_sessions
    and ...` short-circuits before `_is_equity_ticker` is ever consulted."""
    monkeypatch.delenv('OPENCLAW_BT_EVENT_GATE', raising=False)
    _events(tmp_path, monkeypatch, [d.date() for d in DATES])
    with_events = _sim()
    _events(tmp_path, monkeypatch, [])
    without_events = _sim()
    assert with_events['entries_event_gated'] == 0
    assert without_events['entries_event_gated'] == 0
    assert len(with_events['trades']) > 0
    assert with_events['trades'] == without_events['trades']


def test_exit_on_t_is_not_gated(tmp_path, monkeypatch, calendar):
    """Fix round 1 — exits are untouched. A position opened well before T-1
    must still exit normally exactly on the gated T session: the entry gate
    only ever skips a NEW entry, never the exit walk of a trade already
    opened. (In this engine's classic path, simulate_trade resolves the full
    exit at entry time — so this is a placement guard against a future edit
    reinstating a bar-level `continue` above the exit machinery, not a
    behavioral discriminator on its own.)"""
    t = DATES[10].date()
    anchor = DATES[5]  # earliest bar with a signal: min_lookback(1)+5 gate
                       # means prices_to_date needs >=6 rows, so DATES[0..4]
                       # never generate a signal at all — DATES[5] is the
                       # first entry this stub can actually open.
    _events(tmp_path, monkeypatch, [t])
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    # max_hold_days=5: entry at DATES[5] walks DATES[6..10] (5 bars) with no
    # bracket touch (wide stop/target vs. the flat rising price path) ->
    # exits at the last bar in the window, DATES[10] == t, reason='max_hold'.
    out = _sim(max_hold_days=5)
    opened_at_anchor = [tr for tr in out['trades'] if tr['entry_date'] == anchor.date()]
    assert len(opened_at_anchor) == 1
    assert opened_at_anchor[0]['exit_date'] == t
    assert opened_at_anchor[0]['exit_reason'] == 'max_hold'


def test_crypto_ticker_is_not_gated(tmp_path, monkeypatch, calendar):
    """Fix round 1 item 1 — the gate is per-signal and equity-only. On a
    mixed equity+crypto bar, only the equity (AAA) entry is blocked;
    BTC-USD (dash form) is never blocked, and entries_event_gated counts
    only the equity signal. The predecessor's whole-bar version would have
    counted 4 (both tickers, both gated sessions) and killed BOTH tickers'
    entries on those sessions."""
    t_minus_1, t = DATES[9].date(), DATES[10].date()
    _events(tmp_path, monkeypatch, [t])
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    out = _sim_multi(('AAA', 'BTC-USD'))
    aaa_entries = {tr['entry_date'] for tr in out['trades'] if tr['ticker'] == 'AAA'}
    btc_entries = {tr['entry_date'] for tr in out['trades'] if tr['ticker'] == 'BTC-USD'}
    assert t_minus_1 not in aaa_entries
    assert t not in aaa_entries
    assert t_minus_1 in btc_entries
    assert t in btc_entries
    assert out['entries_event_gated'] == 2


def test_inactive_event_row_is_excluded(tmp_path, monkeypatch, calendar):
    """Deactivated rows (active=False, written by
    ingest_macro_events.py --deactivate) must not gate. Includes a positive
    control (the same row WITH active=True DOES gate) so this doesn't pass
    vacuously the way an unreadable-master test would."""
    d = DATES[10].date()
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')

    _events_with_active(tmp_path, monkeypatch, [(d, True)])
    assert _sim()['entries_event_gated'] == 2      # positive control: T-1 + T

    _events_with_active(tmp_path, monkeypatch, [(d, False)])
    assert _sim()['entries_event_gated'] == 0


def test_holiday_adjacent_t_minus_one_uses_the_prior_trading_session(tmp_path, monkeypatch):
    """T-1 means the previous NYSE SESSION, not the calendar day before (see
    the lib.macro_events module docstring: 'a post-holiday release gates the
    session before the holiday'). Build a calendar with one session dropped
    (a modelled holiday) between the true T-1 and the event session, and
    confirm gated_sessions() skips over the gap rather than gating the
    (non-trading) calendar day before.

    Deliberately does NOT reuse the `calendar` fixture (distinct filename +
    its own tc.clear_cache() calls) — the trading_calendar _load() cache is
    keyed on (path, mtime_ns), so sharing the fixture's file risks a stale
    read; this test also asserts on lib.macro_events.gated_sessions()
    directly (\"the gated set\") rather than routing through _sim()."""
    cal_dates = pd.date_range('2026-08-01', '2026-09-30', freq='B')
    holiday = dt.date(2026, 9, 15)        # dropped from the calendar (modelled closure)
    before_gap = dt.date(2026, 9, 14)     # the true T-1 (prior TRADING session)
    event_session = dt.date(2026, 9, 16)  # T (the session right after the gap)
    assert holiday in {d.date() for d in cal_dates}          # sanity: was a business day
    assert before_gap in {d.date() for d in cal_dates}
    assert event_session in {d.date() for d in cal_dates}

    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in cal_dates if d.date() != holiday]
    cal_path = tmp_path / 'holiday_cal.parquet'
    pd.DataFrame(rows).to_parquet(cal_path, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(cal_path))
    tc.clear_cache()
    monkeypatch.setattr(tc, '_alpaca_sessions',
                        lambda a, b: (_ for _ in ()).throw(AssertionError('no alpaca probe')))
    try:
        _events(tmp_path, monkeypatch, [event_session])
        gated = me.gated_sessions(cal_dates[0].date(), cal_dates[-1].date())
        assert before_gap in gated       # T-1: the prior TRADING session
        assert holiday not in gated      # the dropped session itself: never a session, never gated
        assert event_session in gated    # T
    finally:
        tc.clear_cache()


def test_options_run_event_gate_is_off_even_with_flag_set(monkeypatch):
    """Fix round 1 item 2 — `event_gate` in config_json must be 'off'
    whenever `_sim_fn` is not `_per_bar_simulate` (any options run), even
    with OPENCLAW_BT_EVENT_GATE=1 set: an options run never calls
    _per_bar_simulate at all, so it must never be misreported as gated.

    Unit-level per the brief ('patch/inspect the literal builder if
    needed'): run_backtest() needs a live Postgres connection to reach this
    literal, which this box's hard constraints forbid. Instead, parse the
    module with `ast`, locate the actual `'event_gate'` VALUE expression
    node in the config_json dict literal, compile just that expression, and
    eval it under three (env, _sim_fn) bindings. A source substring/regex
    check is not enough here — the expression's own text contains the
    literal strings 'on'/'off' and a nested `os.environ.get(...) == '1'`
    parenthesized call, so a naive `\\((.*?)\\)` non-greedy match closes on
    the wrong parenthesis and evaluates something else entirely."""
    src_path = ROOT / 'src' / 'backtest' / 'unified_backtest.py'
    tree = ast.parse(src_path.read_text())

    value_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == 'event_gate':
                    value_node = v
                    break
            if value_node is not None:
                break
    assert value_node is not None, "'event_gate' key not found in any dict literal"

    # eval() here is safe: `code` is compiled from an AST node parsed out of
    # THIS REPO's own unified_backtest.py (not attacker- or user-supplied
    # text), scoped to exactly the 'event_gate' value expression, and run
    # with an explicit, minimal globals dict (`os` + the two names the
    # expression itself references) — no builtins-based escape surface
    # beyond what compiling any trusted local source already implies.
    expr = ast.fix_missing_locations(ast.Expression(body=value_node))
    code = compile(expr, '<event_gate_literal>', 'eval')

    class _NotPerBarSimulate:
        pass

    _glb = {'os': os, '_per_bar_simulate': ub._per_bar_simulate}

    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    assert eval(code, dict(_glb, _sim_fn=ub._per_bar_simulate)) == 'on'
    assert eval(code, dict(_glb, _sim_fn=_NotPerBarSimulate())) == 'off'

    monkeypatch.delenv('OPENCLAW_BT_EVENT_GATE', raising=False)
    assert eval(code, dict(_glb, _sim_fn=ub._per_bar_simulate)) == 'off'
