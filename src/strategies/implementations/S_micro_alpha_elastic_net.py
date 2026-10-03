from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from strategies.universe_default import sp500 as universe_filter

__all__ = ['MicroAlphaElasticNet']

STRATEGY_ID = 'S_micro_alpha_elastic_net'
INSTRUMENT_CLASS = 'equity'


class MicroAlphaElasticNet(BaseStrategy):
    """Elastic-net ensemble of weak cross-sectional 'micro alpha' signals -> LONG/SHORT decile, monthly.

    Hull/Bakosova/Cocquemas/Sinclair/Fast, "Micro Alphas" (JPM 2026,
    doi.org/10.3905/jpm.2026.039): many individually insignificant candidate
    signals, combined via nonlinear/time-varying feature transforms and a
    regularized (elastic-net) aggregator, jointly predict the equity risk
    premium even though no single signal passes a standalone significance
    test. Implemented here as: feature_transform(candidate signals) ->
    walk-forward cross-validated elastic-net fit on trailing monthly
    cross-sections -> rank the fitted score -> LONG top decile / SHORT
    bottom decile.
    """

    id                = STRATEGY_ID
    name              = 'MicroAlphaElasticNet'
    description       = ("Elastic-net ensemble of weak price/fundamental/macro-interaction 'micro alpha' "
                          "characteristics, cross-validated walk-forward on trailing monthly cross-sections; "
                          "LONG top decile / SHORT bottom decile, monthly rebalance.")
    tier              = 2
    min_lookback      = 1260
    signal_frequency  = 'monthly'
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    _DECILE_PCT        = 0.10
    _BASE_SIZE         = 0.03
    _REBAL_STEP        = 21     # ~1 trading month
    _N_TRAIN_SNAPSHOTS = 24     # trailing monthly snapshots in the training panel
    _MIN_TRAIN_ROWS    = 200
    _FEATURE_COLS = ['mom_12_1', 'mom_3m', 'rev_5d', 'vol_21d', 'bm', 'roe', 'inv',
                      'mom_vix_interact', 'rev_convexity']

    def _raw_features(self, psub: pd.DataFrame, financials: dict, vix_rank: float) -> pd.DataFrame | None:
        """Cross-sectional 'micro alpha' candidate signals at the LAST row of psub,
        rank-normalized to [0,1], plus nonlinear/time-varying interaction terms."""
        if len(psub) < 253:
            return None
        cur  = psub.iloc[-1]
        safe = lambda s: s.replace(0, np.nan)

        raw = {
            'mom_12_1': psub.iloc[-22] / safe(psub.iloc[-253]) - 1,
            'mom_3m':   cur / safe(psub.iloc[-64]) - 1,
            'rev_5d':   -(cur / safe(psub.iloc[-6]) - 1),
            'vol_21d':  -(psub.pct_change().iloc[-21:].std()),
        }

        if financials:
            bm, roe, inv = {}, {}, {}
            for t in psub.columns:
                fin = financials.get(t)
                if not fin:
                    continue
                p = float(cur.get(t) or 0)
                if p <= 0:
                    continue
                bv = fin.get('totalStockholdersEquity') or fin.get('bookValue')
                ni = fin.get('returnOnEquity') or fin.get('netIncome')
                ta = fin.get('totalAssetsGrowth')
                if bv is not None:
                    v = float(bv) / p
                    if np.isfinite(v):
                        bm[t] = v
                if ni is not None:
                    v = float(ni)
                    if np.isfinite(v):
                        roe[t] = v
                if ta is not None:
                    v = -float(ta)
                    if np.isfinite(v):
                        inv[t] = v
            if bm:
                raw['bm'] = pd.Series(bm)
            if roe:
                raw['roe'] = pd.Series(roe)
            if inv:
                raw['inv'] = pd.Series(inv)

        df = pd.DataFrame(raw).reindex(psub.columns)
        ranked = df.rank(pct=True)

        # Nonlinear + time-varying feature_transform per the paper's thesis:
        # momentum's edge is regime-conditional (interact with macro vol level)
        # and short-term reversal's edge is convex (bigger moves mean-revert harder).
        mom = ranked['mom_12_1'] if 'mom_12_1' in ranked else pd.Series(0.5, index=ranked.index)
        rev = ranked['rev_5d']   if 'rev_5d'   in ranked else pd.Series(0.5, index=ranked.index)
        ranked['mom_vix_interact'] = mom * float(vix_rank)
        ranked['rev_convexity']    = (rev - 0.5) ** 2

        ranked = ranked.reindex(columns=self._FEATURE_COLS)
        # Graceful degradation: a characteristic that's globally unavailable this
        # cycle (e.g. financials not loaded) becomes a neutral/no-signal column
        # rather than NaN-ing out every row in the training panel.
        for col in self._FEATURE_COLS:
            if ranked[col].isna().all():
                ranked[col] = 0.5
        return ranked

    def _vix_rank(self, aux_data: dict, as_of_idx: pd.Index, lookback: int = 756) -> float:
        """Percentile rank (0-1) of the latest VIX level vs its trailing ~3y history.
        Returns 0.5 (neutral) when VIX is unavailable so the interaction term degrades gracefully."""
        macro = (aux_data or {}).get('macro') or {}
        vix = None
        for key in ('VIX', '^VIX', 'vix'):
            if key in macro and macro[key] is not None and len(macro[key]) > 0:
                vix = macro[key]
                break
        if vix is None:
            return 0.5
        ser = pd.Series(vix).dropna()
        if not isinstance(ser.index, pd.DatetimeIndex):
            ser.index = pd.to_datetime(ser.index)
        ser = ser[ser.index <= as_of_idx[-1]].tail(lookback)
        if len(ser) < 20:
            return 0.5
        return float((ser.rank(pct=True)).iloc[-1])

    def _train_panel(self, prices: pd.DataFrame, financials: dict, aux_data: dict) -> tuple:
        """Walk-forward training panel: trailing monthly snapshots, each paired with its
        realized 21-day-forward cross-sectional-excess return. Returns (X, y) arrays."""
        n = len(prices)
        rows_X, rows_y = [], []
        for k in range(self._N_TRAIN_SNAPSHOTS, 0, -1):
            end = n - self._REBAL_STEP * k
            fwd = end + self._REBAL_STEP
            if end < 253 or fwd >= n:
                continue
            snap = prices.iloc[:end]
            vix_r = self._vix_rank(aux_data, snap.index)
            feats = self._raw_features(snap, financials, vix_r)
            if feats is None:
                continue
            ret = prices.iloc[fwd] / prices.iloc[end] - 1
            ret = ret.replace([np.inf, -np.inf], np.nan)
            excess = ret - ret.median()
            panel = feats.join(excess.rename('y')).dropna()
            if panel.empty:
                continue
            rows_X.append(panel[self._FEATURE_COLS].values)
            rows_y.append(panel['y'].values)
        if not rows_X:
            return None, None
        return np.vstack(rows_X), np.concatenate(rows_y)

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime inactive)', file=sys.stderr)
            return []

        # Monthly rebalance gate
        if len(prices) % self._REBAL_STEP != 0 and not self.cadence_reset(regime):
            print('[debug] signals=0 (off-cycle)', file=sys.stderr)
            return []

        scale   = self.position_scale(regime_state)
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 100:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []
        if len(prices) < self.min_lookback:
            print(f'[debug] signals=0 (lookback too short: {len(prices)})', file=sys.stderr)
            return []

        psub       = prices[tickers]
        financials = (aux_data or {}).get('financials') or {}

        X_train, y_train = self._train_panel(psub, financials, aux_data)
        if X_train is None or len(X_train) < self._MIN_TRAIN_ROWS:
            print('[debug] signals=0 (insufficient training panel)', file=sys.stderr)
            return []

        vix_r = self._vix_rank(aux_data, psub.index)
        cur_feats = self._raw_features(psub, financials, vix_r)
        if cur_feats is None:
            print('[debug] signals=0 (no current features)', file=sys.stderr)
            return []

        mu    = np.nanmean(X_train, axis=0)
        sigma = np.nanstd(X_train, axis=0)
        sigma = np.where(sigma > 1e-9, sigma, 1.0)
        X_train_std = (np.nan_to_num(X_train, nan=0.0) - mu) / sigma

        from sklearn.linear_model import ElasticNetCV
        from sklearn.model_selection import TimeSeriesSplit
        try:
            model = ElasticNetCV(
                l1_ratio=[0.1, 0.5, 0.7, 0.9, 1.0],
                cv=TimeSeriesSplit(n_splits=4), max_iter=2000, random_state=0,
            )
            model.fit(X_train_std, y_train)
        except Exception as exc:
            print(f'[debug] signals=0 (elastic-net fit failed: {exc})', file=sys.stderr)
            return []

        cur_std   = (cur_feats.fillna(cur_feats.median()).values - mu) / sigma
        score     = pd.Series(model.predict(cur_std), index=cur_feats.index).dropna()
        if len(score) < 30:
            print(f'[debug] signals=0 (score coverage={len(score)})', file=sys.stderr)
            return []

        n_dec         = max(1, min(int(len(score) * self._DECILE_PCT), self.MAX_SIGNALS // 2))
        long_tickers  = score.nlargest(n_dec).index.tolist()
        short_tickers = score.nsmallest(n_dec).index.tolist()
        pos_size      = min(self._BASE_SIZE * scale, 0.10)
        latest        = psub.iloc[-1]
        signals: List[Signal] = []

        for ticker, direction, bucket in (
            *[(t, 'LONG', long_tickers) for t in long_tickers],
            *[(t, 'SHORT', short_tickers) for t in short_tickers],
        ):
            price = float(latest.get(ticker, np.nan))
            if np.isnan(price) or price <= 0:
                continue
            st = self.compute_stops_and_targets(
                psub[ticker].dropna(), direction, price, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker, direction=direction,
                entry_price=round(price, 4),
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=pos_size, confidence='MED',
                signal_params={'factor': 'micro_alpha_elastic_net',
                               'score': round(float(score[ticker]), 6),
                               'alpha': round(float(model.alpha_), 6),
                               'l1_ratio': round(float(model.l1_ratio_), 4)},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]


if __name__ == '__main__':
    import os
    import json
    from backtest.quick_backtest import run_backtest_with_regime_partition

    prices_path  = 'data/master/prices.parquet'
    regimes_path = 'data/master/historical_regimes.parquet'
    if not os.path.exists(prices_path) or not os.path.exists(regimes_path):
        print('Missing data files — skipping backtest', file=sys.stderr)
        sys.exit(0)

    px = pd.read_parquet(prices_path).sort_index()
    reg = pd.read_parquet(regimes_path).sort_index()
    reg.index = pd.to_datetime(reg.index)
    px.index  = pd.to_datetime(px.index)

    universe = [c for c in px.columns if c.isalpha() and len(c) <= 5][:500]
    strategy = MicroAlphaElasticNet()
    rows = []
    for i in range(strategy.min_lookback, len(px), strategy._REBAL_STEP):
        snap    = px.iloc[:i]
        dt      = snap.index[-1]
        r_row   = reg[reg.index <= dt]
        regime_s = r_row['state'].iloc[-1] if not r_row.empty and 'state' in r_row else 'LOW_VOL'
        signals = strategy.generate_signals(snap, {'state': regime_s}, universe)
        for s in signals:
            fwd = px[s.ticker].reindex(px.index[i:i + 21]).dropna()
            if len(fwd) < 1:
                continue
            ret = (fwd.iloc[-1] - s.entry_price) / s.entry_price
            if s.direction == 'SHORT':
                ret = -ret
            risk = abs(s.entry_price - s.stop_loss)
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(dt.date()),
                         'regime_state': regime_s, 'pnl': float(ret),
                         'r_multiple': float(ret * s.entry_price / risk) if risk > 0 else 0.0})

    if not rows:
        print('No trades generated', file=sys.stderr)
        sys.exit(1)

    result = run_backtest_with_regime_partition(
        pd.DataFrame(rows), strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
