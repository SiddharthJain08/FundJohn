"""Crypto engine Phase 3, Task 7 — per-class regime-of-record inside
_sharpe_cadence_path. Harness extended from test_sizer_benchmark_hurdle_wiring.py
with crypto signals, a crypto regime (CRISIS) and its own weights rows / S_m.
No DB / Redis / broker: every touchpoint is stubbed."""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import pytest  # noqa: E402

import execution.regime_blended_sizer as _sizer  # noqa: E402
from execution import benchmark_sizing as bz      # noqa: E402

NAV, LAM = 100_000.0, 2.0
EQ_REGIME, CR_REGIME = 'LOW_VOL', 'CRISIS'
POOL_ENV = 'OPENCLAW_CRYPTO_POOLED_SIZING'


def _account(nmbp=None):
    a = {'equity': NAV, 'regt_buying_power': 2 * NAV, 'long_market_value': 0, 'cash': NAV}
    if nmbp is not None:
        a['non_marginable_buying_power'] = nmbp
    return a


def _params(min_acting=1, liq=1.0):
    return {'liquidity_param': liq, 'min_signal_notional_usd': 1, 'min_signal_notional_pct': 0.00001,
            'position_circuit_breaker_pct': 0.02, 'min_cumulative_sharpe': 3.0,
            'min_acting_strategies': min_acting}


def _carried(sid, ticker, direction='LONG'):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction,
            'signal_date': date(2026, 11, 9), 'entry_price': 100.0, 'stop_loss': 95.0,
            'target_1': 110.0, 'target_2': 120.0, 'signal_params': {}}


def _row(sid, eff):
    return {'strategy_id': sid, 'daily_weight': eff, 'effective_sharpe': eff, 'cadence_days': 21.0, 'bt_n': 600}


EQ_ROWS = [_row('S_beta_spy', 2.0), _row('S_hi', 2.6), _row('S_lo', 1.5),
           _row('S_cr_a', 9.0)]       # a crypto id's EQUITY-regime row: must never size crypto
CR_ROWS = [_row('S_beta_btc', 1.0), _row('S_cr_a', 2.0), _row('S_cr_b', 0.5),
           _row('S_hi', 7.0)]         # an equity id's crypto-regime row: ignored
EQ_CARRIED = [_carried('S_beta_spy', 'SPY'), _carried('S_hi', 'ZZTA'), _carried('S_lo', 'ZZTB')]
CR_CARRIED = [_carried('S_beta_btc', 'BTC-USD'), _carried('S_cr_a', 'ETH-USD'),
              _carried('S_cr_b', 'SOL-USD'), _carried('S_cr_a', 'USDC-USD')]
CLASSES = {'S_beta_btc': 'crypto', 'S_cr_a': 'crypto', 'S_cr_b': 'crypto',
           'S_beta_spy': 'equity', 'S_hi': 'equity', 'S_lo': 'equity'}
CRYPTO_TKRS = {'BTC-USD', 'ETH-USD', 'SOL-USD', 'USDC-USD'}


class H:
    """Per-run capture."""
    def __init__(self):
        self.lines, self.written, self.load_current, self.loader_calls, self.flatten = [], [], [], [], []


