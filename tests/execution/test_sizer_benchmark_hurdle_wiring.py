"""Spec §2.5 wiring: OFF -> byte-identical sizing + shadow line; ON -> hurdle
applied, benchmark ticker exempt and uncapped by the hurdle."""
import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402
from execution import benchmark_sizing as bz      # noqa: E402

NAV, LAM = 100_000.0, 2.0


def _account():
    return {'equity': NAV, 'regt_buying_power': 2 * NAV, 'long_market_value': 0, 'cash': NAV}


def _params():
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 1, 'min_signal_notional_pct': 0.00001,
            'position_circuit_breaker_pct': 0.02, 'min_cumulative_sharpe': 3.0, 'min_acting_strategies': 1}


def _carried(sid, ticker, direction='LONG'):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction,
            'signal_date': date(2026, 8, 28), 'entry_price': 100.0, 'stop_loss': 95.0,
            'target_1': 110.0, 'target_2': 120.0, 'signal_params': {}}


def _row(sid, eff):
    return {'strategy_id': sid, 'daily_weight': eff, 'effective_sharpe': eff, 'cadence_days': 21.0, 'bt_n': 600}


ROWS = [_row('S_beta_spy', 2.0), _row('S_hi', 2.6), _row('S_lo', 1.5)]
CARRIED = [_carried('S_beta_spy', 'SPY'), _carried('S_hi', 'ZZTA'), _carried('S_lo', 'ZZTB')]


def run(monkeypatch, flag, s_m=2.0, lines=None, budget=False, max_nav_frac=1.0):
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    for g in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
              'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE', 'OPENCLAW_TRADE_WEIGHT_FACTOR',
              'OPENCLAW_INTRADAY_REDEPLOY'):
        monkeypatch.delenv(g, raising=False)
    if flag: monkeypatch.setenv(bz.BENCH_SIZING_ENV, '1')
    else:    monkeypatch.setenv(bz.BENCH_SIZING_ENV, '0')
    monkeypatch.setenv(bz.BETA_BUDGET_ENV, '1' if budget else '0')
    monkeypatch.setattr(bz, 'benchmark_max_nav_frac', lambda default=1.0, conn=None: max_nav_frac)
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda weight_by_strat, cadence_by_strat=None, **_kw: list(CARRIED))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', lambda t, *a, **k: t)
    monkeypatch.setattr(_sizer, '_post_corr_cumsharpe_log', lambda line: (lines.append(line) if lines is not None else None))
    monkeypatch.setattr(_sizer, '_maybe_flatten_zero_conviction', lambda *a, **k: None)
    # The per-ticker conviction cap is armed under EOD_RECONCILE and would clamp
    # SPY/ZZTA here; it is not this task's subject (Task 11 exempts benchmark
    # tickers), so lift it out of the way.
    monkeypatch.setattr(_sizer, 'PER_TICKER_CAP_SHARPE_FRAC', 10.0)
    with _mock.patch('execution.strategy_weights.load_current', return_value=list(ROWS)), \
         _mock.patch('execution.benchmark_sleeve.load_benchmark_sleeve_ids', return_value={'S_beta_spy'}), \
         _mock.patch('execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing', return_value=s_m):
        return _sizer.size_positions(signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
                                     run_date=date(2026, 8, 29), strategy_state={},
                                     regime_params=_params(), confirmer=lambda p: {})


def targets(orders):
    return {o['ticker']: o['target_usd'] for o in orders if o['action'] not in ('close_long', 'close_short')}


def test_off_is_byte_identical_and_logs_shadow(monkeypatch):
    lines = []
    t = targets(run(monkeypatch, flag=False, lines=lines))
    gross = LAM * NAV
    # raw S_adj shares: 2.0 : 2.6 : 1.5 (per-ticker cap lifted in run())
    assert abs(t['SPY'] - gross * 2.0 / 6.1) < 1e-6
    assert abs(t['ZZTA'] - gross * 2.6 / 6.1) < 1e-6
    assert abs(t['ZZTB'] - gross * 1.5 / 6.1) < 1e-6
    assert any(l.startswith('bench_sizing.shadow[LOW_VOL]: S_m=2.00') and 'dropped=1/3' in l for l in lines)


def test_on_applies_hurdle(monkeypatch):
    t = targets(run(monkeypatch, flag=True))
    # after hurdle: SPY 2.0 (exempt), ZZTA 0.6, ZZTB dropped -> gross 2.6
    gross = LAM * NAV
    assert 'ZZTB' not in t
    assert abs(t['ZZTA'] - gross * 0.6 / 2.6) < 1e-6
    assert abs(t['SPY'] - gross * 2.0 / 2.6) < 1e-6     # benchmark ticker keeps its full S_adj


def test_on_with_no_s_m_falls_back_to_raw(monkeypatch):
    t = targets(run(monkeypatch, flag=True, s_m=None))
    assert set(t) == {'SPY', 'ZZTA', 'ZZTB'}


