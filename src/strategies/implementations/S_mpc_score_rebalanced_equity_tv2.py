from __future__ import annotations
import sys
import math
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal
from strategies.safe_ops import EPS, safe_div

INSTRUMENT_CLASS = "equity"
STRATEGY_ID = "S_mpc_score_rebalanced_equity_tv2"

__all__ = ["MpcScoreRebalancedEquityTv2"]

TREND_LOOKBACK_DAYS    = 252   # variant: classic 12-1 momentum, not an MA ratio
TREND_SKIP_DAYS        = 21    # skip the most recent month (reversal exclusion)
BETA_WINDOW_DAYS       = 252   # variant: full-year beta window, not 120d
MKT_DIR_WINDOW_DAYS    = 63    # variant: ~1 quarter market-direction lookback, not ~1 month
FORECAST_FIT_DAYS      = 90    # GBM drift/vol fit window on the market index itself
FORECAST_HORIZON_DAYS  = 21    # one-step-ahead forecast horizon checked against today
FORECAST_BAND_K        = 2.0   # sigma multiple for the forecast-band-breach trigger
DD_LOOKBACK_DAYS        = 42    # variant: ~2 trading months, not 1
DD_STOP_PCT            = 0.12  # variant: looser book-level drawdown stop
DD_DERISK_FACTOR       = 0.4   # extra de-risking applied when the DD stop is the trigger
BETA_CAP_HI            = 1.3   # beta cap ceiling in a strong market
BETA_CAP_LO            = 0.5   # beta cap floor in a weak market
BETA_CAP_SLOPE         = 4.0   # cap tightens linearly with the market-direction return
SCORE_SELECT_QUANTILE  = 0.90  # variant: select by score percentile threshold, not fixed top-N
MAX_SINGLE_WEIGHT      = 0.10
TARGET_ANNUAL_VOL      = 0.12
POSITION_SCALER        = 0.5
MIN_UNIVERSE_SIZE      = 200   # per source spec: minimum_universe_size
MIN_HISTORY_DAYS       = 504   # per source spec: min_lookback_required


