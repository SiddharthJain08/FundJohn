# S_ast_earnings_announcement_premium — Earnings Announcement Premium
# Source: https://quantpedia.com/strategies/earnings-announcement-premium/
# Long high-VCR expected announcers / short high-VCR non-announcers, monthly rebalance.
# Clean-room port; reference implementation used for rule + parameters only.
from __future__ import annotations
import sys
import pandas as pd
from pathlib import Path
from typing import List
from strategies.base import BaseStrategy, Signal
from src.strategies.universe_default import no_otc as universe_filter

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_ast_earnings_announcement_premium'

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_N_MONTHS    = 48    # ideal lookback window (months); adaptive to available data
_N_MONTHS_MIN = 6   # minimum months required to compute VCR
_N_ANN_MAX   = 16   # cap on announcement-month volumes used in numerator
_MAX_PER_LEG = 15   # cap per long/short leg

_PRICES_VOL: pd.DataFrame | None = None
_EARN_DF: pd.DataFrame | None = None


def _load_vol() -> pd.DataFrame:
    global _PRICES_VOL
    if _PRICES_VOL is not None:
        return _PRICES_VOL
    p = _ROOT / 'data' / 'master' / 'prices.parquet'
    if not p.exists():
        _PRICES_VOL = pd.DataFrame()
        return _PRICES_VOL
    cutoff = (pd.Timestamp.today() - pd.DateOffset(months=_N_MONTHS + 3)).strftime('%Y-%m-%d')
    df = pd.read_parquet(p, columns=['ticker', 'date', 'volume'],
                         filters=[('date', '>=', cutoff)])
    df['date'] = pd.to_datetime(df['date'])
    _PRICES_VOL = df
    return _PRICES_VOL


def _load_earn() -> pd.DataFrame:
    global _EARN_DF
    if _EARN_DF is not None:
        return _EARN_DF
    p = _ROOT / 'data' / 'master' / 'earnings.parquet'
    if not p.exists():
        _EARN_DF = pd.DataFrame(columns=['ticker', 'date'])
        return _EARN_DF
    df = pd.read_parquet(p, columns=['ticker', 'date'])
    df['date'] = pd.to_datetime(df['date'])
    _EARN_DF = df.dropna(subset=['date'])
    return _EARN_DF


class EarningsAnnouncementPremium(BaseStrategy):
    """Long high-VCR expected announcers / short high-VCR non-announcers (monthly).

    Clean-room port from QuantConnect blueprint.
    Source: https://quantpedia.com/strategies/earnings-announcement-premium/
    """

    id               = STRATEGY_ID
    name             = 'EarningsAnnouncementPremium'
    description      = ('Long expected earnings announcers vs short non-announcers '
                        'in the top volume-concentration-ratio quintile, monthly rebalance.')
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 126   # ~6 months minimum; VCR window adaptive
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

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

        idx   = prices.index
        today = idx[-1]

        # Monthly trigger: act only on first available trading bar of the month
        month_bars = idx[(idx.year == today.year) & (idx.month == today.month)]
        if len(month_bars) == 0 or today != month_bars[0]:
            return []

        vol_df  = _load_vol()
        earn_df = _load_earn()
        if vol_df.empty or earn_df.empty:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        cutoff   = today - pd.DateOffset(months=_N_MONTHS)
        univ_set = set(universe) & set(prices.columns)

        # --- Build monthly volumes over 48-month window ---
        vol_win = vol_df[
            (vol_df['date'] >= cutoff) & (vol_df['date'] <= today) &
            (vol_df['ticker'].isin(univ_set))
        ].copy()
        if vol_win.empty:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        vol_win['ym'] = vol_win['date'].dt.to_period('M')
        monthly = (vol_win.groupby(['ticker', 'ym'])['volume']
                         .sum().reset_index()
                         .sort_values(['ticker', 'ym']))

        # --- Mark announcement months (point-in-time) ---
        earn_pit = earn_df[earn_df['date'] <= today].copy()
        earn_pit['ym'] = earn_pit['date'].dt.to_period('M')
        ann_keys = earn_pit[['ticker', 'ym']].drop_duplicates()
        ann_keys = ann_keys[ann_keys['ticker'].isin(univ_set)].copy()
        ann_keys['is_ann'] = True

        monthly = monthly.merge(ann_keys, on=['ticker', 'ym'], how='left')
        monthly['is_ann'] = monthly['is_ann'].fillna(False)

        # --- Compute VCR per ticker ---
        vcr_map: dict[str, float] = {}
        for ticker, grp in monthly.groupby('ticker'):
            if len(grp) < _N_MONTHS_MIN:
                continue   # need at least 6 months of data
            total = float(grp['volume'].sum())
            if total <= 0:
                continue
            ann_vols = grp.loc[grp['is_ann'], 'volume']
            if ann_vols.empty:
                continue   # no announcement months — skip
            last_n = ann_vols.iloc[-min(_N_ANN_MAX, len(ann_vols)):]
            vcr_map[ticker] = float(last_n.sum()) / total

        if len(vcr_map) < 5:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # --- Top quintile by VCR ---
        sorted_t = sorted(vcr_map, key=vcr_map.__getitem__, reverse=True)
        q_n  = max(len(sorted_t) // 5, 1)
        top_q = sorted_t[:q_n]

        # --- Classify: expected announcer = announced same month last year ---
        earn_set: dict[str, set] = (
            earn_pit.groupby('ticker')
            .apply(lambda g: set(zip(g['date'].dt.year, g['date'].dt.month)))
            .to_dict()
        )
        prev_y, cur_m = today.year - 1, today.month
        longs  = [t for t in top_q if (prev_y, cur_m) in earn_set.get(t, set())]
        shorts = [t for t in top_q if t not in longs]

        # --- Value-weighted signals (market cap from financials, equal-weight fallback) ---
        fin = (aux_data or {}).get('financials', {})

        def _mcap(t: str) -> float:
            f  = fin.get(t, {})
            mc = f.get('market_cap') or f.get('marketCap')
            return float(mc) if mc and float(mc) > 0 else 1.0

        def _make_leg(leg: list, direction: str, conf: str) -> List[Signal]:
            if not leg:
                return []
            leg   = leg[:_MAX_PER_LEG]
            caps  = [_mcap(t) for t in leg]
            total_cap = sum(caps) or 1.0
            scale = self.position_scale(regime_state)
            sigs: List[Signal] = []
            for t, cap in zip(leg, caps):
                if t not in prices.columns:
                    continue
                ts = prices[t].dropna()
                if ts.empty:
                    continue
                price = float(ts.iloc[-1])
                w   = float(cap) / total_cap
                pct = round(max(0.005, min(w * scale * 0.5, 0.10)), 4)
                st  = self.compute_stops_and_targets(
                    ts, direction, price, regime_state=regime_state)
                sigs.append(Signal(
                    ticker=t, direction=direction, entry_price=price,
                    stop_loss=st['stop'], target_1=st['t1'],
                    target_2=st['t2'], target_3=st['t3'],
                    position_size_pct=pct, confidence=conf,
                    signal_params={
                        'vcr': round(vcr_map.get(t, 0.0), 4),
                        'strategy': STRATEGY_ID,
                    },
                ))
            return sigs

        signals = _make_leg(longs, 'LONG', 'HIGH') + _make_leg(shorts, 'SHORT', 'MED')
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
