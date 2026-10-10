"""
VIX Curve, VVIX and Dealer Gamma Predict the Width of the Next US Session,
Not Its Direction — Krüger, T. (2026).
https://kruegeralgorithms.com/en/research/vix-term-structure-vvix-gamma-predict-range-not-direction

Hypothesis: when the VIX term structure is inverted (VIX9D > VIX, or
VIX > VIX3M), VVIX is elevated vs its own trailing band, or dealer gamma
(GEX) is net short, the next session's realized RANGE is systematically
wider — not its direction. Long-volatility (ATM straddle) exposure should
be sized up ahead of range-expansion sessions and scaled down (or flipped
short) when the opposite holds.

Implementation note: SPX/^GSPC carries no option chain in the master
parquet; SPY is the tradable, chain-covered proxy of the same underlying
beta (precedent: S_model_free_iv_forecast, S_svi_vol_surface_relative_value).
VIX/VIX9D/VIX3M/VVIX are read as point-in-time series from
aux_data['macro'] (macro.parquet); dealer gamma (GEX) is read from
aux_data['options']['SPY']['gex'] (options_eod OI x gamma aggregation) and
treated as OPTIONAL — the paper's own 4-filter design degrades gracefully
to the 3 always-available vol-curve filters when the options chain is thin
or absent for a given session (pre-2025-05 coverage gap).

Signal rule (daily, four independent flags):
  flag_vix9d  = VIX9D > VIX                         (near-term inversion)
  flag_vix3m  = VIX   > VIX3M                        (term-structure inversion)
  flag_vvix   = VVIX  > its own trailing 75th pctile (vol-of-vol elevated)
  flag_gamma  = GEX   < 0                            (dealers net short gamma; optional)

  n_avail = number of flags with usable data this session (>= 2 required —
            VIX9D/VIX/VIX3M/VVIX are macro.parquet-resident so n_avail is
            almost always 3 or 4; GEX degrades it to 3 when options_eod is
            missing/thin).
  n_true  = number of those flags that fired.

  n_true >= 2              -> BUY_VOL  (long ATM straddle: range expansion
                                         expected, confirmed by >=2 filters).
  n_true == 0               -> SELL_VOL (short ATM straddle: curve normal,
                                         VVIX calm, dealer long-gamma —
                                         compression expected).
  n_true == 1 (ambiguous)   -> no signal.

Reported metrics: falsification-style test vs a random-market baseline,
held in both the search period (to 2018) and a disjoint holdout
(2019-2026, n=1,949 holdout-days, t 2.6-5.4) across SPX/NQ/Dow/DAX under
the same 4-filter design. Out-of-sample per source metadata.
"""
from __future__ import annotations
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, OptionSpec

__all__ = ['VixGammaRangeWidth']

INSTRUMENT_CLASS = 'option'
STRATEGY_ID = 'S_vix_gamma_range_width'

UNDERLYING        = 'SPY'
VVIX_WINDOW        = 252    # trailing window for VVIX percentile band
VVIX_MIN_PERIODS   = 60
VVIX_PCTILE        = 0.75
MIN_FLAGS_AVAIL    = 2
BUY_VOL_MIN_TRUE   = 2
DTE_TARGET         = 14
ROLL_DTE           = 5
HOLD_DAYS          = 3