def test_budget_applies_only_with_both_flags(monkeypatch):
    lines = []
    # max_nav_frac lifted out of the way (like PER_TICKER_CAP_SHARPE_FRAC=10.0
    # in run()): this fixture's SPY share is 5.5/6.1 of a 2x-levered gross, i.e.
    # 180.3% of NAV — the §3.4 NAV cap (default frac=1.0) would otherwise bind
    # here too, which is not this test's subject (see test_budget_nav_cap_*
    # below for the dedicated cap coverage).
    t = targets(run(monkeypatch, flag=True, budget=True, max_nav_frac=10.0, lines=lines))
    gross = LAM * NAV
    # pool = 2.0 (ZZTA hands S_m) + 1.5 (ZZTB dropped, whole |S|) = 3.5; SPY = 2.0 + 3.5 = 5.5 of Σ 6.1
    assert 'ZZTB' not in t
    assert abs(t['ZZTA'] - gross * 0.6 / 6.1) < 1e-6
    assert abs(t['SPY'] - gross * 5.5 / 6.1) < 1e-6
    assert any('bench_sizing.apply[LOW_VOL]' in l and 'beta_budget=apply pool=3.5' in l for l in lines)


def test_budget_flag_alone_is_rule_c_shadow_and_prints_budget_shadow(monkeypatch):
    lines = []
    t = targets(run(monkeypatch, flag=False, budget=True, lines=lines))
    gross = LAM * NAV
    assert abs(t['SPY'] - gross * 2.0 / 6.1) < 1e-6           # raw S_adj book, byte-identical to today
    assert any('bench_sizing.shadow[LOW_VOL]' in l and 'beta_budget=shadow pool=3.5' in l for l in lines)


def test_rule_c_on_budget_off_is_unchanged(monkeypatch):
    t = targets(run(monkeypatch, flag=True, budget=False))
    gross = LAM * NAV
    assert abs(t['SPY'] - gross * 2.0 / 2.6) < 1e-6           # Task-wiring behaviour from 2026-08-29


def test_budget_with_no_s_m_falls_back_to_raw(monkeypatch):
    t = targets(run(monkeypatch, flag=True, budget=True, s_m=None))
    assert set(t) == {'SPY', 'ZZTA', 'ZZTB'}


def test_budget_nav_cap_clamps_benchmark_without_redistribution(monkeypatch):
    t = targets(run(monkeypatch, flag=True, budget=True, max_nav_frac=0.5))
    gross = LAM * NAV
    assert abs(t['SPY'] - 0.5 * NAV) < 1e-6                    # 5.5/6.1 * 200k = 180k -> clamped to 50k
    assert abs(t['ZZTA'] - gross * 0.6 / 6.1) < 1e-6           # alpha untouched (no renorm-up)


def test_nav_cap_inert_when_budget_off(monkeypatch):
    t = targets(run(monkeypatch, flag=True, budget=False, max_nav_frac=0.5))
    gross = LAM * NAV
    assert abs(t['SPY'] - gross * 2.0 / 2.6) < 1e-6           # 153.8k > 50k, NOT clamped


def test_alert_when_the_benchmark_is_shed_downstream_of_the_budget(monkeypatch, caplog):
    """Final fix wave #2: the budget redirects ~78 % of NAV to the benchmark
    ticker, but the emission tail's asset-eligibility / entry-hygiene /
    net-exposure gates can still drop it. That silently destroys the redirected
    pool, so it must WARN + alert."""
    import logging
    alerts = []
    monkeypatch.setattr(_sizer, '_post_ops_alert', lambda line: alerts.append(line))
    # Drop SPY inside _emit_orders_from_targets (bare module-level call there).
    monkeypatch.setattr(_sizer, '_apply_asset_eligibility_gate',
                        lambda t, broker, eligibility=None: {k: v for k, v in t.items() if k != 'SPY'})
    with caplog.at_level(logging.WARNING, logger='execution.regime_blended_sizer'):
        t = targets(run(monkeypatch, flag=True, budget=True, max_nav_frac=10.0))
    assert 'SPY' not in t
    assert len(alerts) == 1 and 'redirected pool NOT deployed' in alerts[0]
    assert ('bench_sizing: beta budget applied but no benchmark ticker survived to the '
            "emitted orders (bench=['SPY'])") in caplog.text


def test_no_shed_alert_on_the_normal_budget_path(monkeypatch, caplog):
    """The benchmark survives -> silence (the alert must not be a per-cycle noise source)."""
    import logging
    alerts = []
    monkeypatch.setattr(_sizer, '_post_ops_alert', lambda line: alerts.append(line))
    with caplog.at_level(logging.WARNING, logger='execution.regime_blended_sizer'):
        t = targets(run(monkeypatch, flag=True, budget=True, max_nav_frac=10.0))
    assert 'SPY' in t and alerts == []
    assert 'redirected pool NOT deployed' not in caplog.text


def test_no_shed_alert_when_the_budget_did_not_apply(monkeypatch, caplog):
    """Budget OFF: even a shed benchmark ticker is not this alert's business."""
    import logging
    alerts = []
    monkeypatch.setattr(_sizer, '_post_ops_alert', lambda line: alerts.append(line))
    monkeypatch.setattr(_sizer, '_apply_asset_eligibility_gate',
                        lambda t, broker, eligibility=None: {k: v for k, v in t.items() if k != 'SPY'})
    with caplog.at_level(logging.WARNING, logger='execution.regime_blended_sizer'):
        targets(run(monkeypatch, flag=True, budget=False))
    assert alerts == [] and 'redirected pool NOT deployed' not in caplog.text
