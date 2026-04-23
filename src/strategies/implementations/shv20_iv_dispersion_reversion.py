"""
S-HV20: Cross-Sectional IV Rank Dispersion / Reversion
Cross-section z-scores: high-z SELL_VOL outliers, low-z BUY_VOL laggards.
Source: Driessen, Maenhout & Vilkov (2009) JoF; Goyal & Saretto (2009) JFE
"""
from __future__ import annotations
import statistics
from typing import List
from ..base import BaseStrategy, Signal

class IVDispersionReversion(BaseStrategy):
    id = "S_HV20_iv_dispersion_reversion"
    version = "1.0.0"
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING']
    Z_SELL_THRESH: float = 1.5
    Z_BUY_THRESH: float = -1.5
    VRP_SELL_MIN: float = 0.03
    MIN_UNIVERSE: int = 10
    TOP_N: int = 10

    def generate_signals(self, prices, regime, universe, aux_data) -> List[Signal]:
        import math
        rank_data = [(t, float(o["iv_rank"]), o)
                     for t, o in (aux_data or {}).get("options", {}).items()
                     if o.get("iv_rank") is not None
                     and math.isfinite(float(o["iv_rank"]))]
        if len(rank_data) < self.MIN_UNIVERSE:
            return []
        ranks = [r[1] for r in rank_data]
        mean_r = statistics.mean(ranks)
        # Py 3.13's statistics.stdev uses Fraction internally and chokes on
        # floats that can't be represented exactly — use a manual population
        # stdev to avoid 'float' object has no attribute 'numerator' errors.
        std_r = math.sqrt(sum((x - mean_r) ** 2 for x in ranks) / max(len(ranks) - 1, 1))
        if std_r < 1e-6:
            return []
        candidates = []
        for ticker, iv_rank, opts in rank_data:
            z = (iv_rank - mean_r) / std_r
            vrp = opts.get("vrp") or 0.0
            price = opts.get("last_price")
            if not price or price <= 0: continue
            direction = None
            score = 0.0
            if z >= self.Z_SELL_THRESH and vrp >= self.VRP_SELL_MIN:
                direction = "SELL_VOL"; score = z * (1.0 + vrp)
            elif z <= self.Z_BUY_THRESH:
                direction = "BUY_VOL"; score = abs(z)
            if direction:
                candidates.append((score, ticker, direction, iv_rank, z, vrp, price, opts))
        candidates.sort(key=lambda x: x[0], reverse=True)
        signals = []
        buy_n = sell_n = 0
        for score, ticker, direction, iv_rank, z, vrp, price, opts in candidates:
            if direction == "BUY_VOL" and buy_n >= self.TOP_N // 2: continue
            if direction == "SELL_VOL" and sell_n >= self.TOP_N // 2: continue
            scale = min(abs(z)/1.5, 2.0)
            size = min(0.012 + 0.008*scale, 0.04)
            confidence = "HIGH" if abs(z) >= 2.0 else "MED"
            signals.append(Signal(
                ticker=ticker, direction=direction, entry_price=price,
                stop_loss=round(price*0.95,2),
                target_1=round(price*1.07,2), target_2=round(price*1.12,2), target_3=round(price*1.18,2),
                position_size_pct=round(size,4), confidence=confidence,
                signal_params={"strategy_id":self.id,"iv_rank":round(iv_rank,2),"z_score":round(z,3),
                    "vrp":round(vrp,4),"cross_section_mean":round(mean_r,2),"cross_section_stdev":round(std_r,2),"score":round(score,4)},
            ))
            if direction == "BUY_VOL": buy_n += 1
            else: sell_n += 1
        return signals
