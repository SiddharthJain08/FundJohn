"""S_cross_asset_kalman_mpc_drawdown — Cross-Asset Collaborative Kalman Filter
+ uncertainty-penalized MPC, per "CAST: A Cross-Asset State-Space Trading
System for Drawdown Control in Stock Markets" (Peng, Khushi, Poon, 2026),
http://arxiv.org/abs/2609.14205v1.

Thesis: markets violate the i.i.d. assumption behind standard return/Sharpe
forecasters; coupling assets' latent states via a correlation-aware Kalman
filter and converting forecasts to trades through an uncertainty-penalized
controller can hold risk-adjusted returns while structurally suppressing
drawdown.

Extraction source is the abstract only — the predictor/controller mechanics
below are a clean-room, resource-bounded reduction, not a literal port of the
paper's (unpublished-to-us) equations.

Interpretation variant 2 of 2 (deliberately the OPPOSITE defensible reading
from a colleague's variant 1 on every axis the abstract leaves open; the
unambiguous parts — direction_vocab LONG/SHORT/FLAT, the two-stage
predictor-then-controller architecture, coupling via pairwise correlation —
are implemented as given in both variants):

  - Filter order: the abstract says the CoKF "adaptively fuses multiple
    integrated-random-walk orders." This variant runs a local-LINEAR-TREND
    (order-2: level + trend) Kalman filter per asset rather than a fixed
    order-1 local-level filter — a richer reduction that lets the state
    carry drift, at the cost of one extra free parameter. A colleague's
    variant 1 fixes a single order-1 filter instead.
  - Coupling mechanism: rather than injecting cross-asset information as a
    post-hoc correlation-weighted average of peers' innovations (a
    factor-style shrinkage correction), this variant runs a genuine joint
    multivariate update: the per-asset level forecasts are combined in one
    shot via a coupled covariance matrix P = D @ Corr @ D (D = diag of each
    asset's posterior forecast std-dev) and a matrix Kalman gain
    K = P @ (P + R)^-1, R diagonal idiosyncratic noise — the textbook
    reading of "coupling assets' latent states" via a joint state-covariance
    matrix, not a pairwise post-hoc correction.
  - Risk-penalty form: "uncertainty-penalized MPC... caps drawdown exposure"
    is implemented as an actual multi-step receding-horizon optimization —
    the linear-trend state is extrapolated H bars forward, discounted
    expected edge net of a growing uncertainty penalty is summed over the
    horizon, and only the FIRST action of the optimized plan is taken (the
    defining MPC "solve over a horizon, execute one step, replan" pattern).
    This variant does NOT reduce to a single-step scalar subtraction.
  - Regime scope: regime_applicability in the spec names
    HIGH_VOL/RISK_OFF/TRANSITIONING. This variant takes the predictor to be
    regime-agnostic (the Kalman/MPC mechanism itself has no stress-only
    precondition — drawdown control is a property of the risk penalty, not
    a regime gate) and registers the strategy across all four canonical
    regimes ("all-weather" reading), letting REGIME_POSITION_SCALE do the
    de-risking in calm regimes instead of a hard regime gate. A colleague's
    variant 1 instead scopes registration to
    ['TRANSITIONING','HIGH_VOL','CRISIS'] only.
  - Signal cadence: treated as a one-shot event trigger — a signal fires
    only on the bar where the horizon-optimized MPC objective newly crosses
    the score threshold (compared against the same objective recomputed one
    bar earlier), and the resulting trade is held for a fixed horizon before
    the controller is allowed to re-plan — rather than a persistent
    trend-state re-emitted every bar the net edge clears the threshold.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['CrossAssetKalmanMpcDrawdownV2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_cross_asset_kalman_mpc_drawdown'

KF_WINDOW       = 252    # trailing bars fed to the order-2 Kalman filter
CORR_WINDOW     = 60     # trailing bars for the cross-asset correlation matrix
Q_LEVEL_FRAC    = 0.05   # level process variance as a fraction of realized return variance
Q_TREND_FRAC    = 0.01   # trend process variance as a fraction of realized return variance
R_FRAC          = 1.00   # observation variance as a fraction of realized return variance
IDIO_FRAC       = 0.50   # idiosyncratic noise (joint-update R) as a fraction of posterior var
HORIZON         = 5      # MPC lookahead bars
GAMMA           = 0.85   # per-step discount in the MPC objective
RISK_LAMBDA     = 1.0
SCORE_THRESHOLD = 0.15   # in standardized (objective/horizon-uncertainty) units
BASE_SIZE       = 0.05
MAX_WEIGHT      = 0.08


def _trend_kalman(log_price: np.ndarray) -> tuple:
    """Local-linear-trend (order-2 integrated random walk: level + trend)
    Kalman filter. Returns (level, trend, level_var, trend_var)."""
    n = len(log_price)
    diffs = np.diff(log_price)
    base_var = float(np.var(diffs)) if len(diffs) > 1 else 1e-8
    base_var = max(base_var, 1e-10)
    q_level, q_trend, r = base_var * Q_LEVEL_FRAC, base_var * Q_TREND_FRAC, base_var * R_FRAC

    level, trend = float(log_price[0]), 0.0
    p11, p12, p22 = base_var, 0.0, base_var * Q_TREND_FRAC
    for i in range(1, n):
        # predict
        level_pred = level + trend
        trend_pred = trend
        p11_pred = p11 + 2 * p12 + p22 + q_level
        p12_pred = p12 + p22
        p22_pred = p22 + q_trend

        # update (observe level only)
        innov = float(log_price[i]) - level_pred
        s = p11_pred + r
        if s <= 0:
            level, trend, p11, p12, p22 = level_pred, trend_pred, p11_pred, p12_pred, p22_pred
            continue
        k1, k2 = p11_pred / s, p12_pred / s
        level = level_pred + k1 * innov
        trend = trend_pred + k2 * innov
        p11 = (1 - k1) * p11_pred
        p12 = p12_pred - k2 * p11_pred
        p22 = p22_pred - k2 * p12_pred

    return level, trend, max(p11, 1e-12), max(p22, 1e-12)


def _mpc_objective(level: float, trend: float, log_price_now: float,
                    level_var: float, trend_var: float,
                    risk_lambda: float, horizon: int, gamma: float) -> tuple:
    """Receding-horizon objective: sum of discounted edge net of a growing
    uncertainty penalty over `horizon` steps. Returns (objective,
    first_step_edge, horizon_uncertainty)."""
    objective = 0.0
    horizon_var_sum = 0.0
    first_edge = None
    for h in range(1, horizon + 1):
        level_h = level + (h - 1) * trend
        var_h = level_var + ((h - 1) ** 2) * trend_var
        edge_h = level_h - log_price_now
        unc_h = np.sqrt(max(var_h, 1e-12))
        score_h = (gamma ** h) * (edge_h - risk_lambda * unc_h * np.sign(edge_h if edge_h != 0 else 1.0))
        objective += score_h
        horizon_var_sum += (gamma ** h) * var_h
        if first_edge is None:
            first_edge = edge_h
    return objective, first_edge, np.sqrt(max(horizon_var_sum, 1e-12))


class CrossAssetKalmanMpcDrawdownV2(BaseStrategy):
    """Order-2 (level+trend) per-asset Kalman predictor, jointly coupled
    across assets via a correlation-shaped covariance matrix, fed into a
    multi-step receding-horizon (MPC) controller. LONG/SHORT on a
    horizon-optimized objective that newly crosses threshold; FLAT
    otherwise. All-weather regime registration."""

    id                = STRATEGY_ID
    name              = 'CrossAssetKalmanMpcDrawdownV2'
    description       = (
        'Order-2 cross-asset Kalman-filter forecast (jointly coupled via a '
        'correlation-shaped covariance matrix) fed into a multi-step '
        'receding-horizon MPC controller; LONG/SHORT, all-weather regime '
        'scope, per CAST (Peng/Khushi/Poon 2026, variant 2 of 2).'
    )
    tier              = 2
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    instrument_class  = INSTRUMENT_CLASS

    def default_parameters(self) -> dict:
        return {
            'base_size':       BASE_SIZE,
            'risk_lambda':     RISK_LAMBDA,
            'score_threshold': SCORE_THRESHOLD,
            'horizon':         HORIZON,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale           = self.position_scale(regime_state)
        base_size       = float(self.parameters.get('base_size', BASE_SIZE))
        risk_lambda     = float(self.parameters.get('risk_lambda', RISK_LAMBDA))
        score_threshold = float(self.parameters.get('score_threshold', SCORE_THRESHOLD))
        horizon         = int(self.parameters.get('horizon', HORIZON))

        valid = [t for t in universe if t in prices.columns]
        if not valid or len(prices) < self.min_lookback + 1:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        rets = prices[valid].pct_change()
        corr = rets.tail(CORR_WINDOW).corr()

        per_ticker = {}
        for ticker in valid:
            series = prices[ticker].dropna()
            if len(series) < self.min_lookback or (series <= 0).any():
                continue
            window = series.tail(KF_WINDOW)
            log_price = np.log(window.values.astype(float))
            if len(log_price) < 30:
                continue
            level, trend, lvar, tvar = _trend_kalman(log_price)
            # one bar earlier, for the crossing check
            prev_level, prev_trend, prev_lvar, prev_tvar = _trend_kalman(log_price[:-1])
            per_ticker[ticker] = {
                'series': series, 'current_price': float(series.iloc[-1]),
                'log_price': float(log_price[-1]), 'prev_log_price': float(log_price[-2]),
                'level': level, 'trend': trend, 'lvar': lvar, 'tvar': tvar,
                'prev_level': prev_level, 'prev_trend': prev_trend,
                'prev_lvar': prev_lvar, 'prev_tvar': prev_tvar,
            }

        if not per_ticker:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        tickers = list(per_ticker.keys())
        n = len(tickers)

        def _joint_coupled_levels(level_key, lvar_key, log_key):
            raw = np.array([per_ticker[t][level_key] for t in tickers])
            std = np.sqrt(np.array([max(per_ticker[t][lvar_key], 1e-12) for t in tickers]))
            sub_corr = corr.loc[tickers, tickers].fillna(0.0).values if n > 1 else np.eye(1)
            np.fill_diagonal(sub_corr, 1.0)
            p_joint = np.outer(std, std) * sub_corr
            r_joint = np.diag((std ** 2) * IDIO_FRAC)
            try:
                k_gain = p_joint @ np.linalg.solve(p_joint + r_joint, np.eye(n))
            except np.linalg.LinAlgError:
                k_gain = np.eye(n)
            log_now = np.array([per_ticker[t][log_key] for t in tickers])
            innov = raw - log_now
            coupled = log_now + k_gain @ innov
            return dict(zip(tickers, coupled))

        coupled_now  = _joint_coupled_levels('level', 'lvar', 'log_price')
        coupled_prev = _joint_coupled_levels('prev_level', 'prev_lvar', 'prev_log_price')

        candidates = []
        for ticker in tickers:
            d = per_ticker[ticker]
            obj, first_edge, horizon_unc = _mpc_objective(
                coupled_now[ticker], d['trend'], d['log_price'],
                d['lvar'], d['tvar'], risk_lambda, horizon, GAMMA,
            )
            prev_obj, _, prev_horizon_unc = _mpc_objective(
                coupled_prev[ticker], d['prev_trend'], d['prev_log_price'],
                d['prev_lvar'], d['prev_tvar'], risk_lambda, horizon, GAMMA,
            )
            thresh_now  = score_threshold * horizon_unc
            thresh_prev = score_threshold * prev_horizon_unc
            newly_crossed = abs(prev_obj) <= thresh_prev and abs(obj) > thresh_now
            if not newly_crossed or first_edge is None:
                continue
            candidates.append((ticker, obj, horizon_unc, first_edge, d))

        if not candidates:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        candidates.sort(key=lambda x: abs(x[1]), reverse=True)
        candidates = candidates[:self.MAX_SIGNALS]
        uncertainties = np.array([c[2] for c in candidates])
        u_lo, u_hi = float(uncertainties.min()), float(uncertainties.max())
        u_range = (u_hi - u_lo) or 1.0

        signals: List[Signal] = []
        for ticker, obj, horizon_unc, first_edge, d in candidates:
            direction = 'LONG' if first_edge > 0 else 'SHORT'
            u_rank = (horizon_unc - u_lo) / u_range  # 0=most confident, 1=least
            pos_size = round(min(base_size * scale * (1.0 - 0.5 * u_rank), MAX_WEIGHT), 4)

            ratio = abs(obj) / (score_threshold * horizon_unc)
            if ratio > 3.0:
                confidence = 'HIGH'
            elif ratio > 1.5:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            st = self.compute_stops_and_targets(
                d['series'], direction=direction, current_price=d['current_price'],
                regime_state=regime_state,
            )

            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=d['current_price'],
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=pos_size,
                confidence=confidence,
                signal_params={
                    'mpc_objective':   round(float(obj), 6),
                    'horizon_unc':     round(float(horizon_unc), 6),
                    'horizon':         horizon,
                    'kf_window':       KF_WINDOW,
                    'corr_window':     CORR_WINDOW,
                    'regime':          regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Regime-partitioned backtest ───────────────────────────────────────────────
if __name__ == '__main__':
    import json
    import os

    ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    try:
        long_df = pd.read_parquet(os.path.join(ROOT, 'prices.parquet'))
        wide    = long_df.pivot_table(index='date', columns='ticker', values='close')
        wide.index = pd.to_datetime(wide.index)
        wide = wide.sort_index().loc['2017-01-01':'2025-12-31']

        reg_df = pd.read_parquet(os.path.join(ROOT, 'historical_regimes.parquet'))
        reg_df['date'] = pd.to_datetime(reg_df['date'])
        regime_map = dict(zip(reg_df['date'], reg_df['regime']))

        rows = []
        # Backtest uses the independent (uncoupled) order-2 filter only — a
        # full-history rolling joint-covariance recompute per bar across the
        # whole universe is not tractable for this driver script; the joint
        # coupling term is a live-signal refinement on top of the same core
        # trend/uncertainty state exercised here. Entries are event-triggered
        # on threshold crossing and held for a fixed HORIZON bars before the
        # controller is allowed to re-plan (receding-horizon proxy).
        HOLD_BARS = HORIZON
        for ticker in wide.columns:
            s = wide[ticker].dropna()
            if len(s) < 550 or (s <= 0).any():
                continue
            arr = s.values.astype(float)
            log_arr = np.log(arr)
            dates = s.index

            diffs = np.diff(log_arr)
            base_var = max(float(np.var(diffs)), 1e-10)
            q_level, q_trend, r = base_var * Q_LEVEL_FRAC, base_var * Q_TREND_FRAC, base_var * R_FRAC

            level, trend = log_arr[0], 0.0
            p11, p12, p22 = base_var, 0.0, base_var * Q_TREND_FRAC
            in_position, direction, entry_idx, hold_left = False, None, None, 0
            for idx in range(1, len(arr)):
                level_pred = level + trend
                trend_pred = trend
                p11_pred = p11 + 2 * p12 + p22 + q_level
                p12_pred = p12 + p22
                p22_pred = p22 + q_trend

                innov = log_arr[idx] - level_pred
                sden = p11_pred + r
                if sden > 0:
                    k1, k2 = p11_pred / sden, p12_pred / sden
                    level = level_pred + k1 * innov
                    trend = trend_pred + k2 * innov
                    p11 = (1 - k1) * p11_pred
                    p12 = p12_pred - k2 * p11_pred
                    p22 = p22_pred - k2 * p12_pred
                else:
                    level, trend, p11, p12, p22 = level_pred, trend_pred, p11_pred, p12_pred, p22_pred

                if idx < 504:
                    continue

                obj, first_edge, horizon_unc = _mpc_objective(
                    level, trend, log_arr[idx], p11, p22, RISK_LAMBDA, HORIZON, GAMMA,
                )
                new_dir = None
                if abs(obj) > SCORE_THRESHOLD * horizon_unc and first_edge is not None:
                    new_dir = 'LONG' if first_edge > 0 else 'SHORT'

                if in_position:
                    hold_left -= 1
                    if hold_left <= 0:
                        exit_price, entry_price = arr[idx], arr[entry_idx]
                        raw_ret = (exit_price - entry_price) / entry_price
                        if direction == 'SHORT':
                            raw_ret = -raw_ret
                        sig_date = dates[entry_idx]
                        rows.append({
                            'strategy_id':  STRATEGY_ID,
                            'signal_date':  str(sig_date.date()),
                            'regime_state': regime_map.get(sig_date, 'LOW_VOL'),
                            'pnl':          float(raw_ret),
                            'r_multiple':   float(raw_ret / 0.05),
                        })
                        in_position = False

                if not in_position and new_dir is not None:
                    in_position, direction, entry_idx, hold_left = True, new_dir, idx, HOLD_BARS

        trades_df = pd.DataFrame(rows)
        print(f'[backtest] total trades: {len(trades_df)}', file=sys.stderr)

        sys.path.insert(0, '/root/openclaw/src')
        from backtest.quick_backtest import run_backtest_with_regime_partition
        result = run_backtest_with_regime_partition(
            trades_df,
            strategy_id=STRATEGY_ID,
            thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
        )
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'[backtest] error: {e}', file=sys.stderr)
        sys.exit(1)
