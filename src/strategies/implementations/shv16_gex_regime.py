"""
S-HV16: GEX Regime Classifier
Negative GEX: dealers short gamma -> BUY_VOL
Positive GEX + VRP: dealers long gamma -> SELL_VOL
Source: Bollen & Whaley (2004) JF
"""
from __future__ import annotations
from typing import List
from ..base import BaseStrategy, Signal

class GEXRegime(BaseStrategy):
    id = "S_HV16_gex_regime"
    version = "1.0.0"
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING', 'LOW_VOL']
    GEX_NEGATIVE_THRESH: float = -500.0
    GEX_POSITIVE_THRESH: float = 500.0
    VRP_MIN_SELL: float = 0.03
    IV_RANK_MIN_SELL: float = 45.0
    TOP_N: int = 8

    def generate_signals(self, prices, regime, universe, aux_data) -> List[Signal]:
        candidates = []
        for ticker, opts in aux_data.get('options', {}).items():
            gex = opts.get("gex")
            if gex is None:
                continue
            iv_rank = opts.get("iv_rank") or 50.0
            vrp = opts.get("vrp") or 0.0
            price = opts.get("last_price")
            if not price or price <= 0:
                continue
            direction = None
            score = 0.0
            if gex <= self.GEX_NEGATIVE_THRESH:
                direction = "BUY_VOL"
                score = abs(gex) / abs(self.GEX_NEGATIVE_THRESH)
            elif gex >= self.GEX_POSITIVE_THRESH and vrp >= self.VRP_MIN_SELL and iv_rank >= self.IV_RANK_MIN_SELL:
                direction = "SELL_VOL"
                score = (gex / self.GEX_POSITIVE_THRESH) * (1.0 + vrp)
            if direction:
                candidates.append((score, ticker, direction, iv_rank, gex, vrp, price, opts))
        candidates.sort(key=lambda x: x[0], reverse=True)
        signals = []
        buy_n = sell_n = 0
        for score, ticker, direction, iv_rank, gex, vrp, price, opts in candidates:
            if direction == "BUY_VOL" and buy_n >= self.TOP_N // 2:
                continue
            if direction == "SELL_VOL" and sell_n >= self.TOP_N // 2:
                continue
            scale = min(score, 2.0)
            size = min(0.015 + 0.01 * (abs(gex) / 2000.0) * scale, 0.045)
            confidence = "HIGH" if abs(gex) >= 1500 and iv_rank > 55 else "MED"
            stop_pct = 0.05 if direction == "BUY_VOL" else 0.04
            signals.append(Signal(
                ticker=ticker, direction=direction, entry_price=price,
                stop_loss=round(price*(1-stop_pct),2),
                target_1=round(price*1.08,2), target_2=round(price*1.14,2), target_3=round(price*1.20,2),
                position_size_pct=round(size,4), confidence=confidence,
                signal_params={"strategy_id":self.id,"gex":round(gex,2),"iv_rank":round(iv_rank,2),"vrp":round(vrp,4),"score":round(score,4)},
            ))
            if direction == "BUY_VOL": buy_n += 1
            else: sell_n += 1
        return signals
