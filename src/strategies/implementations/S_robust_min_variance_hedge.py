from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List, Tuple, Optional
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

INSTRUMENT_CLASS = "etp"
STRATEGY_ID = "S_robust_min_variance_hedge"

__all__ = ["RobustMinVarHedge"]

# Spot → hedge ETF pairs (equity/bond/commodity, per paper § 4)
ETF_PAIRS: List[Tuple[str, str]] = [
    ("SPY", "TLT"),   # broad equity vs long-term bonds
    ("QQQ", "TLT"),   # tech vs long-term bonds
    ("IWM", "SHY"),   # small cap vs short-term bonds
    ("GLD", "SLV"),   # gold vs silver
    ("XLE", "GLD"),   # energy vs gold
    ("XLF", "SHY"),   # financials vs short-term bonds
    ("EEM", "EFA"),   # emerging vs developed markets
]

AR_ORDER    = 1   # AR(1) for hedge variance forecast (paper § 3)
FORECAST_H  = 1   # 1-step ahead
RV_LOOKBACK = 21  # rolling realized-vol window (days)
AR_LOOKBACK = 63  # AR fitting window (days)
BASE_PCT    = 0.06  # base spot leg weight (6% of portfolio)


class RobustMinVarHedge(BaseStrategy):
    """
    Robust AR-uncertainty hedge ratio: h*=sigma_SF/(sigma_F2_hat+Theta_F).
    LONG spot ETF, SHORT hedge ETF with quantity scaled by robust hedge ratio.
    Ref: Ravagnani et al. (2026) arXiv:2604.02126
    """

    id          = STRATEGY_ID
    name        = "RobustMinVarHedge"
    description = ("AR forecast-error uncertainty robust hedge: closed-form h*=sigma_SF/"
                   "(sigma_F2_hat+Theta_F); LONG spot, SHORT hedge ETP pairs.")
    tier        = 2

    # Hedging thesis works in all regimes; more alpha in stressed periods
    active_in_regimes = ["LOW_VOL", "TRANSITIONING", "HIGH_VOL", "CRISIS"]

    # ------------------------------------------------------------------ helpers

    def _realized_cov(
        self, prices: pd.DataFrame, spot: str, hedge: str
    ) -> Tuple[Optional[float], Optional[float]]:
        """Daily-return realized variance(hedge) and covariance(spot,hedge), annualised."""
        if spot not in prices.columns or hedge not in prices.columns:
            return None, None
        df = prices[[spot, hedge]].ffill().tail(RV_LOOKBACK + 1).dropna()
        if len(df) < RV_LOOKBACK // 2:
            return None, None
        rets = df.pct_change().dropna()
        if len(rets) < 5:
            return None, None
        sigma_F2 = float(rets[hedge].var() * 252)
        sigma_SF = float(rets[spot].cov(rets[hedge]) * 252)
        return sigma_F2, sigma_SF

    def _ar1_forecast(self, rv_series: pd.Series) -> Tuple[float, float]:
        """
        Fit AR(1) to the realized-variance series.
        Returns (forecast, Theta_F) where Theta_F is the 1-step forecast error variance.
        """
        n = len(rv_series)
        if n < 15:
            m = float(rv_series.mean())
            return m, float(rv_series.var())
        y = rv_series.values[1:]
        X = np.column_stack([np.ones(n - 1), rv_series.values[:-1]])
        try:
            coeffs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        except Exception:
            m = float(rv_series.mean())
            return m, float(rv_series.var())
        phi_0, phi_1 = float(coeffs[0]), float(coeffs[1])
        forecast = max(phi_0 + phi_1 * float(rv_series.iloc[-1]), 1e-8)
        sigma_eps2 = float(np.mean((y - X @ coeffs) ** 2))
        # Closed-form Theta_F for AR(1) h-step ahead
        if abs(phi_1) < 1.0:
            Theta_F = sigma_eps2 * sum(phi_1 ** (2 * j) for j in range(FORECAST_H))
        else:
            Theta_F = sigma_eps2 * FORECAST_H
        return forecast, float(max(Theta_F, 1e-12))

    # ------------------------------------------------------------------ main

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
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

        if len(prices) < AR_LOOKBACK + 5:
            print(f"[debug] signals=0 (insufficient history rows={len(prices)})", file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        signals: List[Signal] = []

        for spot_tkr, hedge_tkr in ETF_PAIRS:
            # --- Step 1: realized covariance ----------------------------------
            sigma_F2, sigma_SF = self._realized_cov(prices, spot_tkr, hedge_tkr)
            if sigma_F2 is None or sigma_F2 <= 0:
                continue

            # --- Step 2: AR(1) forecast of hedge realized variance ------------
            hedge_rets = prices[hedge_tkr].pct_change().dropna().tail(AR_LOOKBACK + 1)
            # 5-day rolling realized variance as the input series
            rv_series = hedge_rets.rolling(5).var().dropna() * 252
            if len(rv_series) < 20:
                continue
            sigma_F2_hat, Theta_F = self._ar1_forecast(rv_series)

            # --- Step 3: robust hedge ratio -----------------------------------
            # h*_t = sigma_SF / (sigma_F2_hat + Theta_F)   [Eq. 3.5 in paper]
            denom = sigma_F2_hat + Theta_F
            if denom <= 0:
                continue
            h_star = sigma_SF / denom
            if h_star <= 0:
                continue  # no meaningful hedge (negative covariance)
            h_star = float(np.clip(h_star, 0.05, 5.0))

            # Current prices
            spot_px  = float(prices[spot_tkr].dropna().iloc[-1])
            hedge_px = float(prices[hedge_tkr].dropna().iloc[-1])
            if spot_px <= 0 or hedge_px <= 0:
                continue

            # Confidence: lower uncertainty_ratio → more certain forecast
            unc_ratio = Theta_F / (sigma_F2_hat + 1e-10)
            confidence = "HIGH" if unc_ratio < 0.2 else ("MED" if unc_ratio < 0.5 else "LOW")

            spot_pct  = float(min(BASE_PCT * scale, 0.10))
            hedge_pct = float(min(BASE_PCT * scale * h_star, 0.10))

            spot_stops  = self.compute_stops_and_targets(
                prices[spot_tkr].dropna(),  "LONG",  spot_px,  regime_state=regime_state
            )
            hedge_stops = self.compute_stops_and_targets(
                prices[hedge_tkr].dropna(), "SHORT", hedge_px, regime_state=regime_state
            )

            common_params = {
                "spot_ticker":   spot_tkr,
                "hedge_ticker":  hedge_tkr,
                "h_robust":      h_star,
                "sigma_SF":      sigma_SF,
                "sigma_F2":      sigma_F2,
                "sigma_F2_hat":  sigma_F2_hat,
                "Theta_F":       Theta_F,
            }

            # LONG spot
            signals.append(Signal(
                ticker            = spot_tkr,
                direction         = "LONG",
                entry_price       = spot_px,
                stop_loss         = float(spot_stops["stop"]),
                target_1          = float(spot_stops["t1"]),
                target_2          = float(spot_stops["t2"]),
                target_3          = float(spot_stops["t3"]),
                position_size_pct = spot_pct,
                confidence        = confidence,
                signal_params     = {**common_params, "role": "spot"},
            ))

            # SHORT hedge
            signals.append(Signal(
                ticker            = hedge_tkr,
                direction         = "SHORT",
                entry_price       = hedge_px,
                stop_loss         = float(hedge_stops["stop"]),
                target_1          = float(hedge_stops["t1"]),
                target_2          = float(hedge_stops["t2"]),
                target_3          = float(hedge_stops["t3"]),
                position_size_pct = hedge_pct,
                confidence        = confidence,
                signal_params     = {**common_params, "role": "hedge"},
            ))

        signals = signals[: self.MAX_SIGNALS]
        print(f"[debug] signals={len(signals)}", file=sys.stderr)
        return signals
