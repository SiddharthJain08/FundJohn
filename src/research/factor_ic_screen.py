#!/usr/bin/env python3
"""factor_ic_screen.py — rank-IC / quantile / turnover decay screen (spec D1).

Sits between the cheap factor prescreen and the ~900 s unified backtest in the
research gate chain. Where factor_prescreen answers "does this strategy emit
anything at all?", this answers "does what it emits rank the cross-section in a
way that survives cost?" — and skips the backtest when the answer is provably
no.

WHAT IT COMPUTES, per rebalance date:
  - Spearman rank IC at H = 5 / 10 / 21 sessions (factor[t] vs forward return
    over [t, t+H]), on NON-OVERLAPPING dates spaced max(step, H) sessions apart
    (overlapping windows correlate the IC series and inflate ICIR).
  - ICIR = mean(IC) / std(IC) * sqrt(252 / H).
  - First- vs second-half mean IC at H=5 (decay).
  - Rank autocorrelation of the factor itself between consecutive rebalances.
  - Quintile mean forward returns, the Q5 - Q1 long-short mean, monotonicity.
  - Jaccard turnover of the extreme-quintile membership, and the per-rebalance
    round-trip cost that turnover implies.

VERDICTS
  flat    |IC_5| < 0.01 AND |ls_q5q1| < turnover x round-trip cost.
          The factor neither ranks nor pays for its own trading. Under
          OPENCLAW_IC_SCREEN=1 this is the one verdict that skips the backtest.
  weak    |ICIR_5| < 0.30 — real but unstable. Annotate, still backtest.
  skipped Not enough cross-section to judge (see below). Annotate, still
          backtest.
  pass    Everything else.

WHY `skipped` EXISTS (not in the spec's three-verdict list; added 2026-09-12
after review). A long-only decile strategy — low_volatility_us and the ~40 other
`nsmallest`/`nlargest` decile implementations — expresses itself as 10-50
non-NaN cells drawn from two or three confidence levels. Rank IC on that is
tie-dominated and pd.qcut cannot form five distinct quantiles, so |IC_5| and
|ls_q5q1| both collapse toward zero and EVERY decile strategy would score
`flat` and lose its backtest. That is the same false-block class
factor_prescreen's `zero_signals_on_fallback_universe` soft pass exists to
prevent. A rebalance only counts toward the verdict when it carries at least
MIN_CROSS_SECTION non-NaN values AND MIN_DISTINCT_VALUES distinct ones; fewer
than MIN_REBALANCES such rebalances => `skipped`, never `flat`.

MEMORY: prices are read ONLY through factor_prescreen.load_price_window (the
two-pass, row-group-stats, ticker-pushdown reader) sliced to ~2 years. This box
is 2-core / 8 GB / no swap.

CLI (one JSON line on stdout, exit 0 whenever the screen COMPLETES; exit 1 on
any infra problem, which the orchestrator treats as ic_screen_infra_fail and
passes through):
    python3 -m research.factor_ic_screen --strategy-file <path> \
        [--sessions 504] [--max-tickers 300] [--step 5]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

HORIZONS            = (5, 10, 21)
REBALANCE_STEP      = 5
LOOKBACK_SESSIONS   = 504          # ~2 years, per spec
DEFAULT_MAX_TICKERS = 300

# Mirror of backtest.unified_backtest.INSTRUMENT_COST_BPS['equity'] (:88) — the
# ONE-WAY half-spread in bp. Duplicated rather than imported so this screen
# never drags unified_backtest's parquet loaders into the process.
# Keep in sync.
ONE_WAY_COST_BPS = 10.0

# Cross-section quality floors — see the `skipped` note above.
# MIN_CROSS_SECTION mirrors backtest.quick_backtest.MIN_CROSS_SECTION (:60).
MIN_CROSS_SECTION   = 20
MIN_DISTINCT_VALUES = 5
MIN_REBALANCES      = 12

FLAT_IC_ABS   = 0.01
WEAK_ICIR_ABS = 0.30

CONFIDENCE_WEIGHT = {'HIGH': 1.0, 'MED': 0.6, 'LOW': 0.3}
DIRECTION_SIGN    = {'LONG': 1.0, 'BUY_VOL': 1.0,
                     'SHORT': -1.0, 'SELL_VOL': -1.0, 'FLAT': 0.0}


def round_trip_cost(one_way_bps: float = ONE_WAY_COST_BPS) -> float:
    """Round-trip (in + out) cost as a return fraction."""
    return 2.0 * float(one_way_bps) / 10_000.0


def _spearman(a: pd.Series, b: pd.Series) -> Optional[float]:
    """Spearman rank correlation over the common non-NaN index. None when
    fewer than MIN_CROSS_SECTION pairs survive or either side is constant
    (rank correlation is undefined on a constant)."""
    joined = pd.concat([a, b], axis=1).dropna()
    if len(joined) < MIN_CROSS_SECTION:
        return None
    x, y = joined.iloc[:, 0], joined.iloc[:, 1]
    if x.nunique() < 2 or y.nunique() < 2:
        return None
    rho = x.rank().corr(y.rank())
    if rho is None or (isinstance(rho, float) and math.isnan(rho)):
        return None
    return float(rho)


def forward_returns(closes: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """close[t+H] / close[t] - 1, indexed at t. Never reads a bar before t, and
    the trailing H rows are NaN by construction — no look-ahead into a window
    that has not closed."""
    return closes.shift(-int(horizon)) / closes - 1.0


def rebalance_dates(factor: pd.DataFrame, session_index, horizon: int, step: int) -> list:
    """Usable evaluation dates spaced at least max(step, horizon) SESSIONS
    apart. Spacing is measured on `session_index` (the close panel's trading
    calendar), not on list positions, so a factor panel populated only every
    `step` sessions still yields non-overlapping windows. Non-overlap matters:
    ICIR = mean/std * sqrt(252/H) assumes independent per-period ICs."""
    spacing = max(int(step), int(horizon))
    pos = {d: i for i, d in enumerate(session_index)}
    picked, last = [], None
    for d in factor.index:
        i = pos.get(d)
        if i is None:
            continue
        if factor.loc[d].notna().sum() < MIN_CROSS_SECTION:
            continue
        if last is None or (i - last) >= spacing:
            picked.append(d)
            last = i
    return picked


def ic_series(factor: pd.DataFrame, fwd: pd.DataFrame, dates: list) -> List[float]:
    out = []
    for d in dates:
        rho = _spearman(factor.loc[d], fwd.loc[d])
        if rho is not None:
            out.append(rho)
    return out


def _icir(ics: List[float], horizon: int) -> Optional[float]:
    if len(ics) < 2:
        return None
    mean = sum(ics) / len(ics)
    var = sum((x - mean) ** 2 for x in ics) / (len(ics) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return float((mean / sd) * math.sqrt(252.0 / float(horizon)))


def quintile_stats(factor: pd.DataFrame, fwd: pd.DataFrame, dates: list, n_q: int = 5):
    """(quintile_means, ls_q5q1, monotonic, turnover) at the caller's horizon.

    turnover is the mean Jaccard DISTANCE between consecutive rebalances'
    extreme-quintile membership (Q1 union Q5) — the names the strategy would
    actually have to trade — matching factor_prescreen.compute_stats'
    turnover_proxy shape."""
    per_q = [[] for _ in range(n_q)]
    ls, extremes = [], []
    for d in dates:
        row = factor.loc[d].dropna()
        r = fwd.loc[d]
        row = row[row.index.intersection(r.dropna().index)]
        if len(row) < MIN_CROSS_SECTION or row.nunique() < MIN_DISTINCT_VALUES:
            continue
        try:
            labels = pd.qcut(row.rank(method='first'), n_q, labels=False)
        except ValueError:
            continue
        means = []
        for q in range(n_q):
            members = row.index[labels == q]
            m = float(r[members].mean()) if len(members) else float('nan')
            means.append(m)
            if not math.isnan(m):
                per_q[q].append(m)
        if not math.isnan(means[0]) and not math.isnan(means[-1]):
            ls.append(means[-1] - means[0])
        extremes.append(set(row.index[labels == n_q - 1]) | set(row.index[labels == 0]))

    quintile_means = [float(sum(v) / len(v)) if v else None for v in per_q]
    ls_q5q1 = float(sum(ls) / len(ls)) if ls else None
    ok = [m for m in quintile_means if m is not None]
    monotonic = bool(
        len(ok) == n_q
        and (all(ok[i] < ok[i + 1] for i in range(n_q - 1))
             or all(ok[i] > ok[i + 1] for i in range(n_q - 1)))
    )
    turnovers = []
    for a, b in zip(extremes, extremes[1:]):
        union = a | b
        turnovers.append(1.0 - (len(a & b) / len(union) if union else 0.0))
    turnover = float(sum(turnovers) / len(turnovers)) if turnovers else None
    return quintile_means, ls_q5q1, monotonic, turnover


def compute_ic_screen(factor: pd.DataFrame, closes: pd.DataFrame, *,
                       horizons=HORIZONS, step: int = REBALANCE_STEP,
                       one_way_bps: float = ONE_WAY_COST_BPS) -> dict:
    """The pure core. `factor` is a date x ticker panel (NaN = no opinion);
    `closes` is the aligned close panel. Returns the JSON-serialisable verdict
    dict documented at the top of this module."""
    factor = factor.sort_index()
    closes = closes.sort_index()
    common = [c for c in factor.columns if c in closes.columns]
    factor = factor[common]
    closes = closes[common]
    session_index = closes.index

    h0 = int(horizons[0])
    fwd0 = forward_returns(closes, h0)
    dates0 = rebalance_dates(factor, session_index, h0, step)

    qualifying = 0
    for d in dates0:
        row = factor.loc[d].dropna()
        if len(row) >= MIN_CROSS_SECTION and row.nunique() >= MIN_DISTINCT_VALUES:
            qualifying += 1

    ic, icir = {}, {}
    for h in horizons:
        fwd_h = forward_returns(closes, h)
        dates_h = rebalance_dates(factor, session_index, h, step)
        series_h = ic_series(factor, fwd_h, dates_h)
        ic[str(h)] = float(sum(series_h) / len(series_h)) if series_h else None
        icir[str(h)] = _icir(series_h, h)

    series0 = ic_series(factor, fwd0, dates0)
    half = len(series0) // 2
    ic_half = {
        'first':  float(sum(series0[:half]) / half) if half else None,
        'second': (float(sum(series0[half:]) / len(series0[half:]))
                   if series0[half:] else None),
    }

    ac = []
    for a, b in zip(dates0, dates0[1:]):
        rho = _spearman(factor.loc[a], factor.loc[b])
        if rho is not None:
            ac.append(rho)
    rank_ac = float(sum(ac) / len(ac)) if ac else None

    quintile_means, ls_q5q1, monotonic, turnover = quintile_stats(factor, fwd0, dates0)
    # No measurable turnover yet => charge a full round trip (conservative: it
    # makes `flat` HARDER to reach, never easier).
    cost = round_trip_cost(one_way_bps) * (turnover if turnover is not None else 1.0)

    ic0 = ic[str(h0)]
    icir0 = icir[str(h0)]
    if qualifying < MIN_REBALANCES:
        verdict, reason = 'skipped', 'insufficient_cross_section'
    elif (ic0 is not None and abs(ic0) < FLAT_IC_ABS
          and ls_q5q1 is not None and abs(ls_q5q1) < cost):
        verdict, reason = 'flat', 'ic_below_noise_and_ls_below_cost'
    elif icir0 is None or abs(icir0) < WEAK_ICIR_ABS:
        verdict, reason = 'weak', 'icir_below_threshold'
    else:
        verdict, reason = 'pass', None

    return {
        'ic':                      ic,
        'icir':                    icir,
        'ic_half':                 ic_half,
        'rank_ac':                 rank_ac,
        'ls_q5q1':                 ls_q5q1,
        'quintile_means':          quintile_means,
        'monotonic':               monotonic,
        'turnover':                turnover,
        'cost_per_rebalance':      cost,
        'n_rebalances':            len(dates0),
        'n_qualifying_rebalances': qualifying,
        'horizons':                [int(h) for h in horizons],
        'verdict':                 verdict,
        'reason':                  reason,
    }


def factor_from_signals(daily_signals: List[list], dates: list,
                         universe: List[str]) -> pd.DataFrame:
    """Wide date x ticker factor panel from generate_signals output.

    Value = direction sign x magnitude, where magnitude is position_size_pct
    when the strategy set one (it carries the strategy's own conviction
    ordering) and the confidence weight otherwise. NaN = the strategy said
    nothing about that ticker that day — which is the honest encoding: a
    long-only decile strategy really has no opinion on the other 90 %, and the
    resulting thin cross-section is what MIN_DISTINCT_VALUES detects."""
    frame = pd.DataFrame(index=pd.Index(dates, name='date'),
                          columns=list(universe), dtype='float64')
    for d, sigs in zip(dates, daily_signals):
        for s in (sigs or []):
            t = getattr(s, 'ticker', None)
            if t is None or t not in frame.columns:
                continue
            sign = DIRECTION_SIGN.get(getattr(s, 'direction', None), 0.0)
            size = getattr(s, 'position_size_pct', None)
            if isinstance(size, (int, float)) and size and not pd.isna(size):
                mag = abs(float(size))
            else:
                mag = CONFIDENCE_WEIGHT.get(getattr(s, 'confidence', None), 0.5)
            frame.at[d, t] = sign * mag
    return frame


def run_ic_screen(strategy_file: str, *, sessions: int = LOOKBACK_SESSIONS,
                   max_tickers: int = DEFAULT_MAX_TICKERS,
                   step: int = REBALANCE_STEP) -> dict:
    """Drive the strategy over ~2 years of sliced history and screen the factor
    panel it produces. Raises on any infra problem — main() turns a raise into
    exit 1, which the orchestrator treats as ic_screen_infra_fail."""
    from backtest import factor_prescreen as fp

    cls = fp._load_strategy_class(strategy_file)

    # Same aux-dependent bypass factor_prescreen applies (:614-630): this screen
    # never populates real aux_data, so an aux-dependent strategy would emit
    # nothing here regardless of legitimacy and its "IC" would be meaningless.
    instrument_class = fp._resolve_instrument_class(getattr(cls, 'id', None), strategy_file)
    if instrument_class == 'option' or fp._module_reads_aux_data(strategy_file):
        return {'ic': {}, 'icir': {}, 'ic_half': {}, 'rank_ac': None,
                'ls_q5q1': None, 'quintile_means': None, 'monotonic': None,
                'turnover': None, 'cost_per_rebalance': None,
                'n_rebalances': 0, 'n_qualifying_rebalances': 0,
                'horizons': [int(h) for h in HORIZONS],
                'verdict': 'skipped', 'reason': 'ic_screen_skipped_aux_dependent'}

    instance = cls()
    declared = getattr(instance, 'min_lookback', None)
    try:
        declared = int(declared) if declared is not None else None
    except (TypeError, ValueError):
        declared = None
    lookback = fp.DEFAULT_LOOKBACK
    if declared is not None:
        lookback = max(lookback, declared + fp.MIN_LOOKBACK_PAD)
    lookback = min(lookback, fp.MAX_LOOKBACK_BARS)

    close_wide, universe, _src = fp.load_price_window(sessions, max_tickers, lookback)
    n_rows = len(close_wide.index)
    if n_rows < 1:
        raise RuntimeError('empty price panel for the IC screen window')

    start_idx = max(0, n_rows - int(sessions))
    regime = fp._benign_regime()

    dates, daily = [], []
    for i in range(start_idx, n_rows, max(1, int(step))):
        # Full history up to and including bar i — mirrors unified_backtest's
        # per-bar close_wide.loc[:current_date], so a long-lookback strategy
        # sees the same panel shape it would see in the real backtest.
        prices_to_date = close_wide.iloc[:i + 1]
        try:
            sigs = instance.generate_signals(prices_to_date, regime, universe, aux_data=None)
        except TypeError:
            sigs = instance.generate_signals(prices_to_date, regime, universe)
        dates.append(close_wide.index[i])
        daily.append(sigs or [])

    factor = factor_from_signals(daily, dates, universe).reindex(close_wide.index)
    return compute_ic_screen(factor, close_wide, step=int(step))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Rank-IC / quantile / turnover screen (spec D1)')
    ap.add_argument('--strategy-file', required=True)
    ap.add_argument('--sessions', type=int, default=LOOKBACK_SESSIONS,
                     help='decision sessions screened (default 504 ~ 2 years)')
    ap.add_argument('--max-tickers', type=int, default=DEFAULT_MAX_TICKERS)
    ap.add_argument('--step', type=int, default=REBALANCE_STEP,
                     help='sessions between driven bars (default 5)')
    args = ap.parse_args(argv)

    try:
        result = run_ic_screen(args.strategy_file, sessions=args.sessions,
                                max_tickers=args.max_tickers, step=args.step)
    except Exception as e:  # noqa: BLE001 — any infra failure -> exit 1
        print(f'ic screen infra error: {e}', file=sys.stderr)
        return 1

    # allow_nan=False: every field above is either a finite float or None by
    # construction (see compute_ic_screen), so this never raises in practice —
    # it is a hard guarantee for the Node-side JSON.parse() in the orchestrator
    # (Task 8), which chokes on a bare `NaN` token in the stream.
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