class MpcScoreRebalancedEquityTv2(BaseStrategy):
    """
    Varner (2026) "Interpretable Allocation and Model-Predictive Rebalancing
    of Equity Portfolios" (arXiv:2611.03022) - select/weight a long-only
    equity book off a bounded [0,1] preference score blending each stock's
    trend, its market beta (soft-capped tighter as the market weakens), and
    the market's own recent direction; rebalance on whichever fires first: a
    fixed quarterly horizon, a GBM forecast-band breach on the market index,
    or a book-level drawdown stop.

    Interpretation choices (variant 2 of 2 - deliberately different from
    variant 1): trend_i is classic 12-1 cross-sectional momentum (252d return
    skipping the most recent 21d), not a 200d MA ratio; beta_i uses the full
    standard 252d cov/var window vs SPY, not a shortened 120d window; mkt_dir
    is the sign of SPY's trailing 63d (~1 quarter) return, not ~1 month; the
    max-beta cap is a SOFT multiplicative penalty on the score (names over
    cap are down-weighted, never hard-excluded) rather than a hard filter;
    selection is a score-percentile threshold (top decile by score) rather
    than a fixed top-N count; weights are EQUAL among the selected names
    (capped and renormalized), not score-proportional; the GBM forecast-band
    trigger is fit directly on the market index (SPY) rather than a
    synthetic proxy of the selected basket, and the drawdown-stop trigger is
    evaluated on the live equal-weighted selected basket's own realized path
    over a longer (~2 month) lookback with a looser threshold. The
    turnover-cap constraint is, as in variant 1, not separately modeled -
    turnover is bounded by construction through the trigger-gated rebalance
    cadence.
    """

    id                = STRATEGY_ID
    name              = "MpcScoreRebalancedEquityTv2"
    description       = ("12-1 momentum + soft-capped beta + quarterly market-direction score "
                         "selects and equal-weights a long-only equity book; rebalanced on a "
                         "quarterly horizon, GBM forecast-band breach on the market index, or "
                         "basket drawdown stop, whichever fires first.")
    tier              = 2
    min_lookback      = MIN_HISTORY_DAYS
    signal_frequency  = "quarterly"
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

        uni = set(universe) if universe else set(prices.columns)
        tickers = [t for t in prices.columns if t in uni and t != "SPY"]
        if len(tickers) < MIN_UNIVERSE_SIZE:
            print(f"[debug] signals=0 (universe too small: {len(tickers)})", file=sys.stderr)
            return []

        cols = tickers + (["SPY"] if "SPY" in prices.columns else [])
        panel = prices[cols].ffill()
        if len(panel) < MIN_HISTORY_DAYS:
            print(f"[debug] signals=0 (insufficient history rows={len(panel)})", file=sys.stderr)
            return []

        mkt = panel["SPY"] if "SPY" in panel.columns else panel[tickers].mean(axis=1)
        mkt_ret = mkt.pct_change()

        # --- Rebalance trigger: quarterly horizon | forecast-band breach (market index) | basket DD stop ---
        idx = panel.index
        cadence_fire = isinstance(idx, pd.DatetimeIndex) and len(idx) >= 2 and idx[-1].quarter != idx[-2].quarter
        cadence_fire = cadence_fire or self.cadence_reset(regime)

        forecast_fire = False
        if len(mkt) >= FORECAST_FIT_DAYS + FORECAST_HORIZON_DAYS + 1:
            fit_rets = mkt_ret.iloc[-(FORECAST_FIT_DAYS + FORECAST_HORIZON_DAYS):-FORECAST_HORIZON_DAYS]
            drift, vol = float(fit_rets.mean()), float(fit_rets.std(ddof=1))
            anchor = float(mkt.iloc[-(FORECAST_HORIZON_DAYS + 1)])
            expected = anchor * (1.0 + drift) ** FORECAST_HORIZON_DAYS
            band = anchor * vol * math.sqrt(FORECAST_HORIZON_DAYS) * FORECAST_BAND_K
            forecast_fire = abs(float(mkt.iloc[-1]) - expected) > max(band, EPS)

        # --- Score: bounded trend (12-1 momentum) + soft-capped beta vs quarterly market direction ---
        mom_end = panel[tickers].iloc[-(TREND_SKIP_DAYS + 1)]
        mom_start = panel[tickers].iloc[-TREND_LOOKBACK_DAYS] if len(panel) >= TREND_LOOKBACK_DAYS else panel[tickers].iloc[0]
        trend = safe_div(mom_end - mom_start, mom_start)
        trend_z = safe_div(trend - trend.mean(), trend.std(ddof=1) + EPS)

        rets = panel[tickers].pct_change().iloc[-BETA_WINDOW_DAYS:]
        mkt_win = mkt_ret.iloc[-BETA_WINDOW_DAYS:]
        mkt_var = float(mkt_win.var(ddof=1))
        beta = rets.apply(lambda s: float(s.cov(mkt_win)) / mkt_var if mkt_var > EPS else 0.0)

        mkt_dir_ret = float(mkt_ret.iloc[-MKT_DIR_WINDOW_DAYS:].sum())
        mkt_dir_sign = 1.0 if mkt_dir_ret >= 0 else -1.0
        beta_cap = float(np.clip(BETA_CAP_HI + BETA_CAP_SLOPE * mkt_dir_ret, BETA_CAP_LO, BETA_CAP_HI))

        # soft penalty: names under the cap are untouched; names over it are down-weighted
        # proportionally to how far beta exceeds the cap, never excluded outright
        beta_penalty = (beta_cap / beta.clip(lower=EPS)).clip(upper=1.0)

        raw = trend_z + 0.5 * beta * mkt_dir_sign
        score = (1.0 / (1.0 + np.exp(-raw.clip(-20, 20)))) * beta_penalty
        score = score.dropna()
        if len(score) < 10:
            print("[debug] signals=0 (insufficient scored names)", file=sys.stderr)
            return []

        threshold = float(score.quantile(SCORE_SELECT_QUANTILE))
        selected = score[score >= threshold]
        if selected.empty:
            print("[debug] signals=0 (no names above score quantile)", file=sys.stderr)
            return []

        # equal-weight among selected (variant: not score-proportional)
        weight = pd.Series(1.0 / len(selected), index=selected.index)
        weight = weight.clip(upper=MAX_SINGLE_WEIGHT)
        total = float(weight.sum())
        if total > EPS:
            weight = weight / total
        weight = weight.clip(upper=MAX_SINGLE_WEIGHT)

        basket_ret = panel[selected.index].pct_change().mean(axis=1)
        basket_idx = (1.0 + basket_ret.fillna(0.0)).cumprod()

        dd_fire = False
        if len(basket_idx) >= DD_LOOKBACK_DAYS:
            window = basket_idx.iloc[-DD_LOOKBACK_DAYS:]
            dd = safe_div(window - window.cummax(), window.cummax())
            dd_fire = bool(dd.iloc[-1] <= -DD_STOP_PCT)

        if not (cadence_fire or forecast_fire or dd_fire):
            print("[debug] signals=0 (no rebalance trigger)", file=sys.stderr)
            return []
        reason = "drawdown_stop" if dd_fire else ("forecast_breach" if forecast_fire else "quarterly_horizon")
        scale = self.position_scale(regime_state)

        basket_vol = float(self.ind.realized_vol(basket_idx, 60, log=True).iloc[-1])
        vol_scale = float(np.clip(safe_div(TARGET_ANNUAL_VOL, basket_vol, fill=1.0), 0.4, 1.0))
        if reason == "drawdown_stop":
            vol_scale *= DD_DERISK_FACTOR

        latest = panel[tickers].iloc[-1]
        conf_rank = selected.rank(pct=True)
        signals: List[Signal] = []
        for t, s in selected.items():
            price = float(latest.get(t, np.nan))
            if not np.isfinite(price) or price <= 0:
                continue
            pos_pct = float(min(weight.get(t, 0.0) * POSITION_SCALER * scale * vol_scale, MAX_SINGLE_WEIGHT))
            if pos_pct <= 0:
                continue
            cp = float(conf_rank.get(t, 0.5))
            confidence = "HIGH" if cp >= 0.8 else ("MED" if cp >= 0.4 else "LOW")
            stops = self.compute_stops_and_targets(panel[t].dropna(), "LONG", price, regime_state=regime_state)
            signals.append(Signal(
                ticker=t, direction="LONG", entry_price=round(price, 4),
                stop_loss=stops["stop"], target_1=stops["t1"], target_2=stops["t2"], target_3=stops["t3"],
                position_size_pct=pos_pct, confidence=confidence,
                signal_params={
                    "score": float(s), "trend_z": float(trend_z.get(t, 0.0)), "beta": float(beta.get(t, 0.0)),
                    "beta_cap": beta_cap, "mkt_dir_sign": mkt_dir_sign, "vol_scale": vol_scale,
                    "rebalance_reason": reason,
                },
            ))

        signals = signals[: self.MAX_SIGNALS]
        print(f"[debug] signals={len(signals)}", file=sys.stderr)
        return signals
