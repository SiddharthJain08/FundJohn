from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
try:
    from lib.price_panel import is_equity_ticker as _is_equity_ticker
except Exception:  # pragma: no cover — lib not importable ⇒ treat every column as equity
    def _is_equity_ticker(t):
        return True

__all__ = ['SparseCCAMeanRevert']


class SparseCCAMeanRevert(BaseStrategy):
    """Sparse portfolios formed by maximizing mean reversion via sparse CCA; trade long/short on spread z-score."""

    id          = 'S_sparse_cca_mean_revert'
    name        = 'SparseCCAMeanRevert'
    description = 'Sparse portfolios formed by maximizing mean reversion via sparse CCA exhibit negative autocorrelation; trade long/short on spread z-score.'
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    LOOKBACK    = 252
    LAG         = 5
    K_ASSETS    = 10
    Z_ENTRY     = 1.5
    Z_EXIT      = 0.25
    SIZE_PER    = 0.03   # 3% per leg asset × 10 = 30% max total
    MAX_MISSING_ROWS = 5  # completeness tolerance per leg over the price window (see below)

    def generate_signals(self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []
        scale = self.position_scale(regime_state)

        # --- data prep ---
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        min_rows = self.LOOKBACK + self.LAG + 20
        # Rows are EQUITY sessions: keep a row only if some equity column has a
        # print, so weekend rows contributed by 7-day tickers (…-USD) never make
        # every equity column look incomplete and the window is min_rows+10
        # equity sessions. The engine applies the same calendar upstream under
        # OPENCLAW_EQUITY_TRADING_CALENDAR=1 (no-op here in that case); this
        # makes the strategy correct without relying on it (review 2026-09-25).
        px_all = prices[tickers]
        _eq_cols = [c for c in px_all.columns if _is_equity_ticker(c)]
        px_all = (px_all.loc[px_all[_eq_cols].notna().any(axis=1)] if _eq_cols
                  else px_all.dropna(how='all'))
        px = px_all.dropna(axis=1, thresh=min_rows).tail(min_rows + 10)
        if px.shape[1] < 10 or len(px) < self.LOOKBACK + self.LAG:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Row-wise ANY-nan drop (`.dropna()`) empties the frame across a wide,
        # sparsely-populated live universe (~5.8k tickers): one NaN anywhere
        # in a row removes the whole row. Drop only rows that are entirely
        # NaN (a genuinely missing session) — per-ticker gaps are handled
        # below (autocorr loop's `s.dropna()` + `< 60` floor, the
        # complete-price-history filter on `sparse_tickers`, and the
        # min_count-guarded spread sum further down).
        rets = px.pct_change().iloc[1:].dropna(how='all')
        if len(rets) < self.LOOKBACK:
            print('[debug] signals=0', file=sys.stderr)
            return []

        r = rets.tail(self.LOOKBACK)

        # --- sparse selection via per-asset lag autocorrelation (faithful approx of sparse CCA) ---
        autocorrs: dict[str, float] = {}
        for col in r.columns:
            s = r[col].dropna()
            if len(s) < 60:
                continue
            try:
                ac = float(s.autocorr(lag=self.LAG))
                if not np.isnan(ac):
                    autocorrs[col] = ac
            except Exception:
                continue

        if len(autocorrs) < 10:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Select K_ASSETS most negatively autocorrelated (most mean-reverting),
        # restricted to legs that pass the completeness rule below (first and
        # last row valid, at most MAX_MISSING_ROWS interior misses).
        px_live = px.dropna(how='all')   # rows already on the equity calendar (see above)
        # Completeness is a TOLERANCE, not zero-NaN (review 2026-09-25): a
        # data-layer hole such as the 2026-09-15/16 collect miss (~5k tickers
        # with no row for two sessions) would otherwise exclude nearly the whole
        # universe from selection for as long as those dates sit inside the
        # window (~14 months) — a silent, non-random selection bias. A leg
        # qualifies when its FIRST row (the spread's normalisation base) and
        # its LAST row (today's tradeable price) are valid and it has at most
        # MAX_MISSING_ROWS interior misses; a missing day becomes a NaN spread
        # day via min_count below and is skipped by the rolling mean/std.
        # Never forward-fill: stale prints would flatten the spread and
        # understate roll_std.
        sorted_tickers = sorted(autocorrs, key=lambda t: autocorrs[t])
        complete_tickers = [t for t in sorted_tickers
                            if pd.notna(px_live[t].iloc[0]) and pd.notna(px_live[t].iloc[-1])
                            and int(px_live[t].isna().sum()) <= self.MAX_MISSING_ROWS]
        if len(complete_tickers) < 10:
            print('[debug] signals=0', file=sys.stderr)
            return []
        sparse_tickers = complete_tickers[:self.K_ASSETS]

        # Rank-based weights: proportional to -autocorr, normalized by abs-sum
        raw = np.array([-autocorrs[t] for t in sparse_tickers])
        abs_sum = float(np.abs(raw).sum())
        if abs_sum < 1e-8:
            print('[debug] signals=0', file=sys.stderr)
            return []
        weights = {t: float(raw[i] / abs_sum) for i, t in enumerate(sparse_tickers)}

        # --- compute portfolio spread z-score ---
        px_sparse = px_live[sparse_tickers].copy()
        # Normalize each price to base 1 at start of window
        base = px_sparse.iloc[0].replace(0, np.nan)   # first row is valid by selection above
        px_norm = px_sparse.div(base)
        w_series = pd.Series(weights)
        # Defense in depth: the complete-tickers selection above should make
        # every cell here non-NaN by construction, but DataFrame.sum(axis=1)
        # defaults to skipna=True, which would silently treat any NaN cell
        # that slips through as a zero contribution instead of propagating
        # NaN — silently understating (or, if every leg were missing that
        # day, zeroing out) the portfolio spread rather than flagging the
        # day as unusable. min_count=K forces any such row to be NaN
        # instead of a fabricated partial (or zero) sum; roll.mean()/
        # roll.std() then skip NaN days (pandas default skipna=True)
        # rather than being poisoned by a fabricated value, and the
        # explicit isnan guards below stop a NaN from ever reaching the
        # Z_ENTRY compare silently (NaN comparisons are always False in
        # Python, so an unguarded NaN z would fall through the elif/else
        # exactly like a real no-signal day).
        spread = px_norm.mul(w_series).sum(axis=1, min_count=len(sparse_tickers))

        if len(spread) < self.LOOKBACK:
            print('[debug] signals=0', file=sys.stderr)
            return []

        roll = spread.tail(self.LOOKBACK)
        roll_mean = float(roll.mean())
        roll_std  = float(roll.std())
        if np.isnan(roll_mean) or np.isnan(roll_std) or roll_std < 1e-8:
            print('[debug] signals=0', file=sys.stderr)
            return []

        last_spread = float(spread.iloc[-1])
        if np.isnan(last_spread):
            print('[debug] signals=0', file=sys.stderr)
            return []
        z = float((last_spread - roll_mean) / roll_std)

        # --- threshold check ---
        if z < -self.Z_ENTRY:
            portfolio_dir = 'LONG'
        elif z > self.Z_ENTRY:
            portfolio_dir = 'SHORT'
        else:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        confidence = 'HIGH' if abs(z) > 2.0 else 'MED'
        today_px = px.iloc[-1]
        size = round(scale * self.SIZE_PER, 4)
        signals: List[Signal] = []

        for ticker in sparse_tickers:
            w = weights.get(ticker, 0.0)
            if abs(w) < 0.005:
                continue
            current_price = float(today_px.get(ticker, np.nan))
            # Review 2026-09-25: a trailing all-NaN panel row survives the
            # completeness check (it is dropped from px_live) but `today_px`
            # reads it — float('nan') <= 0 is False, so NaN entries leaked.
            if not np.isfinite(current_price) or current_price <= 0:
                continue

            # LONG portfolio: buy positive-weight assets, sell negative-weight
            if portfolio_dir == 'LONG':
                direction = 'LONG' if w > 0 else 'SHORT'
            else:
                direction = 'SHORT' if w > 0 else 'LONG'

            stops = self.compute_stops_and_targets(
                px[ticker].dropna(), direction, current_price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=current_price,
                stop_loss=float(stops['stop']),
                target_1=float(stops['t1']),
                target_2=float(stops['t2']),
                target_3=float(stops['t3']),
                position_size_pct=size,
                confidence=confidence,
                signal_params={
                    'z_score':  round(z, 3),
                    'autocorr': round(autocorrs.get(ticker, 0.0), 4),
                    'weight':   round(w, 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
