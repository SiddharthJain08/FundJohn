"""Spec §2.4 exemption (i): a ticker with a benchmark-sleeve contributor passes
min_acting_strategies even when it is the only strategy acting on it."""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402

NAV, LAM = 100_000.0, 2.0


def _account():
    return {'equity': NAV, 'regt_buying_power': 2 * NAV, 'long_market_value': 0, 'cash': NAV}


def _params(min_acting=2):
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 1, 'min_signal_notional_pct': 0.00001,
            'position_circuit_breaker_pct': 0.02, 'min_cumulative_sharpe': 3.0,
            'min_acting_strategies': min_acting}


def _carried(sid, ticker, direction='LONG'):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction,
            'signal_date': date(2026, 8, 28), 'entry_price': 100.0, 'stop_loss': 95.0,
            'target_1': 110.0, 'target_2': 120.0, 'signal_params': {}}


def _row(sid, eff):
    return {'strategy_id': sid, 'daily_weight': eff, 'effective_sharpe': eff, 'cadence_days': 21.0, 'bt_n': 600}


def run(monkeypatch, rows, carried, bench_ids, params=None):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    for g in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
              'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE',
              'OPENCLAW_BENCH_RELATIVE_SIZING', 'OPENCLAW_TRADE_WEIGHT_FACTOR'):
        monkeypatch.delenv(g, raising=False)
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda weight_by_strat, cadence_by_strat=None, **_kw: list(carried))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', lambda t, *a, **k: t)
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: None)
    monkeypatch.setattr(_sizer, '_maybe_flatten_zero_conviction', lambda *a, **k: None)
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(rows)), \
         _mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value=set(bench_ids)), \
         _mock.patch('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', return_value=None):
        return _sizer.size_positions(signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
                                     run_date=date(2026, 8, 29), strategy_state={},
                                     regime_params=params or _params(), confirmer=lambda p: {})


def targets(orders):
    return {o['ticker']: o['target_usd'] for o in orders if o['action'] not in ('close_long', 'close_short')}


def test_lone_benchmark_ticker_passes_min_acting_2(monkeypatch):
    t = targets(run(monkeypatch, [_row('S_beta_spy', 2.0)], [_carried('S_beta_spy', 'SPY')], {'S_beta_spy'}))
    assert 'SPY' in t and t['SPY'] > 0


def test_lone_alpha_ticker_still_gated(monkeypatch):
    t = targets(run(monkeypatch, [_row('S_x', 2.0)], [_carried('S_x', 'ZZTA')], {'S_beta_spy'}))
    assert 'ZZTA' not in t


def test_registry_flag_is_the_switch(monkeypatch):
    # Same book, but the registry says nobody is a benchmark sleeve -> SPY gated like any ticker.
    t = targets(run(monkeypatch, [_row('S_beta_spy', 2.0)], [_carried('S_beta_spy', 'SPY')], set()))
    assert 'SPY' not in t


def test_cancelled_benchmark_ticker_is_not_exempt(monkeypatch):
    # Equal-Sharpe long/short on the same ticker gives S_net = 0 -> net sign 0
    # -> acting 0 -> gated, and the benchmark exemption must not resurrect it
    # (spec §2.4 i requires the benchmark contributor to act IN the net direction).
    t = targets(run(monkeypatch,
                     [_row('S_beta_spy', 2.0), _row('S_x', 2.0)],
                     [_carried('S_beta_spy', 'SPY'), _carried('S_x', 'SPY', direction='SHORT')],
                     {'S_beta_spy'}))
    assert 'SPY' not in t


def run_rulec(monkeypatch, rows, carried, bench_ids, s_m=0.8, params=None):
    """Same harness but with rule C + beta budget LIVE (flags on, S_m given),
    the correlation-removal step stubbed to identity (it reads DB + the price
    panel), and the benchmark Sharpe patched."""
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    monkeypatch.setenv('OPENCLAW_BENCH_RELATIVE_SIZING', '1')
    monkeypatch.setenv('OPENCLAW_BENCH_BETA_BUDGET', '1')
    for g in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
              'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE',
              'OPENCLAW_TRADE_WEIGHT_FACTOR'):
        monkeypatch.delenv(g, raising=False)
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda weight_by_strat, cadence_by_strat=None, **_kw: list(carried))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', lambda t, *a, **k: t)
    monkeypatch.setattr(_sizer, '_apply_benchmark_corr_removal', lambda t, *a, **k: t)
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: None)
    monkeypatch.setattr(_sizer, '_maybe_flatten_zero_conviction', lambda *a, **k: None)
    import execution.benchmark_sizing as _bsz
    monkeypatch.setattr(_bsz, 'benchmark_max_nav_frac', lambda default=1.0, conn=None: 1.0)
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(rows)), \
         _mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value=set(bench_ids)), \
         _mock.patch('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', return_value=s_m):
        return _sizer.size_positions(signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
                                     run_date=date(2026, 8, 29), strategy_state={},
                                     regime_params=params or _params(), confirmer=lambda p: {})


def test_gate_dropped_ticker_never_feeds_the_beta_pool(monkeypatch):
    """Operator directive 2026-09-08: NAV distributes over the SURVIVORS.
    Adding a ticker that the acting gate drops must change NOTHING — not the
    benchmark's dollars (its base must not be swept into the pool), not the
    survivors' dollars (it must not dilute the normalizer)."""
    rows = [_row('S_beta_spy', 2.0), _row('S_a', 2.0), _row('S_b', 1.5), _row('S_lone', 3.0)]
    survivors_book = [_carried('S_beta_spy', 'SPY'),
                      _carried('S_a', 'AAA'), _carried('S_b', 'AAA')]   # AAA: 2 acting
    with_gated = survivors_book + [_carried('S_lone', 'ZZTA')]          # ZZTA: 1 acting -> gated
    t_base = targets(run_rulec(monkeypatch, rows, survivors_book, {'S_beta_spy'}))
    t_plus = targets(run_rulec(monkeypatch, rows, with_gated, {'S_beta_spy'}))
    assert 'ZZTA' not in t_plus
    assert abs(t_plus['SPY'] - t_base['SPY']) < 1.0, \
        f"gate-dropped ticker leaked into the beta pool: SPY {t_base['SPY']} -> {t_plus['SPY']}"
    assert abs(t_plus['AAA'] - t_base['AAA']) < 1.0, \
        f"gate-dropped ticker diluted the normalizer: AAA {t_base['AAA']} -> {t_plus['AAA']}"


def test_survivors_and_bench_consume_the_full_gross_budget(monkeypatch):
    """Post-move the λ·NAV budget lands on survivors + benchmark (per-ticker
    caps aside), instead of being hollowed out by gate-dropped weight."""
    rows = [_row('S_beta_spy', 2.0), _row('S_a', 2.0), _row('S_b', 1.5), _row('S_lone', 3.0)]
    book = [_carried('S_beta_spy', 'SPY'), _carried('S_a', 'AAA'), _carried('S_b', 'AAA'),
            _carried('S_lone', 'ZZTA')]
    t = targets(run_rulec(monkeypatch, rows, book, {'S_beta_spy'}))
    gross = sum(abs(v) for v in t.values())
    # The per-ticker conviction cap shaves without redistributing, so the
    # gross lands somewhat below λ·NAV (84.5% in this fixture). The defect
    # this pins against left only ~62% deployed (2026-09-08 live: 124/200).
    assert gross > 0.8 * LAM * NAV, \
        f'gross {gross:.0f} should approach lam*NAV={LAM * NAV:.0f} over the survivors'