class VixGammaRangeWidth(BaseStrategy):
    """VIX term-structure inversion + VVIX elevation + low/negative dealer
    gamma predict next-session RANGE expansion (not direction) -> long ATM
    straddle; the clean inverse (all three/four filters calm) predicts
    compression -> short ATM straddle (Krüger 2026)."""

    id                = STRATEGY_ID
    name              = 'VIX Curve / VVIX / Dealer Gamma Range Width'
    description       = ('VIX9D>VIX, VIX>VIX3M, elevated VVIX, or short dealer gamma (>=2 of the '
                          'available flags) predicts next-session range expansion -> BUY_VOL; all '
                          'flags calm -> SELL_VOL.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 252
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'vvix_window':      VVIX_WINDOW,
            'vvix_pctile':      VVIX_PCTILE,
            'min_flags_avail':  MIN_FLAGS_AVAIL,
            'buy_vol_min_true': BUY_VOL_MIN_TRUE,
            'base_size_pct':    0.015,
            'max_size_pct':     0.04,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        signals: List[Signal] = []
        if prices is None or prices.empty or UNDERLYING not in prices.columns:
            print('[debug] signals=0 (no SPY column)', file=sys.stderr)
            return signals

        regime_state = regime.get('state', 'LOW_VOL') if isinstance(regime, dict) else 'LOW_VOL'
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime gate)', file=sys.stderr)
            return signals

        series = prices[UNDERLYING].dropna()
        if len(series) < self.min_lookback:
            print(f'[debug] signals=0 (insufficient history: {len(series)})', file=sys.stderr)
            return signals

        current_price = float(series.iloc[-1])
        if not (current_price == current_price and current_price > 0):
            print('[debug] signals=0 (bad last price)', file=sys.stderr)
            return signals

        p = self.parameters
        vvix_window     = int(p.get('vvix_window', VVIX_WINDOW))
        vvix_pctile     = float(p.get('vvix_pctile', VVIX_PCTILE))
        min_flags_avail = int(p.get('min_flags_avail', MIN_FLAGS_AVAIL))
        buy_vol_min_true = int(p.get('buy_vol_min_true', BUY_VOL_MIN_TRUE))

        macro = (aux_data or {}).get('macro') or {}
        vix   = self._get_macro(macro, 'VIX',   series.index)
        vix9d = self._get_macro(macro, 'VIX9D', series.index)
        vix3m = self._get_macro(macro, 'VIX3M', series.index)
        vvix  = self._get_macro(macro, 'VVIX',  series.index)

        flags: dict = {}

        if vix9d is not None and vix is not None:
            flags['vix9d_gt_vix'] = bool(float(vix9d.iloc[-1]) > float(vix.iloc[-1]))

        if vix is not None and vix3m is not None:
            flags['vix_gt_vix3m'] = bool(float(vix.iloc[-1]) > float(vix3m.iloc[-1]))

        if vvix is not None and len(vvix.dropna()) >= VVIX_MIN_PERIODS:
            vvix_band = vvix.rolling(vvix_window, min_periods=VVIX_MIN_PERIODS).quantile(vvix_pctile)
            band_val = vvix_band.iloc[-1]
            if pd.notna(band_val):
                flags['vvix_elevated'] = bool(float(vvix.iloc[-1]) > float(band_val))

        opts_map = (aux_data or {}).get('options') or {}
        opts = opts_map.get(UNDERLYING) or {}
        gex = opts.get('gex')
        if gex is not None:
            try:
                flags['dealer_gamma_short'] = bool(float(gex) < 0.0)
            except (TypeError, ValueError):
                pass

        n_avail = len(flags)
        n_true = sum(1 for v in flags.values() if v)
        if n_avail < min_flags_avail:
            print(f'[debug] signals=0 (n_avail={n_avail} < {min_flags_avail})', file=sys.stderr)
            return signals

        if n_true >= buy_vol_min_true:
            direction = 'BUY_VOL'
        elif n_true == 0:
            direction = 'SELL_VOL'
        else:
            print(f'[debug] signals=0 (ambiguous: n_true={n_true}/{n_avail})', file=sys.stderr)
            return signals

        ratio = n_true / n_avail if direction == 'BUY_VOL' else (n_avail - n_true) / n_avail
        confidence = 'HIGH' if ratio >= 0.75 else ('MED' if ratio >= 0.5 else 'LOW')

        scale = self.position_scale(regime_state)
        base_size = float(p.get('base_size_pct', 0.015))
        max_size = float(p.get('max_size_pct', 0.04))
        size = min(base_size * scale * (1.0 + ratio), max_size)

        stops = self.compute_stops_and_targets(
            series, 'SHORT' if direction == 'SELL_VOL' else 'LONG',
            current_price, atr_multiplier=2.0, regime_state=regime_state,
        )

        option_spec = OptionSpec(
            underlying    = UNDERLYING,
            right         = 'call',
            strike_rule   = 'atm',
            dte_target    = DTE_TARGET,
            structure     = 'straddle',
            hedge         = 'delta',
            hedge_cadence = 'daily',
            roll_dte      = ROLL_DTE,
        )

        signals.append(Signal(
            ticker            = UNDERLYING,
            direction         = direction,
            entry_price       = round(current_price, 4),
            stop_loss         = stops['stop'],
            target_1          = stops['t1'],
            target_2          = stops['t2'],
            target_3          = stops['t3'],
            position_size_pct = round(size, 4),
            confidence        = confidence,
            signal_params     = {
                'flags':       {k: bool(v) for k, v in flags.items()},
                'n_true':      n_true,
                'n_avail':     n_avail,
                'hold_days':   HOLD_DAYS,
            },
            option_spec       = option_spec,
        ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _get_macro(macro: dict, series_name: str, date_index: pd.DatetimeIndex) -> 'pd.Series | None':
        """Extract a named series from macro dict (dict[str, pd.Series]) and align
        to date_index via forward-fill. Returns None if series is absent/empty."""
        if not isinstance(macro, dict):
            return None
        s = macro.get(series_name)
        if not isinstance(s, pd.Series) or s.empty:
            return None
        s = s.dropna().sort_index()
        if s.empty:
            return None
        aligned = s.reindex(date_index, method='ffill')
        if pd.isna(aligned.iloc[-1]):
            return None
        return aligned


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json
    from backtest.unified_backtest import load_prices_panels, load_regimes
    from strategies.aux_data_loader import load_aux_data

    prices_df, _bars = load_prices_panels(tickers=[UNDERLYING])
    reg_series = load_regimes()

    strat = VixGammaRangeWidth()
    spy = prices_df[UNDERLYING].dropna()
    rows = []
    idx = spy.index
    start = max(strat.min_lookback, 252)
    for i in range(start, len(idx) - HOLD_DAYS):
        d  = idx[i]
        xd = idx[i + HOLD_DAYS]

        prices_upto = prices_df.iloc[: i + 1]
        aux = load_aux_data(d.strftime('%Y-%m-%d'))

        prior_regimes = reg_series[reg_series.index <= d]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'
        regime_dict = {'state': rstate}

        sigs = strat.generate_signals(prices_upto, regime_dict, [UNDERLYING], aux_data=aux)
        if not sigs:
            continue
        sig = sigs[0]

        c0 = float(spy.loc[d])
        cx = float(spy.loc[xd])
        if c0 <= 0:
            continue

        # Proxy straddle P&L: realized |move| over the hold window vs the
        # prior 20d trailing realized daily vol (expected move). BUY_VOL
        # profits when realized exceeds expected; SELL_VOL profits when it
        # falls short. No strike/theta model — directional-magnitude proxy
        # only, consistent with the sibling vol-timing strategies' backtests.
        hist = spy.loc[:d].pct_change().dropna().iloc[-20:]
        if len(hist) < 20:
            continue
        expected_move = float(hist.std())
        realized_move = abs(cx - c0) / c0
        edge = realized_move - expected_move
        pnl = edge if sig.direction == 'BUY_VOL' else -edge

        rows.append({
            'strategy_id': STRATEGY_ID, 'signal_date': d, 'regime_state': rstate,
            'pnl': pnl, 'r_multiple': round(pnl / 0.02, 4),
        })

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
