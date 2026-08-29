"""Spec D6 / §2.6: no beta cap. Benchmark tickers skip the per-ticker
conviction cap and never enter the asset-correlation cluster filter."""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402
from execution import benchmark_sizing as bz      # noqa: E402

NAV, LAM, CAP = 100_000.0, 2.0, _sizer.PER_TICKER_CAP_SHARPE_FRAC


def _account():
    return {'equity': NAV, 'regt_buying_power': 2 * NAV, 'long_market_value': 0, 'cash': NAV}


def _params():
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 1, 'min_signal_notional_pct': 0.00001,
            'position_circuit_breaker_pct': 0.02, 'min_cumulative_sharpe': 3.0, 'min_acting_strategies': 1}


def _carried(sid, ticker):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': 'LONG', 'signal_date': date(2026, 8, 28),
            'entry_price': 100.0, 'stop_loss': 95.0, 'target_1': 110.0, 'target_2': 120.0, 'signal_params': {}}


def _row(sid, eff):
    return {'strategy_id': sid, 'daily_weight': eff, 'effective_sharpe': eff, 'cadence_days': 21.0, 'bt_n': 600}


def run(monkeypatch, rows, carried, corr_spy=None, bench_flag='1'):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    # B3 (final fix wave, 2026-08-29): the cap/cluster-cap exemptions are
    # gated on this flag; bench_flag=None exercises them OFF (test below).
    if bench_flag is None:
        monkeypatch.delenv(bz.BENCH_SIZING_ENV, raising=False)
    else:
        monkeypatch.setenv(bz.BENCH_SIZING_ENV, bench_flag)
    for g in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
              'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE', 'OPENCLAW_TRADE_WEIGHT_FACTOR',
              'OPENCLAW_INTRADAY_REDEPLOY'):
        monkeypatch.delenv(g, raising=False)
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda weight_by_strat, cadence_by_strat=None, **_kw: list(carried))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: None)
    monkeypatch.setattr(_sizer, '_maybe_flatten_zero_conviction', lambda *a, **k: None)
    if corr_spy is not None:
        monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', corr_spy)
    else:
        monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', lambda t, *a, **k: t)
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(rows)), \
         _mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value={'S_beta_spy'}), \
         _mock.patch('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', return_value=2.0):
        return _sizer.size_positions(signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
                                     run_date=date(2026, 8, 29), strategy_state={},
                                     regime_params=_params(), confirmer=lambda p: {})


def targets(orders):
    return {o['ticker']: o['target_usd'] for o in orders if o['action'] not in ('close_long', 'close_short')}


def test_benchmark_ticker_not_capped_alpha_still_capped(monkeypatch):
    # SPY 2.0 (exempt) + ZZTA 2.6 -> hurdled 0.6. gross 2.6 -> SPY raw share 153.8k.
    t = targets(run(monkeypatch, [_row('S_beta_spy', 2.0), _row('S_hi', 2.6)],
                    [_carried('S_beta_spy', 'SPY'), _carried('S_hi', 'ZZTA')]))
    assert abs(t['SPY'] - LAM * NAV * 2.0 / 2.6) < 1e-6          # uncapped (cap would be 60k)
    assert t['ZZTA'] <= CAP * (2.6 + 1.0) * LAM * NAV + 1e-6    # alpha cap formula still reads raw S_adj


def test_cluster_cap_receives_exclude_set(monkeypatch):
    seen = {}
    def spy(target_usd, conviction, nav, lam=1.0, exclude=None):
        seen['exclude'] = set(exclude or ()); return target_usd
    run(monkeypatch, [_row('S_beta_spy', 2.0), _row('S_hi', 2.6)],
        [_carried('S_beta_spy', 'SPY'), _carried('S_hi', 'ZZTA')], corr_spy=spy)
    assert seen['exclude'] == {'SPY'}


def test_cap_exemptions_inert_with_flag_off(monkeypatch):
    # B3: OPENCLAW_BENCH_RELATIVE_SIZING unset -> _bench_exempt is empty even
    # though SPY is still a registered, net-direction-qualified benchmark
    # ticker (bench_sleeve mock unchanged) -> SPY IS capped like any alpha
    # ticker, and the cluster cap receives an empty exclude set.
    seen = {}
    def spy(target_usd, conviction, nav, lam=1.0, exclude=None):
        seen['exclude'] = set(exclude or ()); return target_usd
    t = targets(run(monkeypatch, [_row('S_beta_spy', 2.0), _row('S_hi', 2.6)],
                    [_carried('S_beta_spy', 'SPY'), _carried('S_hi', 'ZZTA')],
                    corr_spy=spy, bench_flag=None))
    # No hurdle applied either (flag off): raw S_adj shares 2.0:2.6, gross 4.6.
    # Raw SPY share = LAM*NAV*2.0/4.6 ≈ 86,957 > cap of CAP*(2+1)*LAM*NAV = 60,000.
    assert abs(t['SPY'] - CAP * (2.0 + 1.0) * LAM * NAV) < 1e-6
    assert seen['exclude'] == set()


def test_apply_asset_corr_cap_reinserts_excluded_untouched(monkeypatch):
    monkeypatch.setattr(_sizer, '_load_asset_corr_cfg', lambda: (True, 0.6, 0.20))
    import execution.asset_correlation as _ac
    import execution.asset_correlation_filter as _acf
    monkeypatch.setattr(_ac, 'price_return_corr', lambda tickers, window=63: {t: {u: 0.9 for u in tickers} for t in tickers})
    calls = {}
    def cap(target_usd, conviction, corr, nav, cap_pct, corr_thr, lam):
        calls['tickers'] = set(target_usd)
        return {t: v * 0.5 for t, v in target_usd.items()}, {'clusters': [], 'total_gross_before': 0,
                                                             'total_gross_after': 0, 'released_usd': 0}
    monkeypatch.setattr(_acf, 'cap_correlated_clusters', cap)
    out = _sizer._apply_asset_corr_cap({'SPY': 150_000.0, 'ZZTA': 40_000.0, 'ZZTB': 30_000.0},
                                       {'SPY': 2.0, 'ZZTA': 2.6, 'ZZTB': 2.2}, NAV, lam=LAM, exclude={'SPY'})
    assert calls['tickers'] == {'ZZTA', 'ZZTB'}
    assert out == {'SPY': 150_000.0, 'ZZTA': 20_000.0, 'ZZTB': 15_000.0}
