"""
First-Candle EMA(12) Breakout.

Source: Krüger, T. (2026). "+982% From the First Candle? Rebuilt, It Is
Leverage Times a Bull Market, With No Detectable Lead Over Always-Long."
https://kruegeralgorithms.com/en/research/first-candle-ema-backtest-leverage-bull-market

Unambiguous rule from the abstract: wait for the first candle of the session
to close, compare that close to an EMA(12); go LONG if the close is above
the EMA, SHORT if below. The authors rebuild this viral +982% rule across
Nasdaq/S&P 500/Dow/DAX (2015-2018 and 2019-2026) and conclude the headline
return is fully explained by implied leverage plus bull-market drift — the
rule shows no statistically detectable lead over an always-long baseline.
This is a FALSIFICATION study; implemented anyway so our own promotion gate
can confirm/deny on our own data.

Interpretation choices (variant 1 of 2 — the abstract is explicit about the
trigger direction and the EMA(12) period but leaves the candle granularity,
the comparison price, and the hold horizon open, since our ledger carries
no 5-minute intraday bars, only daily OHLC):

  * Candle proxy: the "first candle's close" becomes TODAY'S OPEN — the
    earliest tradable print of the session — compared against EMA(12) of
    PRIOR daily closes (strictly before today, no look-ahead). A colleague
    building variant 2 would more likely default to the more obvious daily
    translation: today's CLOSE vs EMA(12) of closes including today (a
    same-day trend-confirmation read, decided only once the session is
    already over). Variant 1 instead preserves the original rule's causal
    structure — the decision is made at the first print, before the day's
    outcome is known — which is also what makes the "implied leverage"
    framing work: a round-trip decision taken at the open, not a lagging
    trend filter read off the close.
  * Universe: broad equity (default sp500), ranked by breakout magnitude
    and capped at MAX_SIGNALS per day — not the four cash indices in the
    paper (no index futures/cash-index data in the ledger), since the
    mechanism (first print vs a short EMA) is asset-agnostic and the paper
    itself is testing whether the rule has ANY edge, not an index-specific
    one.
  * Stops/targets: ATR(14) computed on closes strictly BEFORE today (same
    no-look-ahead boundary as the EMA), anchored off today's open as the
    entry price — standard BaseStrategy bracket geometry, not hand-rolled.
  * Direction: always LONG or always SHORT, never FLAT — matching the
    paper's own framing that this rule is "always in the market," which is
    precisely the leverage/drift mechanism being falsified.

Reported metrics: backtest periods 2015-2018 and 2019-2026 (to 2026-06-05),
out-of-sample. No sharpe/drawdown figures given in the abstract — the
headline claim is the qualitative one (no detectable lead over always-long).
Overfitting flags: data_snooping_bias.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['FirstCandleEma12Breakout']

INSTRUMENT_CLASS = 'equity'

STRATEGY_ID       = 'S_first_candle_ema12_breakout'
EMA_SPAN          = 12
MIN_HIST_BARS     = EMA_SPAN + 20     # warm-up needed for EMA(12) + ATR(14)
BASE_POSITION_CAP = 0.03              # per-name cap after scale/n split
MAX_SIGNALS_KEEP  = 30

_PRICES_PATH = Path(__file__).parents[3] / 'data' / 'master' / 'prices.parquet'


@lru_cache(maxsize=8)
def _load_open_close_cached(tickers: Tuple[str, ...]) -> dict:
    """Load {ticker: DataFrame(date, open, close)} filtered to `tickers`.

    Mirrors the predicate-pushdown pattern used elsewhere in this codebase
    (see S_vp_macd_index_sensitivity.py) — the standard `prices` argument
    passed to generate_signals is a wide CLOSE-only panel, but this rule
    needs today's OPEN, so we read it straight from the master parquet,
    column-pruned and ticker-filtered at the row-group level.
    """
    if not _PRICES_PATH.exists():
        return {}
    try:
        df = pd.read_parquet(
            _PRICES_PATH,
            columns=['ticker', 'date', 'open', 'close'],
            filters=[('ticker', 'in', list(tickers))],
        )
        df['date'] = pd.to_datetime(df['date'])
        df.sort_values('date', inplace=True)
        result = {}
        for tkr, grp in df.groupby('ticker'):
            result[tkr] = grp.set_index('date')[['open', 'close']]
        return result
    except Exception as exc:
        print(f'[{STRATEGY_ID}] OHLC load failed: {exc}', file=sys.stderr)
        return {}


def _load_open_close(tickers: List[str]) -> dict:
    return _load_open_close_cached(tuple(sorted(set(tickers))))


class FirstCandleEma12Breakout(BaseStrategy):
    """Enter at today's open LONG if above EMA(12) of prior closes, SHORT if
    below — a daily-bar adaptation of the viral "first candle" breakout rule
    the source paper falsifies as leverage + bull-market drift.
    """

    id                = STRATEGY_ID
    name              = 'First-Candle EMA(12) Breakout'
    description       = (
        'Daily-bar adaptation: LONG if today\'s open is above EMA(12) of '
        'prior closes, SHORT if below. Source paper finds no detectable '
        'edge over always-long once leverage and bull-market drift are '
        'accounted for.'
    )
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = MAX_SIGNALS_KEEP

    def default_parameters(self) -> dict:
        return {'ema_span': EMA_SPAN}

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

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale   = self.position_scale(regime_state)
        ema_sp  = int(self.parameters.get('ema_span', EMA_SPAN))
        as_of   = prices.index[-1]

        candidate_tickers = [t for t in universe if t in prices.columns]
        if len(candidate_tickers) < 3:
            print('[debug] signals=0', file=sys.stderr)
            return []

        oc_map = _load_open_close(candidate_tickers)
        if not oc_map:
            print('[debug] signals=0 (OHLC unavailable)', file=sys.stderr)
            return []

        ranked: List[dict] = []
        for ticker in candidate_tickers:
            oc = oc_map.get(ticker)
            if oc is None:
                continue
            oc = oc[oc.index <= as_of].dropna()
            if len(oc) < MIN_HIST_BARS:
                continue

            open_today = float(oc['open'].iloc[-1])
            if open_today <= 0:
                continue

            hist_close = oc['close'].iloc[:-1]   # strictly before today — no look-ahead
            if len(hist_close) < ema_sp + 5:
                continue

            ema_prior = float(hist_close.ewm(span=ema_sp, adjust=False).mean().iloc[-1])
            if not np.isfinite(ema_prior) or ema_prior <= 0:
                continue

            gap_pct = (open_today - ema_prior) / ema_prior
            if gap_pct == 0:
                continue

            ranked.append({
                'ticker':      ticker,
                'direction':   'LONG' if gap_pct > 0 else 'SHORT',
                'gap_pct':     gap_pct,
                'open':        open_today,
                'hist_close':  hist_close,
            })

        if not ranked:
            print('[debug] signals=0', file=sys.stderr)
            return []

        ranked.sort(key=lambda r: abs(r['gap_pct']), reverse=True)
        keep = ranked[:self.MAX_SIGNALS]
        n    = len(keep)
        per_name = min(round(scale / max(n, 1), 4), BASE_POSITION_CAP)

        signals: List[Signal] = []
        for i, rec in enumerate(keep):
            stops = self.compute_stops_and_targets(
                rec['hist_close'], rec['direction'], rec['open'], regime_state=regime_state,
            )
            rank_frac = i / max(n, 1)
            if rank_frac < 0.15:
                confidence = 'HIGH'
            elif rank_frac < 0.5:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker            = rec['ticker'],
                direction         = rec['direction'],
                entry_price       = float(round(rec['open'], 4)),
                stop_loss         = float(stops['stop']),
                target_1          = float(stops['t1']),
                target_2          = float(stops['t2']),
                target_3          = float(stops['t3']),
                position_size_pct = per_name,
                confidence        = confidence,
                signal_params     = {
                    'ema_span':  ema_sp,
                    'gap_pct':   round(rec['gap_pct'], 6),
                    'regime':    regime_state,
                    'source':    'kruegeralgorithms.com:first-candle-ema-backtest-leverage-bull-market',
                },
                features          = {'open_vs_ema12_gap_pct': round(rec['gap_pct'], 6)},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json
    from backtest.unified_backtest import load_prices_panels, load_regimes

    _, bars_by_ticker = load_prices_panels()
    reg_series = load_regimes()

    rows = []
    for ticker, bars in bars_by_ticker.items():
        bars = bars.sort_index().dropna(subset=['open', 'close'])
        if len(bars) < max(MIN_HIST_BARS, 252) + 5:
            continue

        close = bars['close']
        open_ = bars['open']
        ema_prior_series = close.shift(1).ewm(span=EMA_SPAN, adjust=False).mean()
        idx = bars.index
        start = max(MIN_HIST_BARS, 252)

        for i in range(start, len(idx)):
            d        = idx[i]
            o        = float(open_.iloc[i])
            ema_v    = float(ema_prior_series.iloc[i])
            c_same   = float(close.iloc[i])
            if not np.isfinite(o) or o <= 0 or not np.isfinite(ema_v) or ema_v <= 0:
                continue

            gap = (o - ema_v) / ema_v
            if gap == 0:
                continue
            direction = 1 if gap > 0 else -1
            pnl = direction * (c_same - o) / o

            prior_regimes = reg_series[reg_series.index <= d]
            rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'

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
