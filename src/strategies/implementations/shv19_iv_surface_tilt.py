"""
S-HV19: Vega-Weighted IV Surface Tilt
Uses pre-computed iv_centroid_delta and surface_premium from engine.py.
Source: Carr & Wu (2009) JFE; Pan (2002) JFE
"""
from __future__ import annotations
from typing import List
from ..base import BaseStrategy, Signal

class IVSurfaceTilt(BaseStrategy):
    id = "S_HV19_iv_surface_tilt"
    version = "1.0.0"
    regime_filter = ["HIGH_VOL", "NEUTRAL"]
    CENTROID_CALL_THRESH: float = 0.55
    CENTROID_PUT_THRESH: float = 0.45
    SURFACE_PREMIUM_MIN: float = 0.03
    IV_RANK_MIN_BUY: float = 50.0
    TOP_N: int = 8

    def generate_signals(self, market_data: dict, opts_map: dict) -> List[Signal]:
        candidates = []
        for ticker, opts in opts_map.items():
            cd = opts.get("iv_centroid_delta")
            if cd is None:
                continue
            sp = opts.get("surface_premium")
            iv_rank = opts.get("iv_rank") or 50.0
            vrp = opts.get("vrp") or 0.0
            price = opts.get("last_price")
            if not price or price <= 0:
                continue
            direction = None
            score = 0.0
            if cd > self.CENTROID_CALL_THRESH and sp is not None and sp > self.SURFACE_PREMIUM_MIN:
                direction = "SELL_VOL"
                score = (cd - 0.50) * (1.0 + sp)
            elif cd < self.CENTROID_PUT_THRESH and iv_rank >= self.IV_RANK_MIN_BUY:
                direction = "BUY_VOL"
                score = (0.50 - cd) * (1.0 + iv_rank / 100.0)
            if direction:
                candidates.append((score, ticker, direction, iv_rank, cd, sp, vrp, price, opts))
        candidates.sort(key=lambda x: x[0], reverse=True)
        signals = []
        buy_n = sell_n = 0
        for score, ticker, direction, iv_rank, cd, sp, vrp, price, opts in candidates:
            if direction == "BUY_VOL" and buy_n >= self.TOP_N // 2: continue
            if direction == "SELL_VOL" and sell_n >= self.TOP_N // 2: continue
            scale = min(score * 4.0, 2.0)
            size = min(0.012 + 0.008*scale, 0.04)
            sp_val = sp if sp is not None else 0.0
            confidence = "HIGH" if abs(cd-0.50) > 0.08 and sp_val > 0.05 else "MED"
            stop_pct = 0.05 if direction == "BUY_VOL" else 0.04
            signals.append(Signal(
                ticker=ticker, direction=direction, entry_price=price,
                stop_loss=round(price*(1-stop_pct),2),
                target_1=round(price*1.07,2), target_2=round(price*1.12,2), target_3=round(price*1.18,2),
                position_size_pct=round(size,4), confidence=confidence,
                signal_params={"strategy_id":self.id,"iv_centroid_delta":round(cd,4),
                    "surface_premium":round(sp_val,4),"iv_rank":round(iv_rank,2),"vrp":round(vrp,4),"score":round(score,4)},
            ))
            if direction == "BUY_VOL": buy_n += 1
            else: sell_n += 1
        return signals
