"""
S-HV17: Earnings Straddle Fade
SELL_VOL when implied_move > 1.20x historical realized near earnings.
Source: Muravyev & Pearson (2020) JFE; Batch & Savor (2014) JF
State: staging — requires earnings_dte in engine.py opts_dict via FMP calendar
"""
from __future__ import annotations
import math
from typing import List
from ..base import BaseStrategy, Signal

class EarningsStraddleFade(BaseStrategy):
    id = "S_HV17_earnings_straddle_fade"
    version = "1.0.0"
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING']
    IMPLIED_MOVE_RATIO_MIN: float = 1.20
    IV_RANK_MIN: float = 65.0
    LOOKBACK_DAYS: int = 504
    REALIZED_WINDOW: int = 21
    TOP_N: int = 5

    def generate_signals(self, prices, regime, universe, aux_data) -> List[Signal]:
        candidates = []
        prices_df = prices
        for ticker, opts in aux_data.get('options', {}).items():
            iv_rank = opts.get("iv_rank")
            if iv_rank is None or iv_rank < self.IV_RANK_MIN:
                continue
            near_iv = opts.get("near_iv")
            if not near_iv or near_iv <= 0:
                continue
            price = opts.get("last_price")
            if not price or price <= 0:
                continue
            earnings_dte = opts.get("earnings_dte")
            if earnings_dte is None or not (0 <= earnings_dte <= 5):
                continue
            dte_used = max(int(earnings_dte), 1)
            implied_move = near_iv * math.sqrt(dte_used / 252.0)
            historical_realized = None
            if prices_df is not None:
                try:
                    px = prices_df[prices_df["ticker"] == ticker].copy()
                    if len(px) >= self.REALIZED_WINDOW:
                        px = px.sort_values("date").tail(self.LOOKBACK_DAYS)
                        px["ret"] = px["close"].pct_change().abs()
                        roll = px["ret"].rolling(self.REALIZED_WINDOW).std() * math.sqrt(self.REALIZED_WINDOW)
                        historical_realized = float(roll.dropna().median())
                except Exception:
                    pass
            if not historical_realized or historical_realized <= 0:
                continue
            ratio = implied_move / historical_realized
            if ratio < self.IMPLIED_MOVE_RATIO_MIN:
                continue
            candidates.append((ratio*(iv_rank/100.0), ticker, iv_rank, near_iv, implied_move, historical_realized, ratio, price, opts))
        candidates.sort(key=lambda x: x[0], reverse=True)
        signals = []
        for score, ticker, iv_rank, near_iv, implied_move, hist_real, ratio, price, opts in candidates[:self.TOP_N]:
            scale = min(ratio / self.IMPLIED_MOVE_RATIO_MIN, 2.0)
            size = min(0.010 + 0.005*(iv_rank/100.0)*scale, 0.035)
            confidence = "HIGH" if iv_rank > 80 and ratio >= 1.40 else "MED"
            signals.append(Signal(
                ticker=ticker, direction="SELL_VOL", entry_price=price,
                stop_loss=round(price*0.94,2),
                target_1=round(price*1.06,2), target_2=round(price*1.10,2), target_3=round(price*1.15,2),
                position_size_pct=round(size,4), confidence=confidence,
                signal_params={"strategy_id":self.id,"iv_rank":round(iv_rank,2),"near_iv":round(near_iv,4),
                    "implied_move":round(implied_move,4),"historical_realized":round(hist_real,4),
                    "ratio":round(ratio,3),"earnings_dte":opts.get("earnings_dte")},
            ))
        return signals
