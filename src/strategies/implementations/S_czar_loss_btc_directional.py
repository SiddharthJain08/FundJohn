"""
CZAR Loss BTC Directional (Pfeffer, Kruijssen, Stecker & Longmore 2026)
Source: http://arxiv.org/abs/2609.36061v1

Variant 2/2: lightgbm is outside the import allowlist; rather than
sample-reweighting a vanilla squared-error GBM (variant 1's route), this
hand-rolls CZAR's closed-form gradient/Hessian and Newton-boosts shallow
sklearn trees against it (XGBoost-style), closer to the paper's actual
training mechanism. CZAR here = squared error + a penalty activated only
when |prediction| under-shoots |realized move| (the zero-bias failure
mode the paper targets): L(r,p)=(r-p)^2 + LAMBDA*max(0,|r|-|p|)^2;
grad=2(p-r)-2*LAMBDA*max(0,|r|-|p|)*sign(p); hess=2+2*LAMBDA*1{|p|<|r|}.
Also reads the paper's pseudocode literally on direction — LONG/SHORT by
sign(y_hat) past threshold — vs. variant 1's long-only convention, since
long/short is not actually constrained by the spec. Daily cadence (master
data is daily OHLC, not the paper's intraday bars).
"""
from __future__ import annotations

import sys
from typing import List, Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['CzarLossBtcDirectional']

INSTRUMENT_CLASS = 'crypto'
STRATEGY_ID      = 'S_czar_loss_btc_directional'
TICKER           = 'BTC-USD'

MIN_LOOKBACK    = 252   # paper's min_lookback_required
TRAIN_WINDOW    = 300   # trailing bars fed to the boosting loop (variant 2 pick)
LAGS            = (1, 2, 3, 5, 8, 13)
VOL_WINDOW      = 14
VOL_THRESH_MULT = 0.60   # lower bar than variant 1 — we size both directions
N_ROUNDS        = 25
LEARNING_RATE   = 0.15
LAMBDA_ZERO     = 1.5    # zero-bias penalty weight in the CZAR loss
BASE_SIZE       = 0.07

def _empty(msg: str) -> List[Signal]:
    print(msg, file=sys.stderr)
    print('[debug] signals=0', file=sys.stderr)
    return []

def _build_features(log_rets: pd.Series) -> pd.DataFrame:
    feat = {f'lag_{k}': log_rets.shift(k) for k in LAGS}
    feat['vol'] = log_rets.rolling(VOL_WINDOW).std()
    feat['mom_5'] = log_rets.rolling(5).sum()
    return pd.DataFrame(feat, index=log_rets.index)

def _czar_grad_hess(r: np.ndarray, p: np.ndarray) -> tuple:
    """Closed-form gradient/Hessian of the CZAR loss at predictions p."""
    shortfall = np.maximum(0.0, np.abs(r) - np.abs(p))
    sign_p = np.sign(p)
    grad = 2.0 * (p - r) - 2.0 * LAMBDA_ZERO * shortfall * sign_p
    hess = 2.0 + 2.0 * LAMBDA_ZERO * (shortfall > 0).astype(float)
    return grad, hess

def _fit_and_predict(series: pd.Series) -> Optional[dict]:
    """Newton-boost shallow trees against CZAR's closed-form grad/Hess."""
    try:
        from sklearn.tree import DecisionTreeRegressor
    except Exception:
        return None
    log_rets = np.log(series / series.shift(1)).dropna()
    if len(log_rets) < MIN_LOOKBACK:
        return None
    feat = _build_features(log_rets)
    data = feat.join(log_rets.shift(-1).rename('target')).dropna()
    if len(data) < MIN_LOOKBACK // 2:
        return None
    data = data.iloc[-(TRAIN_WINDOW + 1):]
    if len(data) < 60:
        return None
    cols = list(feat.columns)
    X_train = data.iloc[:-1][cols].to_numpy(dtype=float)
    y_train = data.iloc[:-1]['target'].to_numpy(dtype=float)
    x_latest = feat.iloc[[-1]][cols].to_numpy(dtype=float)
    if not np.isfinite(x_latest).all() or not np.isfinite(X_train).all():
        return None
    try:
        f_train = np.zeros_like(y_train)
        f_latest = np.zeros(1)
        for _ in range(N_ROUNDS):
            grad, hess = _czar_grad_hess(y_train, f_train)
            newton_step = -grad / np.clip(hess, 1e-6, None)
            tree = DecisionTreeRegressor(max_depth=2, min_samples_leaf=10, random_state=42)
            tree.fit(X_train, newton_step, sample_weight=hess)
            f_train = f_train + LEARNING_RATE * tree.predict(X_train)
            f_latest = f_latest + LEARNING_RATE * tree.predict(x_latest)
        y_hat = float(f_latest[0])
    except Exception:
        return None
    vol_now = float(log_rets.iloc[-VOL_WINDOW:].std())
    if not np.isfinite(y_hat) or not np.isfinite(vol_now) or vol_now <= 0:
        return None
    return {'y_hat': y_hat, 'vol_now': vol_now, 'n_train': len(X_train)}

