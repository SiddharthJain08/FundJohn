"""
S_microcap_insider_purchase_momentum — Microcap Insider Purchase Momentum

Gradient-boosting on SEC Form 4 insider purchase features — especially distance
from 52-week high — predicts positive abnormal returns in microcap stocks
($30M–$500M), because illiquid markets incorporate information slowly and
momentum-confirmed insider buys filter for high-conviction signals.

Source: Zhao, H. (2026). "Insider Purchase Signals in Microcap Equities:
Gradient Boosting Detection of Abnormal Returns." arXiv:2602.06198v1.
"""

from __future__ import annotations

import math
import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import small_cap_liquid as universe_filter

INSTRUMENT_CLASS = "equity"
STRATEGY_ID = "S_microcap_insider_purchase_momentum"


class MicrocapInsiderPurchaseMomentum(BaseStrategy):
    """Rule-based approximation of the gradient-boosting insider-purchase signal
    targeting microcap equities ($30M–$500M market cap).

    Key features from paper (§3–4):
    - dist_52wk_high: (close − rolling_max_252d) / rolling_max_252d
    - price_momentum_flag: 30-day return > 10% (post-run-up insider buys > CAR)
    - purchase_value: signals insider conviction level
    - transaction_recency: fresher signals decay less

    Scoring replaces GBC.predict_proba() with a rank-weighted composite of
    the above features. LONG when composite score ≥ 0.20 (mirrors paper threshold).
    Hold ~20 trading days (signal_params['hold_days']).
    """

    id                = STRATEGY_ID
    name              = "Microcap Insider Purchase Momentum"
    description       = ("Momentum-confirmed insider open-market purchases in microcaps "
                         "($30M–$500M) proxy for slow information incorporation in illiquid markets.")
    tier              = 2
    signal_frequency  = "daily"
    min_lookback      = 252   # need 252d for 52-week high
    active_in_regimes = ["LOW_VOL", "TRANSITIONING"]

    def default_parameters(self) -> dict:
        return {
            "lookback_days":        20,    # Form 4 recency window (trading days)
            "min_purchase_value":   10_000, # single tx min USD (microcap insiders buy less)
            "score_threshold":      0.20,  # LONG if composite score >= this
            "hold_days":            20,    # target hold in trading days
            "base_size_pct":        0.02,  # % of portfolio per signal (small — microcap illiquid)
            "max_signals":          15,
            # Feature weights for composite score (sum to 1.0)
            "w_momentum":           0.40,  # 30d price momentum > 10% (strongest predictor per §4)
            "w_proximity":          0.35,  # dist from 52wk high (higher = more room, better CAR)
            "w_recency":            0.15,  # days since disclosure (fresher = higher)
            "w_value":              0.10,  # log-scaled purchase value
        }

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get("state", "LOW_VOL")
        if not self.should_run(regime_state):
            return []

        insider_data = (aux_data or {}).get("insider_txns", {})
        if not insider_data:
            print(f"[debug] signals=0", file=sys.stderr)
            return []

        p = self.parameters
        scale = self.position_scale(regime_state)

        # Reference date from prices index
        ref_date = prices.index[-1]
        if isinstance(ref_date, str):
            ref_date = pd.to_datetime(ref_date)

        # Calendar buffer: ~1.5× trading days → calendar days
        cutoff_cal = ref_date - pd.Timedelta(days=int(p["lookback_days"] * 2))

        scored: list[dict] = []

        for ticker in universe:
            if ticker not in prices.columns:
                continue

            ts = prices[ticker].dropna()
            if len(ts) < self.min_lookback:
                continue

            current_price = float(ts.iloc[-1])
            if current_price <= 0:
                continue

            # ── Feature 1: distance from 52-week high ──────────────────────
            high_252 = float(ts.iloc[-252:].max())
            dist_52wk = (current_price - high_252) / high_252   # ≤ 0; closer to 0 = near high
            # Invert for scoring: near-high insider buys score HIGHEST per paper §4
            # (post-run-up confirmation, not mean-reversion)
            prox_score = max(0.0, 1.0 + dist_52wk)  # 1.0 = at high, 0.0 = -100% below

            # ── Feature 2: 30-day price momentum ──────────────────────────
            if len(ts) >= 30:
                ret_30d = float((ts.iloc[-1] - ts.iloc[-30]) / ts.iloc[-30])
            else:
                ret_30d = 0.0
            momentum_score = 1.0 if ret_30d > 0.10 else (0.5 if ret_30d > 0.0 else 0.0)

            # ── Insider transactions ───────────────────────────────────────
            txns = insider_data.get(ticker, [])
            if not txns:
                continue

            # Collect open-market purchases within lookback window
            buys: list[dict] = []
            for t in txns:
                try:
                    txn_date = pd.to_datetime(t.get("transactionDate") or t.get("transaction_date") or "")
                    if txn_date < cutoff_cal:
                        continue
                    ttype = (t.get("transactionType") or t.get("transaction_type") or "").upper()
                    if not ("BUY" in ttype or "PURCHASE" in ttype):
                        continue
                    value = float(t.get("value") or 0)
                    if value < p["min_purchase_value"]:
                        continue
                    buys.append({
                        "date": txn_date,
                        "value": value,
                        "name": t.get("reportingName") or t.get("insider_name") or "UNKNOWN",
                    })
                except Exception:
                    continue

            if not buys:
                continue

            # ── Feature 3: recency score (most recent purchase) ──────────
            latest_buy_date = max(b["date"] for b in buys)
            days_since = max(1, (ref_date - latest_buy_date).days)
            recency_score = max(0.0, 1.0 - days_since / (p["lookback_days"] * 2))

            # ── Feature 4: log-scaled purchase value ─────────────────────
            total_value = sum(b["value"] for b in buys)
            # Log-scale: $10K → 0.0, $1M → ~0.7, $10M → ~1.0 (normalized by log(1e7))
            value_score = min(1.0, math.log10(max(total_value, p["min_purchase_value"])) / 7.0)

            # ── Composite score ───────────────────────────────────────────
            w = p
            score = (
                w["w_momentum"]  * momentum_score
                + w["w_proximity"] * prox_score
                + w["w_recency"]   * recency_score
                + w["w_value"]     * value_score
            )

            if score < p["score_threshold"]:
                continue

            scored.append({
                "ticker": ticker,
                "score": score,
                "ts": ts,
                "current_price": current_price,
                "ret_30d": ret_30d,
                "dist_52wk": dist_52wk,
                "total_value": total_value,
                "buy_count": len(buys),
                "distinct_insiders": len({b["name"] for b in buys}),
                "latest_buy_date": str(latest_buy_date.date()),
            })

        # Sort by composite score descending; cap at max_signals
        scored.sort(key=lambda x: x["score"], reverse=True)
        scored = scored[: p["max_signals"]]

        signals: List[Signal] = []
        for item in scored:
            ts = item["ts"]
            ticker = item["ticker"]
            current_price = item["current_price"]
            stops = self.compute_stops_and_targets(
                ts, "LONG", current_price, regime_state=regime_state
            )

            # Confidence: HIGH if momentum confirmed + distinct insiders > 1
            if item["ret_30d"] > 0.10 and item["distinct_insiders"] >= 2:
                conf = "HIGH"
            elif item["score"] >= 0.40:
                conf = "MED"
            else:
                conf = "LOW"

            signals.append(Signal(
                ticker            = ticker,
                direction         = "LONG",
                entry_price       = float(current_price),
                stop_loss         = float(stops["stop"]),
                target_1          = float(stops["t1"]),
                target_2          = float(stops["t2"]),
                target_3          = float(stops["t3"]),
                position_size_pct = round(float(p["base_size_pct"]) * scale, 4),
                confidence        = conf,
                signal_params     = {
                    "composite_score":     round(item["score"], 4),
                    "ret_30d":             round(item["ret_30d"], 4),
                    "dist_52wk_high":      round(item["dist_52wk"], 4),
                    "total_purchase_value": int(item["total_value"]),
                    "distinct_insiders":   item["distinct_insiders"],
                    "buy_count":           item["buy_count"],
                    "latest_buy_date":     item["latest_buy_date"],
                    "hold_days":           p["hold_days"],
                },
            ))

        print(f"[debug] signals={len(signals)}", file=sys.stderr)
        return signals
