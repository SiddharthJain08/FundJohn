"""
S_ast_combining_fundamental_fscore_and_equity_short_term_reversals
Source: https://quantpedia.com/strategies/combining-fundamental-fscore-and-equity-short-term-reversals/

LONG past 1-month losers with Piotroski F-Score >= 6 (strong fundamentals),
SHORT past 1-month winners with Piotroski F-Score <= 2 (weak fundamentals).
Universe: large-cap subset (top 40% by market cap). Monthly rebalance.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import no_otc as universe_filter

__all__ = ['AstCombiningFscoreShortTermReversals']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_ast_combining_fundamental_fscore_and_equity_short_term_reversals'

_REVERSAL_WINDOW = 21   # 1-month return lookback (trading days)


def _safe_float(v) -> float | None:
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _piotroski_fscore(fin: dict) -> int | None:
    """
    Piotroski-inspired score (0-9) from available financials dict.
    Prior-year change signals use level-based proxies when prior-year
    data is absent (as is typical with our single-period parquet).
    Returns None if < 3 signals are evaluable.
    """
    net_income    = _safe_float(fin.get('net_income') or fin.get('netIncome'))
    total_assets  = _safe_float(fin.get('total_assets') or fin.get('totalAssets'))
    oper_income   = _safe_float(fin.get('operating_income') or fin.get('operatingIncome'))
    gross_margin  = _safe_float(fin.get('gross_margin') or fin.get('grossProfitMargin'))
    debt_equity   = _safe_float(fin.get('debt_equity_ratio') or fin.get('debtEquityRatio'))
    total_liab    = _safe_float(fin.get('total_liabilities') or fin.get('totalLiabilities'))
    revenue_growth = _safe_float(fin.get('revenue_growth'))
    working_cap   = _safe_float(fin.get('working_capital') or fin.get('workingCapital'))
    revenue       = _safe_float(fin.get('revenue'))

    # Prior-year aliases (rarely populated but checked)
    net_income_py    = _safe_float(fin.get('netIncomePriorYear') or fin.get('netIncomePY'))
    total_assets_py  = _safe_float(fin.get('totalAssetsPriorYear') or fin.get('totalAssetsPY'))
    gross_margin_py  = _safe_float(fin.get('grossMarginPriorYear') or fin.get('grossMarginPY'))
    long_term_debt   = _safe_float(fin.get('longTermDebt') or fin.get('long_term_debt'))
    long_term_debt_py = _safe_float(fin.get('longTermDebtPriorYear') or fin.get('longTermDebtPY'))
    shares           = _safe_float(fin.get('sharesOutstanding') or fin.get('commonStockSharesOutstanding'))
    shares_py        = _safe_float(fin.get('sharesOutstandingPriorYear') or fin.get('sharesOutstandingPY'))
    current_ratio    = _safe_float(fin.get('currentRatio') or fin.get('current_ratio'))
    current_ratio_py = _safe_float(fin.get('currentRatioPriorYear') or fin.get('currentRatioPY'))

    if net_income is None or total_assets is None or total_assets <= 0:
        return None

    score = 0
    n = 0
    roa = net_income / total_assets

    # F1: ROA > 0
    n += 1; score += int(roa > 0)

    # F2: CFO > 0 — proxy: operating income > 0
    if oper_income is not None:
        n += 1; score += int(oper_income > 0)

    # F3: ΔROA — real if prior year available, else proxy via positive revenue_growth
    if net_income_py is not None and total_assets_py is not None and total_assets_py > 0:
        n += 1; score += int(roa > net_income_py / total_assets_py)
    elif revenue_growth is not None:
        n += 1; score += int(revenue_growth > 0.05)   # improving top line ≈ improving efficiency

    # F4: Accrual — proxy: operating income > net income (cash-quality signal)
    if oper_income is not None:
        n += 1; score += int(oper_income > net_income)

    # F5: Δleverage — real if prior year, else level: low debt/equity
    if long_term_debt is not None and long_term_debt_py is not None and total_assets_py is not None and total_assets_py > 0:
        n += 1; score += int(long_term_debt / total_assets < long_term_debt_py / total_assets_py)
    elif debt_equity is not None:
        n += 1; score += int(debt_equity <= 1.0)
    elif total_liab is not None:
        n += 1; score += int(total_liab / total_assets < 0.5)

    # F6: Δcurrent ratio — real if available, else proxy: working capital > 0
    if current_ratio is not None and current_ratio_py is not None:
        n += 1; score += int(current_ratio > current_ratio_py)
    elif working_cap is not None:
        n += 1; score += int(working_cap > 0)

    # F7: No new equity (shares not increased)
    if shares is not None and shares_py is not None and shares_py > 0:
        n += 1; score += int(shares <= shares_py)

    # F8: Δgross margin — real if prior year, else proxy: meaningful margin
    if gross_margin is not None and gross_margin_py is not None:
        n += 1; score += int(gross_margin > gross_margin_py)
    elif gross_margin is not None:
        n += 1; score += int(gross_margin > 0.15)

    # F9: Δasset turnover — proxy: revenue growth > 0
    if revenue_growth is not None:
        n += 1; score += int(revenue_growth > 0)
    elif revenue is not None:
        n += 1; score += int(revenue / total_assets > 0.3)

    if n < 3:
        return None
    return score


class AstCombiningFscoreShortTermReversals(BaseStrategy):
    """LONG past-month losers with high Piotroski F-Score; SHORT past-month winners with low F-Score."""

    id               = STRATEGY_ID
    name             = 'AstCombiningFscoreShortTermReversals'
    description      = ('Past 1-month losers with strong Piotroski F-Scores (7-9) outperform past '
                        'winners with weak F-Scores (0-3), combining mean-reversion with fundamental '
                        'quality screening.')
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = _REVERSAL_WINDOW + 5
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
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Monthly rebalance: fire only when month turns (first trading day of new month)
        if len(prices) >= 2:
            curr_month = pd.Timestamp(prices.index[-1]).month
            prev_month = pd.Timestamp(prices.index[-2]).month
            if curr_month == prev_month:
                print('[debug] signals=0', file=sys.stderr)
                return []

        if len(prices) < _REVERSAL_WINDOW + 1:
            print('[debug] signals=0', file=sys.stderr)
            return []

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Step 1: compute F-Score and 1-month return per ticker ─────────────
        fscore_map: dict[str, int] = {}
        return_map: dict[str, float] = {}
        mcap_map: dict[str, float] = {}

        for ticker in universe:
            if ticker not in prices.columns:
                continue
            price_series = prices[ticker].dropna()
            if len(price_series) < _REVERSAL_WINDOW + 1:
                continue
            entry_price = float(price_series.iloc[-1])
            if entry_price < 5.0:
                continue

            ret_1m = float(price_series.iloc[-1] / price_series.iloc[-1 - _REVERSAL_WINDOW] - 1)
            if not np.isfinite(ret_1m):
                continue
            return_map[ticker] = ret_1m

            fin = financials.get(ticker)
            if fin:
                fs = _piotroski_fscore(fin)
                if fs is not None:
                    fscore_map[ticker] = fs
                    mc = _safe_float(fin.get('marketCap') or fin.get('market_cap'))
                    if mc and mc > 0:
                        mcap_map[ticker] = mc

        if not fscore_map or not return_map:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: large-cap filter — top 40% by market cap ─────────────────
        if mcap_map:
            cap_threshold = pd.Series(mcap_map).quantile(0.60)
            large_cap_set = {t for t, mc in mcap_map.items() if mc >= cap_threshold}
            large_cap_set |= {t for t in fscore_map if t not in mcap_map}
        else:
            large_cap_set = set(fscore_map.keys())

        candidates = {t for t in fscore_map if t in return_map and t in large_cap_set}
        if len(candidates) < 10:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 3: quintile selection by 1-month return ──────────────────────
        # Score thresholds scale with available n_signals; 6+ = strong (≈7-9/9),
        # 2- = weak (≈0-3/9) on typical 7-8 evaluable-signal scale.
        ret_series = pd.Series({t: return_map[t] for t in candidates})
        q20 = ret_series.quantile(0.20)
        q80 = ret_series.quantile(0.80)

        long_pool  = [t for t in candidates if return_map[t] <= q20 and fscore_map[t] >= 6]
        short_pool = [t for t in candidates if return_map[t] >= q80 and fscore_map[t] <= 2]

        if not long_pool and not short_pool:
            print('[debug] signals=0', file=sys.stderr)
            return []

        signals: List[Signal] = []
        total = max(len(long_pool) + len(short_pool), 1)

        for ticker in long_pool:
            price_series = prices[ticker].dropna()
            entry_price  = float(price_series.iloc[-1])
            stops = self.compute_stops_and_targets(price_series, 'LONG', entry_price, regime_state=regime_state)
            pos_size = min(scale / total, 0.10)
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=entry_price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(pos_size, 4),
                confidence='HIGH' if fscore_map[ticker] >= 8 else 'MED',
                signal_params={
                    'fscore': fscore_map[ticker],
                    'ret_1m': round(return_map[ticker], 4),
                    'strategy': 'fscore_reversal_long',
                },
            ))

        for ticker in short_pool:
            price_series = prices[ticker].dropna()
            entry_price  = float(price_series.iloc[-1])
            stops = self.compute_stops_and_targets(price_series, 'SHORT', entry_price, regime_state=regime_state)
            pos_size = min(scale / total, 0.10)
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=entry_price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(pos_size, 4),
                confidence='HIGH' if fscore_map[ticker] <= 1 else 'MED',
                signal_params={
                    'fscore': fscore_map[ticker],
                    'ret_1m': round(return_map[ticker], 4),
                    'strategy': 'fscore_reversal_short',
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
