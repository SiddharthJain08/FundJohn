"""
S_risk_neutral_skew_cross_section — Risk-Neutral Skewness Cross-Section
Dennis & Mayhew (2002 JF): Cross-sectional residual risk-neutral skew.
SELL_VOL stocks with most negative residual (overpriced downside protection);
BUY_VOL stocks with most positive residual (cheap protection). Weekly rebalance.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['RiskNeutralSkewCrossSection']


class RiskNeutralSkewCrossSection(BaseStrategy):
    """Cross-sectional residual risk-neutral skew factor: SELL_VOL / BUY_VOL quintiles."""

    id          = 'S_risk_neutral_skew_cross_section'
    name        = 'RiskNeutralSkewCrossSection'
    description = (
        'Stocks with anomalously negative residual risk-neutral skewness have '
        'overpriced downside protection exploitable via cross-sectional put-spread selling.'
    )
    tier             = 2
    min_lookback     = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    RV_WINDOW  = 21
    QUINTILE   = 0.20
    BASE_SIZE  = 0.012

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []
        # Weekly rebalance: Mondays only
        last_date = prices.index[-1]
        if hasattr(last_date, 'dayofweek') and last_date.dayofweek != 0:
            print('[debug] signals=0 (non-rebalance day)', file=sys.stderr)
            return []

        aux = aux_data or {}
        options    = aux.get('options_eod')
        financials = aux.get('financials')
        if options is None or (hasattr(options, 'empty') and options.empty):
            print('[debug] signals=0 (no options_eod)', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── IV skew per ticker ────────────────────────────────────────────────
        iv_skew = self._get_iv_skew(options, universe)
        if len(iv_skew) < 20:
            print(f'[debug] signals=0 (iv_skew only {len(iv_skew)} tickers)', file=sys.stderr)
            return []

        tickers = list(iv_skew.index)
        avail   = [t for t in tickers if t in prices.columns]
        if len(avail) < 20:
            print('[debug] signals=0 (price unavailable)', file=sys.stderr)
            return []

        # ── Covariates ────────────────────────────────────────────────────────
        ret     = prices[avail].pct_change().fillna(0)
        mkt_ret = ret.mean(axis=1)
        n       = min(252, len(ret))
        ret_w   = ret.iloc[-n:]
        mkt_w   = mkt_ret.iloc[-n:].values
        market_rv   = float(pd.Series(mkt_w).rolling(self.RV_WINDOW).std().iloc[-1] * np.sqrt(252))
        index_skew  = float(iv_skew.reindex(avail).dropna().median())
        log_size    = self._log_size(financials, avail, prices)
        log_vol     = ret.rolling(self.RV_WINDOW).std().iloc[-1].reindex(avail).apply(
            lambda x: np.log(max(x, 1e-8)) if not np.isnan(x) else np.nan
        )

        # ── Beta ──────────────────────────────────────────────────────────────
        beta_map = {}
        for tkr in avail:
            y = ret_w[tkr].values
            valid = ~np.isnan(y) & ~np.isnan(mkt_w)
            if valid.sum() < 60:
                continue
            A = np.column_stack([np.ones(valid.sum()), mkt_w[valid]])
            try:
                coef, _, _, _ = np.linalg.lstsq(A, y[valid], rcond=None)
                beta_map[tkr] = float(coef[1])
            except Exception:
                pass

        # ── Cross-sectional OLS: residual skew ───────────────────────────────
        rows = []
        for tkr in avail:
            vals = {
                'iv_skew': iv_skew.get(tkr),
                'beta': beta_map.get(tkr),
                'log_size': log_size.get(tkr),
                'log_vol': log_vol.get(tkr),
            }
            if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in vals.values()):
                continue
            rows.append({'ticker': tkr, **vals})

        if len(rows) < 20:
            print(f'[debug] signals=0 ({len(rows)} valid rows)', file=sys.stderr)
            return []

        df = pd.DataFrame(rows).set_index('ticker')
        y_cs = df['iv_skew'].values
        X_cs = np.column_stack([
            np.ones(len(df)), df['beta'].values,
            np.full(len(df), market_rv), np.full(len(df), index_skew),
            df['log_size'].values, df['log_vol'].values,
        ])
        try:
            coef, _, _, _ = np.linalg.lstsq(X_cs, y_cs, rcond=None)
            df['resid'] = y_cs - X_cs @ coef
        except Exception:
            print('[debug] signals=0 (OLS failed)', file=sys.stderr)
            return []

        df['rank'] = df['resid'].rank(pct=True)
        sell_cands = df[df['rank'] <= self.QUINTILE]
        buy_cands  = df[df['rank'] >= 1 - self.QUINTILE]
        cur_prices = prices.iloc[-1]
        signals: List[Signal] = []

        for tkr, row in sell_cands.iterrows():
            cp = float(cur_prices.get(tkr, np.nan))
            if np.isnan(cp) or cp <= 0:
                continue
            stops = self.compute_stops_and_targets(
                prices[tkr].dropna(), 'SHORT', cp, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=tkr, direction='SELL_VOL',
                entry_price=round(cp, 4), stop_loss=stops['stop'],
                target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=round(self.BASE_SIZE * scale, 4),
                confidence='HIGH' if row['rank'] <= 0.05 else 'MED',
                signal_params={'residual_skew': round(float(row['resid']), 5),
                               'rank': round(float(row['rank']), 3),
                               'beta': round(float(row['beta']), 3)},
                features={'iv_skew': round(float(row['iv_skew']), 5)},
            ))

        for tkr, row in buy_cands.iterrows():
            cp = float(cur_prices.get(tkr, np.nan))
            if np.isnan(cp) or cp <= 0:
                continue
            stops = self.compute_stops_and_targets(
                prices[tkr].dropna(), 'LONG', cp, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=tkr, direction='BUY_VOL',
                entry_price=round(cp, 4), stop_loss=stops['stop'],
                target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=round(self.BASE_SIZE * scale, 4),
                confidence='HIGH' if row['rank'] >= 0.95 else 'MED',
                signal_params={'residual_skew': round(float(row['resid']), 5),
                               'rank': round(float(row['rank']), 3),
                               'beta': round(float(row['beta']), 3)},
                features={'iv_skew': round(float(row['iv_skew']), 5)},
            ))

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_iv_skew(self, options, universe: list) -> pd.Series:
        """Series(ticker → iv_skew). Handles dict and DataFrame formats."""
        if isinstance(options, dict):
            return pd.Series({t: float(v) for t in universe
                              for v in [options.get(t, {}).get('skew_20d',
                                        options.get(t, {}).get('iv_skew'))]
                              if v is not None})
        if not isinstance(options, pd.DataFrame) or options.empty:
            return pd.Series(dtype=float)
        iv_col  = next((c for c in ['implied_volatility', 'iv'] if c in options.columns), None)
        tkr_col = next((c for c in ['ticker', 'symbol'] if c in options.columns), None)
        if iv_col is None or tkr_col is None:
            return pd.Series(dtype=float)
        date_col  = next((c for c in ['date', 'quote_date'] if c in options.columns), None)
        mon_col   = next((c for c in ['moneyness', 'strike_pct'] if c in options.columns), None)
        delta_col = next((c for c in ['delta', 'Delta'] if c in options.columns), None)
        right_col = next((c for c in ['right', 'option_type', 'type'] if c in options.columns), None)
        snap = options[options[date_col] == options[date_col].max()] if date_col else options
        result = {}
        for tkr, grp in snap[snap[tkr_col].isin(universe)].groupby(tkr_col):
            puts = grp[grp[right_col].astype(str).str.upper().isin(['P', 'PUT'])] if right_col else grp
            if mon_col:
                otm = puts[puts[mon_col] < 0.95][iv_col].mean()
                atm = grp[grp[mon_col].between(0.97, 1.03)][iv_col].mean()
            elif delta_col:
                otm = puts[puts[delta_col].abs() < 0.35][iv_col].mean()
                atm = grp[grp[delta_col].abs().between(0.45, 0.55)][iv_col].mean()
            else:
                sv = np.sort(grp[iv_col].dropna().values)
                otm, atm = (sv[0], sv[len(sv)//2]) if len(sv) >= 2 else (np.nan, np.nan)
            if not (np.isnan(float(otm)) or np.isnan(float(atm))):
                result[tkr] = float(otm) - float(atm)
        return pd.Series(result)

    def _log_size(self, financials, tickers: list, prices: pd.DataFrame) -> pd.Series:
        """Log market cap; fallback to log price."""
        if financials is not None and isinstance(financials, pd.DataFrame) and not financials.empty:
            cap_col = next((c for c in ['market_cap', 'marketCap', 'mktcap'] if c in financials.columns), None)
            if cap_col:
                grp = financials.groupby(level='ticker') if 'ticker' in getattr(financials.index, 'names', []) else None
                caps = grp[cap_col].last().reindex(tickers) if grp else financials[cap_col].reindex(tickers)
                valid = caps[caps > 0].dropna()
                if len(valid) > 10:
                    return np.log(valid).reindex(tickers)
        last = prices.iloc[-1].reindex(tickers)
        return np.log(last[last > 0]).reindex(tickers)
