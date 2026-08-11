"""SP-6 per-ticker conviction cap (operator formula, 2026-06-04).

    cap_usd(ticker) = PER_TICKER_CAP_SHARPE_FRAC × |gate_net_sharpe(ticker)| × λ × NAV

(0.05 at introduction; 0.10 since the 2026-08-11 breadth-experiment epoch —
assertions below read the constant so they track operator retunes.)

Bounds the few-survivor concentration failure mode: the sharpe_cadence
normalization (Σ|target_usd| = λ×NAV) renormalizes the WHOLE book onto
whatever cleared the cum-sharpe gate, so a 1-survivor day would otherwise
put 2× equity into one name (observed in the 2026-06-03 diagnosis,
/root/sp6_phaseA_conviction_gate_diagnosis_2026-06-04.md §4).

The cap reads |gate_net_sharpe| — the corr-adjusted S_adj that is now the sole
conviction gate. For single-strategy tickers S_adj == daily_weight
(= effective_sharpe / sqrt(cadence)), so these fixtures set daily_weight to that
invariant.

Contract under test:
  1. EOD mode, single survivor → |target| clamped to CAP·|S_adj|·λ·NAV.
  2. EOD mode, cap not binding → targets byte-identical (no-op on fat days).
  3. Legacy (non-EOD) mode → NO cap (byte-identical legacy path).
  4. No renorm-up: shaved capital is NOT redistributed to other tickers.
"""
import sys
import os
from datetime import date
from pathlib import Path
import unittest.mock as _mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer


NAV = 100_000.0
CAP = _sizer.PER_TICKER_CAP_SHARPE_FRAC
LAM = 2.0  # stubbed _load_lambda


def _account(equity=NAV):
    return {'equity': equity, 'regt_buying_power': 2 * equity,
            'long_market_value': 0, 'cash': equity}


def _params():
    # min_cumulative_sharpe given explicitly so _resolve_min_cumulative_sharpe
    # never reaches for the DB; tiny min-notional so that gate is inert here.
    return {'liquidity_param': 1.0,
            'min_signal_notional_usd': 1,
            'min_signal_notional_pct': 0.00001,
            'position_circuit_breaker_pct': 0.02,
            'min_cumulative_sharpe': 3.0}


def _carried(sid, ticker, direction='LONG'):
    """Row shape returned by _load_approved_carried_signals."""
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction,
            'signal_date': date(2026, 6, 3), 'entry_price': 100.0,
            'stop_loss': 95.0, 'target_1': 110.0, 'target_2': 120.0,
            'signal_params': {}}


def _weights_row(sid, eff_sharpe, daily_weight=None, cadence_days=1.0):
    # The corr-adjusted gate reads daily_weight (= effective_sharpe / sqrt(cadence),
    # the real weights-pipeline invariant), NOT raw effective_sharpe. Default the
    # fixture to that invariant so single-strategy S_adj == effective_sharpe here.
    if daily_weight is None:
        daily_weight = eff_sharpe / (cadence_days ** 0.5)
    return {'strategy_id': sid, 'daily_weight': daily_weight,
            'effective_sharpe': eff_sharpe, 'cadence_days': cadence_days}


def _run_eod(monkeypatch, weights_rows, carried_rows, broker=None, params=None):
    """Drive size_positions through the full sharpe_cadence path with all
    external surfaces (weights DB, carried-set DB, λ DB, broker, confirmer)
    stubbed. The corr-adjusted gate is unconditional; clear all ortho flags so
    the cap reads the same corr S_adj the gate does. Returns the emitted orders."""
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    for gate in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT',
                 'OPENCLAW_STRATEGY_ORTHO_SHADOW', 'OPENCLAW_STRATEGY_BRACKET_STACK',
                 'OPENCLAW_OPTION_DELTA_HEDGE'):
        monkeypatch.delenv(gate, raising=False)

    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda weight_by_strat, cadence_by_strat=None, **_kw: list(carried_rows))
    monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: dict(broker or {}))

    with _mock.patch('execution.strategy_weights.load_current', return_value=list(weights_rows)):
        return _sizer.size_positions(
            signals=[], account_state=_account(), regime={'state': 'LOW_VOL'},
            run_date=date(2026, 6, 4), strategy_state={},
            regime_params=params or _params(), confirmer=lambda proposals: {},
        )


