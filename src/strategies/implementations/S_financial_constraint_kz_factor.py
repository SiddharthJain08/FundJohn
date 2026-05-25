"""
S_financial_constraint_kz_factor — Lamont, Polk & Saá-Requejo (1997 NBER w6210)
KZ-index long/short: LONG bottom-quintile (unconstrained), SHORT top-quintile (constrained).
kz = -1.002*(CF/K) + 0.283*Q + 3.139*(Debt/Cap) - 39.368*(Div/K) - 1.315*(Cash/K)
Annual rebalance using prior-fiscal-year fundamentals; equal-weight within quintile.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['FinancialConstraintKZFactor']


class FinancialConstraintKZFactor(BaseStrategy):
    """KZ-index long/short factor: LONG unconstrained (low KZ), SHORT constrained (high KZ)."""

    id               = 'S_financial_constraint_kz_factor'
    name             = 'FinancialConstraintKZFactor'
    description      = ('Long-unconstrained / short-constrained portfolio based on the KZ index '
                        'captures a constraint risk premium not explained by size, value, or momentum.')
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Step 1: compute KZ score for each ticker ─────────────────────────
        kz_scores: dict[str, float] = {}

        for ticker in universe:
            if ticker not in prices.columns:
                continue
            fin = financials.get(ticker)
            if not fin:
                continue

            # Capital (K) — net PP&E; skip if zero to avoid division by zero
            capital = (fin.get('propertyPlantEquipmentNet')
                       or fin.get('netPPE')
                       or fin.get('propertyPlantAndEquipmentNet') or 0.0)
            capital = float(capital)
            if capital <= 0:
                continue

            # Cash flow (CF) = operating cash flow proxy
            cf = (fin.get('operatingCashFlow')
                  or fin.get('cashFlowFromOperations')
                  or fin.get('freeCashFlow') or 0.0)
            if cf == 0.0:
                net_income = float(fin.get('netIncome') or 0.0)
                dna        = float(fin.get('depreciationAndAmortization') or
                                   fin.get('depreciation') or 0.0)
                cf = net_income + dna
            cf = float(cf)

            # Tobin's Q = (market cap + total debt) / total assets
            # Use shares * price as market cap proxy
            shares = (fin.get('commonStockSharesOutstanding')
                      or fin.get('sharesOutstanding') or 0.0)
            shares = float(shares)
            ts = prices[ticker].dropna()
            if len(ts) < 5:
                continue
            price = float(ts.iloc[-1])
            mkt_cap = shares * price if shares > 0 else 0.0

            total_assets = float(fin.get('totalAssets') or 0.0)
            if total_assets <= 0:
                continue

            total_debt = float(fin.get('totalDebt')
                               or fin.get('longTermDebt') or 0.0)
            tobin_q = (mkt_cap + total_debt) / total_assets if total_assets > 0 else 1.0

            # Debt / Total Capital
            equity = float(fin.get('totalStockholdersEquity')
                           or fin.get('totalEquity')
                           or fin.get('bookValue') or 0.0)
            total_cap = total_debt + max(equity, 0.0)
            debt_ratio = (total_debt / total_cap) if total_cap > 0 else 0.5

            # Dividends (Div) — use absolute value; often negative in cash flow statements
            div = abs(float(fin.get('dividendsPaid')
                            or fin.get('commonStockDividendsPaid')
                            or fin.get('dividendsCommon') or 0.0))

            # Cash
            cash = float(fin.get('cashAndCashEquivalents')
                         or fin.get('cash')
                         or fin.get('cashAndShortTermInvestments') or 0.0)

            kz = (
                -1.002 * (cf   / capital) +
                 0.283 * tobin_q +
                 3.139 * debt_ratio +
                -39.368 * (div  / capital) +
                -1.315 * (cash / capital)
            )

            # Guard against numerical blowups from very small capital
            if not np.isfinite(kz):
                continue
            kz_scores[ticker] = float(kz)

        if len(kz_scores) < 10:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: quintile selection ────────────────────────────────────────
        tickers = list(kz_scores.keys())
        kz_arr  = np.array([kz_scores[t] for t in tickers], dtype=float)
        n       = len(tickers)
        q_cut   = max(n // 5, 1)
        q_cut   = min(q_cut, self.MAX_SIGNALS // 2)

        sorted_idx  = np.argsort(kz_arr)
        long_idx    = sorted_idx[:q_cut]    # low KZ = unconstrained → LONG
        short_idx   = sorted_idx[-q_cut:]   # high KZ = constrained  → SHORT

        per_pos = min(round(scale / max(len(long_idx), 1), 4), 0.05)

        signals: List[Signal] = []

        for idx in long_idx:
            t  = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            kz_v  = float(kz_arr[idx])
            conf  = 'HIGH' if kz_v < -2.0 else ('MED' if kz_v < 0.0 else 'LOW')
            signals.append(Signal(
                ticker            = t,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_pos,
                confidence        = conf,
                signal_params     = {'kz_score': round(kz_v, 4), 'quintile': 1},
            ))

        for idx in short_idx:
            t  = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            kz_v  = float(kz_arr[idx])
            conf  = 'HIGH' if kz_v > 4.0 else ('MED' if kz_v > 2.0 else 'LOW')
            signals.append(Signal(
                ticker            = t,
                direction         = 'SHORT',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_pos,
                confidence        = conf,
                signal_params     = {'kz_score': round(kz_v, 4), 'quintile': 5},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
