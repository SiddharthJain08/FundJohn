"""
S12 — Insider Cluster Buy
Cluster buying signal: ≥3 insiders buying within 20 trading days, net buy value > $500K.
Data via aux_data['insider_txns']. Active in LOW_VOL and TRANSITIONING regimes.
"""

import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List
from ..base import BaseStrategy, Signal


class InsiderClusterBuy(BaseStrategy):
    id               = 'S12_insider'
    name             = 'Insider Cluster Buy'
    description      = "Signal when ≥3 insiders buy within 20 days with net value > $500K."
    tier             = 2
    signal_frequency = 'daily'
    min_lookback     = 20
    # Insider movements are regime-agnostic per operator (2026-05-28): the
    # informational content of insider cluster buys does not decay across
    # LOW_VOL/TRANSITIONING/HIGH_VOL. CRISIS remains excluded (extreme
    # tail-risk regime; no insider edge documented during panics).
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def default_parameters(self) -> dict:
        return {
            'min_insiders':      3,         # minimum distinct insiders
            'lookback_days':     20,        # trading days to scan
            'min_net_buy_value': 500_000,   # USD
            'min_buy_value':     50_000,    # single transaction minimum
            'base_size_pct':     0.03,
            # SELL-cluster constants
            'min_sell_insiders':   5,
            'min_net_sell_value':  2_000_000,
            'require_zero_buys':   True,
            # Per-ticker re-fire cooldown after a stop-out (trading days). The
            # insider cluster persists for days; without this, the strategy
            # re-enters daily as a losing ticker bleeds (CHTR fired 19x in May
            # 2026, stop-out 11x, cum -30.7pp). aux_data['recent_stop_outs'] is
            # populated by aux_data_loader from strategy_backtest_trades.
            'cooldown_after_stop_days': 10,
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

        # Per-ticker post-stop-out cooldown (Bug 3). recent_stop_outs is a dict
        # {ticker: last_stop_out_date} populated by aux_data_loader. Missing /
        # malformed entries fail OPEN (no skip).
        recent_stops = (aux_data or {}).get('recent_stop_outs') or {}
        cooldown_days = int(p.get('cooldown_after_stop_days', 10))

        for ticker in universe:
            if ticker not in prices.columns:
                continue

            # Cooldown: skip tickers that stopped out within the last N trading days.
            last_stop = recent_stops.get(ticker)
            if last_stop is not None:
                try:
                    last_stop_ts = pd.to_datetime(last_stop)
                    if (ref_date - last_stop_ts).days < cooldown_days:
                        continue   # cooldown active — skip this ticker
                except (TypeError, ValueError):
                    pass   # malformed date — fail open

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
            stops = self.compute_stops_and_targets(ts, 'LONG', current_price, regime_state=regime_state)

            conf = 'HIGH' if distinct_insiders >= 5 and net_buy_value >= 2_000_000 else 'MED'
            if conf == 'MED':
                continue   # gate: only HIGH-conf BUY clusters fire

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

        # SELL-CLUSTER BRANCH (gated; default OFF)
        if os.environ.get('OPENCLAW_S12_SELL_CLUSTER') == '1':
            signals.extend(self._generate_sell_signals(
                params=p,
                insider_data=insider_data,
                prices=prices,
                universe=universe,
                regime_state=regime_state,  # NEW
                aux_data=aux_data,          # NEW: cooldown
            ))

        signals.sort(
            key=lambda s: (
                s.signal_params.get('net_buy_value', 0) or 0
            ) + (
                s.signal_params.get('net_sell_value', 0) or 0
            ),
            reverse=True,
        )
        return signals[:8]

    def _generate_sell_signals(
        self,
        params: dict,
        insider_data: dict,
        prices,
        universe,
        regime_state: str = 'LOW_VOL',  # NEW
        aux_data: dict = None,          # NEW: cooldown
    ) -> list:
        """Emit SHORT signals on insider sell-clusters.

        Gates:
          - distinct_sellers >= params['min_sell_insiders']
          - len(buys) == 0  (when params['require_zero_buys'] is True)
          - net_sell_value >= params['min_net_sell_value']
        """
        if not insider_data:
            return []

        scale = self.position_scale(regime_state)

        try:
            ref_date = prices.index[-1]
            if isinstance(ref_date, str):
                ref_date = pd.to_datetime(ref_date)
        except (AttributeError, IndexError):
            return []

        cutoff = ref_date - pd.Timedelta(days=int(params.get('lookback_days', 20)) * 1.5)

        # Per-ticker post-stop-out cooldown (Bug 3). Mirrors the BUY branch.
        recent_stops = (aux_data or {}).get('recent_stop_outs') or {}
        cooldown_days = int(params.get('cooldown_after_stop_days', 10))

        out = []
        for ticker in universe:
            if ticker not in prices.columns:
                continue

            # Cooldown: skip tickers that stopped out within the last N trading days.
            last_stop = recent_stops.get(ticker)
            if last_stop is not None:
                try:
                    last_stop_ts = pd.to_datetime(last_stop)
                    if (ref_date - last_stop_ts).days < cooldown_days:
                        continue
                except (TypeError, ValueError):
                    pass

            txns = insider_data.get(ticker, [])
            if not txns:
                continue

            # Classify transactions within the lookback window
            sells = []
            buys = []
            for t in txns:
                td_raw = t.get('transactionDate') or t.get('transaction_date')
                if td_raw is None:
                    continue
                try:
                    td = pd.to_datetime(td_raw)
                except (TypeError, ValueError):
                    continue
                if td < cutoff:
                    continue

                ttype = (t.get('transactionType') or t.get('transaction_type') or '').upper()
                if 'SALE' in ttype or 'SELL' in ttype:
                    sells.append(t)
                elif 'PURCHASE' in ttype or 'BUY' in ttype:
                    buys.append(t)

            if not sells:
                continue

            if params.get('require_zero_buys', True) and buys:
                continue

            distinct_sellers = len({
                (t.get('reportingName') or t.get('insider_name') or '') for t in sells
            })
            if distinct_sellers < int(params['min_sell_insiders']):
                continue

            net_sell_value = sum(
                float(t.get('value') or t.get('net_value') or 0.0) for t in sells
            )
            if net_sell_value < float(params['min_net_sell_value']):
                continue

            ts = prices[ticker].dropna()
            if len(ts) < self.min_lookback:
                continue

            current_price = float(ts.iloc[-1])
            if current_price <= 0:
                continue

            stops = self.compute_stops_and_targets(ts, 'SHORT', current_price, regime_state=regime_state)

            out.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(float(params['base_size_pct']) * scale, 4),
                confidence        = 'HIGH',
                signal_params     = {
                    'distinct_insiders': int(distinct_sellers),
                    'net_sell_value':    round(net_sell_value, 0),
                    'sell_count':        len(sells),
                    'lookback_days':     int(params.get('lookback_days', 20)),
                    'cluster_kind':      'SELL',
                },
            ))
        return out