def _open_by_ticker(orders):
    return {o['ticker']: o for o in orders if o['action'] not in ('close_long', 'close_short')
            and o['strategy_id'] not in ('__close_orphan__', '__flip_close__')}


class TestPerTickerCapEOD:

    def test_single_survivor_capped(self, monkeypatch):
        """1 surviving ticker (sharpe 3.5) must NOT absorb the whole λ×NAV:
        |target| = CAP × 3.5 × 2.0 × 100k ($35k at 0.05 / $70k at 0.10),
        not $200k."""
        orders = _run_eod(
            monkeypatch,
            weights_rows=[_weights_row('S1', eff_sharpe=3.5)],
            carried_rows=[_carried('S1', 'STX')],
        )
        opens = _open_by_ticker(orders)
        assert 'STX' in opens, f'expected an STX open, got {orders}'
        expected_cap = CAP * 3.5 * LAM * NAV
        assert abs(opens['STX']['target_usd'] - expected_cap) < 1e-6, (
            f"single survivor must be capped at {expected_cap}, "
            f"got {opens['STX']['target_usd']}"
        )
        assert abs(opens['STX']['notional_usd'] - expected_cap) < 1e-6

    def test_cap_not_binding_is_noop(self, monkeypatch):
        """Fat day: 10 equal tickers → proportional target $20k each, cap
        CAP×8×λ×NAV ($80k+ each) → targets unchanged (byte-identical)."""
        tickers = [f'T{i:02d}' for i in range(10)]
        orders = _run_eod(
            monkeypatch,
            weights_rows=[_weights_row('S1', eff_sharpe=8.0)],
            carried_rows=[_carried('S1', t) for t in tickers],
        )
        opens = _open_by_ticker(orders)
        assert len(opens) == 10
        for t in tickers:
            assert abs(opens[t]['target_usd'] - (LAM * NAV / 10)) < 1e-6, (
                f'{t}: cap must not bind on fat days, got {opens[t]["target_usd"]}'
            )

    def test_no_renorm_after_cap(self, monkeypatch):
        """Two tickers, one capped: the shaved capital must NOT be
        redistributed to the other (no renorm-up).

        S_hi (sharpe 6 on A) + S_lo (sharpe 2.5 on B), single-strategy each so
        corr S_adj == effective_sharpe. The cap binds for every ticker iff
        Σ|S_adj| < 1/CAP (raw share λ·NAV·s/Σs vs cap CAP·s·λ·NAV), so the
        fixture keeps Σ = 8.5 < 1/CAP = 10 at CAP=0.10: raw A≈141k > cap
        120k, raw B≈59k > cap 50k. Both clamp; crucially B stays at ITS
        cap, not pumped up by A's shave (capped gross < λ×NAV)."""
        assert 6.0 + 2.5 < 1.0 / CAP, 'fixture premise: cap must bind for both'
        orders = _run_eod(
            monkeypatch,
            weights_rows=[_weights_row('S_hi', eff_sharpe=6.0),
                          _weights_row('S_lo', eff_sharpe=2.5)],
            carried_rows=[_carried('S_hi', 'AAA'), _carried('S_lo', 'BBB')],
        )
        opens = _open_by_ticker(orders)
        assert abs(opens['AAA']['target_usd'] - CAP * 6.0 * LAM * NAV) < 1e-6
        assert abs(opens['BBB']['target_usd'] - CAP * 2.5 * LAM * NAV) < 1e-6
        total = sum(abs(o['target_usd']) for o in opens.values())
        assert total < LAM * NAV - 1, 'capped gross must land BELOW λ×NAV (no renorm-up)'

    def test_short_side_capped_symmetrically(self, monkeypatch):
        """A short survivor clamps to −cap (magnitude rule, sign preserved)."""
        orders = _run_eod(
            monkeypatch,
            weights_rows=[_weights_row('S1', eff_sharpe=4.0)],
            carried_rows=[_carried('S1', 'XYZ', direction='SHORT')],
        )
        opens = _open_by_ticker(orders)
        assert 'XYZ' in opens
        assert abs(opens['XYZ']['target_usd'] - (-CAP * 4.0 * LAM * NAV)) < 1e-6

    # NOTE: the cap's self-consistency with the gate quantity (cap reads the
    # SAME corr S_adj the gate uses, not a naive sum) is covered end-to-end by
    # test_corr_cumsharpe_integration.test_on_path_activates_and_caps_on_corr_sadj
    # (two correlated strategies → cap reflects the corr-deflated S_adj). The
    # legacy deflated-gate variant was removed with the legacy cum-sharpe gate.


