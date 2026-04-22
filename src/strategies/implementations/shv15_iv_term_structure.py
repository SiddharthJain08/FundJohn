"""S-HV15: IV Term Structure Inversion (Backwardation Fade).

Quantpedia #43 / Alexander & Korovilas (2013) / Barclays VRP research.
ts_ratio = near_iv(5-35 DTE) / far_iv(55-95 DTE).
Backwardation (ratio > 1.20): near-term vol overpriced, will compress => SELL_VOL.
Steep contango (ratio < 0.85): near-term vol cheap => BUY_VOL.
In HIGH_VOL regime: spike-mean-reversion trades on inverted names.
Pre-computed ts_ratio, near_iv, far_iv in engine.py. Zero LLM tokens.
"""
from __future__ import annotations
from typing import Any
from src.strategies.base import BaseStrategy, Signal

TS_BACKWARDATION   = 1.20   # near > far by 20% => sell near-term vol
TS_CONTANGO        = 0.85   # far > near by 15% => buy near-term vol
MIN_NEAR_IV        = 0.20   # filter penny stocks / micro-vol
MIN_IV_RANK_SELL   = 50     # only sell in elevated-vol context
TOP_N              = 8


class IVTermStructure(BaseStrategy):
    id            = 'S_HV15_iv_term_structure'
    name          = 'IV Term Structure'
    version       = '1.0.0'
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING']

    def generate_signals(self, prices, regime, universe, aux_data) -> list[Signal]:
        prices       = ({c: prices[c].dropna() for c in prices.columns} if hasattr(prices, 'columns') else prices.get('prices', {}))
        regime       = regime or {}
        regime_state = regime.get('state', 'LOW_VOL')

        if not self.should_run(regime_state):
            return []

        candidates: list[tuple[float, str, str, dict]] = []

        for ticker, opts in aux_data.get('options', {}).items():
            ts_ratio = opts.get('ts_ratio')
            near_iv  = opts.get('near_iv')
            far_iv   = opts.get('far_iv')
            iv_rank  = opts.get('iv_rank')

            if ts_ratio is None or near_iv is None or far_iv is None or iv_rank is None:
                continue
            if near_iv < MIN_NEAR_IV:
                continue

            if ts_ratio >= TS_BACKWARDATION and iv_rank >= MIN_IV_RANK_SELL:
                # Inverted curve: near-term fear premium will mean-revert
                score = ts_ratio
                candidates.append((score, ticker, 'SELL_VOL', opts))
            elif ts_ratio <= TS_CONTANGO and iv_rank < 40:
                # Steep contango: near-term cheap, buying vol has edge
                score = 1.0 / ts_ratio if ts_ratio > 0 else 0
                candidates.append((score, ticker, 'BUY_VOL', opts))

        candidates.sort(key=lambda x: x[0], reverse=True)
        candidates = candidates[:TOP_N]

        signals: list[Signal] = []
        for score, ticker, direction, opts in candidates:
            ts = prices.get(ticker, [])
            if len(ts) < 5:
                continue
            current_price = float(ts.iloc[-1])
            if current_price <= 0:
                continue

            equity_dir = 'SHORT' if direction == 'SELL_VOL' else 'LONG'
            stops  = self.compute_stops_and_targets(ts, equity_dir, current_price, atr_multiplier=2.0)
            scale  = self.position_scale(regime_state)
            ts_rat = opts.get('ts_ratio', 1.0)
            magnitude = abs(ts_rat - 1.0)
            size = min(0.015 + 0.01 * (magnitude / 0.20) * scale, 0.045)

            iv_rank = opts.get('iv_rank', 50)
            confidence = 'HIGH' if magnitude >= 0.30 and iv_rank >= 65 else 'MED'

            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(size, 4),
                confidence        = confidence,
                signal_params     = {
                    'ts_ratio':  round(ts_rat, 4),
                    'near_iv':   opts.get('near_iv'),
                    'far_iv':    opts.get('far_iv'),
                    'iv_rank':   iv_rank,
                },
            ))
        return signals
