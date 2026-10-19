"""
CohortBaseStrategy — bridge for 2026-04 Top-10 cohort strategies.

The cohort's author wrote ten strategies against an older FundJohn signal-
generation surface: `generate_signals(self, market_data: dict, opts_map: dict)`,
subclassing a `BaseStrategy` from `base_strategy.py` and pulling a `Signal`
dataclass from `models.signal`. The current engine calls strategies with
`(prices, regime, universe, aux_data)` and expects `src.strategies.base.BaseStrategy`
subclasses emitting `src.strategies.base.Signal`.

Rather than rewrite each cohort strategy's internals, this class bridges the two
surfaces. A cohort strategy subclasses `CohortBaseStrategy`, declares
`regime_filter` (cohort vocabulary), and implements
`_generate_signals_cohort(market_data, opts_map)`. This class handles:

    - `regime_filter`    → `active_in_regimes` (plus synonym expansion inherited
      from BaseStrategy's __init_subclass__, so tags like 'NEUTRAL' just work).
    - `regime` dict      → `market_data['regime'] = {'label': regime_state}`
      (cohort strategies read `regime.get('label')`).
    - `aux_data['macro']` (dict of pd.Series) → `market_data['vix_close'/...]`.
    - `aux_data['vol_indices']` DataFrame (if present) → vix9d/skew/vix6m.
    - Wide `prices` DataFrame → SPY close history, spy_prev_close, spx_close.
    - `aux_data['prices_30m']` → today's RTH (ET) spy_30m_bars, intraday_30m_bars,
      intraday_30m_session, spy_10d_volume_avg (prior sessions only).
    - `aux_data['options'][ticker]` → `opts_map[ticker]` with cohort field
      aliases (iv_spread_atm_oi_weighted, smirk_otmput_atmcall, iv_30d, iv_90d,
      rv20, last_price fallback). `avg_dollar_volume_30d` comes from the
      prices_30m RTH bars (mean daily $-volume over the prior 30 sessions).

Strategies that need fields not yet available (e.g., forward
earnings_calendar, iv_history, SPX options) simply return [] until those feeds
land — cohort strategies already skip-on-missing per their author's own "never
raise" discipline.
"""
from __future__ import annotations

import logging
from abc import abstractmethod
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.strategies.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


def tag_rth_30m(df30: "pd.DataFrame") -> "pd.DataFrame":
    """Tag 30-minute bars with their ET `session` ('YYYY-MM-DD') and `time_et`
    ('HH:MM') and keep only regular-trading-hours rows (09:30 <= ET start <
    16:00). `datetime` is normalised to tz-UTC (naive input == UTC). The stored
    `date` column is ignored: after-hours bars carry the next day's label.
    Single source of truth shared by CohortBaseStrategy._translate_prices_30m
    and the backtester's intraday session simulator. Requires a `datetime`
    column; the input frame is not mutated.
    """
    df = df30.copy()
    dt = pd.to_datetime(df['datetime'], utc=True)  # naive input == UTC
    et = dt.dt.tz_convert('America/New_York')
    df['datetime'] = dt
    df['session'] = et.dt.strftime('%Y-%m-%d')
    df['time_et'] = et.dt.strftime('%H:%M')
    return df[(df['time_et'] >= '09:30') & (df['time_et'] < '16:00')]