def run(monkeypatch, *, crypto=True, pooled=False, bench_flag=False, s_m_eq=2.0, s_m_cr=0.8,
        broker=None, nmbp=None, hold=None, eq_frac=10.0, btc_frac=0.25, gross_frac=0.50,
        eq_carried=None, cr_params=None, eq_params=None, lane='eod', eq_rows=None, cr_carried=None,
        sigs=None, scalars=None, h=None):
    h = h if h is not None else H()
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    if lane == 'eod':
        monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    else:                                   # production same-day lane: active-window loader
        monkeypatch.delenv('OPENCLAW_EOD_SIGNAL_REGISTER', raising=False)
        monkeypatch.delenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', raising=False)
        monkeypatch.setattr(_sizer, '_check_force_fire_flag', lambda: False)
    for g in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
              'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE', 'OPENCLAW_TRADE_WEIGHT_FACTOR',
              'OPENCLAW_INTRADAY_REDEPLOY', 'OPENCLAW_STRATEGY_SIZE_SCALAR', 'OPENCLAW_QP_SHADOW',
              'OPENCLAW_STRATEGY_CADENCE_STOP_NORM', 'OPENCLAW_ASSET_CORR_CAP_SHADOW'):
        monkeypatch.delenv(g, raising=False)
    if pooled:
        monkeypatch.setenv(POOL_ENV, '1')
    else:
        monkeypatch.delenv(POOL_ENV, raising=False)
    monkeypatch.setenv(bz.BENCH_SIZING_ENV, '1' if bench_flag else '0')
    monkeypatch.setenv(bz.BETA_BUDGET_ENV, '0')

    def _max_frac(default=1.0, conn=None, *, instrument_class='equity'):
        return btc_frac if instrument_class == 'crypto' else eq_frac
    monkeypatch.setattr(bz, 'benchmark_max_nav_frac', _max_frac)
    monkeypatch.setattr(bz, 'crypto_gross_max_nav_frac', lambda default=0.5, conn=None: gross_frac)

    def _carried_loader(weight_by_strat, cadence_by_strat=None, regime_state=None, *,
                        strategy_ids=None, tradable_classes=None):
        h.loader_calls.append({'regime_state': regime_state, 'strategy_ids': strategy_ids,
                               'tradable_classes': tradable_classes, 'sids': set(weight_by_strat)})
        if strategy_ids is None:          # equity call: us_equity universe only
            return [dict(s) for s in (eq_carried if eq_carried is not None else EQ_CARRIED)]
        return [dict(s) for s in (cr_carried or CR_CARRIED) if s['strategy_id'] in strategy_ids]
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals', _carried_loader)

    def _window_loader(regime_state, weight_by_strat, cadence_by_strat, *,
                       strategy_ids=None, tradable_classes=None):
        h.loader_calls.append({'regime_state': regime_state, 'strategy_ids': strategy_ids,
                               'tradable_classes': tradable_classes, 'sids': set(weight_by_strat),
                               'lane': 'window'})
        if strategy_ids is None:
            return [dict(s) for s in (eq_carried if eq_carried is not None else EQ_CARRIED)]
        return [dict(s) for s in CR_CARRIED if s['strategy_id'] in strategy_ids]
    monkeypatch.setattr(_sizer, '_load_active_window_signals', _window_loader)

    def _load_current(regime_state):
        h.load_current.append(regime_state)
        return [dict(r) for r in ((EQ_ROWS if eq_rows is None else eq_rows)
                                  if regime_state == EQ_REGIME else CR_ROWS)]

    def _s_m(regime_state, run_date, *, benchmark='SPY', horizon=None, **_k):
        return s_m_cr if benchmark == 'BTC-USD' else s_m_eq

    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: dict(broker or {}))
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd_with_class',
                        lambda: (dict(broker or {}), {}))     # never the real CLI
    monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', lambda t, *a, **k: t)
    monkeypatch.setattr(_sizer, '_apply_benchmark_corr_removal', lambda t, b, nav: t)
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: h.lines.append(line))
    monkeypatch.setattr(_sizer, '_post_crypto_sizing_line', lambda line: h.lines.append(line))
    monkeypatch.setattr(_sizer, '_write_crypto_sizing_last_line', lambda line: h.written.append(line))
    monkeypatch.setattr(_sizer, '_load_crypto_hold_state', lambda: hold or {'sleeve': None, 'tickers': {}})
    monkeypatch.setattr(_sizer, '_strategy_classes', lambda: dict(CLASSES))
    monkeypatch.setattr(_sizer, '_load_trade_factor_anchor', lambda default=None: 1000)

    def _flat(*a, **k):
        h.flatten.append(k.get('instrument_class'))
        return None
    monkeypatch.setattr(_sizer, '_maybe_flatten_zero_conviction', _flat)
    monkeypatch.setattr(_sizer, 'PER_TICKER_CAP_SHARPE_FRAC', 10.0)
    import execution.regime_param_resolver as _rpr
    monkeypatch.setattr(_rpr, 'size_scalars_for_regime', lambda r: dict((scalars or {}).get(r, {})))
    if scalars:
        monkeypatch.setenv('OPENCLAW_STRATEGY_SIZE_SCALAR', '1')
    import execution.strategy_similarity as _ss
    monkeypatch.setattr(_ss, 'load_groups', lambda r: None)
    kw = {}
    if crypto:
        kw = {'crypto_regime_state': CR_REGIME, 'crypto_params': cr_params or _params()}
    with _mock.patch('execution.strategy_weights.load_current', side_effect=_load_current), \
         _mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids',
                     return_value={'S_beta_spy', 'S_beta_btc'}), \
         _mock.patch('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', side_effect=_s_m):
        if sigs is None:
            sigs = [] if lane == 'eod' else [dict(x) for x in EQ_CARRIED]
        orders = _sizer.size_positions(signals=sigs, account_state=_account(nmbp), regime={'state': EQ_REGIME},
                                       run_date=date(2026, 11, 10), strategy_state={},
                                       regime_params=eq_params or _params(), confirmer=lambda p: {}, **kw)
    return orders, h


def targets(orders):
    return {o['ticker']: o['target_usd'] for o in orders if o['action'] not in ('close_long', 'close_short')}


def eq_t(orders):
    return {t: v for t, v in targets(orders).items() if t not in CRYPTO_TKRS}


def cr_t(orders):
    return {o['ticker']: o['target_usd'] for o in orders if o['ticker'] in CRYPTO_TKRS}


def shadow_lines(h, mode='shadow'):
    return [l for l in h.lines if l.startswith('crypto_sizing.%s[' % mode)]


# ── Review Focus 1 ───────────────────────────────────────────────────────────

