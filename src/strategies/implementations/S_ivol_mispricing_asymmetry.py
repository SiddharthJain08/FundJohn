"""
S_ivol_mispricing_asymmetry — Arbitrage Asymmetry & IVOL Puzzle
Stambaugh, Yu & Yuan (JF 2015): LONG high-IVOL underpriced stocks,
SHORT high-IVOL overpriced stocks; asymmetric arbitrage corrects only the
underpriced leg while short-sale constraints preserve overpricing.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['IvolMispricingAsymmetry']


class IvolMispricingAsymmetry(BaseStrategy):
    """LONG high-IVOL underpriced, SHORT high-IVOL overpriced; equal-weight legs."""

    id          = 'S_ivol_mispricing_asymmetry'
    name        = 'IvolMispricingAsymmetry'
    description = 'Arbitrage asymmetry IVOL puzzle: long underpriced, short overpriced high-IVOL stocks'
    tier        = 2
    signal_frequency = 'monthly'
    min_lookback     = 504

    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    # Tuning knobs
    IVOL_LOOKBACK    = 21   # trading days for FF3-residual IVOL
    IVOL_TERCILE_THR = 0.67 # top-tercile cut (top 33%)
    MISP_QUINTILE    = 0.20 # top/bottom 20% for mispricing quintiles
    BASE_SIZE        = 0.012

    # ── helper: cross-sectional pct-rank (0=underpriced, 1=overpriced) ─────
    @staticmethod
    def _pctrank(s: pd.Series) -> pd.Series:
        return s.rank(pct=True, na_option='keep')

    # ── helper: annualised IVOL from a returns series ──────────────────────
    @staticmethod
    def _ivol(ret: pd.Series, mkt: pd.Series, smb: pd.Series, hml: pd.Series) -> float:
        df = pd.concat([ret, mkt, smb, hml], axis=1).dropna()
        if len(df) < 10:
            return np.nan
        X = df.iloc[:, 1:].values
        y = df.iloc[:, 0].values
        try:
            coef, res, _, _ = np.linalg.lstsq(
                np.column_stack([np.ones(len(X)), X]), y, rcond=None
            )
            resid = y - np.column_stack([np.ones(len(X)), X]) @ coef
            return float(np.std(resid, ddof=1) * np.sqrt(252))
        except Exception:
            return np.nan

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

        scale = self.position_scale(regime_state)
        aux   = aux_data or {}

        # ── build returns ──────────────────────────────────────────────────
        closes = prices.copy()
        tickers = [t for t in universe if t in closes.columns]
        if len(tickers) < self.minimum_universe_size_floor():
            print('[debug] signals=0', file=sys.stderr)
            return []

        closes = closes[tickers].sort_index()
        ret = closes.pct_change().fillna(0)

        # ── proxy FF3 factors from SPY / IWM / LQD if available ───────────
        mkt_proxy = ret.mean(axis=1)  # equal-weight mkt proxy
        # SMB: small (bottom-half mktcap) minus large; HML: proxy flat if no data
        smb_proxy = pd.Series(0.0, index=ret.index)
        hml_proxy = pd.Series(0.0, index=ret.index)

        # ── Step 1: compute IVOL for each ticker ───────────────────────────
        last_date = closes.index[-1]
        recent_ret = ret.iloc[-self.IVOL_LOOKBACK:]
        recent_mkt = mkt_proxy.iloc[-self.IVOL_LOOKBACK:]
        recent_smb = smb_proxy.iloc[-self.IVOL_LOOKBACK:]
        recent_hml = hml_proxy.iloc[-self.IVOL_LOOKBACK:]

        ivol_vals: dict = {}
        for tkr in tickers:
            ivol_vals[tkr] = self._ivol(
                recent_ret[tkr], recent_mkt, recent_smb, recent_hml
            )

        ivol_series = pd.Series(ivol_vals).dropna()
        if len(ivol_series) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        ivol_rank = ivol_series.rank(pct=True)

        # ── Step 2: composite mispricing score (MISP) ─────────────────────
        # Use available proxies from financials; degrade gracefully if absent
        fin = aux.get('financials', pd.DataFrame())
        misp_components: list[pd.Series] = []

        if fin is not None and not fin.empty:
            fin_latest = (
                fin.sort_index().groupby(level='ticker').last()
                if 'ticker' in fin.index.names
                else fin
            )
            def _col_rank(col):
                if col in fin_latest.columns:
                    s = fin_latest[col].reindex(tickers)
                    return self._pctrank(s)
                return None

            # Momentum 12-1: price-based
            if len(closes) >= 252:
                mom = (closes.iloc[-21] / closes.iloc[-252] - 1).reindex(tickers)
                misp_components.append(self._pctrank(mom))

            # Accruals (ACC) — higher accruals = more overpriced
            acc = _col_rank('accruals')
            if acc is not None: misp_components.append(acc)

            # Asset growth (AG)
            ag = _col_rank('asset_growth')
            if ag is not None: misp_components.append(ag)

            # ROA — high ROA = underpriced, so invert
            roa = _col_rank('roa')
            if roa is not None: misp_components.append(1.0 - roa)

            # Net stock issuance (NSI) — higher issuance = overpriced
            nsi = _col_rank('net_stock_issuance')
            if nsi is not None: misp_components.append(nsi)

            # Gross profit (GP) — higher GP = underpriced, so invert
            gp = _col_rank('gross_profit_assets')
            if gp is not None: misp_components.append(1.0 - gp)

        # Fallback: if no financials, use 12-month momentum as sole mispricing proxy
        if not misp_components:
            if len(closes) >= 252:
                mom = (closes.iloc[-21] / closes.iloc[-252] - 1).reindex(tickers)
                misp_components.append(self._pctrank(mom))
            else:
                # Use short-window momentum as last resort
                if len(closes) >= 63:
                    mom = (closes.iloc[-1] / closes.iloc[-63] - 1).reindex(tickers)
                    misp_components.append(self._pctrank(mom))

        if not misp_components:
            print('[debug] signals=0', file=sys.stderr)
            return []

        misp_df    = pd.concat(misp_components, axis=1).dropna(how='all')
        misp_score = misp_df.mean(axis=1).reindex(tickers).dropna()

        # ── Step 3: construct LONG / SHORT candidates ──────────────────────
        common = ivol_rank.index.intersection(misp_score.index)
        if len(common) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        iv = ivol_rank.reindex(common)
        ms = misp_score.reindex(common)

        top_ivol    = iv >= self.IVOL_TERCILE_THR
        underpriced = ms <= self.MISP_QUINTILE   # bottom quintile MISP
        overpriced  = ms >= (1 - self.MISP_QUINTILE)  # top quintile MISP

        long_cands  = common[top_ivol & underpriced].tolist()
        short_cands = common[top_ivol & overpriced].tolist()

        signals: List[Signal] = []
        current_prices = closes.iloc[-1]

        for tkr in long_cands[:self.MAX_SIGNALS // 2]:
            cp = float(current_prices.get(tkr, np.nan))
            if np.isnan(cp) or cp <= 0:
                continue
            stops = self.compute_stops_and_targets(
                closes[tkr].dropna(), 'LONG', cp, regime_state=regime_state
            )
            conf = 'HIGH' if iv.get(tkr, 0) >= 0.90 else 'MED'
            signals.append(Signal(
                ticker=tkr,
                direction='LONG',
                entry_price=round(cp, 4),
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(self.BASE_SIZE * scale, 4),
                confidence=conf,
                signal_params={
                    'ivol': round(float(ivol_vals.get(tkr, 0)), 4),
                    'misp_score': round(float(ms.get(tkr, 0)), 4),
                    'leg': 'long_underpriced',
                },
                features={'ivol': round(float(ivol_vals.get(tkr, 0)), 4)},
            ))

        for tkr in short_cands[:self.MAX_SIGNALS // 2]:
            cp = float(current_prices.get(tkr, np.nan))
            if np.isnan(cp) or cp <= 0:
                continue
            stops = self.compute_stops_and_targets(
                closes[tkr].dropna(), 'SHORT', cp, regime_state=regime_state
            )
            conf = 'HIGH' if iv.get(tkr, 0) >= 0.90 else 'MED'
            signals.append(Signal(
                ticker=tkr,
                direction='SHORT',
                entry_price=round(cp, 4),
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(self.BASE_SIZE * scale, 4),
                confidence=conf,
                signal_params={
                    'ivol': round(float(ivol_vals.get(tkr, 0)), 4),
                    'misp_score': round(float(ms.get(tkr, 0)), 4),
                    'leg': 'short_overpriced',
                },
                features={'ivol': round(float(ivol_vals.get(tkr, 0)), 4)},
            ))

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    @staticmethod
    def minimum_universe_size_floor() -> int:
        return 50
