"""
Investor Attention Market Timing — Chen, Tang, Yao & Zhou (2021)
"Investor Attention and Stock Returns", Journal of Financial and Quantitative Analysis.
https://doi.org/10.1017/s0022109021000090

PLS composite attention index from price/volume-proxy/volatility features predicts
the equity risk premium. LONG SPY when the attention index is positive (reversal of
attention-driven price pressure expected); FLAT otherwise.

Proxies used (adapted to available ledger columns):
  P1 — SPY absolute return (trading-activity attention)
  P2 — cross-sectional average absolute return (market-wide turnover activity)
  P3 — VIX level (fear/attention from macro)
  P4 — cross-sectional return dispersion (breadth-vol attention)
  P5 — realized-vol spread: high-variance decile minus low-variance decile (§4)

GSVI and news-count proxies listed as optional in the strategy spec are not in the
ledger and are excluded; the 5 available proxies suffice for PLS 1-component.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['InvestorAttentionMarketTiming']

INSTRUMENT_CLASS  = 'equity'
STRATEGY_ID       = 'S_investor_attention_market_timing'
MARKET_PROXY      = 'SPY'
TRAIN_WINDOW      = 504     # ~2 trading years lookback for PLS fit [§3]
VOL_WINDOW        = 21      # short-term volume-activity zscore window
TURNOVER_WINDOW   = 252     # market turnover zscore window
DISP_WINDOW       = 63      # dispersion / realized-vol windows [§2, §4]
BASE_SIZE         = 0.10    # base gross fraction of portfolio


def _zscore(series: pd.Series, window: int, min_periods: int = 20) -> pd.Series:
    mu  = series.rolling(window, min_periods=min_periods).mean()
    sd  = series.rolling(window, min_periods=min_periods).std()
    return (series - mu) / sd.replace(0, np.nan)


def _build_proxies(
    prices: pd.DataFrame,
    vix_series: pd.Series,
) -> pd.DataFrame:
    """Return a DataFrame of 5 z-scored attention proxies aligned to prices.index."""
    rets = prices.pct_change()

    # P1: SPY absolute return — short-term trading activity
    spy_abs_ret = rets[MARKET_PROXY].abs()
    p1 = _zscore(spy_abs_ret, VOL_WINDOW)

    # P2: cross-sectional average absolute return — market-wide activity proxy
    cs_abs_ret = rets.abs().mean(axis=1)
    p2 = _zscore(cs_abs_ret, TURNOVER_WINDOW)

    # P3: VIX level — fear / investor attention [§2]
    vix_aligned = vix_series.reindex(prices.index, method='ffill')
    p3 = _zscore(vix_aligned, TURNOVER_WINDOW)

    # P4: cross-sectional return dispersion [§2]
    cs_disp = rets.std(axis=1)
    p4 = _zscore(cs_disp, DISP_WINDOW)

    # P5: realized-vol spread — high-variance vs low-variance stocks [§4]
    rv_trailing = rets.rolling(DISP_WINDOW).std()
    rv80 = rv_trailing.quantile(0.8, axis=1)
    rv20 = rv_trailing.quantile(0.2, axis=1)
    vol_spread = rv80 - rv20
    p5 = _zscore(vol_spread, DISP_WINDOW)

    return pd.DataFrame({'p1': p1, 'p2': p2, 'p3': p3, 'p4': p4, 'p5': p5},
                        index=prices.index)


class InvestorAttentionMarketTiming(BaseStrategy):
    """
    PLS-composite investor attention index times the market (SPY).
    LONG when the 1-component PLS score is positive; FLAT otherwise.
    """

    id                = STRATEGY_ID
    name              = 'InvestorAttentionMarketTiming'
    description       = (
        'PLS composite investor attention index (Chen et al. 2021) '
        'times the S&P 500 proxy: LONG SPY when attention score > 0, else FLAT.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = TRAIN_WINDOW + DISP_WINDOW
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'train_window': TRAIN_WINDOW,
            'base_size':    BASE_SIZE,
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
        if MARKET_PROXY not in prices.columns:
            print(f'[debug] {STRATEGY_ID}: signals=0 (SPY not in prices)', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] {STRATEGY_ID}: signals=0 (regime={regime_state} excluded)', file=sys.stderr)
            return []

        # VIX level (P3). Prefer the ^VIX column in the price panel (point-in-time,
        # no per-bar I/O) — the backtest/live panel carries it as a ticker. Fall
        # back to aux_data macro['VIX'] if a caller wires it that way. (The prior
        # code read ONLY aux_data['macro']['VIX'], which the loader never
        # populates → every bar bailed "VIX unavailable" → 0 trades.)
        vix_series = None
        if '^VIX' in prices.columns:
            vix_series = prices['^VIX'].dropna()
        if vix_series is None or getattr(vix_series, 'empty', True):
            vix_series = ((aux_data or {}).get('macro', {}) or {}).get('VIX')
        if vix_series is None or getattr(vix_series, 'empty', True):
            print(f'[debug] {STRATEGY_ID}: signals=0 (VIX unavailable)', file=sys.stderr)
            return []

        # Need enough cross-sectional tickers for meaningful dispersion proxies
        cs_cols = [c for c in prices.columns if c in universe or c == MARKET_PROXY]
        if len(cs_cols) < 10:
            cs_cols = list(prices.columns)
        # Cap the cross-section to bound memory/compute. Without a universe
        # resolver the universe is every ticker (~5800) and the proxy build OOMs;
        # a few hundred names give a stable market-breadth signal. Always keep SPY
        # and never let ^VIX leak into the equity cross-section.
        MAX_CS = 600
        cs_cols = [c for c in cs_cols if c != '^VIX']
        if len(cs_cols) > MAX_CS:
            keep = [c for c in cs_cols if c != MARKET_PROXY][:MAX_CS - 1]
            cs_cols = [MARKET_PROXY] + keep
        prices_cs = prices[cs_cols].dropna(axis=1, how='all')
        if MARKET_PROXY not in prices_cs.columns:
            print(f'[debug] {STRATEGY_ID}: signals=0 (SPY dropped from cs_cols)', file=sys.stderr)
            return []

        train_window = int(self.parameters.get('train_window', TRAIN_WINDOW))
        min_required = train_window + DISP_WINDOW + 5
        if len(prices_cs) < min_required:
            print(f'[debug] {STRATEGY_ID}: signals=0 (insufficient bars={len(prices_cs)})',
                  file=sys.stderr)
            return []

        # Build proxy matrix
        proxies = _build_proxies(prices_cs, vix_series)

        # Forward return for PLS target: spy_return[t+1] is the y variable
        spy_fwd_ret = prices_cs[MARKET_PROXY].pct_change().shift(-1)

        # Align proxies + target; drop rows with any NaN
        PROXY_COLS = ['p1', 'p2', 'p3', 'p4', 'p5']
        data = proxies[PROXY_COLS].join(spy_fwd_ret.rename('fwd_ret')).dropna()

        # Need at least train_window + 1 obs (train window excludes last obs, no fwd_ret)
        if len(data) < train_window + 2:
            print(f'[debug] {STRATEGY_ID}: signals=0 (data after dropna={len(data)})',
                  file=sys.stderr)
            return []

        # Latest bar's proxy values (no fwd_ret needed here)
        latest_proxies_series = proxies[PROXY_COLS].iloc[-1]
        if latest_proxies_series.isna().any():
            print(f'[debug] {STRATEGY_ID}: signals=0 (NaN in latest proxies)', file=sys.stderr)
            return []

        # Training set: last train_window obs from the aligned data (excludes the current bar)
        train_df = data.iloc[-train_window:]
        if len(train_df) < 50:
            print(f'[debug] {STRATEGY_ID}: signals=0 (train set too small)', file=sys.stderr)
            return []

        X_train = train_df[PROXY_COLS].values.astype(float)
        y_train = train_df['fwd_ret'].values.astype(float)

        # Fit PLS with 1 component (§3: PLS extraction)
        try:
            from sklearn.cross_decomposition import PLSRegression
            pls = PLSRegression(n_components=1, scale=True, max_iter=500)
            pls.fit(X_train, y_train)
            X_now = latest_proxies_series.values.reshape(1, -1).astype(float)
            pred  = pls.predict(X_now)
            # sklearn returns (1,) or (1,1) depending on y shape at fit time
            attention_index = float(np.ravel(pred)[0])
        except Exception as exc:
            print(f'[debug] {STRATEGY_ID}: signals=0 (PLS error: {exc})', file=sys.stderr)
            return []

        if attention_index <= 0.0:
            # Attention-driven price pressure not expected to reverse → stay FLAT
            print(f'[debug] {STRATEGY_ID}: signals=0 (attention_index={attention_index:.4f} ≤ 0)',
                  file=sys.stderr)
            return []

        # Generate LONG SPY signal
        spy_price = float(prices_cs[MARKET_PROXY].dropna().iloc[-1])
        if spy_price <= 0:
            print(f'[debug] {STRATEGY_ID}: signals=0 (invalid SPY price)', file=sys.stderr)
            return []

        scale    = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('base_size', BASE_SIZE)) * scale, 4)
        pos_size = max(0.01, min(pos_size, 0.20))

        st = self.compute_stops_and_targets(
            prices_cs[MARKET_PROXY].dropna(),
            direction='LONG',
            current_price=spy_price,
            regime_state=regime_state,
        )

        # Conviction scales with magnitude of attention index
        abs_ai = abs(attention_index)
        confidence = 'HIGH' if abs_ai > 0.002 else ('MED' if abs_ai > 0.0005 else 'LOW')

        sig = Signal(
            ticker            = MARKET_PROXY,
            direction         = 'LONG',
            entry_price       = spy_price,
            stop_loss         = float(st['stop']),
            target_1          = float(st['t1']),
            target_2          = float(st['t2']),
            target_3          = float(st['t3']),
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'attention_index': round(attention_index, 6),
                'regime':          regime_state,
                'train_n':         len(train_df),
            },
        )

        print(
            f'[debug] {STRATEGY_ID}: signals=1 '
            f'attention_index={attention_index:.6f} regime={regime_state} size={pos_size}',
            file=sys.stderr,
        )
        return [sig]


def run_regime_backtest():
    """Offline helper: compute regime-partitioned backtest metrics for lifecycle promotion gate.

    Loads prices + macro from master parquets, runs the attention-index logic over the
    full history, marks trades with the regime active on each signal date, then calls
    run_backtest_with_regime_partition() so the lifecycle gate can read
    result['eligible_regimes_proposed'].
    """
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    import pandas as pd
    import numpy as np
    from backtest.quick_backtest import run_backtest_with_regime_partition

    prices_path  = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'master', 'prices.parquet')
    macro_path   = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'master', 'macro.parquet')
    regime_path  = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'master', 'historical_regimes.parquet')

    # Load close prices (wide: date x ticker)
    raw = pd.read_parquet(prices_path, columns=['ticker', 'date', 'close'])
    prices = raw.pivot(index='date', columns='ticker', values='close')
    prices.index = pd.to_datetime(prices.index)
    prices.sort_index(inplace=True)

    if MARKET_PROXY not in prices.columns:
        print(f'[backtest] SPY not in prices — aborting', file=sys.stderr)
        return None

    # Load VIX
    mac = pd.read_parquet(macro_path)
    mac['date'] = pd.to_datetime(mac['date'])
    vix_df = mac[mac['series'] == 'VIX']
    vix_series = vix_df.set_index('date')['value'].sort_index()

    # Load historical regimes
    reg = pd.read_parquet(regime_path)
    reg['date'] = pd.to_datetime(reg['date'] if 'date' in reg.columns else reg.index)
    if 'date' in reg.columns:
        reg = reg.set_index('date')
    regime_col = next((c for c in ('regime_state', 'state', 'regime') if c in reg.columns), None)

    # Build attention proxies
    cs_cols = [c for c in prices.columns if c in prices.columns]
    prices_cs = prices[cs_cols].dropna(axis=1, how='all')
    proxies   = _build_proxies(prices_cs, vix_series)
    PROXY_COLS = ['p1', 'p2', 'p3', 'p4', 'p5']
    spy_ret   = prices_cs[MARKET_PROXY].pct_change()
    spy_fwd   = spy_ret.shift(-1)

    data = proxies[PROXY_COLS].join(spy_fwd.rename('fwd_ret')).dropna()

    # Rolling-window PLS: for each bar t, train on [t-TRAIN_WINDOW, t), predict t
    from sklearn.cross_decomposition import PLSRegression
    rows = []
    for i in range(TRAIN_WINDOW, len(data) - 1):
        train = data.iloc[i - TRAIN_WINDOW:i]
        today = data.iloc[[i]]
        pls = PLSRegression(n_components=1, scale=True, max_iter=500)
        try:
            pls.fit(train[PROXY_COLS].values, train['fwd_ret'].values)
            ai = float(np.ravel(pls.predict(today[PROXY_COLS].values))[0])
        except Exception:
            continue
        signal_date = data.index[i]
        regime_state = 'LOW_VOL'
        if regime_col and signal_date in reg.index:
            regime_state = str(reg.loc[signal_date, regime_col])
        # Mark as LONG when ai > 0; pnl = next-day SPY return
        direction = 'LONG' if ai > 0.0 else 'FLAT'
        pnl = float(data['fwd_ret'].iloc[i]) if direction == 'LONG' else 0.0
        rows.append({
            'strategy_id': STRATEGY_ID,
            'signal_date': str(signal_date.date()),
            'regime_state': regime_state,
            'direction': direction,
            'pnl': pnl,
            'r_multiple': pnl / max(abs(pnl), 1e-6) if direction == 'LONG' else 0.0,
        })

    trades_df = pd.DataFrame(rows)
    if trades_df.empty:
        print('[backtest] no trades generated', file=sys.stderr)
        return None

    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(f'[backtest] eligible_regimes_proposed={result.get("eligible_regimes_proposed")}',
          file=sys.stderr)
    return result


if __name__ == '__main__':
    run_regime_backtest()
