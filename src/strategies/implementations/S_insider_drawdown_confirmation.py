"""
Insider Drawdown Confirmation — informed bottom-fishing in beaten-down names.

Hypothesis: insider buying is most informative when the stock is deep in
drawdown — insiders committing personal capital >=30% below the 252d high are
expressing conviction that the decline is overdone (vs. routine/uninformed
buys near highs). Require price >=30% below the trailing 252d high AND
trailing 45d NET insider buys >= $200k across >=2 distinct insiders -> LONG.
The drawdown conditioning distinguishes this from S12_insider's unconditional
cluster-buy signal. <=8/day; 21d per-name cooldown (stateless: skip names
whose thresholds were already fully met by transactions older than 21 days —
those fired on an earlier bar).

Data: close panel (engine) + aux_data['insider_txns'] (trailing-45d
transaction dicts: transactionDate, transactionType, reportingName, value,
shares, role, sharesOwnedAfter). Returns [] when aux is absent.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['InsiderDrawdownConfirmation']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_insider_drawdown_confirmation'

_OFFICER_TOKENS = ('CEO', 'CFO', 'COO', 'CHIEF', 'PRESIDENT', 'OFFICER', 'DIRECTOR')


class InsiderDrawdownConfirmation(BaseStrategy):
    """LONG deep-drawdown names with fresh multi-insider net buying."""

    id                = STRATEGY_ID
    name              = 'Insider Drawdown Confirmation'
    description       = ('Price >=30% below 252d high + trailing 45d net insider buys >= $200k '
                         'across >=2 distinct insiders -> LONG informed bottom-fishing; '
                         '<=8/day, 21d per-name cooldown.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 260
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 8

    MIN_DRAWDOWN     = 0.30
    WINDOW_DAYS      = 45           # calendar days (matches the aux slice envelope)
    MIN_NET_BUYS     = 200_000.0
    MIN_BUYERS       = 2
    COOLDOWN_DAYS    = 21           # calendar days
    MIN_PRICE        = 3.0
    MAX_TXN_VALUE    = 5e9          # single-filing sanity cap (data noise guard)
    BASE_SIZE_LONG   = 0.015

    def default_parameters(self) -> dict:
        return {
            'min_drawdown':  self.MIN_DRAWDOWN,
            'window_days':   self.WINDOW_DAYS,
            'min_net_buys':  self.MIN_NET_BUYS,
            'min_buyers':    self.MIN_BUYERS,
            'cooldown_days': self.COOLDOWN_DAYS,
        }

    @staticmethod
    def _equity_rows(prices: pd.DataFrame) -> pd.DataFrame:
        """Drop union-calendar rows where every equity column is NaN."""
        eq_cols = [c for c in prices.columns
                   if not str(c).startswith('^') and '-USD' not in str(c)
                   and '=F' not in str(c) and '=X' not in str(c)]
        if not eq_cols:
            return prices
        return prices.loc[prices[eq_cols].notna().any(axis=1).values]

    def _net_buy_stats(self, txns: list, cutoff: pd.Timestamp,
                       latest: pd.Timestamp) -> dict:
        """Aggregate buy/sell stats for transactions dated in (cutoff, latest]."""
        buyers, officer_buys, buy_count = set(), 0, 0
        buy_val, sell_val = 0.0, 0.0
        for t in txns:
            try:
                td = pd.to_datetime(t.get('transactionDate'))
            except (TypeError, ValueError):
                continue
            if pd.isna(td) or td <= cutoff or td > latest:
                continue
            ttype = (t.get('transactionType') or '').upper()
            try:
                val = float(t.get('value') or 0.0)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(val) or val < 0 or val > self.MAX_TXN_VALUE:
                continue
            if 'PURCHASE' in ttype or 'BUY' in ttype:
                buy_val += val
                buy_count += 1
                buyers.add(t.get('reportingName') or 'UNKNOWN')
                role = (t.get('role') or '').upper()
                if any(tok in role for tok in _OFFICER_TOKENS):
                    officer_buys += 1
            elif 'SALE' in ttype or 'SELL' in ttype:
                sell_val += val
        return {
            'net':          buy_val - sell_val,
            'buyers':       len(buyers),
            'buy_count':    buy_count,
            'officer_buys': officer_buys,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or len(prices) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        insider = (aux_data or {}).get('insider_txns') or {}
        if not insider:
            print('[debug] signals=0', file=sys.stderr)
            return []

        eq = self._equity_rows(prices)
        if len(eq) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        p = self.parameters
        min_dd    = float(p.get('min_drawdown', self.MIN_DRAWDOWN))
        window    = int(p.get('window_days', self.WINDOW_DAYS))
        min_net   = float(p.get('min_net_buys', self.MIN_NET_BUYS))
        min_buyer = int(p.get('min_buyers', self.MIN_BUYERS))
        cooldown  = int(p.get('cooldown_days', self.COOLDOWN_DAYS))

        ref = pd.Timestamp(eq.index[-1])
        win_cutoff  = ref - pd.Timedelta(days=window)
        cool_cutoff = ref - pd.Timedelta(days=cooldown)

        uni = set(universe) if universe else None
        rows = []
        for ticker, txns in insider.items():
            if ticker not in eq.columns or not txns:
                continue
            if uni is not None and ticker not in uni:
                continue

            ts = eq[ticker].dropna()
            if len(ts) < 252:
                continue
            cur = float(ts.iloc[-1])
            if not np.isfinite(cur) or cur < self.MIN_PRICE:
                continue
            high252 = float(ts.iloc[-252:].max())
            if high252 <= 0 or cur > high252 * (1.0 - min_dd):
                continue

            now = self._net_buy_stats(txns, win_cutoff, ref)
            if now['buyers'] < min_buyer or now['net'] < min_net:
                continue

            # Stateless 21d cooldown: if the thresholds were ALREADY fully met by
            # transactions dated <= ref-21d, the signal fired on an earlier bar.
            old = self._net_buy_stats(txns, win_cutoff, cool_cutoff)
            if old['buyers'] >= min_buyer and old['net'] >= min_net:
                continue

            rows.append((ticker, cur, high252, now))

        rows.sort(key=lambda r: r[3]['net'], reverse=True)
        rows = rows[:self.MAX_SIGNALS]

        scale = self.position_scale(regime_state)
        signals: List[Signal] = []
        for ticker, cur, high252, st in rows:
            if st['net'] >= 1_000_000 and st['buyers'] >= 3:
                conf = 'HIGH'
            elif st['net'] >= 400_000:
                conf = 'MED'
            else:
                conf = 'LOW'
            series = eq[ticker].dropna()
            stops = self.compute_stops_and_targets(series, 'LONG', cur,
                                                   regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=cur,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(self.BASE_SIZE_LONG * scale, 6),
                confidence=conf,
                signal_params={
                    'net_buy_value':   round(st['net'], 0),
                    'distinct_buyers': st['buyers'],
                    'buy_count':       st['buy_count'],
                    'officer_buys':    st['officer_buys'],
                    'drawdown_pct':    round(cur / high252 - 1.0, 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
