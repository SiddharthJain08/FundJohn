"""
Insider Seller Strike — routine-seller silence as private good news.

Source: Gao, Ma & Ng, "The Sound of Silence: What do insiders NOT trading
tell us?" (insider silence literature). Routine insider sellers (regular
diversification/10b5-1-style programs) who abruptly STOP selling are, on
average, sitting on private good news — the absence of the routine sale is
the signal, not a sale/purchase itself.

Signal: a name qualifies when its insiders show sells in >= 5 distinct
calendar months inside the t-15m..t-3m window (routine-seller pattern) AND
zero sells in the trailing 90 days (the strike). LONG, monthly rebalance,
<= 10 names per firing.

Data: aux_data['insider_history_long'] — 450-day (~15 month) rolling window
of insider transaction dicts per ticker (transactionDate pd.Timestamp,
transactionType e.g. 'S-Sale', value, shares, role). NOTE: 450d ~ 15 months,
so the t-15m..t-3m routine window JUST fits inside the slice. Returns []
gracefully when the aux key is absent (validator / no-insider environments).
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['InsiderSellerStrike']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_insider_seller_strike'


class InsiderSellerStrike(BaseStrategy):
    """LONG routine insider sellers that have gone silent for 90 days."""

    id                = STRATEGY_ID
    name              = 'Insider Seller Strike'
    description       = ('Routine insider sellers (sells in >=5 distinct months of t-15m..t-3m) with ZERO '
                         'sells in the trailing 90d -> LONG (seller silence = private good news; Gao-Ma-Ng). '
                         'Monthly, <=10 names.')
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 10

    ROUTINE_MIN_MONTHS = 5      # distinct sell-months required in t-15m..t-3m
    SILENCE_DAYS       = 90     # zero sells in this trailing window
    MIN_PRICE          = 5.0
    BASE_SIZE_LONG     = 0.015

    def default_parameters(self) -> dict:
        return {
            'routine_min_months': self.ROUTINE_MIN_MONTHS,
            'silence_days':       self.SILENCE_DAYS,
            'min_price':          self.MIN_PRICE,
        }

    @staticmethod
    def _equity_view(prices: pd.DataFrame) -> pd.DataFrame:
        """Equity columns on equity trading days (drops crypto/index-only union rows)."""
        cols = [t for t in prices.columns
                if isinstance(t, str) and not t.startswith('^')
                and '-USD' not in t and '=F' not in t and '=X' not in t]
        if not cols:
            return pd.DataFrame()
        return prices[cols].dropna(how='all')

    @staticmethod
    def _month_boundary(index: pd.Index) -> bool:
        if len(index) < 2:
            return False
        d1, d0 = pd.Timestamp(index[-1]), pd.Timestamp(index[-2])
        return d1.month != d0.month

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

        eq = self._equity_view(prices)
        # Union-calendar guard: only act on bars that ARE equity sessions.
        if eq.empty or eq.index[-1] != prices.index[-1]:
            print('[debug] signals=0', file=sys.stderr)
            return []

        if not self._month_boundary(eq.index):
            print('[debug] signals=0', file=sys.stderr)
            return []

        hist = (aux_data or {}).get('insider_history_long') or {}
        if not hist:
            print('[debug] signals=0', file=sys.stderr)
            return []

        min_months = int(self.parameters.get('routine_min_months', self.ROUTINE_MIN_MONTHS))
        silence_d  = int(self.parameters.get('silence_days', self.SILENCE_DAYS))
        min_price  = float(self.parameters.get('min_price', self.MIN_PRICE))

        asof = pd.Timestamp(eq.index[-1])
        silence_cut = asof - pd.Timedelta(days=silence_d)
        uni = set(universe) if universe else None

        candidates = []   # (-routine_months, -n_sells, ticker, meta)
        for ticker, txns in hist.items():
            if not isinstance(ticker, str) or ticker not in eq.columns:
                continue
            if uni is not None and ticker not in uni:
                continue
            if not txns:
                continue
            sell_dates = []
            for txn in txns:
                try:
                    ttype = str(txn.get('transactionType', '') or '')
                    if not ttype.upper().startswith('S'):
                        continue          # only 'S-Sale' rows count as sells
                    d = pd.Timestamp(txn.get('transactionDate'))
                except Exception:
                    continue
                if pd.isna(d) or d > asof:
                    continue
                sell_dates.append(d)
            if not sell_dates:
                continue
            # The strike: ZERO sells in the trailing 90 days.
            recent = [d for d in sell_dates if d > silence_cut]
            if recent:
                continue
            # Routine pattern: sells in >= min_months distinct calendar months
            # inside t-15m..t-3m (aux slice already floors at t-450d).
            past = [d for d in sell_dates if d <= silence_cut]
            months = {(d.year, d.month) for d in past}
            if len(months) < min_months:
                continue
            series = eq[ticker].dropna()
            if series.empty:
                continue
            price = float(series.iloc[-1])
            if not np.isfinite(price) or price < min_price:
                continue
            last_sell_age = int((asof - max(past)).days)
            candidates.append((-len(months), -len(past), ticker,
                               {'months': len(months), 'n_sells': len(past),
                                'last_sell_age_days': last_sell_age,
                                'price': price, 'series': series}))

        candidates.sort(key=lambda c: (c[0], c[1], c[2]))

        scale = self.position_scale(regime_state)
        signals: List[Signal] = []
        for _, _, ticker, m in candidates[:self.MAX_SIGNALS]:
            stops = self.compute_stops_and_targets(m['series'], 'LONG', m['price'],
                                                   regime_state=regime_state)
            conf = 'HIGH' if m['months'] >= 9 else ('MED' if m['months'] >= 7 else 'LOW')
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=m['price'],
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(self.BASE_SIZE_LONG * scale, 6),
                confidence=conf,
                signal_params={
                    'routine_sell_months':  m['months'],
                    'sells_in_window':      m['n_sells'],
                    'last_sell_age_days':   m['last_sell_age_days'],
                    'silence_days':         silence_d,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
