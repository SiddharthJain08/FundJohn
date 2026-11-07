from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal
from strategies.safe_ops import EPS

INSTRUMENT_CLASS = "etp"
STRATEGY_ID = "S_eigenportfolio_collision_penalty"

__all__ = ["EigenportfolioCollisionPenalty"]

# Cross-asset ETF panel (equities / govt+credit bonds / commodities / real estate / FX).
ETF_PANEL = [
    "SPY", "QQQ", "IWM", "EFA", "EEM",
    "TLT", "IEF", "SHY", "HYG", "LQD",
    "GLD", "SLV", "USO", "DBC",
    "VNQ", "UUP",
]

MOM_WINDOW_DAYS  = 126   # ~26-week trailing total return for the base tilt (variant choice)
COV_WINDOW_WEEKS = 104   # 2Y trailing weekly-return window for the eigenportfolio estimate
PENALTY_LAMBDA   = 0.6   # overlap-penalty strength (partial toll, not full projection removal)
GROSS_TARGET     = 1.0   # target gross |weight| of the candidate book pre-scaling
POSITION_SCALER  = 0.5   # maps renormalized |weight| -> position_size_pct before regime scale
N_LONG_SHORT     = 5     # top/bottom N names by momentum z-score get a position


class EigenportfolioCollisionPenalty(BaseStrategy):
    """
    Benhamou et al. (2026) "Penalize the Common Trade" (arXiv:2611.03453) —
    before finalizing a momentum tilt across a cross-asset ETF panel, project
    the candidate book onto the panel's leading eigenportfolio (the dominant
    common-return mode of the trailing correlation matrix) and subtract an
    overlap penalty proportional to that projection, pushing the book away
    from the crowded "common trade". Weekly rebalance.

    Interpretation choices made where the abstract/pseudocode is silent
    (variant 1 of 2 — deliberately different from a colleague's read):
    base tilt = 126-trading-day (~26w) momentum z-score, not 52w; the common
    -return mode is extracted from the trailing CORRELATION matrix (not raw
    covariance) over a 104-week window so no single high-vol ETP (e.g. USO)
    dominates PC1 by scale alone; the penalty is a single orthogonal
    pushback w' = w - lambda*(w.v)*v with lambda=0.6 (a partial toll, not a
    full projection-removal), then the book is renormalized back to its
    original gross exposure.
    """

    id                = STRATEGY_ID
    name              = "EigenportfolioCollisionPenalty"
    description       = ("Cross-asset ETF momentum tilt penalized by projection onto the panel's "
                         "leading eigenportfolio (crowded common-return mode); weekly rebalance.")
    tier              = 2
    min_lookback      = 756
    signal_frequency  = "weekly"
    active_in_regimes = ["LOW_VOL", "TRANSITIONING", "HIGH_VOL"]

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print("[debug] signals=0 (empty prices)", file=sys.stderr)
            return []

        regime_state = regime.get("state", "LOW_VOL")
        if not self.should_run(regime_state):
            print("[debug] signals=0 (regime filtered)", file=sys.stderr)
            return []

        tickers = [t for t in ETF_PANEL if t in prices.columns]
        if len(tickers) < 10:
            print(f"[debug] signals=0 (panel too small: {len(tickers)})", file=sys.stderr)
            return []

        panel = prices[tickers].ffill().dropna(how="all")
        if len(panel) < self.min_lookback:
            print(f"[debug] signals=0 (insufficient history rows={len(panel)})", file=sys.stderr)
            return []

        # Weekly rebalance gate — fire only on a new ISO week, or on a regime flip.
        if isinstance(panel.index, pd.DatetimeIndex) and len(panel.index) >= 2:
            last_wk = panel.index[-1].isocalendar()[1]
            prev_wk = panel.index[-2].isocalendar()[1]
            if last_wk == prev_wk and not self.cadence_reset(regime):
                print("[debug] signals=0 (mid-week, no rebalance)", file=sys.stderr)
                return []

        scale = self.position_scale(regime_state)

        # --- Step 1: base candidate tilt — 126d momentum z-score ------------
        if len(panel) < MOM_WINDOW_DAYS + 5:
            print("[debug] signals=0 (insufficient momentum window)", file=sys.stderr)
            return []
        mom_ret = panel.iloc[-1] / panel.iloc[-MOM_WINDOW_DAYS] - 1.0
        mom_ret = mom_ret.replace([np.inf, -np.inf], np.nan).dropna()
        if len(mom_ret) < 10:
            print("[debug] signals=0 (momentum nan-out)", file=sys.stderr)
            return []
        z = (mom_ret - mom_ret.mean()) / (mom_ret.std(ddof=1) + EPS)

        ranked = z.sort_values()
        n = min(N_LONG_SHORT, len(ranked) // 2)
        if n < 2:
            print("[debug] signals=0 (panel too small for L/S split)", file=sys.stderr)
            return []
        short_names = ranked.index[:n]
        long_names = ranked.index[-n:]

        w = pd.Series(0.0, index=panel.columns)
        w.loc[long_names] = GROSS_TARGET / (2 * n)
        w.loc[short_names] = -GROSS_TARGET / (2 * n)

        # --- Step 2: leading eigenportfolio off the trailing correlation ---
        try:
            weekly = panel.resample("W-FRI").last().pct_change().dropna(how="all")
            weekly = weekly.tail(COV_WINDOW_WEEKS)
            weekly = weekly.dropna(axis=1, thresh=max(20, int(len(weekly) * 0.8)))
            weekly = weekly.dropna(how="any")
        except Exception:
            print("[debug] signals=0 (weekly resample failed)", file=sys.stderr)
            return []
        common = [t for t in panel.columns if t in weekly.columns]
        if len(weekly) < 30 or len(common) < 10:
            print(f"[debug] signals=0 (weekly cov insufficient rows={len(weekly)} cols={len(common)})",
                  file=sys.stderr)
            return []

        corr = np.nan_to_num(weekly[common].corr().values, nan=0.0)
        try:
            eigvals, eigvecs = np.linalg.eigh(corr)
        except np.linalg.LinAlgError:
            print("[debug] signals=0 (eig failed)", file=sys.stderr)
            return []
        v = eigvecs[:, -1]   # eigh returns ascending eigenvalues — last col = leading eigenvector
        v = v / (np.linalg.norm(v) + EPS)
        eig_v = pd.Series(v, index=common)

        # --- Step 3: overlap penalty — orthogonal pushback on the candidate book
        w_common = w.reindex(common).fillna(0.0)
        proj = float(w_common.values @ eig_v.values)   # candidate book's loading on PC1
        w_adj = w_common - PENALTY_LAMBDA * proj * eig_v
        gross = w_adj.abs().sum()
        if gross <= EPS:
            print("[debug] signals=0 (penalty zeroed the book)", file=sys.stderr)
            return []
        w_adj = w_adj * (GROSS_TARGET / gross)   # renormalize to the original gross exposure

        latest = panel.iloc[-1]
        conf_rank = w_adj.abs().rank(pct=True)
        signals: List[Signal] = []
        for t, weight in w_adj.items():
            if abs(weight) < EPS:
                continue
            price = float(latest.get(t, np.nan))
            if not np.isfinite(price) or price <= 0:
                continue
            direction = "LONG" if weight > 0 else "SHORT"
            pos_pct = float(min(abs(weight) * POSITION_SCALER * scale, 0.10))
            if pos_pct <= 0:
                continue
            conf_pct = float(conf_rank.get(t, 0.5))
            confidence = "HIGH" if conf_pct >= 0.8 else ("MED" if conf_pct >= 0.4 else "LOW")
            stops = self.compute_stops_and_targets(
                panel[t].dropna(), direction, price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=t,
                direction=direction,
                entry_price=round(price, 4),
                stop_loss=stops["stop"],
                target_1=stops["t1"],
                target_2=stops["t2"],
                target_3=stops["t3"],
                position_size_pct=pos_pct,
                confidence=confidence,
                signal_params={
                    "raw_weight": float(w_common.get(t, 0.0)),
                    "adj_weight": float(weight),
                    "pc1_loading": float(eig_v.get(t, 0.0)),
                    "book_pc1_projection": proj,
                    "penalty_lambda": PENALTY_LAMBDA,
                },
            ))

        signals = signals[: self.MAX_SIGNALS]
        print(f"[debug] signals={len(signals)}", file=sys.stderr)
        return signals
