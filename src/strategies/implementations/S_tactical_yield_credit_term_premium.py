"""
S_tactical_yield_credit_term_premium — Meb Faber's Tactical Yield

Monthly tactical allocation to IEF (7-10yr Treasury ETF) and/or LQD (IG
Corporate Bond ETF) based on the expanding-window percentile rank of the term
premium (10yr Treasury yield − 3-mo T-bill) and credit premium (IG corporate
yield − 3-mo T-bill).

Rule: allocate 50% to IEF when term_premium pct_rank ≥ 50th pct of full history;
      allocate 50% to LQD when credit_premium pct_rank ≥ 50th pct of full history;
      remainder in cash (T-Bills / FLAT).

Reference: Allocate Smartly / Meb Faber "T-Bills and Chill" TAA (1930–present).
Yield series required: GS10, TB3MS, BAMLC0A0CMEY in macro.parquet.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['TacticalYieldCreditTermPremium']

STRATEGY_ID      = 'S_tactical_yield_credit_term_premium'
INSTRUMENT_CLASS = 'equity'

# Fixed allocation per leg (before regime scale)
ALLOC_PER_ASSET = 0.50
MIN_LOOKBACK    = 252   # ~1yr of daily bars for meaningful expanding pct-rank
PCT_THRESHOLD   = 0.50  # top-half of expanding distribution

# FRED series names expected in macro.parquet
YIELD_10YR = 'GS10'            # 10-Year Treasury Constant Maturity Rate
YIELD_3MO  = 'TB3MS'           # 3-Month T-Bill Secondary Market Rate
YIELD_IG   = 'BAMLC0A0CMEY'   # ICE BofA IG Corporate Bond Effective Yield


class TacticalYieldCreditTermPremium(BaseStrategy):
    """
    LONG IEF when term premium ranks ≥ 50th pct of its expanding history.
    LONG LQD when credit premium ranks ≥ 50th pct of its expanding history.
    Remainder in cash. Monthly rebalance.
    """

    id                = STRATEGY_ID
    name              = 'Tactical Yield — Credit & Term Premium'
    description       = (
        'LONG IEF/LQD only when term/credit premium ranks in the top half of '
        'expanding history; otherwise hold cash — Meb Faber Tactical Yield'
    )
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = MIN_LOOKBACK
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 2

    def default_parameters(self) -> dict:
        return {
            'alloc_ief':            ALLOC_PER_ASSET,
            'alloc_lqd':            ALLOC_PER_ASSET,
            'percentile_threshold': PCT_THRESHOLD,
            'near_month_end_days':  4,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = (regime or {}).get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── 1. Extract yield series ───────────────────────────────────────────
        yield_data = self._extract_yields(aux_data, prices=prices)
        if yield_data is None:
            print('[TacticalYield] macro yield series unavailable — returning []',
                  file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        term_premium, credit_premium = yield_data
        if len(term_premium) < MIN_LOOKBACK or len(credit_premium) < MIN_LOOKBACK:
            print(f'[TacticalYield] insufficient yield history '
                  f'(term={len(term_premium)}, credit={len(credit_premium)}) '
                  f'— need {MIN_LOOKBACK}', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── 2. Monthly gate: fire only near last trading day of each month ────
        last_date = pd.to_datetime(str(prices.index[-1]))
        month_end = (last_date + pd.offsets.MonthEnd(0))
        days_to_me = (month_end - last_date).days
        max_gap = int(self.parameters.get('near_month_end_days', 4))
        if days_to_me > max_gap:
            print(f'[TacticalYield] {days_to_me}d to month-end > {max_gap} — skip',
                  file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── 3. Expanding-window percentile rank ───────────────────────────────
        term_pct   = self._expanding_pct_rank(term_premium)
        credit_pct = self._expanding_pct_rank(credit_premium)

        latest_term   = float(term_pct.iloc[-1])
        latest_credit = float(credit_pct.iloc[-1])

        threshold = float(self.parameters.get('percentile_threshold', PCT_THRESHOLD))
        scale     = self.position_scale(regime_state)
        signals: List[Signal] = []

        # ── 4. IEF signal (term premium gate) ────────────────────────────────
        if latest_term >= threshold:
            ief_px, ief_stops = self._price_and_stops(prices, 'IEF', regime_state)
            signals.append(Signal(
                ticker            = 'IEF',
                direction         = 'LONG',
                entry_price       = ief_px,
                stop_loss         = ief_stops['stop'],
                target_1          = ief_stops['t1'],
                target_2          = ief_stops['t2'],
                target_3          = ief_stops['t3'],
                position_size_pct = round(
                    float(self.parameters.get('alloc_ief', ALLOC_PER_ASSET)) * scale, 4),
                confidence        = 'HIGH' if latest_term >= 0.75 else 'MED',
                signal_params     = {
                    'term_prem_pct_rank': round(latest_term, 4),
                    'term_prem_bps':      round(float(term_premium.iloc[-1]) * 100, 1),
                    'regime':             regime_state,
                },
            ))

        # ── 5. LQD signal (credit premium gate) ──────────────────────────────
        if latest_credit >= threshold:
            lqd_px, lqd_stops = self._price_and_stops(prices, 'LQD', regime_state)
            signals.append(Signal(
                ticker            = 'LQD',
                direction         = 'LONG',
                entry_price       = lqd_px,
                stop_loss         = lqd_stops['stop'],
                target_1          = lqd_stops['t1'],
                target_2          = lqd_stops['t2'],
                target_3          = lqd_stops['t3'],
                position_size_pct = round(
                    float(self.parameters.get('alloc_lqd', ALLOC_PER_ASSET)) * scale, 4),
                confidence        = 'HIGH' if latest_credit >= 0.75 else 'MED',
                signal_params     = {
                    'credit_prem_pct_rank': round(latest_credit, 4),
                    'credit_prem_bps':      round(float(credit_premium.iloc[-1]) * 100, 1),
                    'regime':               regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    # ── helpers ────────────────────────────────────────────────────────────────

    def _extract_yields(
        self, aux_data, prices: 'pd.DataFrame | None' = None
    ) -> 'tuple[pd.Series, pd.Series] | None':
        """
        Return (term_premium, credit_premium) as date-aligned Series, or None.

        Priority:
          1. FRED yield series (GS10 / TB3MS / BAMLC0A0CMEY) from macro.parquet — high fidelity.
          2. Price-based proxies from IEF / LQD / SHY in prices DataFrame — fallback.
        """
        # ── attempt 1: FRED yield series ────────────────────────────────────
        macro_df = (aux_data or {}).get('macro')
        if isinstance(macro_df, pd.DataFrame) and not macro_df.empty:
            if 'series' in macro_df.columns and 'value' in macro_df.columns:
                def pull(name: str) -> 'pd.Series | None':
                    rows = macro_df[macro_df['series'] == name].copy()
                    if rows.empty:
                        return None
                    rows['date'] = pd.to_datetime(rows['date'])
                    rows = rows.sort_values('date').drop_duplicates('date')
                    return rows.set_index('date')['value'].astype(float)

                gs10  = pull(YIELD_10YR)
                tb3ms = pull(YIELD_3MO)
                ig    = pull(YIELD_IG)
                if gs10 is not None and tb3ms is not None and ig is not None:
                    joined = pd.concat(
                        [gs10.rename('g'), tb3ms.rename('t'), ig.rename('c')], axis=1
                    ).dropna()
                    if not joined.empty:
                        return joined['g'] - joined['t'], joined['c'] - joined['t']

        # ── attempt 2: price-based proxies ──────────────────────────────────
        # When bond ETF yields are unavailable, use 12-month excess returns of
        # IEF vs SHY (term premium proxy) and LQD vs SHY (credit premium proxy).
        # High excess return → bonds outperformed cash → carry was rewarded.
        if prices is None or prices.empty:
            return None
        for t in ('IEF', 'LQD', 'SHY'):
            if t not in prices.columns:
                return None

        try:
            monthly = (
                prices[['IEF', 'LQD', 'SHY']]
                .resample('ME').last()
                .dropna()
            )
        except Exception:
            monthly = (
                prices[['IEF', 'LQD', 'SHY']]
                .resample('M').last()
                .dropna()
            )
        if len(monthly) < 14:
            return None

        ief_ret = monthly['IEF'].pct_change(12)
        lqd_ret = monthly['LQD'].pct_change(12)
        shy_ret = monthly['SHY'].pct_change(12)

        idx = ief_ret.dropna().index.intersection(
            lqd_ret.dropna().index
        ).intersection(shy_ret.dropna().index)
        if len(idx) < 6:
            return None

        term_premium   = (ief_ret - shy_ret).loc[idx]
        credit_premium = (lqd_ret - shy_ret).loc[idx]
        return term_premium, credit_premium

    @staticmethod
    def _expanding_pct_rank(s: pd.Series) -> pd.Series:
        """
        Expanding-window percentile rank in [0, 1] with no lookahead bias.
        Uses pandas expanding rank — O(n log n) total.
        """
        rank  = s.expanding().rank()
        count = s.expanding().count()
        return (rank - 1) / (count - 1).clip(lower=1)

    def _price_and_stops(
        self, prices: pd.DataFrame, ticker: str, regime_state: str
    ) -> 'tuple[float, dict]':
        """Return (current_price, stops_dict) for ticker; fall back to par if missing."""
        if ticker in prices.columns:
            series = prices[ticker].dropna()
            if len(series) >= 14:
                price = float(series.iloc[-1])
                stops = self.compute_stops_and_targets(
                    series, 'LONG', price, regime_state=regime_state)
                return price, stops
        # Fallback: par = 100, conservative 2% stop, 5/10/20% targets
        return 100.0, {'stop': 98.0, 't1': 105.0, 't2': 110.0, 't3': 120.0}


# ── Backtest (run standalone) ──────────────────────────────────────────────────
if __name__ == '__main__':
    import json as _json
    import numpy as np

    macro_path  = 'data/master/macro.parquet'
    prices_path = 'data/master/prices.parquet'
    regimes_path = 'data/master/historical_regimes.parquet'

    macro_df   = pd.read_parquet(macro_path)
    prices_raw = pd.read_parquet(prices_path)
    regimes    = pd.read_parquet(regimes_path)

    # Pivot prices to wide format (date × ticker)
    if prices_raw.index.name == 'date' or (
            prices_raw.index.dtype != object and
            not isinstance(prices_raw.columns, pd.MultiIndex)):
        prices_wide = prices_raw
    else:
        prices_wide = prices_raw

    # Verify yield series are present
    available_series = macro_df['series'].unique().tolist() if 'series' in macro_df.columns else []
    required = [YIELD_10YR, YIELD_3MO, YIELD_IG]
    missing  = [s for s in required if s not in available_series]
    if missing:
        print(
            f'[backtest] MISSING yield series {missing} in macro.parquet — '
            f'available: {available_series}\n'
            f'Cannot run backtest without FRED yield data (GS10/TB3MS/BAMLC0A0CMEY).\n'
            f'Populate macro.parquet with these series to enable this strategy.',
            file=sys.stderr,
        )
        sys.exit(0)

    # Map historical regimes
    regime_map = regimes.set_index('date')['regime']

    strat = TacticalYieldCreditTermPremium()
    dates = prices_wide.loc['2017-01-01':'2025-12-31'].index
    records = []
    HOLD_DAYS = 21  # ~1 month hold

    for i in range(strat.min_lookback, len(dates) - HOLD_DAYS, 21):
        sd  = dates[i]
        rs  = str(regime_map.asof(sd)) if sd >= regime_map.index[0] else 'LOW_VOL'
        if pd.isna(rs) or rs == 'nan':
            rs = 'LOW_VOL'

        window = prices_wide.iloc[:i + 1]
        sigs   = strat.generate_signals(
            prices=window,
            regime={'state': rs},
            universe=list(prices_wide.columns),
            aux_data={'macro': macro_df},
        )
        if not sigs:
            continue

        exit_idx  = min(i + HOLD_DAYS, len(dates) - 1)
        exit_date = dates[exit_idx]

        for sig in sigs:
            t = sig.ticker
            if t not in prices_wide.columns:
                continue
            ep = sig.entry_price
            xp = (prices_wide.loc[exit_date, t]
                  if exit_date in prices_wide.index else float('nan'))
            if pd.isna(xp) or ep <= 0:
                continue
            ret   = (xp - ep) / ep
            pnl   = ret if sig.direction == 'LONG' else -ret
            sdist = abs(ep - sig.stop_loss)
            rmult = pnl / (sdist / ep) if sdist > 0 else 0.0
            records.append({
                'strategy_id':  STRATEGY_ID,
                'signal_date':  str(sd.date()),
                'regime_state': rs,
                'pnl':          float(pnl),
                'r_multiple':   float(rmult),
            })

    trades_df = pd.DataFrame(records)
    if trades_df.empty:
        print('No trades produced — check yield data coverage or date range.',
              file=sys.stderr)
        sys.exit(1)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps({
        'trade_count':               len(trades_df),
        'mean_pnl':                  round(float(trades_df['pnl'].mean()), 4),
        'eligible_regimes_proposed': result['eligible_regimes_proposed'],
        'regime_partition': {
            r: {k: round(v, 4) if isinstance(v, float) else v
                for k, v in s.items()}
            for r, s in result['regime_partition'].items()
        },
    }, indent=2))