class CzarLossBtcDirectional(BaseStrategy):
    """Newton-boosted (closed-form CZAR grad/Hess) directional predictor on
    BTC-USD daily log-returns (Pfeffer et al. 2026); LONG/SHORT when
    predicted magnitude clears a vol-scaled threshold, else flat."""

    id                = STRATEGY_ID
    name              = 'CZAR Loss BTC Directional'
    description       = (
        "Newton-boosted trees trained against CZAR's closed-form zero-bias "
        'gradient/Hessian; LONG or SHORT BTC-USD above a vol-scaled '
        'threshold, else flat.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = MIN_LOOKBACK
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {'vol_thresh_mult': VOL_THRESH_MULT, 'base_size': BASE_SIZE}

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or TICKER not in prices.columns:
            return _empty('[debug] no prices')
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return _empty('[debug] regime gated')
        series = prices[TICKER].dropna()
        fit = _fit_and_predict(series)
        if fit is None:
            return _empty(f'[{STRATEGY_ID}] insufficient/invalid fit')
        vol_mult = float(self.parameters.get('vol_thresh_mult', VOL_THRESH_MULT))
        threshold = vol_mult * fit['vol_now']
        if abs(fit['y_hat']) <= threshold:
            return _empty(f'[{STRATEGY_ID}] |y_hat|={abs(fit["y_hat"]):.5f} <= thresh={threshold:.5f}')
        direction = 'LONG' if fit['y_hat'] > 0 else 'SHORT'
        current_price = float(series.iloc[-1])
        ratio = abs(fit['y_hat']) / threshold
        confidence = 'HIGH' if ratio >= 2.2 else ('MED' if ratio >= 1.4 else 'LOW')
        confidence_mult = {'HIGH': 1.0, 'MED': 0.7, 'LOW': 0.45}[confidence]
        scale = self.position_scale(regime_state)
        base_size = float(self.parameters.get('base_size', BASE_SIZE))
        pos_size = round(base_size * confidence_mult * scale, 4)
        if pos_size < 0.001:
            return _empty(f'[{STRATEGY_ID}] pos_size<0.001')
        st = self.compute_stops_and_targets(
            series, direction=direction, current_price=current_price, regime_state=regime_state)

        sig = Signal(
            ticker=TICKER, direction=direction, entry_price=current_price,
            stop_loss=float(st['stop']), target_1=float(st['t1']),
            target_2=float(st['t2']), target_3=float(st['t3']),
            position_size_pct=pos_size, confidence=confidence,
            signal_params={
                'y_hat': round(fit['y_hat'], 6), 'vol_now': round(fit['vol_now'], 6),
                'threshold': round(threshold, 6), 'ratio': round(ratio, 3),
                'n_train': fit['n_train'], 'regime': regime_state,
            },
        )
        print(f'[{STRATEGY_ID}] dir={direction} y_hat={fit["y_hat"]:.5f} thresh={threshold:.5f} '
              f'ratio={ratio:.2f} size={pos_size} regime={regime_state}', file=sys.stderr)
        print('[debug] signals=1', file=sys.stderr)
        return [sig]

if __name__ == '__main__':
    import os, json
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    long_df = pd.read_parquet(f'{ROOT}/prices.parquet', filters=[('ticker', '==', TICKER)])
    prices_s = (long_df.assign(date=pd.to_datetime(long_df['date']))
                .set_index('date')['close'].sort_index().dropna())
    reg_df = pd.read_parquet(f'{ROOT}/historical_regimes.parquet')
    reg_df['date'] = pd.to_datetime(reg_df['date'])
    reg_df = reg_df.sort_values('date')

    def _lookup_regime(dt):
        mask = reg_df['date'] <= dt
        return str(reg_df.loc[mask, 'regime'].iloc[-1]) if mask.any() else 'LOW_VOL'

    rows, in_trade, ep, ed, er, edir = [], False, None, None, None, None
    for pos in range(MIN_LOOKBACK, len(prices_s), 5):
        dt, price = prices_s.index[pos], float(prices_s.iloc[pos])
        fit, reg = _fit_and_predict(prices_s.iloc[:pos + 1]), _lookup_regime(dt)
        thresh = VOL_THRESH_MULT * fit['vol_now'] if fit else None
        active = fit is not None and abs(fit['y_hat']) > (thresh or 0)
        cur_dir = ('LONG' if fit['y_hat'] > 0 else 'SHORT') if active else None
        if active and (not in_trade or cur_dir != edir):
            if in_trade:
                pnl = (price - ep) / ep if edir == 'LONG' else (ep - price) / ep
                rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(ed)[:10],
                             'regime_state': er, 'pnl': pnl, 'r_multiple': pnl / 0.02})
            in_trade, ep, ed, er, edir = True, price, dt, reg, cur_dir
        elif not active and in_trade:
            pnl = (price - ep) / ep if edir == 'LONG' else (ep - price) / ep
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(ed)[:10],
                         'regime_state': er, 'pnl': pnl, 'r_multiple': pnl / 0.02})
            in_trade = False
    if in_trade:
        price = float(prices_s.iloc[-1])
        pnl = (price - ep) / ep if edir == 'LONG' else (ep - price) / ep
        rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(ed)[:10],
                     'regime_state': er, 'pnl': pnl, 'r_multiple': pnl / 0.02})
    trades_df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    print(f'[backtest] completed_trades={len(trades_df)}', file=sys.stderr)
    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 3, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