def test_pooled_shadow_leaves_equity_targets_identical(monkeypatch):
    broker = {'BTC-USD': 5_000.0, 'ZZTA': 1_000.0}     # a held crypto position, flag unset
    base, h0 = run(monkeypatch, crypto=False, broker=broker)
    shadow, h1 = run(monkeypatch, crypto=True, broker=broker)
    assert base and shadow == base                       # full order dicts, not just targets
    assert not any(o['ticker'] in CRYPTO_TKRS for o in shadow)
    gross = LAM * NAV
    assert abs(targets(shadow)['ZZTA'] - gross * 2.6 / 6.1) < 1e-6
    assert shadow_lines(h0) == [] and h0.written == []
    lines = shadow_lines(h1)
    assert len(lines) == 1 and h1.written == lines
    ln = lines[0]
    assert ln.startswith('crypto_sizing.shadow[CRISIS]: S_m=0.80 n=2 ')
    assert 'stable_excluded=1' in ln and 'dropped=1' in ln and "bench=['BTC-USD']" in ln
    assert 'gross=0 ' not in ln and 'funding_bind=unknown' in ln
    # shadow loads crypto inputs EXPLICITLY (crypto universe) while the equity call is untouched
    eq_calls = [c for c in h1.loader_calls if c['strategy_ids'] is None]
    cr_calls = [c for c in h1.loader_calls if c['strategy_ids'] is not None]
    assert len(eq_calls) == 1 and eq_calls[0]['tradable_classes'] is None
    assert len(cr_calls) == 1 and cr_calls[0]['tradable_classes'] == ('crypto',)


def test_pooled_shadow_block_failure_is_fail_open(monkeypatch):
    base, _ = run(monkeypatch, crypto=False)
    monkeypatch.setattr(_sizer, '_crypto_pooled_block',
                        lambda **k: (_ for _ in ()).throw(RuntimeError('boom')))
    out, h = run(monkeypatch, crypto=True)
    assert out == base and shadow_lines(h) == []


def test_pooled_apply_emits_crypto_targets(monkeypatch):
    orders, h = run(monkeypatch, pooled=True, gross_frac=10.0)
    gross = LAM * NAV
    # pooled normaliser: Σ|w| = 6.1 (equity, rule C shadow) + 1.0 BTC + 1.2 ETH (hurdled 2.0-0.8)
    G = 6.1 + 2.2
    e, c = eq_t(orders), cr_t(orders)
    assert abs(e['ZZTA'] - gross * 2.6 / G) < 1e-6 and abs(e['SPY'] - gross * 2.0 / G) < 1e-6
    assert set(c) == {'BTC-USD', 'ETH-USD'}                  # SOL hurdled out, USDC excluded
    assert abs(c['BTC-USD'] - gross * 1.0 / G) < 1e-6
    assert abs(c['ETH-USD'] - gross * 1.2 / G) < 1e-6
    assert len(shadow_lines(h, 'apply')) == 1 and shadow_lines(h) == []


def test_crypto_weights_read_crypto_regime_row(monkeypatch):
    orders, h = run(monkeypatch, pooled=True, s_m_cr=None, gross_frac=10.0)
    assert EQ_REGIME in h.load_current and CR_REGIME in h.load_current
    c = cr_t(orders)
    # S_cr_a's crypto row (2.0) sizes ETH, not its equity row (9.0); no hurdle (S_m None)
    assert abs(c['ETH-USD'] / c['BTC-USD'] - 2.0) < 1e-9
    assert abs(c['SOL-USD'] / c['BTC-USD'] - 0.5) < 1e-9
    cr_call = [x for x in h.loader_calls if x['strategy_ids'] is not None][0]
    assert cr_call['sids'] == {'S_beta_btc', 'S_cr_a', 'S_cr_b'}   # S_hi's crypto row ignored
    assert cr_call['strategy_ids'] == {'S_beta_btc', 'S_cr_a', 'S_cr_b'}


def test_crypto_min_acting_and_liquidity_from_crypto_params(monkeypatch):
    orders, h = run(monkeypatch, pooled=True, gross_frac=10.0, cr_params=_params(min_acting=2, liq=0.5))
    c = cr_t(orders)
    assert set(c) == {'BTC-USD'}          # ETH has one acting strategy < 2; the sleeve is exempt
    G = 6.1 + 1.0
    assert abs(c['BTC-USD'] - LAM * 0.5 * NAV * 1.0 / G) < 1e-6
    assert abs(eq_t(orders)['ZZTA'] - LAM * NAV * 2.6 / G) < 1e-6   # equity keeps its own liq/min_acting


# ── Review Focus 3 (per-class hurdle) ───────────────────────────────────────

def test_per_class_hurdle_uses_each_s_m(monkeypatch):
    orders, h = run(monkeypatch, pooled=True, bench_flag=True, s_m_eq=2.0, s_m_cr=0.8, gross_frac=10.0)
    e, c = eq_t(orders), cr_t(orders)
    G = (2.0 + 0.6) + (1.0 + 1.2)     # SPY + ZZTA(2.6-2.0) ; BTC + ETH(2.0-0.8)
    assert 'ZZTB' not in e and 'SOL-USD' not in c
    assert abs(e['ZZTA'] - LAM * NAV * 0.6 / G) < 1e-6
    assert abs(c['ETH-USD'] - LAM * NAV * 1.2 / G) < 1e-6