class TestLegacyPathUncapped:

    def test_gate_off_no_cap(self, monkeypatch):
        """OPENCLAW_EOD_RECONCILE unset → legacy path byte-identical:
        a single ticker keeps the FULL λ×NAV target (no cap applied)."""
        monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)
        monkeypatch.delenv('OPENCLAW_EOD_SIGNAL_REGISTER', raising=False)
        monkeypatch.delenv('OPENCLAW_INTRADAY_REDEPLOY', raising=False)
        for gate in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_CORR_WEIGHT',
                     'OPENCLAW_STRATEGY_ORTHO_SHADOW', 'OPENCLAW_STRATEGY_BRACKET_STACK',
                     'OPENCLAW_OPTION_DELTA_HEDGE'):
            monkeypatch.delenv(gate, raising=False)

        monkeypatch.setattr(_sizer, '_load_active_window_signals',
                            lambda regime_state, weight_by_strat, cadence_by_strat:
                                [_carried('S1', 'STX')])
        monkeypatch.setattr(_sizer, '_load_lambda', lambda default=2.0, *, intraday=False: LAM)
        monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})

        with _mock.patch('execution.strategy_weights.load_current',
                         return_value=[_weights_row('S1', eff_sharpe=3.5)]):
            # Legacy path requires non-empty `signals` to pass the cadence
            # gate (`if not passed: return []` precedes the sizing path).
            orders = _sizer.size_positions(
                signals=[_carried('S1', 'STX')], account_state=_account(),
                regime={'state': 'LOW_VOL'},
                run_date=date(2026, 6, 4), strategy_state={},
                regime_params=_params(), confirmer=lambda proposals: {},
            )
        opens = _open_by_ticker(orders)
        assert 'STX' in opens
        assert abs(opens['STX']['target_usd'] - LAM * NAV) < 1e-6, (
            'legacy path must remain uncapped (byte-identical when EOD gate is off)'
        )


class TestClusterCapLambdaWiring:

    def test_cluster_cap_receives_global_lambda_not_blended(self, monkeypatch):
        """Operator ruling 2026-08-11: the asset-corr cluster cap scales with
        the GLOBAL position_sizing_lambda, NOT the regime-dampened effective
        lam (= lam_global x liquidity_param) used for gross scaling. With
        liquidity_param 0.5 the blended lam is LAM*0.5 — the wrapper must
        still receive LAM."""
        seen = {}

        def spy(target_usd, conviction, nav, lam=1.0):
            seen['lam'] = lam
            return target_usd

        monkeypatch.setattr(_sizer, '_apply_asset_corr_cap', spy)
        params = dict(_params())
        params['liquidity_param'] = 0.5
        _run_eod(
            monkeypatch,
            weights_rows=[_weights_row('S1', eff_sharpe=3.5)],
            carried_rows=[_carried('S1', 'STX')],
            params=params,
        )
        assert seen.get('lam') == LAM, (
            f"cluster cap must receive the global lambda {LAM}, "
            f"got {seen.get('lam')} (blended would be {LAM * 0.5})"
        )
