"""
S12 — Insider Cluster Buy
Cluster buying signal: ≥3 insiders buying within 20 trading days, net buy value > $500K.
Data via aux_data['insider_txns']. Active in LOW_VOL and TRANSITIONING regimes.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List
from ..base import BaseStrategy, Signal


class InsiderClusterBuy(BaseStrategy):
    id = 'insider_cluster_buy'
    name             = 'Insider Cluster Buy'
    description      = "Signal when ≥3 insiders buy within 20 days with net value > $500K."
    tier             = 2
    signal_frequency = 'daily'
    min_lookback     = 20
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    def default_parameters(self) -> dict:
        return {
            'min_insiders':      3,         # minimum distinct insiders
            'lookback_days':     20,        # trading days to scan
            'min_net_buy_value': 500_000,   # USD
            'min_buy_value':     50_000,    # single transaction minimum
            'base_size_pct':     0.03,
        }

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        insider_data = (aux_data or {}).get('insider_txns', {})
        if not insider_data:
            return []

        scale  = self.position_scale(regime_state)
        p      = self.parameters
        signals = []

        # Reference date: last date in prices
        ref_date = prices.index[-1]
        if isinstance(ref_date, str):
            ref_date = pd.to_datetime(ref_date)

        cutoff = ref_date - pd.Timedelta(days=p['lookback_days'] * 1.5)  # calendar buffer

        for ticker in universe:
            if ticker not in prices.columns:
                continue

            txns = insider_data.get(ticker, [])
            if not txns:
                continue

            # Filter to buys within lookback window above min value
            recent_buys = []
            for t in txns:
                try:
                    txn_date = pd.to_datetime(t.get('transactionDate', ''))
                    if txn_date < cutoff:
                        continue
                    txn_type  = (t.get('transactionType', '') or '').upper()
                    value_raw = t.get('value', 0) or 0
                    if ('BUY' in txn_type or 'PURCHASE' in txn_type) and float(value_raw) >= p['min_buy_value']:
                        recent_buys.append({
                            'name':  t.get('reportingName', 'UNKNOWN'),
                            'value': float(value_raw),
                            'date':  txn_date,
                        })
                except Exception:
                    continue

            if not recent_buys:
                continue

            distinct_insiders = len(set(b['name'] for b in recent_buys))
            net_buy_value     = sum(b['value'] for b in recent_buys)

            if distinct_insiders < p['min_insiders']:
                continue
            if net_buy_value < p['min_net_buy_value']:
                continue

            ts = prices[ticker].dropna()
            if len(ts) < self.min_lookback:
                continue

            current_price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', current_price)

            conf = 'HIGH' if distinct_insiders >= 5 and net_buy_value >= 2_000_000 else 'MED'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(p['base_size_pct'] * scale, 4),
                confidence        = conf,
                signal_params     = {
                    'distinct_insiders': distinct_insiders,
                    'net_buy_value':     round(net_buy_value, 0),
                    'buy_count':         len(recent_buys),
                    'lookback_days':     p['lookback_days'],
                },
            ))

        signals.sort(key=lambda s: s.signal_params.get('net_buy_value', 0), reverse=True)
        return signals[:8]
