"""
S_cvar_minvar_etf_diversification_tv2 — Historical-CVaR (EVaR) Equity-Bond
ETF Diversification

Source: Afroz, Shirvani, Appiah, Rachev, Fabozzi (2026), "Revisiting
Equity-Bond Diversification under Tail Risk and Structural Market Change:
Evidence from Global ETFs and Model-Implied Option Prices" (arXiv:2611.21897v1).

Hypothesis (unchanged from the spec): rolling-window min-variance/CVaR
optimization across global equity-bond ETFs reduces tail risk (VaR/CVaR)
versus an equal-weight benchmark, but equal-weight still wins on
Sharpe/Sortino — a tail-risk-reduction-vs-Sharpe trade-off this strategy
treats as a design constraint (active only in the stress band the paper
studies it for) rather than something to optimize away.

Variant 2/2 — same abstract-only extraction as variant 1 (no appendix with
exact tickers/window/cadence/constraint), so this file deliberately takes
the OTHER defensible reading on every axis variant 1 chose freely:
  - Optimization branch: CVaR/EVaR, not min-variance. The spec's own
    pseudocode bifurcates "estimate covariance (min-variance) OR empirical
    loss distribution (CVaR)" — this file takes the CVaR branch literally,
    using the house `lib.convex.min_evar_weights(L, z=0.95)` long-only
    minimum-EVaR solver (EVaR upper-bounds CVaR at the same level; it is
    the only in-repo convex CVaR-family helper, so Σ-estimation is skipped
    entirely rather than feeding a covariance matrix into it).
  - Scenario construction: RAW historical daily returns over the rolling
    window, used directly as the loss scenarios (L = -returns), with NO
    GARCH/EWMA conditional-vol filtering. The spec's pseudocode names
    "empirical loss distribution" for the CVaR branch specifically (as
    opposed to the covariance branch, which is where GARCH/FIGARCH vol
    modeling is discussed) — this variant reads that split literally: the
    CVaR arm is the plain historical-simulation method, not a
    GARCH-conditioned one. A 756-session window on 30 assets is >>3x the
    house observations-to-assets floor, so an unconditional empirical
    distribution is a well-posed choice here, not an underdetermined one.
    This also keeps the file structurally distinct from the sibling
    `S_garch_copula_cvar_scenario` (ARMA-GARCH + Student-t copula Monte
    Carlo scenarios feeding the same min_evar_weights solver) — this
    variant deliberately skips the scenario-simulation machinery and uses
    the empirical sample directly, the "simpler" of the two CVaR readings.
  - EVaR level: z=0.95 (the solver's own documented default and the
    conventional 95% VaR/CVaR level in the tail-risk literature) — stated
    explicitly here since the paper's abstract never fixes a confidence
    level.
  - Universe: a DIFFERENT 30-name equity-bond ETF cut from variant 1 —
    broad-US (SPY/VOO/IVV/VXF) + developed/EM international (VEA/VWO/EWJ/
    EWC/EWA/EWG/EWU) + Asia/EM single-country breadth variant 1 lacked
    (FXI/INDA) for equities (15); core aggregate/Treasury (AGG/BND/GOVT/
    SHY/IEF/TLT/TIP) + credit (LQD/VCIT/VCSH/HYG/JNK/EMB) + muni/intl
    (MUB/BNDX) for bonds (15). A colleague could as defensibly pick
    variant 1's cut; this one swaps in JNK/FXI/INDA/VXF/IVV/VOO in place of
    variant 1's single ticker choices.
  - Rebalance cadence: MONTHLY (first trading day of each calendar month),
    not weekly — "rebalance each window" is unstated cadence in the spec;
    monthly is the more conservative, lower-turnover reading and matches
    the sibling strategy's own cadence choice for the same ambiguity.
  - Coverage floor: 25-of-30 (stricter than variant 1's 20-of-30) — a
    scenario-based EVaR program benefits from using the full named universe
    where available, so this variant tolerates less data-gap shrinkage.
  - Position cap: MAX_WEIGHT=0.10 (tighter than variant 1's 0.15) — EVaR
    optimization can concentrate more aggressively into the single
    historically-least-tail-risky name than a min-variance program does,
    so the per-name cap is pulled in to keep the "diversification" framing
    the paper's title promises.
Unambiguous spec items kept as-is (same as variant 1): min_lookback_required
=756 trading days, minimum_universe_size=30, instrument_class='etp',
regime_applicability HIGH_VOL/RISK_OFF -> canonical HIGH_VOL/CRISIS,
long-only (forced by tooling — the only in-repo convex CVaR helper is
long-only), fallback to equal weight on a degenerate solve (house rule,
and literally the paper's own benchmark). Data: close-price panel only
(engine-supplied `prices`); realized_vol/macro are listed optional/computed
in the spec and are not read here.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from lib.convex import min_evar_weights

__all__ = ['CvarMinVarEtfDiversificationTv2']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_cvar_minvar_etf_diversification_tv2'

# 30-name global equity-bond ETF universe (15 equity / 15 bond) — a
# deliberately different cut from variant 1.
EQUITY_ETFS = [
    'SPY', 'VOO', 'IVV', 'VXF', 'QQQ',           # broad US (large + extended market)
    'VEA', 'VWO', 'EWJ', 'EWC', 'EWA',           # developed/EM ex-US + Japan/Canada/Australia
    'EWG', 'EWU', 'FXI', 'INDA', 'EEM',          # Germany/UK + China/India + broad EM
]
BOND_ETFS = [
    'AGG', 'BND', 'GOVT', 'SHY', 'IEF', 'TLT',   # core aggregate / Treasury ladder
    'TIP', 'LQD', 'VCIT', 'VCSH', 'HYG', 'JNK',  # TIPS / IG credit / HY credit
    'EMB', 'MUB', 'BNDX',                        # EM debt / municipal / international
]
ETF_UNIVERSE = EQUITY_ETFS + BOND_ETFS   # 30 names

EVAR_Z       = 0.95     # EVaR confidence level (standard 95% tail level)
WINDOW       = 756      # ~3y rolling estimation window == min_lookback_required
MIN_ASSETS   = 25       # 25-of-30 coverage floor (stricter than variant 1)
MAX_WEIGHT   = 0.10
HOLD_DAYS    = 21       # ~1 trading month, matches the monthly rebalance cadence


def _historical_cvar_weights(prices_window: pd.DataFrame, tickers: list) -> np.ndarray | None:
    """Long-only minimum-EVaR weights from the RAW empirical historical-
    return loss distribution (no GARCH/EWMA conditioning) — the literal
    "empirical loss distribution" branch of the spec's pseudocode."""
    returns = prices_window[tickers].pct_change().dropna(how='any')
    if len(returns) < 3 * len(tickers):
        return None
    losses = -returns.values   # EVaR/CVaR solvers operate on scenario LOSSES
    return min_evar_weights(losses, z=EVAR_Z)


