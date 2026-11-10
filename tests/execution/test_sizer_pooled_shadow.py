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
        eq_carried=None, cr_params=None, eq_params=None, lane='eod', h=None):
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
        return [dict(s) for s in CR_CARRIED if s['strategy_id'] in strategy_ids]
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
        return [dict(r) for r in (EQ_ROWS if regime_state == EQ_REGIME else CR_ROWS)]

    def _s_m(regime_state, run_date, *, benchmark='SPY', horizon=None, **_k):
        return s_m_cr if benchmark == 'BTC-USD' else s_m_eq

    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: dict(broker or {}))
    monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', lambda t, *a, **k: t)
    monkeypatch.setattr(_sizer, '_apply_benchmark_corr_removal', lambda t, b, nav: t)
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: h.lines.append(line))
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
    monkeypatch.setattr(_rpr, 'size_scalars_for_regime', lambda r: {})
    import execution.strategy_similarity as _ss
    monkeypatch.setattr(_ss, 'load_groups', lambda r: None)
    kw = {}
    if crypto:
        kw = {'crypto_regime_state': CR_REGIME, 'crypto_params': cr_params or _params()}
    with _mock.patch('execution.strategy_weights.load_current', side_effect=_load_current), \
         _mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids',
                     return_value={'S_beta_spy', 'S_beta_btc'}), \
         _mock.patch('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', side_effect=_s_m):
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