class CohortBaseStrategy(BaseStrategy):
    """Base class for cohort strategies using (market_data, opts_map) surface."""

    # Cohort strategies declare this; we project to active_in_regimes below.
    regime_filter: List[str] = []

    def __init_subclass__(cls, **kwargs):
        # Project cohort's regime_filter onto repo's active_in_regimes BEFORE
        # BaseStrategy.__init_subclass__ normalizes (synonyms expand there).
        rf = getattr(cls, 'regime_filter', None)
        if rf and not getattr(cls, 'active_in_regimes', None):
            cls.active_in_regimes = list(rf)
        super().__init_subclass__(**kwargs)

    # ---- Engine surface (called by engine.py) ----
    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        regime = regime or {}
        regime_state = regime.get('state', 'LOW_VOL')

        if not self.should_run(regime_state):
            return []

        aux = aux_data or {}
        try:
            market_data, opts_map = self._build_cohort_dicts(
                prices, regime, universe or [], aux
            )
        except Exception as e:
            logger.warning(f"{self.__class__.__name__}: cohort dict build failed: {e}")
            return []

        try:
            signals = self._generate_signals_cohort(market_data, opts_map)
        except Exception as e:
            logger.warning(f"{self.__class__.__name__}: generate_signals raised: {e}")
            return []

        if not isinstance(signals, list):
            return []

        normalized: List[Signal] = []
        for s in signals:
            if isinstance(s, Signal):
                normalized.append(s)
            else:
                logger.debug(f"{self.__class__.__name__}: dropped non-Signal {type(s)}")
        return normalized[: self.MAX_SIGNALS]

    # ---- Cohort surface (implemented by subclasses) ----
    @abstractmethod
    def _generate_signals_cohort(
        self,
        market_data: Dict[str, Any],
        opts_map: Dict[str, Dict[str, Any]],
    ) -> List[Signal]:
        """Subclasses implement cohort-style signal generation here."""
        raise NotImplementedError

    # ---- Translation ----
    def _build_cohort_dicts(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux: dict,
    ):
        regime_state = regime.get('state', 'LOW_VOL')
        market_data: Dict[str, Any] = {
            'regime': {'label': regime_state, 'state': regime_state},
            'regime_state': regime_state,
            'universe': list(universe or []),
        }

        # ----- Macro series from aux['macro'] (dict of pd.Series) -----
        macro = aux.get('macro') or {}
        for name_src, name_dst_close, name_dst_hist in (
            ('VIX', 'vix_close', 'vix_history'),
            ('VVIX', 'vvix_close', 'vvix_history'),
            ('VIX3M', 'vix3m_close', 'vix3m_history'),
        ):
            ser = macro.get(name_src)
            if ser is not None and hasattr(ser, '__len__') and len(ser):
                try:
                    market_data[name_dst_close] = float(ser.iloc[-1])
                    market_data[name_dst_hist] = [float(x) for x in ser.dropna().tolist()]
                except Exception:
                    pass

        # ----- Extended vol indices (VIX9D / SKEW / VIX6M) from vol_indices parquet -----
        vol_indices = aux.get('vol_indices')
        if vol_indices is not None and hasattr(vol_indices, 'empty') and not vol_indices.empty:
            try:
                last = vol_indices.iloc[-1]
                cols = set(vol_indices.columns)
                for src, dst in (
                    ('vix9d_close', 'vix9d_close'),
                    ('vix9d', 'vix9d_close'),
                    ('skew', 'skew'),
                    ('skew_close', 'skew'),
                    ('vix6m_close', 'vix6m_close'),
                    ('vix6m', 'vix6m_close'),
                ):
                    if src in cols and dst not in market_data:
                        val = last[src]
                        if pd.notna(val):
                            market_data[dst] = float(val)
            except Exception:
                pass
        # Reasonable fallbacks so cohort code's .get() chains don't hit None
        market_data.setdefault('vix9d_close', market_data.get('vix_close'))
        market_data.setdefault('vix6m_close', market_data.get('vix3m_close'))

        # ----- SPY price history (for S_TR02 / S_TR03) -----
        if hasattr(prices, 'columns') and 'SPY' in prices.columns:
            spy_close = prices['SPY'].dropna()
            if len(spy_close):
                market_data['spy_close_history'] = [float(x) for x in spy_close.tolist()]
                market_data['spy_prev_close'] = float(spy_close.iloc[-1])
                market_data['spx_close'] = float(spy_close.iloc[-1])

        # ----- 30-minute bars (ET sessions, RTH only; see _translate_prices_30m) -----
        adv30: Dict[str, float] = {}
        prices_30m = aux.get('prices_30m')
        if prices_30m is not None and hasattr(prices_30m, 'empty') and not prices_30m.empty:
            try:
                adv30 = self._translate_prices_30m(prices_30m, market_data)
            except Exception as e:
                logger.warning(f"30m bars translation skipped: {e}")
                adv30 = {}

        # ----- SPX IV (from a raw engine key if present) -----
        if aux.get('spx_iv_30d') is not None:
            market_data['spx_iv_30d'] = aux['spx_iv_30d']

        # Also surface the raw wide prices in case a cohort strategy wants it
        market_data['prices'] = prices

        # ----- opts_map per ticker with field aliases -----
        opts_map: Dict[str, Dict[str, Any]] = {}
        options_aux = aux.get('options') or {}
        prices_series: Dict[str, pd.Series] = {}
        if hasattr(prices, 'columns'):
            for c in prices.columns:
                try:
                    prices_series[c] = prices[c].dropna()
                except Exception:
                    pass

        for ticker, opts in options_aux.items():
            if not isinstance(opts, dict):
                continue
            ot = dict(opts)

            # Field aliases (cohort naming → engine naming)
            if 'iv_spread' in ot:
                ot.setdefault('iv_spread_atm_oi_weighted', ot['iv_spread'])
            if 'skew_20d' in ot:
                ot.setdefault('smirk_otmput_atmcall', ot['skew_20d'])
            if 'near_iv' in ot:
                ot.setdefault('iv_30d', ot['near_iv'])
                ot.setdefault('atm_iv_30d', ot['near_iv'])
            if 'far_iv' in ot:
                ot.setdefault('iv_90d', ot['far_iv'])
            if 'iv30' in ot:
                ot.setdefault('iv_30d', ot['iv30'])
                ot.setdefault('atm_iv_30d', ot['iv30'])

            # last_price fallback from prices
            if 'last_price' not in ot and ticker in prices_series and len(prices_series[ticker]):
                ot['last_price'] = float(prices_series[ticker].iloc[-1])

            # Annualised realised vol, 20d (rv20) — for stop sizing in S_HV13
            if 'rv20' not in ot and ticker in prices_series:
                ts = prices_series[ticker]
                if len(ts) >= 21:
                    rets = ts.pct_change().dropna().tail(20)
                    if len(rets) >= 5:
                        try:
                            ot['rv20'] = float(rets.std() * (252 ** 0.5))
                        except Exception:
                            pass

            opts_map[ticker] = ot

        # 30-day average dollar volume for liquidity gating (S_TR06), from the
        # prices_30m RTH bars. An options-provided value wins; tickers without
        # an options row still get an entry.
        for tk, adv in adv30.items():
            opts_map.setdefault(tk, {}).setdefault('avg_dollar_volume_30d', adv)

        return market_data, opts_map

    # ---- 30-minute translation ----
    @staticmethod
    def _translate_prices_30m(prices_30m: pd.DataFrame, market_data: Dict[str, Any]) -> Dict[str, float]:
        """Fill the 30-minute keys of `market_data`; return {ticker: adv30}.

        Sessions are ET calendar dates derived from the bar timestamp (the
        stored `date` column is ignored: after-hours bars carry the next day's
        label). Only regular-trading-hours bars (09:30 <= ET start < 16:00) are
        used, so the old extended-hours and the new RTH-only bar conventions
        yield the same 13-bar day. `today` = latest session with an RTH bar.
        """
        adv30: Dict[str, float] = {}
        df30 = prices_30m.copy()
        if 'datetime' not in df30.columns:
            market_data['spy_30m_bars'] = df30.to_dict('records')
            return adv30
        if 'ticker' not in df30.columns:
            df30['ticker'] = 'SPY'

        rth = tag_rth_30m(df30)
        if rth.empty:
            return adv30

        today = rth['session'].max()
        market_data['intraday_30m_session'] = today
        today_rth = rth[rth['session'] == today].sort_values('datetime')
        spy_today = today_rth[today_rth['ticker'] == 'SPY']
        if not spy_today.empty:
            market_data['spy_30m_bars'] = spy_today.to_dict('records')
        market_data['intraday_30m_bars'] = {
            tk: grp.to_dict('records') for tk, grp in today_rth.groupby('ticker')
        }

        prior = rth[rth['session'] < today]
        if prior.empty or 'volume' not in prior.columns:
            return adv30

        # Mean 09:30-bar SPY volume over up to 10 sessions strictly before today.
        opens = prior[(prior['ticker'] == 'SPY') & (prior['time_et'] == '09:30')]
        opens = opens.dropna(subset=['volume']).sort_values('session')
        opens = opens.drop_duplicates('session', keep='last').tail(10)
        if len(opens) >= 5:
            market_data['spy_10d_volume_avg'] = float(opens['volume'].mean())

        # Per-ticker mean daily dollar volume over up to 30 prior sessions.
        px = prior['close']
        if 'vwap' in prior.columns:
            px = prior['vwap'].where(prior['vwap'] > 0, prior['close'])
        dv = (prior['volume'] * px).rename('dv')
        daily = (
            pd.concat([prior[['ticker', 'session']], dv], axis=1)
            .dropna(subset=['dv'])
            .groupby(['ticker', 'session'], as_index=False)['dv'].sum()
            .sort_values(['ticker', 'session'])
        )
        last30 = daily.groupby('ticker').tail(30)
        agg = last30.groupby('ticker')['dv'].agg(['mean', 'count'])
        for tk, row in agg[agg['count'] >= 5].iterrows():
            adv30[tk] = float(row['mean'])
        return adv30