def test_negative_s_m_crypto_floors_hurdle_at_zero(monkeypatch):
    orders, h = run(monkeypatch, pooled=True, s_m_cr=-0.7, gross_frac=10.0)
    c = cr_t(orders)
    assert abs(c['ETH-USD'] / c['BTC-USD'] - 2.0) < 1e-9     # alts keep raw conviction, never boosted
    assert abs(c['SOL-USD'] / c['BTC-USD'] - 0.5) < 1e-9
    assert all(v >= 0 for v in c.values())
    assert 'S_m=-0.70' in shadow_lines(h, 'apply')[0]


# ── caps ────────────────────────────────────────────────────────────────────

def test_crypto_gross_cap_and_bench_cap(monkeypatch):
    orders, h = run(monkeypatch, pooled=True, btc_frac=0.10, gross_frac=0.30)
    c = cr_t(orders)
    G = 6.1 + 2.2
    eth_raw = LAM * NAV * 1.2 / G                    # 28.9k
    btc = 0.10 * NAV                                 # 24.1k raw -> 10k by the BTC D-4' clamp
    k = (0.30 * NAV) / (btc + eth_raw)               # gross cap pro-rata after the clamp
    assert abs(c['BTC-USD'] - btc * k) < 1e-6 and abs(c['ETH-USD'] - eth_raw * k) < 1e-6
    assert abs(sum(abs(v) for v in c.values()) - 0.30 * NAV) < 1e-6
    ln = shadow_lines(h, 'apply')[0]
    assert 'cap_shaved=%.0f' % (btc + eth_raw - 0.30 * NAV) in ln


# ── Review Focus 5 ──────────────────────────────────────────────────────────

def test_hold_flat_zeroes_crypto_targets_only(monkeypatch):
    broker = {'BTC-USD': 5_000.0}
    ref, _ = run(monkeypatch, pooled=True, broker=broker, gross_frac=10.0)
    held, h = run(monkeypatch, pooled=True, broker=broker, gross_frac=10.0,
                  hold={'sleeve': {'since': '2026-11-10T00:00:00Z', 'reason': 'T4a'}, 'tickers': {}})
    assert eq_t(held) == eq_t(ref)                                  # equities unaffected
    cr = [o for o in held if o['ticker'] in CRYPTO_TKRS]
    assert all(o['target_usd'] == 0.0 for o in cr)                  # sleeve included
    assert any(o['ticker'] == 'BTC-USD' for o in cr)                # held BTC is brought to 0
    assert not any(o['ticker'] == 'ETH-USD' and o['target_usd'] for o in held)
    assert 'hold=1' in shadow_lines(h, 'apply')[0]


# ── Review Focus 4 ──────────────────────────────────────────────────────────

def test_funding_pass_scales_crypto_only(monkeypatch):
    ref, _ = run(monkeypatch, pooled=True, gross_frac=10.0, btc_frac=10.0)
    bound, h = run(monkeypatch, pooled=True, gross_frac=10.0, btc_frac=10.0, nmbp=20_000.0)
    assert eq_t(bound) == eq_t(ref)
    c0, c1 = cr_t(ref), cr_t(bound)
    g0 = sum(abs(v) for v in c0.values())
    assert abs(sum(abs(v) for v in c1.values()) - 20_000.0) < 1e-6
    for t in c0:
        assert abs(c1[t] - c0[t] * 20_000.0 / g0) < 1e-6
    assert 'funding_bind=%.0f' % (g0 - 20_000.0) in shadow_lines(h, 'apply')[0]


# ── zero-conviction flatten per class ───────────────────────────────────────