def _month_boundary(idx: pd.DatetimeIndex) -> bool:
    if len(idx) < 2:
        return False
    d1 = pd.Timestamp(idx[-1])
    d0 = pd.Timestamp(idx[-2])
    return (d1.year, d1.month) != (d0.year, d0.month)


class CvarMinVarEtfDiversificationTv2(BaseStrategy):
    """LONG-only historical-CVaR (min-EVaR) monthly allocator across a fixed
    30-name equity-bond ETF universe, scenarios = raw historical daily
    returns (no conditional-vol filtering), active only in HIGH_VOL/CRISIS
    regimes. Falls back to equal weight on a degenerate optimizer solve.
    Afroz et al. 2026, variant 2/2 (CVaR branch of the spec pseudocode)."""

    id                = STRATEGY_ID
    name              = 'Historical-CVaR Equity-Bond ETF Diversification (tv2)'
    description       = ('LONG-only minimum-EVaR allocation across 30 equity-bond ETFs from '
                         'the raw empirical historical-return loss distribution; monthly '
                         'rebalance, active only in HIGH_VOL/CRISIS (Afroz et al. 2026).')
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = WINDOW
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['HIGH_VOL', 'CRISIS']   # spec regime_applicability (HIGH_VOL/RISK_OFF)
    MAX_SIGNALS       = len(ETF_UNIVERSE)

    def default_parameters(self) -> dict:
        return {'max_weight': MAX_WEIGHT}

    def _target_weights(self, prices_sub: pd.DataFrame, tickers: list) -> pd.Series | None:
        w = _historical_cvar_weights(prices_sub, tickers)
        if w is None:
            # Degenerate optimizer solve -> fall back to equal weight (house
            # rule), which is also literally the paper's own benchmark.
            n = len(tickers)
            return pd.Series(1.0 / n, index=tickers)
        return pd.Series(w, index=tickers)

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0 (no price data)', file=sys.stderr)
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime gated)', file=sys.stderr)
            return []

        tickers = [t for t in ETF_UNIVERSE if t in prices.columns]
        if len(tickers) < MIN_ASSETS:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        prices_sub = prices[tickers].dropna(axis=1, how='any').tail(WINDOW + 1)
        tickers = [t for t in tickers if t in prices_sub.columns]
        if len(tickers) < MIN_ASSETS or len(prices_sub) < WINDOW:
            print(f'[debug] signals=0 (insufficient history: rows={len(prices_sub)} cols={len(tickers)})',
                  file=sys.stderr)
            return []

        if not (_month_boundary(prices_sub.index) or self.cadence_reset(regime)):
            print('[debug] signals=0 (not a monthly rebalance day)', file=sys.stderr)
            return []

        try:
            weights = self._target_weights(prices_sub, tickers)
        except Exception as exc:
            print(f'[debug] signals=0 exc={exc}', file=sys.stderr)
            return []
        if weights is None:
            print('[debug] signals=0 (optimizer unresolved)', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        max_weight = float(self.parameters.get('max_weight', MAX_WEIGHT))
        last_row = prices_sub.iloc[-1]

        signals: List[Signal] = []
        for ticker in tickers:
            w = float(weights.get(ticker, 0.0))
            if w < 0.01:
                continue
            price = float(last_row[ticker])
            if not (price == price) or price <= 0:
                continue
            series = prices_sub[ticker].dropna()
            stops = self.compute_stops_and_targets(series, 'LONG', price, regime_state=regime_state)
            pos = round(min(w, max_weight) * scale, 4)
            confidence = 'HIGH' if w > 0.10 else ('MED' if w > 0.04 else 'LOW')
            signals.append(Signal(
                ticker=ticker, direction='LONG', entry_price=price,
                stop_loss=stops['stop'], target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=pos, confidence=confidence,
                signal_params={
                    'evar_weight':  round(w, 4),
                    'regime_scale': round(scale, 3),
                    'hold_days':    HOLD_DAYS,
                },
            ))

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
