"""
SPXW 0DTE LTR Put Ranker — regime/tail-aware selection among 8 delta-targeted
0DTE short puts (or abstain), proxying a learning-to-rank harvest of the
volatility risk premium.

Source: Wysocki, M. (2026), "Harvesting the Volatility Risk Premium: A
Learning-to-Rank Approach" (arXiv:2608.24786v1).

Hypothesis: a ranker over a 9-way daily candidate set {8 delta-targeted
0DTE short puts, SKIP} that conditions strike/abstention choice on regime
+ tail-risk features harvests the VRP more efficiently than a static
short-put benchmark (CBOE PUT/WPUT).

Implementation note: generate_signals must be a pure, deterministic function
of its inputs — we do not train/serve a LightGBM LambdaRank model at signal
time. The ranker's ROLE is reproduced with a closed-form expected-value proxy
per delta bucket, standing in for the paper's learned Sortino-on-bars label:
  score(d) = premium_pct(d) - tail_penalty(d)
  premium_pct(d)  ~ d * iv30 * sqrt(1/252)              (theta harvested)
  tail_penalty(d) ~ d^2 * rv_20 * tail_risk_multiplier  (loss if breached)
tail_risk_multiplier scales with risk-neutral left-skew (rn_skew_30d), the
risk-neutral 10%-down probability (rn_p_dn10_30d) and VVIX (vol-of-vol, a
proxy for the paper's uncertainty-driven abstention gate). The argmax delta
bucket is selected; SKIP (no signal) if the best score is non-positive or
the VRP ratio (iv30/rv_20) shows vol is not rich enough to harvest.

SPX/^GSPC have no options chain in the master parquet (see
S_ito_signature_vol_hedge / S_spx_0dte_opening_range_breakout) — SPY is the
chain-covered proxy; the short put leg trades on SPY.
"""
from __future__ import annotations
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, OptionSpec

__all__ = ['SpxwZeroDteLtrPutRanker']

INSTRUMENT_CLASS = 'option'

UNDERLYING    = 'SPY'
DELTA_BUCKETS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)
ONE_DAY_VOL   = (1.0 / 252.0) ** 0.5
MIN_VRP_RATIO = 1.00   # iv30/rv_20 must clear this to harvest at all
DTE_TARGET    = 0
MAX_SIZE      = 0.05


class SpxwZeroDteLtrPutRanker(BaseStrategy):
    id                = 'S_spxw_0dte_ltr_put_ranker'
    name              = 'SPXW 0DTE LTR Put Ranker'
    description       = (
        "Ranks 8 delta-targeted SPXW 0DTE short puts (SPY chain proxy) plus a "
        "SKIP option daily via a regime/tail-risk-aware expected-value proxy; "
        "sells the argmax strike, held to expiry, or abstains."
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'min_vrp_ratio': MIN_VRP_RATIO,
            'base_size':     0.02,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or UNDERLYING not in prices.columns:
            print('[debug] signals=0 (no SPY column)', file=sys.stderr)
            return []

        regime = regime or {}
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime gate)', file=sys.stderr)
            return []

        series = prices[UNDERLYING].dropna()
        if len(series) < 20:
            print('[debug] signals=0 (insufficient history)', file=sys.stderr)
            return []
        current_price = float(series.iloc[-1])
        if not (current_price == current_price and current_price > 0):
            print('[debug] signals=0 (bad last price)', file=sys.stderr)
            return []

        opts_map = (aux_data or {}).get('options', {}) or {}
        opts = opts_map.get(UNDERLYING)
        if not opts:
            print('[debug] signals=0 (no SPY options aggregates)', file=sys.stderr)
            return []

        iv30  = opts.get('iv30')
        rv_20 = opts.get('rv_20')
        if iv30 is None or rv_20 is None or float(rv_20) <= 0:
            print('[debug] signals=0 (missing iv30/rv_20)', file=sys.stderr)
            return []
        iv30, rv_20 = float(iv30), float(rv_20)

        vrp_ratio = iv30 / rv_20
        min_vrp = float(self.parameters.get('min_vrp_ratio', MIN_VRP_RATIO))
        if vrp_ratio < min_vrp:
            print(f'[debug] signals=0 (vrp_ratio={vrp_ratio:.3f} < {min_vrp})', file=sys.stderr)
            return []

        rn_skew   = opts.get('rn_skew_30d')
        rn_p_dn10 = opts.get('rn_p_dn10_30d')
        left_skew_pen = max(0.0, -float(rn_skew)) if rn_skew is not None else 0.0
        tail_prob_pen = float(rn_p_dn10) if rn_p_dn10 is not None else 0.05

        vvix = ((aux_data or {}).get('vol_indices') or {}).get('vvix_close')
        vvix_pen = max(0.0, (float(vvix) - 90.0) / 90.0) if vvix is not None else 0.0

        tail_risk_multiplier = (1.0 + left_skew_pen) * (1.0 + 5.0 * tail_prob_pen) * (1.0 + vvix_pen)

        scores = []
        for d in DELTA_BUCKETS:
            premium_pct  = d * iv30 * ONE_DAY_VOL
            tail_penalty = (d ** 2) * rv_20 * tail_risk_multiplier
            scores.append((premium_pct - tail_penalty, d))
        scores.sort(key=lambda x: x[0], reverse=True)

        best_score, best_delta = scores[0]
        if best_score <= 0:
            print(f'[debug] signals=0 (best_score={best_score:.5f} <= 0, abstain)', file=sys.stderr)
            return []

        second_score = scores[1][0] if len(scores) > 1 else 0.0
        edge = min(max((best_score - second_score) / max(abs(best_score), 1e-6), 0.0), 1.0)
        confidence = 'HIGH' if (edge > 0.35 and vrp_ratio > 1.2) else ('MED' if edge > 0.10 else 'LOW')

        scale     = self.position_scale(regime_state)
        base_size = float(self.parameters.get('base_size', 0.02))
        size      = min(base_size * scale * (1.0 + edge), MAX_SIZE)

        stops = self.compute_stops_and_targets(
            series, 'LONG', current_price, atr_multiplier=2.0, regime_state=regime_state,
        )

        option_spec = OptionSpec(
            underlying     = UNDERLYING,
            right          = 'put',
            strike_rule    = 'target_delta',
            target_delta   = round(best_delta, 4),
            dte_target     = DTE_TARGET,
            structure      = 'single',
            hedge          = 'none',
            hold_to_expiry = True,
        )

        signal = Signal(
            ticker            = UNDERLYING,
            direction         = 'SELL_VOL',
            entry_price       = current_price,
            stop_loss         = stops['stop'],
            target_1          = stops['t1'],
            target_2          = stops['t2'],
            target_3          = stops['t3'],
            position_size_pct = round(size, 4),
            confidence        = confidence,
            signal_params     = {
                'selected_delta':       best_delta,
                'candidate_scores':     {str(d): round(s, 6) for s, d in scores},
                'vrp_ratio':            round(vrp_ratio, 4),
                'iv30':                 round(iv30, 4),
                'rv_20':                round(rv_20, 4),
                'rn_skew_30d':          round(float(rn_skew), 4) if rn_skew is not None else None,
                'rn_p_dn10_30d':        round(float(rn_p_dn10), 4) if rn_p_dn10 is not None else None,
                'tail_risk_multiplier': round(tail_risk_multiplier, 4),
                'edge':                 round(edge, 4),
                'hold_to_expiry':       True,
                'regime':               regime_state,
            },
            option_spec       = option_spec,
        )

        print('[debug] signals=1', file=sys.stderr)
        return [signal]