def test_zero_conviction_flatten_per_class(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    monkeypatch.setenv('OPENCLAW_FLATTEN_ON_ZERO_CONVICTION', '1')
    monkeypatch.delenv('OPENCLAW_INTRADAY_REDEPLOY', raising=False)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd',
                        lambda: {'AAPL': 1_000.0, 'BTC-USD': 500.0})
    monkeypatch.setattr(_sizer, '_post_flatten_alert', lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(_sizer, '_emit_orders_from_targets',
                        lambda *a, broker=None, **k: seen.append(dict(broker)) or ['x'])
    active = [{'strategy_id': 's', 'ticker': 'T%d' % i} for i in range(_sizer.SIGNAL_SET_MIN_FLOOR)]
    args = (active, EQ_REGIME, {}, NAV, None, None, {}, {}, {}, _account(), [])
    monkeypatch.delenv(POOL_ENV, raising=False)
    assert _sizer._maybe_flatten_zero_conviction(*args, instrument_class='equity') == ['x']
    assert seen[-1] == {'AAPL': 1_000.0}
    assert _sizer._maybe_flatten_zero_conviction(*args, instrument_class='crypto') is None  # legacy lane owns it
    assert _sizer._maybe_flatten_zero_conviction(*args) == ['x']                           # None = today
    assert seen[-1] == {'AAPL': 1_000.0, 'BTC-USD': 500.0}
    monkeypatch.setenv(POOL_ENV, '1')
    assert _sizer._maybe_flatten_zero_conviction(*args, instrument_class='crypto') == ['x']
    assert seen[-1] == {'BTC-USD': 500.0}


def test_equity_empty_apply_emits_crypto_and_holds_equity(monkeypatch):
    # the lone equity name fails the EQUITY acting gate (min_acting=2); crypto keeps min_acting=1
    broker = {'ZZTB': 2_000.0, 'BTC-USD': 1_000.0}
    orders, h = run(monkeypatch, pooled=True, gross_frac=10.0, eq_params=_params(min_acting=2),
                    eq_carried=[_carried('S_lo', 'ZZTB')], broker=broker)
    assert h.flatten == ['equity']                                   # per-class equity flatten asked
    assert not any(o['ticker'] == 'ZZTB' for o in orders)            # equity HELD (flatten not armed)
    c = cr_t(orders)
    assert set(c) == {'BTC-USD', 'ETH-USD'}
    G = 2.2                                                          # crypto absorbs the whole normaliser
    assert abs(c['ETH-USD'] - LAM * NAV * 1.2 / G) < 1e-6
    assert abs(c['BTC-USD'] - 0.25 * NAV) < 1e-6                     # BTC D-4' clamp (raw 90.9k)


def test_shadow_equity_empty_still_logs_crypto_line(monkeypatch):
    kw = dict(eq_params=_params(min_acting=2), eq_carried=[_carried('S_lo', 'ZZTB')],
              broker={'ZZTB': 2_000.0, 'BTC-USD': 1_000.0})
    base, _ = run(monkeypatch, crypto=False, **kw)
    out, h = run(monkeypatch, crypto=True, **kw)
    assert out == base == []
    assert len(shadow_lines(h)) == 1
    assert h.flatten == [None]                                        # today's whole-book call, unchanged
    assert 'n=2' in shadow_lines(h)[0]


# ── loader scoping ─────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, log): self.log = log
    def execute(self, sql, args=None): self.log.append((sql, args))
    def fetchall(self): return []
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Conn:
    def __init__(self, log): self.log = log
    def cursor(self, *a, **k): return _Cur(self.log)
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_carried_loader_strategy_ids_scope_query_and_gap_audit(monkeypatch):
    import psycopg2
    log, classes = [], []
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake/fake')
    monkeypatch.setattr(psycopg2, 'connect', lambda *a, **k: _Conn(log))
    def classes_append(c):
        classes.append(c); return set()
    monkeypatch.setattr(_sizer, 'load_tradable_symbols', lambda c, classes=None: classes_append(classes))
    w = {'S_beta_btc': 1.0, 'S_cr_a': 2.0, 'S_hi': 3.0}
    _sizer._load_approved_carried_signals(w, {}, regime_state=CR_REGIME,
                                          strategy_ids={'S_beta_btc', 'S_cr_a', 'S_cr_x'},
                                          tradable_classes=('crypto',))
    main_sql, main_args = log[0]
    assert sorted(main_args[2]) == ['S_beta_btc', 'S_cr_a']          # S_hi filtered out
    gap_sql, gap_args = log[1]
    assert 'AND es.strategy_id = ANY(%s)' in gap_sql
    assert gap_args[-1] == ['S_beta_btc', 'S_cr_a', 'S_cr_x']
    assert classes == [('crypto',)]
    log.clear(); classes.clear()
    _sizer._load_approved_carried_signals(w, {}, regime_state=EQ_REGIME)   # equity: unscoped, as today
    assert 'AND es.strategy_id = ANY(%s)' not in log[1][0] and len(log[1][1]) == 5
    assert classes == [('us_equity',)]


# ── production same-day lane (active-window loader) ─────────────────────────

def test_sameday_lane_shadow_identical_and_apply_emits(monkeypatch):
    broker = {'BTC-USD': 5_000.0, 'ZZTA': 1_000.0}
    base, _ = run(monkeypatch, crypto=False, broker=broker, lane='sameday')
    shadow, h = run(monkeypatch, crypto=True, broker=broker, lane='sameday')
    assert base and shadow == base
    assert not any(o['ticker'] in CRYPTO_TKRS for o in shadow)
    assert all(c.get('lane') == 'window' for c in h.loader_calls)
    cr_calls = [c for c in h.loader_calls if c['strategy_ids'] is not None]
    assert len(cr_calls) == 1 and cr_calls[0]['tradable_classes'] == ('crypto',)
    assert cr_calls[0]['regime_state'] is None          # rows carry the equity stamp (engine.write_signals)
    assert len(shadow_lines(h)) == 1 and 'n=2' in shadow_lines(h)[0]
    applied, h2 = run(monkeypatch, crypto=True, pooled=True, gross_frac=10.0, broker=broker, lane='sameday')
    assert set(cr_t(applied)) == {'BTC-USD', 'ETH-USD'}
    assert len(shadow_lines(h2, 'apply')) == 1


# ── fix round 1 ─────────────────────────────────────────────────────────────

def test_funding_pass_constrains_net_new_buying_only(monkeypatch):
    # held BTC 10k; pooled BTC target clamped to 25k by the D-4' cap (raw 24.1k ⇒ use btc_frac 0.12)
    broker = {'BTC-USD': 10_000.0}
    ref, _ = run(monkeypatch, pooled=True, gross_frac=10.0, btc_frac=0.12, broker=broker)
    c0 = cr_t(ref)                                     # BTC 12k (clamped), ETH ~28.9k new
    bound, h = run(monkeypatch, pooled=True, gross_frac=10.0, btc_frac=0.12, broker=broker, nmbp=1_000.0)
    c1 = cr_t(bound)
    demand = (c0['BTC-USD'] - 10_000.0) + c0['ETH-USD']
    k = 1_000.0 / demand
    assert abs(c1['BTC-USD'] - (10_000.0 + (c0['BTC-USD'] - 10_000.0) * k)) < 1e-6
    assert abs(c1['ETH-USD'] - c0['ETH-USD'] * k) < 1e-6
    assert c1['BTC-USD'] > 10_000.0                    # never sells a position the book grows
    assert eq_t(bound) == eq_t(ref)
    assert 'funding_bind=%.0f' % (demand - 1_000.0) in shadow_lines(h, 'apply')[0]


def test_crypto_targets_long_only(monkeypatch):
    cr = [_carried('S_beta_btc', 'BTC-USD'), _carried('S_cr_a', 'ETH-USD'),
          _carried('S_cr_b', 'SOL-USD', direction='SHORT')]
    orders, h = run(monkeypatch, pooled=True, s_m_cr=None, gross_frac=10.0, cr_carried=cr)
    c = cr_t(orders)
    assert c.get('SOL-USD', 0.0) == 0.0 and all(v >= 0 for v in c.values())
    assert 'short_clamped=1' in shadow_lines(h, 'apply')[0]
    # equity shorts untouched: an equity SHORT keeps its sign
    eqc = [_carried('S_beta_spy', 'SPY'), _carried('S_hi', 'ZZTA', direction='SHORT')]
    orders2, _ = run(monkeypatch, pooled=True, s_m_cr=None, gross_frac=10.0, cr_carried=cr, eq_carried=eqc)
    assert eq_t(orders2)['ZZTA'] < 0


@pytest.mark.parametrize('case', ['no_weights', 'no_approved', 'sameday_empty_handoff'])
def test_equity_early_returns_still_run_the_crypto_block(monkeypatch, case):
    kw = {'no_weights': dict(eq_rows=[]),
          'no_approved': dict(eq_carried=[]),
          'sameday_empty_handoff': dict(lane='sameday', sigs=[])}[case]
    base, h0 = run(monkeypatch, crypto=False, **kw)
    assert base == [] and shadow_lines(h0) == []            # crypto None ⇒ identical early return
    shadow, h1 = run(monkeypatch, crypto=True, **kw)
    assert shadow == [] and len(shadow_lines(h1)) == 1 and 'n=2' in shadow_lines(h1)[0]
    applied, h2 = run(monkeypatch, crypto=True, pooled=True, gross_frac=10.0,
                      broker={'ZZTA': 1_000.0}, **kw)
    assert set(cr_t(applied)) == {'BTC-USD', 'ETH-USD'}
    assert not any(o['ticker'] == 'ZZTA' for o in applied)  # equities HELD on a data-absent day
    assert len(shadow_lines(h2, 'apply')) == 1


class _LCur:
    def __init__(self, regime_row): self.q, self.regime_row = '', regime_row
    def execute(self, sql, args=None): self.q = sql
    def fetchone(self): return self.regime_row if 'market_regime' in self.q else None
    def fetchall(self): return []
    def close(self): pass


class _LConn:
    def __init__(self, regime_row): self.regime_row = regime_row
    def cursor(self, *a, **k): return _LCur(self.regime_row)
    def close(self): pass
    def rollback(self): pass


def _live_env(monkeypatch, *, account='ok', regime_row=None, crypto=(CR_REGIME, {'liquidity_param': 1.0}),
              sizer=None):
    """Same-day live wrapper on an EMPTY handoff; returns (drv, size calls, posts)."""
    import execution.regime_blended_sizer_live as drv
    import execution.pooled_sizing as _ps
    import execution.alpaca_trader as _at
    import execution.pipeline_orchestrator as _po
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://test/test')
    monkeypatch.delenv('OPENCLAW_EOD_SIGNAL_REGISTER', raising=False)
    monkeypatch.delenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', raising=False)
    monkeypatch.setattr(drv.psycopg2, 'connect', lambda *a, **k: _LConn(regime_row))
    monkeypatch.setattr(_at, '_alpaca_session', lambda: object(), raising=False)
    acct = {'equity': NAV, 'regt_buying_power': 2 * NAV, 'long_market_value': 0.0, 'cash': NAV}
    if account == 'violation':
        acct['trading_blocked'] = True

    def _fetch(sess):
        if account == 'fail':
            raise RuntimeError('alpaca down')
        return dict(acct)
    monkeypatch.setattr(_at, '_fetch_account_state', _fetch, raising=False)
    monkeypatch.setattr(drv, '_account_state_violations',
                        lambda a, params=None: ['trading_blocked=true'] if a.get('trading_blocked') else [])
    posts = []
    monkeypatch.setattr(_po, 'post_channel', lambda *a, **k: posts.append(a))
    monkeypatch.setattr(drv, 'read_handoff', lambda *a, **k: {'regime': {'state': 'LOW_VOL'}, 'signals': []})
    monkeypatch.setattr(sys, 'argv', ['regime_blended_sizer_live', '--date', '2026-11-10'])
    calls = []
    monkeypatch.setattr(drv, 'size_positions', sizer or (lambda **k: calls.append(k) or []))
    monkeypatch.setattr(_ps, 'load_crypto_regime_and_params', lambda conn=None: crypto)
    return drv, calls, posts


def test_live_empty_handoff_without_crypto_regime_is_todays_return(monkeypatch):
    drv, calls, posts = _live_env(monkeypatch, crypto=(None, None))
    monkeypatch.delenv(POOL_ENV, raising=False)
    assert drv.main() == 0 and calls == [] and posts == []


def test_live_empty_handoff_shadow_sizes_crypto_only(monkeypatch, capsys):
    monkeypatch.delenv(POOL_ENV, raising=False)
    drv, calls, posts = _live_env(monkeypatch)         # NO market_regime row: pre-change this day exited 0
    assert drv.main() == 0 and posts == []
    assert len(calls) == 1 and calls[0]['signals'] == [] and calls[0]['crypto_regime_state'] == CR_REGIME


@pytest.mark.parametrize('account,reason', [('fail', 'account_fetch'), ('violation', 'account_state')])
def test_live_empty_handoff_shadow_failures_are_silent(monkeypatch, capsys, account, reason):
    monkeypatch.delenv(POOL_ENV, raising=False)
    drv, calls, posts = _live_env(monkeypatch, account=account)
    assert drv.main() == 0
    assert posts == [] and calls == []
    assert 'crypto_sizing.shadow: skipped (reason=%s)' % reason in capsys.readouterr().out


def test_live_empty_handoff_shadow_sizer_error_is_silent(monkeypatch, capsys):
    monkeypatch.delenv(POOL_ENV, raising=False)
    def boom(**k): raise RuntimeError('x')
    drv, _, posts = _live_env(monkeypatch, sizer=boom)
    assert drv.main() == 0 and posts == []
    assert 'crypto_sizing.shadow: skipped (reason=error:RuntimeError)' in capsys.readouterr().out


def test_live_empty_handoff_apply_runs_full_path_with_alerts(monkeypatch):
    monkeypatch.setenv(POOL_ENV, '1')
    drv, calls, posts = _live_env(monkeypatch, regime_row={'state': 'LOW_VOL'})
    assert drv.main() == 0 and len(calls) == 1 and calls[0]['crypto_regime_state'] == CR_REGIME
    drv, calls, posts = _live_env(monkeypatch, regime_row={'state': 'LOW_VOL'}, account='fail')
    drv.main()
    assert calls == [] and len(posts) == 1 and 'ABORT' in posts[0][1]   # crypto orders need the account


def test_empty_signals_with_crypto_regime_never_consumes_force_fire_key(monkeypatch):
    fired = []
    monkeypatch.delenv('OPENCLAW_EOD_SIGNAL_REGISTER', raising=False)
    monkeypatch.delenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', raising=False)
    monkeypatch.delenv(POOL_ENV, raising=False)
    monkeypatch.setattr(_sizer, '_check_force_fire_flag', lambda: fired.append(1) or False)
    alone = []
    monkeypatch.setattr(_sizer, '_crypto_alone_orders', lambda *a, **k: alone.append(a) or [])
    out = _sizer.size_positions(signals=[], account_state=_account(), regime={'state': EQ_REGIME},
                                run_date=date(2026, 11, 10), strategy_state={}, regime_params=_params(),
                                crypto_regime_state=CR_REGIME, crypto_params=_params())
    assert out == [] and fired == [] and len(alone) == 1
    # no crypto regime ⇒ today's behaviour (the check runs, then the empty return)
    out = _sizer.size_positions(signals=[], account_state=_account(), regime={'state': EQ_REGIME},
                                run_date=date(2026, 11, 10), strategy_state={}, regime_params=_params())
    assert out == [] and fired == [1] and len(alone) == 1


# ── fix round 2 ─────────────────────────────────────────────────────────────

BROKER_MIX = {'BTC-USD': 5_000.0, 'ETH-USD': 2_000.0, 'ZZTA': 1_000.0}


def test_flag_on_block_failure_holds_crypto(monkeypatch, caplog):
    import logging
    base, _ = run(monkeypatch, crypto=False, broker=BROKER_MIX)          # flag unset, equity-only
    monkeypatch.setattr(_sizer, '_crypto_pooled_block',
                        lambda **k: (_ for _ in ()).throw(RuntimeError('boom')))
    with caplog.at_level(logging.WARNING, logger='execution.regime_blended_sizer'):
        out, _ = run(monkeypatch, crypto=True, pooled=True, broker=BROKER_MIX)
    assert not any(o['ticker'] in CRYPTO_TKRS for o in out)                # no orphan close of crypto
    assert out == base
    assert 'crypto_sizing: block unavailable (reason=block_failed) — crypto HELD' in caplog.text


def test_flag_on_without_crypto_regime_holds_crypto(monkeypatch, caplog):
    import logging
    base, _ = run(monkeypatch, crypto=False, broker=BROKER_MIX)
    with caplog.at_level(logging.WARNING, logger='execution.regime_blended_sizer'):
        out, _ = run(monkeypatch, crypto=False, pooled=True, broker=BROKER_MIX)
    assert not any(o['ticker'] in CRYPTO_TKRS for o in out) and out == base
    assert 'reason=no_crypto_regime' in caplog.text


def test_flag_on_without_crypto_regime_flatten_is_equity_scoped(monkeypatch):
    kw = dict(eq_params=_params(min_acting=2), eq_carried=[_carried('S_lo', 'ZZTB')], broker=BROKER_MIX)
    _, h0 = run(monkeypatch, crypto=False, **kw)
    assert h0.flatten == [None]                                           # flag unset: unchanged call
    _, h1 = run(monkeypatch, crypto=False, pooled=True, **kw)
    assert h1.flatten == ['equity']


def test_stable_excluded_counts_tickers(monkeypatch):
    cr = CR_CARRIED + [_carried('S_cr_b', 'USDC-USD'), _carried('S_beta_btc', 'USDT-USD')]
    _, h = run(monkeypatch, cr_carried=cr)
    assert 'stable_excluded=2' in shadow_lines(h)[0]                      # USDC (2 rows) + USDT


def test_shadow_line_eq_scale_ratio(monkeypatch):
    _, h = run(monkeypatch)
    assert 'eq_scale_ratio=%.4f' % (6.1 / (6.1 + 2.2)) in shadow_lines(h)[0]


def test_pooled_attribution_uses_size_scalar_adjusted_weights(monkeypatch):
    orders, _ = run(monkeypatch, pooled=True, s_m_cr=None, gross_frac=10.0,
                    scalars={CR_REGIME: {'S_cr_a': 0.5}})
    eth = [o for o in orders if o['ticker'] == 'ETH-USD'][0]
    contrib = {c['strategy_id']: c['contribution'] for c in eth['contributions']}
    assert abs(contrib['S_cr_a'] - 1.0) < 1e-9                            # 2.0 × 0.5, what sized it


# ── final-review fix wave ───────────────────────────────────────────────────

def test_account_breaker_halt_passes_crypto_targets_through(monkeypatch):
    out = _sizer._apply_account_breaker_gate({'BTC-USD': 1_000.0, 'ZZTA': 500.0}, {}, halted=True)
    assert out['BTC-USD'] == 1_000.0                  # crypto: own Phase 2 breaker
    assert 'ZZTA' not in out                          # equity open still refused


def test_macro_event_gate_skips_crypto(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    out = _sizer._apply_macro_event_gate({'BTC-USD': 1_000.0, 'ZZTA': 500.0}, {},
                                         session=date(2026, 11, 12), events='FOMC_DECISION')
    assert out.get('BTC-USD') == 1_000.0 and 'ZZTA' not in out


def test_entry_hygiene_skips_crypto(monkeypatch):
    monkeypatch.setenv('OPENCLAW_ENTRY_HYGIENE', '1')
    out = _sizer._apply_entry_hygiene_gate(
        {'BTC-USD': 1_000.0, 'ZZTA': 500.0}, {}, stopouts={}, liq={}, params={},
        premarket_vetoes={'BTC-USD', 'ZZTA'}, risk_exits={},
        ownership_blocked=set(), ownership_cycle_date=None, ownership_enforcing=False)
    assert out.get('BTC-USD') == 1_000.0 and not out.get('ZZTA')


def test_btc_clamped_by_ticker_without_the_sleeve(monkeypatch):
    cr = [_carried('S_cr_a', 'BTC-USD')]            # alpha-only BTC, no S_beta_btc contributor
    orders, h = run(monkeypatch, pooled=True, s_m_cr=None, gross_frac=10.0, cr_carried=cr)
    raw = LAM * NAV * 2.0 / (6.1 + 2.0)                # 49.4k ≈ 0.49·NAV
    assert abs(cr_t(orders)['BTC-USD'] - 0.25 * NAV) < 1e-6
    assert 'btc_clamped_usd=%.0f' % (raw - 0.25 * NAV) in shadow_lines(h, 'apply')[0]


class _PCur:
    def __init__(self): self.q = ''
    def execute(self, sql, args=None): self.q = sql
    def fetchone(self):
        if 'corr_cumsharpe_log_until' in self.q:
            return ('2026-09-12',)                     # gate EXPIRED
        if 'botjohn-log' in self.q:
            return ('https://discord.example/webhook',)
        return None
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _PConn:
    def cursor(self, *a, **k): return _PCur()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_crypto_line_posts_with_the_corr_cumsharpe_gate_expired(monkeypatch):
    import psycopg2
    import urllib.request as _ur
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake/fake')
    monkeypatch.setattr(psycopg2, 'connect', lambda *a, **k: _PConn())
    sent = []

    class _Resp:
        def read(self): return b''
    monkeypatch.setattr(_ur, 'urlopen', lambda req, timeout=None: sent.append(req) or _Resp())
    _sizer._post_corr_cumsharpe_log('corr_cumsharpe.live[X]: ...')
    assert sent == []                                  # the equity diag line is gated off
    _sizer._post_crypto_sizing_line('crypto_sizing.shadow[CRISIS]: S_m=0.10 n=1')
    assert len(sent) == 1 and b'crypto_sizing.shadow[CRISIS]' in sent[0].data
    assert sent[0].get_header('User-agent') == 'fundjohn-sizer/1.0'
