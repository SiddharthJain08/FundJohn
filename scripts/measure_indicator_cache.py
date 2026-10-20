#!/usr/bin/env python3
"""measure_indicator_cache.py -- wall time + byte-identity of the D2 indicator cache.

  measure_indicator_cache.py (--strategy SID | --strategy-file PATH)
        [--compare-to SID] [--days 2746] [--tickers 1000] [--seed 0]

Drives ``unified_backtest._per_bar_simulate`` over a SYNTHETIC panel (GBM closes,
late listings, interior NaN gaps, bars derived from closes) twice: with
OPENCLAW_BT_INDICATOR_CACHE unset and =1. Compares every generated signal and the
simulated trades byte-for-byte, prints one ``[bt_cache_measure]`` line and exits 0
iff every identity is true. ``--compare-to SID`` additionally runs that (original,
non-cached) strategy with the cache off and compares its signals to the cached run.

Never reads data/master/* nor Postgres: ``load_aux_data`` is patched to
``{'options': {}}``. The full-size run (1,000 tickers x 2,746 days) is a
CONTROLLER operation: ``nice -n 19``, as a transient systemd unit, outside the
compute window (18:45-20:45Z).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import resource
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / 'src'), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

AUX_STUB = {'options': {}}
END_DATE = '2026-10-16'


def make_panel(days: int, tickers: int, seed: int):
    """(close_wide, bars_by_ticker, regimes). Deterministic in (days, tickers, seed)."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=END_DATE, periods=days)
    cols = [f'ZZTEST{i:04d}' for i in range(tickers)] + ['SPY', 'QQQ']
    n = len(cols)
    rets = rng.normal(0.0004, 0.015, size=(days, n))
    close = pd.DataFrame(100.0 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=cols)
    # ~5 % of tickers list late (leading NaN); >=1 so the property is always exercised.
    n_late = max(1, round(0.05 * tickers))
    for j in rng.choice(tickers, size=n_late, replace=False):
        close.iloc[:int(rng.integers(max(2, days // 10), max(3, days // 3))), j] = np.nan
    # 1 % interior gaps, never in the first rows (keeps the leading edge honest).
    mask = rng.random(close.shape) < 0.01
    mask[:5, :] = False
    close = close.mask(mask)
    bars = {}
    for j, c in enumerate(cols):
        s = close[c].dropna()
        o = s.shift(1).fillna(s)
        wig = pd.Series(rng.uniform(0.001, 0.01, size=len(s)), index=s.index)
        bars[c] = pd.DataFrame({'open': o, 'high': np.maximum(o, s) * (1 + wig),
                                'low': np.minimum(o, s) * (1 - wig), 'close': s})
    regimes = pd.Series('LOW_VOL', index=idx)
    return close, bars, regimes


def _sig_record(date, s) -> tuple:
    return (pd.Timestamp(date).date().isoformat(), s.ticker, s.direction, s.entry_price,
            s.stop_loss, s.target_1, s.target_2, s.target_3, s.position_size_pct,
            s.confidence, json.dumps(s.signal_params, sort_keys=True, default=str))


def _run(cls, close_wide, bars, regimes, *, cache: bool):
    import backtest.unified_backtest as ub
    import strategies.aux_data_loader as adl
    instance = cls()
    start_i = min(getattr(instance, 'min_lookback', 20) + 5, len(close_wide) - 1)
    start, end = close_wide.index[start_i], close_wide.index[-1]
    records: list = []
    orig = instance.generate_signals

    def wrapped(prices, regime, universe, *a, **k):
        sigs = orig(prices, regime, universe, *a, **k)
        d = prices.index[-1]
        for s in sigs or []:
            records.append(_sig_record(d, s))
        return sigs
    instance.generate_signals = wrapped
    with patch.dict(os.environ), \
            patch.object(adl, 'load_aux_data', lambda *a, **k: {'options': {}}), \
            open(os.devnull, 'w') as dn, \
            contextlib.redirect_stderr(dn), contextlib.redirect_stdout(dn):
        os.environ['OPENCLAW_BACKTEST_COUPLED_RECS'] = '0'
        os.environ.pop('OPENCLAW_BT_EVENT_GATE', None)
        os.environ.pop('OPENCLAW_BT_INDICATOR_CACHE', None)
        if cache:
            os.environ['OPENCLAW_BT_INDICATOR_CACHE'] = '1'
        t0 = time.perf_counter()
        sim = ub._per_bar_simulate(instance, close_wide, bars, regimes, start, end,
                                   strategy_id=getattr(instance, 'id', None),
                                   fill_model='same_close')
        secs = time.perf_counter() - t0
    trades = json.dumps(sim['trades'], sort_keys=True, default=str)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    return records, trades, secs, rss


def run_once(cls, close_wide, bars, regimes, *, cache: bool):
    """-> (signal_records, seconds, peak_rss_mb)."""
    records, _t, secs, rss = _run(cls, close_wide, bars, regimes, cache=cache)
    return records, secs, rss


def _load(args_sid, args_file):
    import backtest.unified_backtest as ub
    if args_file:
        return ub.load_strategy_class(args_file), Path(args_file).stem
    path = ub.find_strategy_file(args_sid)
    if not path:
        raise RuntimeError(f'strategy file not found for {args_sid}')
    return ub.load_strategy_class(path), args_sid


def _b(v: bool) -> str:
    return 'true' if v else 'false'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--strategy')
    g.add_argument('--strategy-file')
    ap.add_argument('--compare-to', default=None)
    ap.add_argument('--days', type=int, default=2746)
    ap.add_argument('--tickers', type=int, default=1000)
    ap.add_argument('--seed', type=int, default=0)
    a = ap.parse_args(argv)
    try:
        cls, sid = _load(a.strategy, a.strategy_file)
        close, bars, regimes = make_panel(a.days, a.tickers, a.seed)
        active = list(getattr(cls, 'active_in_regimes', None) or ['LOW_VOL'])
        regimes[:] = active[0]
        r0, t0, s0, _ = _run(cls, close, bars, regimes, cache=False)
        r1, t1, s1, rss = _run(cls, close, bars, regimes, cache=True)
        sig_ok, trd_ok = r0 == r1, t0 == t1
        sha = hashlib.sha256(json.dumps(r1, default=str).encode()).hexdigest()[:12]
        line = (f'[bt_cache_measure] strategy={sid} days={a.days} tickers={a.tickers} '
                f'uncached_s={s0:.2f} cached_s={s1:.2f} speedup={s0 / max(s1, 1e-9):.2f}x '
                f'signals_identical={_b(sig_ok)} trades_identical={_b(trd_ok)} '
                f'n_signals={len(r1)} sha={sha} peak_rss_mb={rss}')
        ok = sig_ok and trd_ok
        if a.compare_to:
            ocls, _ = _load(a.compare_to, None)
            ro, _t, _s, _ = _run(ocls, close, bars, regimes, cache=False)
            o_ok = ro == r1
            line += f' orig={a.compare_to} orig_identical={_b(o_ok)}'
            ok = ok and o_ok
    except (OSError, ValueError, KeyError, RuntimeError) as e:
        print(f'[bt_cache_measure] ERROR {type(e).__name__}: {e}', file=sys.stderr)
        return 2
    print(line)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
