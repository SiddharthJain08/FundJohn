#!/usr/bin/env python3
"""unified_backtest.py — single source-of-truth strategy backtest.

Methodology (locked 2026-05-14):
  - Time scope:  full historical_regimes coverage (~2016-04 → today).
                 Sparse pre-2024 universe is accepted; statistical mass
                 lives in the dense modern era anyway.
  - Regime mode: discovery. Strategy runs on EVERY trading day in the
                 window; we ignore the strategy's own `active_in_regimes`
                 gate (by temporarily overriding it on the instance) so
                 we can observe regime-conditional performance from data.
  - Fidelity:    strategy signals + bracket walk. For each emitted signal
                 we walk forward day-by-day using OHLC bars from
                 prices.parquet, exiting on target_1 hit / stop_loss hit /
                 max_hold_days (default 21). No prefilter, no sizer
                 simulation, no slippage / commission model.
  - Storage:     three tables (strategy_backtest_runs +
                 strategy_backtest_regimes + strategy_backtest_trades),
                 append-only. The latest primary_window=true run per
                 strategy is what the dashboard / sizer / eligibility
                 assigner read.

CLI:
  python3 -m backtest.unified_backtest --strategy-id S_xx [--max-hold-days 21]
  python3 -m backtest.unified_backtest --all-live
  python3 -m backtest.unified_backtest --strategy-file src/strategies/implementations/S_xx.py

Exit codes:
  0  success (run_id printed)
  1  validation failed / import failed
  2  DB unavailable / write failed
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect as _inspect
import json
import math
import os
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = str(ROOT / 'src')
sys.path.insert(0, SRC_DIR)
sys.path.insert(0, str(ROOT))

from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES  # noqa: E402
from strategies.validate_strategy import validate                     # noqa: E402
from backtest import options_backtest  # SP-4 Phase 0
from execution import regime_param_override  # noqa: E402  # per-(strategy, regime) bracket override
from strategies.lifecycle import VALID_INSTRUMENT_CLASSES, _detect_module_instrument_class  # noqa: E402  # SP-4 dispatch

# ── Configuration ────────────────────────────────────────────────────────────
TRADING_DAYS_PER_YEAR = 252
RISK_FREE_DAILY       = 0.05 / TRADING_DAYS_PER_YEAR
DEFAULT_MAX_HOLD_DAYS = 21
DEFAULT_START_DATE    = '2016-04-11'   # earliest historical_regimes row

PRICES_PARQUET = ROOT / 'data' / 'master' / 'prices.parquet'
REGIMES_PARQUET = ROOT / 'data' / 'master' / 'historical_regimes.parquet'

# SP-3: per-instrument-class execution cost (one-way, basis points). equity/etp
# share the equity model in MVP; options/crypto get their own when those engines land.
INSTRUMENT_COST_BPS: dict[str, float] = {"equity": 10.0, "etp": 10.0, "option": 5.0, "crypto": 25.0}


def resolve_cost_model_bps(instrument_class: str) -> float:
    return INSTRUMENT_COST_BPS.get(instrument_class, INSTRUMENT_COST_BPS["equity"])


# Honest cost model (2026-07-27): per-ticker one-way half-spread estimates built
# by scripts/build_ticker_cost_model.py from ADV$/price + measured 15:55 ET NBBO
# spreads. Replaces the flat 10bps for equity/etp when present. The artifact is a
# derived cache — regenerate after large universe changes, never hand-edit.
TICKER_COST_BPS_JSON = ROOT / 'data' / 'derived' / 'ticker_cost_bps.json'


def load_ticker_cost_bps() -> Optional[dict]:
    """{ticker: one-way half-spread bps} from the derived artifact, or None
    (caller falls back to the flat INSTRUMENT_COST_BPS model). Kill switch:
    OPENCLAW_BT_SPREAD_COSTS=0."""
    if os.environ.get('OPENCLAW_BT_SPREAD_COSTS', '1') == '0':
        return None
    try:
        with open(TICKER_COST_BPS_JSON) as f:
            art = json.load(f)
        m = art.get('cost_bps') or {}
        if not m:
            return None
        return {str(k): float(v) for k, v in m.items()}
    except FileNotFoundError:
        _log(f'spread-cost artifact missing ({TICKER_COST_BPS_JSON}) — flat cost model in use')
        return None
    except Exception as e:
        _log(f'spread-cost artifact unreadable ({e}) — flat cost model in use')
        return None


BT_MIN_PRICE_USD = 2.0      # entries below this median close are ungateable-cost names
BT_MIN_ADV_USD = 400_000.0  # ~1% participation headroom for a $4k book slot


def load_bt_asset_gate() -> Optional[dict]:
    """{symbol: (long_ok, short_ok)} mirroring the live sizer's asset-eligibility
    gate (regime_blended_sizer._load_asset_eligibility): new entries require
    tradable+active+easy_to_borrow+fractionable; shorts additionally require
    shortable. A liquidity floor (median close >= BT_MIN_PRICE_USD and ADV$ >=
    BT_MIN_ADV_USD, from the cost artifact) applies to BOTH sides — sub-$2 /
    sub-$400k-ADV names are where the measured spread + impact costs exceed any
    plausible edge (fix 5, 2026-07-27). Snapshot = TODAY'S alpaca_tradable_universe
    — symbols absent from it (delisted/unknown historically) are NOT ETB-gated,
    the point-in-time resolver stays the authority there; the liquidity floor
    still applies when the artifact covers them. Fail-open on any DB error,
    matching live. Modes via OPENCLAW_BT_ASSET_GATE: 'parity' (default),
    'short_only', 'off'. Liquidity floor kill switch: OPENCLAW_BT_LIQ_FLOOR=0."""
    mode = os.environ.get('OPENCLAW_BT_ASSET_GATE', 'parity')
    if mode == 'off':
        return None
    rows = []
    try:
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
            cur.execute("""
                SELECT symbol,
                       (tradable AND status = 'active' AND easy_to_borrow AND fractionable),
                       (tradable AND status = 'active' AND easy_to_borrow AND shortable)
                FROM alpaca_tradable_universe
            """)
            rows = cur.fetchall()
    except Exception as e:
        _log(f'asset gate DB unavailable ({e}) — ETB leg skipped (fail-open, matches live)')
    if mode == 'short_only':
        gate = {sym: (True, bool(short_ok)) for sym, elig, short_ok in rows}
    else:
        gate = {sym: (bool(elig), bool(elig) and bool(short_ok)) for sym, elig, short_ok in rows}
    if os.environ.get('OPENCLAW_BT_LIQ_FLOOR', '1') != '0':
        try:
            with open(TICKER_COST_BPS_JSON) as f:
                art = json.load(f)
            adv = art.get('adv_usd') or {}
            px = art.get('med_close') or {}
            n_liq = 0
            for sym in set(adv) | set(gate):
                a = float(adv.get(sym, 0.0) or 0.0)
                p = float(px.get(sym, 0.0) or 0.0)
                if sym in adv and (a < BT_MIN_ADV_USD or p < BT_MIN_PRICE_USD):
                    gate[sym] = (False, False)
                    n_liq += 1
            _log(f'liquidity floor: {n_liq} symbols below '
                 f'${BT_MIN_PRICE_USD:.0f}/{BT_MIN_ADV_USD / 1e3:.0f}k-ADV blocked')
        except Exception as e:
            _log(f'liquidity floor unavailable ({e}) — ETB-only gate')
    return gate or None


def _log(msg: str) -> None:
    print(f'[unified_backtest] {msg}', flush=True)


# ── Strategy loading (adapted from auto_backtest.py) ─────────────────────────

def _is_strategy_class(obj, module_name=None):
    if not _inspect.isclass(obj) or obj.__name__ == 'BaseStrategy':
        return False
    if _inspect.isabstract(obj):
        return False
    if module_name and getattr(obj, '__module__', None) != module_name:
        return False
    try:
        if issubclass(obj, BaseStrategy):
            return True
    except TypeError:
        pass
    return any(b.__name__ == 'BaseStrategy' for b in obj.__mro__[1:])


def load_strategy_class(filepath: str):
    """Load and return a strategy class from a .py file. Raises on failure."""
    val = validate(filepath)
    if not val['ok']:
        raise RuntimeError(f'contract validation failed: {"; ".join(val["errors"])}')
    abs_path = os.path.abspath(filepath)
    module_name = None
    if SRC_DIR in abs_path:
        rel = os.path.relpath(abs_path, SRC_DIR).replace(os.sep, '.')
        if rel.endswith('.py'):
            module_name = rel[:-3]
    if module_name:
        sys.modules.pop(module_name, None)
        module = importlib.import_module(module_name)
    else:
        spec = importlib.util.spec_from_file_location('_bt_strat', abs_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    classes = [obj for _, obj in _inspect.getmembers(module, _inspect.isclass)
               if _is_strategy_class(obj, module_name=module.__name__)]
    if not classes:
        raise RuntimeError(f'no strategy class in {filepath}')
    return classes[0]


def find_strategy_file(strategy_id: str) -> Optional[str]:
    """Search manifest + implementations dir for the .py file for a strategy_id."""
    impl_dir = ROOT / 'src' / 'strategies' / 'implementations'
    candidate = impl_dir / f'{strategy_id}.py'
    if candidate.exists():
        return str(candidate)
    manifest_path = ROOT / 'src' / 'strategies' / 'manifest.json'
    try:
        m = json.loads(manifest_path.read_text())
        entry = (m.get('strategies') or {}).get(strategy_id) or {}
        f = (entry.get('metadata') or {}).get('canonical_file')
        if f:
            full = impl_dir / f
            if full.exists():
                return str(full)
    except Exception:
        pass
    return None


def _code_sha(filepath: str) -> str:
    """git rev for the file at run-time, or content sha-256 if dirty / not tracked."""
    try:
        r = subprocess.run(['git', '-C', str(ROOT), 'log', '-1', '--format=%H', '--', filepath],
                           capture_output=True, text=True, timeout=5)
        sha = (r.stdout or '').strip()
        if sha:
            # Check if file has uncommitted changes
            diff = subprocess.run(['git', '-C', str(ROOT), 'diff', '--quiet', '--', filepath],
                                  capture_output=True, timeout=5)
            if diff.returncode == 0:
                return sha
            # Dirty — annotate
            return f'{sha[:12]}+dirty'
    except Exception:
        pass
    # Fallback: content hash
    import hashlib
    h = hashlib.sha256()
    h.update(Path(filepath).read_bytes())
    return f'sha256:{h.hexdigest()[:16]}'


# ── Data loading ─────────────────────────────────────────────────────────────

# Equity trading-day calendar — shared with the live engine via lib.price_panel
# so backtest and live never diverge. Aliased to the historical underscore names
# used by load_prices_panels() and run_backtest() below.
from lib.price_panel import (
    is_equity_ticker as _is_equity_ticker,
    apply_equity_calendar as _apply_equity_calendar,
    equity_calendar_enabled as _equity_calendar_enabled,
    calendar_for as _calendar_for,
)


def load_prices_panels(calendar: str = 'union') -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Load prices.parquet and return:
      - close_wide: date × ticker close panel for strategy.generate_signals
      - bars_by_ticker: {ticker: DataFrame indexed by date with open/high/low/close}
        for the bracket walk-forward
    """
    # Column-pruned + pyarrow-DICTIONARY read (2026-07-16). Two same-day evolutions,
    # both forced by the 5-year backfill doubling prices.parquet (9.5M → 18.7M rows),
    # which OOM-killed the bare pd.read_parquet(PRICES_PARQUET) outright (anon-rss
    # 5.8GB, rc=137) on this 2-core/8GB no-swap box:
    #   1. Prune to OHLC — the bare read pulled all 10 columns; `source` alone is a
    #      ~900MB object column, materialised only to be discarded.
    #   2. MEASURED (per-phase VmRSS probe): even pruned, the in-pandas
    #      float32/categorical path peaked at ~2.05GB AT READ, because
    #      pd.read_parquet materialises 18.7M object-dtype ticker strings (~640MB)
    #      AND object-dtype date strings (~750MB) AND float64 OHLC (~285MB) before
    #      any of them is compacted a few lines later. That transient — not the sim
    #      loop — is what OOM-killed the heavy cross-sectionals (low_volatility_us
    #      died at 18s, INSIDE this read). ticker AND date are BOTH
    #      string+RLE_DICTIONARY on disk, so read_dictionary reads their codes
    #      directly (no plain-string decode) and OHLC is cast to float32 IN ARROW;
    #      to_pandas then yields a categorical+float32 panel with no fat pandas
    #      transient. Read peak 2.05GB → 0.99GB, whole-load peak → ~1.15GB (now at
    #      the sort), both measured live on the fleet.
    # Output is byte-identical to the old path, verified by
    # tests/test_arrow_dictionary_read_equivalence.py (close_wide values + column
    # order + bars_by_ticker; exercises multi-row-group dictionary unification and
    # the two traps: a categorical sort reorders columns, and a fully-quarantined
    # ticker can leave a phantom pivot column). Only unified_backtest reads this
    # (fleet/coupling/manual) — NOT the live daily sizer. float32 carries ~7 sig
    # figs (ample for OHLC + stop/target tests; numpy upcasts in mean/std so Sharpe
    # is unaffected). See reference_vps_two_core_cpu: never load the whole parquet.
    _COLS = ['ticker', 'date', 'open', 'high', 'low', 'close']
    import pyarrow as pa
    import pyarrow.parquet as pq
    _tbl = pq.read_table(PRICES_PARQUET, columns=_COLS, read_dictionary=['ticker', 'date'])
    for _c in ('open', 'high', 'low', 'close'):
        _i = _tbl.schema.get_field_index(_c)
        _tbl = _tbl.set_column(_i, _c, _tbl.column(_c).cast(pa.float32()))
    p = _tbl.to_pandas()
    del _tbl
    # SP-2 Phase B Task 5: drop quarantined (ticker, date) rows. filter_quarantined
    # is categorical-safe (empty set → returns df untouched; else .astype(str) on
    # the key col). Runs BEFORE the category normalisation below so a fully-
    # quarantined ticker cannot leave a phantom all-NaN pivot column, and BEFORE
    # pd.to_datetime — it stringifies date to match PG affected_date::TEXT, and here
    # date is still categorical-of-strings, which .astype(str) resolves correctly.
    from src.pipeline.quarantine_filter import filter_quarantined
    p = filter_quarantined(p, 'prices.parquet')
    # ticker is ALREADY categorical (from the dictionary read). Normalise it to
    # EXACTLY reproduce the old object-dtype path: drop unused categories (phantom
    # guard) then reorder to sorted, so the categorical sort_values + pivot below
    # yield lexicographic row/column order identical to pre-rewrite. date needs no
    # reorder — it becomes datetime64 before sort/pivot, so its category order is
    # irrelevant.
    if str(p['ticker'].dtype) == 'category':
        p['ticker'] = p['ticker'].cat.remove_unused_categories()
        p['ticker'] = p['ticker'].cat.reorder_categories(sorted(p['ticker'].cat.categories))
    p['date'] = pd.to_datetime(p['date'])
    p = p.sort_values(['ticker', 'date'])
    # Close panel (wide). Strategies expect index = date, columns = ticker, values = close.
    close_wide = p.pivot(index='date', columns='ticker', values='close')
    # Restore a plain string Index: pivoting on a categorical yields a
    # CategoricalIndex, and strategies treat close_wide.columns as ordinary
    # labels (set ops / isin / reindex). Keeping the panel's public shape
    # identical to before means the memory fix cannot change any result.
    close_wide.columns = pd.Index(close_wide.columns.astype(str), name='ticker')
    close_wide.index.name = 'date'
    if calendar == 'equity':
        close_wide = _apply_equity_calendar(close_wide)
    # observed=True: with a categorical key, groupby would otherwise iterate
    # every category. Every category here comes from the data so the set is the
    # same, but this keeps it explicit (and silences the pandas 2.x default change).
    bars_by_ticker = {str(t): g.set_index('date')[['open','high','low','close']]
                      for t, g in p.groupby('ticker', observed=True)}
    return close_wide, bars_by_ticker


def load_regimes() -> pd.Series:
    """Return a Series of regime_state indexed by date (datetime)."""
    r = pd.read_parquet(REGIMES_PARQUET)
    r['date'] = pd.to_datetime(r['date'])
    r = r.sort_values('date').set_index('date')
    return r['regime'].astype(str)


# ── Simulation core ──────────────────────────────────────────────────────────

def _signal_to_long_short(direction: str) -> int:
    """Normalize a Signal.direction to +1 (long) or -1 (short). Returns 0 if unsupported."""
    if direction is None:
        return 0
    u = str(direction).strip().upper()
    if u in ('LONG', 'BUY', 'BUY_VOL'):
        return 1
    if u in ('SHORT', 'SELL', 'SELL_VOL'):
        return -1
    return 0


def _bar_exit(direction: int, high: float, low: float,
              stop_loss: float, target_1: float, dt_priority: str):
    """Intra-bar bracket decision shared by simulate_trade and the exit-hook
    open-book stepper. Returns (exit_level, reason) or (None, None).
    Long: target when high >= target_1, stop when low <= stop_loss; short
    mirrored. Double-touch resolves by dt_priority ('stop' default)."""
    if direction > 0:
        t_hit = high >= target_1
        s_hit = low <= stop_loss
    else:
        t_hit = low <= target_1
        s_hit = high >= stop_loss
    if t_hit and s_hit:
        if dt_priority == 'target':
            return float(target_1), 'target'
        return float(stop_loss), 'stop'
    if t_hit:
        return float(target_1), 'target'
    if s_hit:
        return float(stop_loss), 'stop'
    return None, None


def simulate_trade(bars: pd.DataFrame, entry_date: pd.Timestamp,
                   direction: int, entry_price: float,
                   stop_loss: float, target_1: float,
                   max_hold_days: int, *,
                   include_entry_bar: bool = False,
                   slippage_bps: float = 0.0) -> dict:
    """Walk forward from entry_date+1 (or entry_date if include_entry_bar=True),
    returning the exit dict:
       {exit_date, exit_price, exit_reason, holding_days, pnl_pct, daily_marks}.
    `daily_marks` is the true per-day mark-to-market path: a list of
    (date, direction*(mark_i/mark_{i-1} - 1)) with mark_0=entry_fill, each
    non-exit day marked to that bar's close, and the exit day marked to
    exit_fill. len(daily_marks) == holding_days ([] when holding_days == 0).
    Longs compound to pnl_pct exactly; shorts do not (path-dependent).

    `exit_price` is the adverse fill (see slippage_bps below), not the raw
    bracket/close level. `mark_0 == entry_fill` (== entry_price when
    slippage_bps == 0.0, since entry_fill collapses to entry_price).

    Long: target hit when high >= target_1; stop when low <= stop_loss.
    Short: target hit when low <= target_1; stop when high >= stop_loss.
    Double-touch bars (both levels inside the bar's range) resolve by
    OPENCLAW_BT_DOUBLE_TOUCH: 'stop' (DEFAULT since 2026-07-27 — the
    conservative reading; the intra-bar path is unknown and live OCO fills
    showed stops realize far more often than targets on wide-range bars) or
    'target' (the pre-2026-07-27 optimistic legacy, kept as an escape hatch).

    If neither fires within max_hold_days → exit at the final day's close.
    If price data ends before max_hold → exit at the last available close
    with reason='end_of_data'.

    include_entry_bar: when True (fill_model='open'), the fill bar itself is
    included in the exit walk (H/L occur after an open fill so bracket exits on
    that bar are legitimate). Default False keeps existing '>'-only behavior.

    slippage_bps: always-adverse per-fill slippage in basis points (one-way).
    Entry fills pay up (long) / sell down (short); exits fill worse than the
    triggering level in the same adverse direction. Default 0.0 reproduces
    the pre-slippage behavior exactly (entry_fill == entry_price, exit_fill
    == exit_level).
    """
    s = float(slippage_bps) / 10000.0
    entry_fill = entry_price * (1.0 + direction * s)  # adverse entry: pay up (long) / sell down (short)
    if include_entry_bar:
        bars_future = bars.loc[bars.index >= entry_date]
    else:
        bars_future = bars.loc[bars.index > entry_date]
    if bars_future.empty:
        return {'exit_date': entry_date, 'exit_price': entry_price,
                'exit_reason': 'end_of_data', 'holding_days': 0, 'pnl_pct': 0.0,
                'daily_marks': []}
    bars_window = bars_future.iloc[:max_hold_days]
    n = len(bars_window)
    daily_marks = []
    prev_mark = entry_fill
    _dt_priority = os.environ.get('OPENCLAW_BT_DOUBLE_TOUCH', 'stop')
    for i, (dt, bar) in enumerate(bars_window.iterrows(), start=1):
        high, low, close = float(bar['high']), float(bar['low']), float(bar['close'])
        exit_level, reason = _bar_exit(direction, high, low, stop_loss, target_1, _dt_priority)
        if exit_level is None and i == n:  # last bar, no bracket -> exit at close
            exit_level = close
            reason = 'max_hold' if n == max_hold_days else 'end_of_data'
        if exit_level is not None:  # this bar is the exit -> adverse exit fill
            exit_fill = exit_level * (1.0 - direction * s)
            daily_marks.append((dt, direction * (exit_fill / prev_mark - 1.0)))
            pnl = direction * (exit_fill - entry_fill) / entry_fill
            return {'exit_date': dt, 'exit_price': exit_fill, 'exit_reason': reason,
                    'holding_days': i, 'pnl_pct': pnl, 'daily_marks': daily_marks}
        # interior non-exit bar -> mark to market at the close (no transaction, no slippage)
        daily_marks.append((dt, direction * (close / prev_mark - 1.0)))
        prev_mark = close
    # Unreachable: the i == n branch always exits. Defensive fallback.
    return {'exit_date': bars_window.index[-1], 'exit_price': entry_fill,
            'exit_reason': 'end_of_data', 'holding_days': n, 'pnl_pct': 0.0,
            'daily_marks': daily_marks}


def _reanchor_bracket(*, ref: float, entry_price: float, direction: int,
                      stop_ref: float, target_ref: float) -> tuple[float, float]:
    """Re-express a stop/target defined as pct distances from ``ref`` so they
    sit the SAME pct distances from ``entry_price`` (the actual fill).

    Mirrors the live executor's re-anchor: preserves R:R geometry across an
    overnight gap instead of carrying absolute levels (which would invert the
    bracket when the fill gaps through a level). ``direction`` is +1 long / -1
    short. Returns (stop_loss, target_1).
    """
    if ref <= 0:
        return stop_ref, target_ref
    if direction > 0:  # long: stop below, target above
        stop_pct   = (ref - stop_ref) / ref
        target_pct = (target_ref - ref) / ref
        return entry_price * (1 - stop_pct), entry_price * (1 + target_pct)
    # short: stop above, target below
    stop_pct   = (stop_ref - ref) / ref
    target_pct = (ref - target_ref) / ref
    return entry_price * (1 + stop_pct), entry_price * (1 - target_pct)


# ── Metric aggregation ───────────────────────────────────────────────────────

def _portfolio_daily_returns(trades: list[dict]) -> tuple[np.ndarray, list[pd.Timestamp]]:
    """Build an equal-weighted daily portfolio return series.

    Methodology: when a trade carries a `daily_marks` list (true daily
    mark-to-market, OPENCLAW_TRUE_MTM_MARKS) each (date, return) is booked on
    its real trading day. Otherwise the trade's total pnl_pct is smeared evenly
    across its holding_days (legacy fallback for options_backtest trades and
    test fixtures). On any given day, the portfolio return is the
    *equal-weighted average* of all trades open that day (the marked-to-market
    view of a portfolio rebalancing daily to equal weight across active positions).

    Non-finite daily marks (NaN/Inf from a corrupt interior price bar) are
    dropped loudly — the smear path is already NaN-guarded upstream by
    `_is_finite_pnl`, but a marks trade can carry a finite pnl_pct yet a NaN
    interior mark, so it must be filtered here too or one bad bar would poison
    every trade sharing that date's average and cumprod the whole equity curve.

    Returns (daily_returns_array, sorted_dates_list). Empty arrays for
    trades that have zero holding_days (degenerate same-day exits).
    """
    if not trades:
        return np.array([]), []
    daily_pnls: dict[pd.Timestamp, list[float]] = {}
    for t in trades:
        hold = int(t.get('holding_days') or 0)
        if hold <= 0:
            continue
        marks = t.get('daily_marks')
        if marks:
            bad = 0
            for d, r in marks:
                rf = float(r)
                if not math.isfinite(rf):
                    bad += 1
                    continue
                daily_pnls.setdefault(pd.Timestamp(d), []).append(rf)
            if bad:
                _log(f'WARNING _portfolio_daily_returns: dropped {bad}/{len(marks)} '
                     f'non-finite daily mark(s) for {t.get("ticker", "?")} '
                     f'(corrupt interior price bar?)')
            continue
        per_day = float(t['pnl_pct']) / hold
        start = pd.Timestamp(t['entry_date'])
        # Distribute over `hold` calendar days starting the day after entry.
        # Bracket-walk's holding_days counts trading days, but for portfolio
        # marking calendar-day spacing is close enough and avoids
        # re-computing trading-day calendars here. Drawdown shape is
        # insensitive to this small approximation.
        for i in range(1, hold + 1):
            d = start + pd.Timedelta(days=i)
            daily_pnls.setdefault(d, []).append(per_day)
    if not daily_pnls:
        return np.array([]), []
    sorted_dates = sorted(daily_pnls.keys())
    daily_returns = np.array([
        sum(daily_pnls[d]) / len(daily_pnls[d])  # equal-weight across active trades
        for d in sorted_dates
    ])
    return daily_returns, sorted_dates


def _is_finite_pnl(t: dict) -> bool:
    """True iff a trade's pnl_pct is a finite real number. Tolerant of None,
    float('nan'), and Decimal('NaN') (some strategies emit Decimal NaN)."""
    try:
        return math.isfinite(float(t.get('pnl_pct')))
    except (TypeError, ValueError):
        return False


def aggregate_metrics(trades: list[dict]) -> dict:
    """Total-level aggregate built from an equal-weight daily portfolio
    equity curve (Fix A, 2026-05-14). Replaces the prior cumulative-product
    of sequential trade pnls which produced spurious 99% drawdowns whenever
    many trades fired in parallel.

    Sharpe, max_dd, and return_pct all derive from the daily portfolio
    return series; hit_rate and avg_pnl_pct stay per-trade for interpretability.

    SP-2 Phase C: also emits sortino and calmar alongside existing keys.
    - sortino: annualized Sortino ratio (downside semi-deviation target=0);
               None if <2 daily points or no downside deviation.
    - calmar: annualized_return_pct / max_dd_pct; None if max_dd_pct==0.
    """
    if not trades:
        return {'sharpe': None, 'max_dd_pct': 0.0, 'return_pct': 0.0,
                'total_trades': 0, 'hit_rate': None, 'avg_holding_days': None,
                'avg_pnl_pct': 0.0, 'sortino': None, 'calmar': None}

    # Guard: a single corrupt price bar yields a NaN pnl_pct that would
    # propagate through the equal-weighted daily-return series and null the
    # whole strategy's Sharpe/max_dd/return (NaN < 1e-9 is False, so the
    # std-floor never trips). Drop non-finite trades from metric computation —
    # loudly, so a recurring data hole stays visible rather than being masked.
    # (2026-06-15 BRK-B 2026-04-07 incident: 1 bad bar nulled 6 strategies.)
    _finite = [t for t in trades if _is_finite_pnl(t)]
    _n_dropped = len(trades) - len(_finite)
    if _n_dropped:
        _log(f'WARNING aggregate_metrics: dropped {_n_dropped}/{len(trades)} '
             f'trade(s) with non-finite pnl_pct (corrupt price bar?)')
    trades = _finite
    if not trades:
        return {'sharpe': None, 'max_dd_pct': 0.0, 'return_pct': 0.0,
                'total_trades': 0, 'hit_rate': None, 'avg_holding_days': None,
                'avg_pnl_pct': 0.0, 'sortino': None, 'calmar': None}

    pnl = np.array([t['pnl_pct'] for t in trades], dtype=float)
    avg_hold = float(np.mean([t['holding_days'] for t in trades]) or 1.0)
    mean_pnl = float(pnl.mean())
    hit_rate = float((pnl > 0).mean())

    # Portfolio metrics from daily-marked equity curve.
    daily_returns, _dates = _portfolio_daily_returns(trades)
    if len(daily_returns) == 0:
        return {
            'sharpe':           None,
            'max_dd_pct':       0.0,
            'return_pct':       0.0,
            'total_trades':     len(trades),
            'hit_rate':         round(hit_rate, 4),
            'avg_holding_days': round(avg_hold, 2),
            'avg_pnl_pct':      round(mean_pnl * 100.0, 4),
            'sortino':          None,
            'calmar':           None,
        }

    eq = np.cumprod(1.0 + daily_returns)
    roll_max = np.maximum.accumulate(eq)
    dd = (eq - roll_max) / roll_max
    max_dd = float(abs(dd.min()))
    return_pct = float((eq[-1] - 1.0) * 100.0)

    std_dr = float(daily_returns.std(ddof=1)) if len(daily_returns) > 1 else 0.0
    if std_dr < 1e-9:
        sharpe = None
    else:
        sharpe = float((daily_returns.mean() - RISK_FREE_DAILY) / std_dr *
                       math.sqrt(TRADING_DAYS_PER_YEAR))

    # Sortino ratio (annualized, target=0).
    # Convention: downside_dev = RMS of negative-only daily returns,
    # i.e. sqrt(mean(r_i**2)) over all r_i < 0. This is equivalent to
    # the full-N denominator form used by many practitioners (only negative
    # return days contribute; the denominator is the total number of
    # *negative* return days, not the total sample size). This differs from
    # the textbook semi-deviation which uses the full-N denominator. Both
    # conventions annualize by multiplying by sqrt(252).
    sortino: Optional[float] = None
    if len(daily_returns) >= 2:
        downside = daily_returns[daily_returns < 0.0]
        if len(downside) > 0:
            downside_dev = float(np.sqrt(np.mean(downside ** 2)))
            if downside_dev > 1e-9:
                sortino = float(daily_returns.mean() / downside_dev *
                                math.sqrt(TRADING_DAYS_PER_YEAR))

    # Calmar ratio: annualized return / max drawdown.
    # Annualized return derived from the equity curve's CAGR over the sample.
    calmar: Optional[float] = None
    if max_dd > 1e-9:
        n_days = len(daily_returns)
        if n_days > 0:
            annualized_return_pct = float((eq[-1] ** (TRADING_DAYS_PER_YEAR / n_days) - 1.0) * 100.0)
            calmar = float(annualized_return_pct / (max_dd * 100.0))

    return {
        'sharpe':           None if sharpe is None else round(sharpe, 4),
        'max_dd_pct':       round(max_dd * 100.0, 4),
        'return_pct':       round(return_pct, 4),
        'total_trades':     len(trades),
        'hit_rate':         round(hit_rate, 4),
        'avg_holding_days': round(avg_hold, 2),
        'avg_pnl_pct':      round(mean_pnl * 100.0, 4),
        'sortino':          None if sortino is None else round(sortino, 4),
        'calmar':           None if calmar is None else round(calmar, 4),
    }


def aggregate_per_regime(trades: list[dict], regimes: pd.Series) -> dict[str, dict]:
    """Group trades by entry_regime, compute aggregates per regime. Also
    counts oos_days_in_regime so the operator can see sample size context.
    """
    out: dict[str, dict] = {}
    by_regime: dict[str, list[dict]] = {r: [] for r in CANONICAL_REGIMES}
    for t in trades:
        r = t.get('entry_regime')
        if r in CANONICAL_REGIMES:
            by_regime[r].append(t)
    regime_day_counts = regimes.value_counts().to_dict()
    for regime, regime_trades in by_regime.items():
        agg = aggregate_metrics(regime_trades)
        agg['trade_count']        = agg.pop('total_trades')
        agg['oos_days_in_regime'] = int(regime_day_counts.get(regime, 0))
        # NULL the sharpe/sortino/calmar under low-sample regimes to avoid noise downstream
        if agg['trade_count'] < 5:
            agg['sharpe'] = None
            agg['sortino'] = None
            agg['calmar'] = None
        out[regime] = agg
    return out


# ── Per-bar simulation core (shared by run_backtest and universe_grid_cli) ────

def _per_bar_simulate(
    instance,
    close_wide: pd.DataFrame,
    bars_by_ticker: dict,
    regimes: pd.Series,
    start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
    *,
    strategy_id: Optional[str] = None,
    resolver=None,
    param_override=None,
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
    fill_model: Optional[str] = None,
    slippage_bps: float = 0.0,
    cost_bps_by_ticker: Optional[dict] = None,
    asset_gate: Optional[dict] = None,
) -> dict:
    """Single source-of-truth for the per-bar simulation loop.

    cost_bps_by_ticker: optional {ticker: one-way half-spread bps} overriding
    the flat ``slippage_bps`` per ticker (honest cost model, 2026-07-27).
    Tickers absent from the map fall back to ``slippage_bps``.

    asset_gate: optional {ticker: (long_ok, short_ok)} execution-eligibility
    map mirroring the live sizer's asset gate (tradable+active+ETB+fractionable;
    shorts additionally require shortable). Tickers ABSENT from the map pass —
    the snapshot is today's Alpaca universe, and absent means delisted/unknown
    historically, where the point-in-time resolver remains the authority.
    Gated entries are counted in the returned ``entries_asset_gated``.

    Encapsulates: min_lookback gate, regime_payload construction,
    generate_signals 4-arg→3-arg fallback, entry_regime tagging,
    stop/target sanity skips, simulate_trade, and per-bar universe-size
    tracking.

    fill_model: None (default) resolves via _default_fill_model() —
    'same_close' unless OPENCLAW_BT_FILL_MODEL overrides. 'same_close'
    (2026-07-29 same-day pivot) fills entry at close[t], the signal bar's
    own close, exit walk from t+1 — mirrors the live 15:00-ET-collect →
    submit-by-close chain. 'close' (legacy t+1) fills entry at close[t+1];
    'open' fills entry at open[t+1] and includes the fill bar in the exit
    walk (H/L of the fill bar are eligible for bracket exits, since they
    occur after the open fill). Raises ValueError on any other value.

    Returns a dict with keys:
      - trades: list[dict]
      - universe_sizes: list[int]  (non-empty only when resolver is not None)
      - days_processed: int
      - days_with_signals: int
      - static_universe: list[str]  (computed from close_wide columns)
      - min_lookback: int           (taken from instance attr or 20)

    ``instance`` must already have ``active_in_regimes`` set to cover all
    regimes (discovery mode); callers are responsible for this.
    ``strategy_id`` is only required when ``resolver`` is not None.
    """
    if fill_model is None:
        fill_model = _default_fill_model()
    if fill_model not in ('close', 'open', 'same_close'):
        raise ValueError(f'fill_model must be "close", "open" or "same_close", '
                         f'got {fill_model!r}')
    _price_col = 'open' if fill_model == 'open' else 'close'
    _include_fill_bar = fill_model == 'open'
    _same_day = fill_model == 'same_close'
    # 2026-07-05: corrected engine (true MTM) is now the STANDING DEFAULT —
    # ON unless explicitly disabled with =0 (escape hatch back to the
    # pre-fix smear-only engine). Was default-OFF (=='1') pre-cutover.
    _true_mtm = os.environ.get('OPENCLAW_TRUE_MTM_MARKS', '1') != '0'

    # Static universe: equity tickers only (no indices, crypto, futures).
    static_universe = [c for c in close_wide.columns
                       if not c.startswith('^') and '-USD' not in c and '=F' not in c]
    min_lookback = getattr(instance, 'min_lookback', 20)

    # Aux-data loader — strategies that need options/financials get it.
    try:
        from strategies.aux_data_loader import load_aux_data
    except Exception:
        load_aux_data = None

    oos_dates = close_wide.loc[start_dt:end_dt].index

    trades: list[dict] = []
    days_processed = 0
    days_with_signals = 0
    entries_asset_gated = 0
    bars_raised = 0
    first_raise: str | None = None
    # Track per-bar universe sizes when resolver is active.
    universe_sizes: list[int] = []
    # Within-run stop-out history: {ticker: last_stop_exit_date}. Built up
    # bar-by-bar from simulate_trade results and fed back into aux_data so
    # per-ticker cooldowns are scoped strictly to the CURRENT backtest run
    # (no cross-run DB contamination — see aux_data_loader._recent_stop_outs).
    run_stop_history: dict = {}

    # Exit-hook open-book path (spec §2). Only for instance.exit_hook=True;
    # every other strategy keeps simulate_trade-at-entry byte-identical.
    _use_open_book = bool(getattr(instance, 'exit_hook', False))
    if _use_open_book and fill_model == 'open':
        raise ValueError('exit_hook strategies support fill_model close/same_close only '
                         '(the open-fill bar-inclusion rule is not modelled in the open book)')
    _dt_priority = os.environ.get('OPENCLAW_BT_DOUBLE_TOUCH', 'stop')
    open_book: list = []
    hook_counters: dict = {}
    if _use_open_book:
        from backtest.open_book import OpenTrade, advance_open_book, resolve_hold_cap

    def _regime_payload_full(state, cur_d):
        return {
            'state':            (str(state) if state is not None and not pd.isna(state) else None),
            'date':             cur_d.isoformat(),
            'one_hot':          {r: (1.0 if r == state else 0.0) for r in CANONICAL_REGIMES},
            'transition_probs': {r1: {r2: (1.0 if r1 == r2 else 0.0) for r2 in CANONICAL_REGIMES}
                                 for r1 in CANONICAL_REGIMES},
        }

    for current_date in oos_dates:
        # Need at least min_lookback days of history before strategy can run
        prices_to_date = close_wide.loc[:current_date]
        if _use_open_book and open_book:
            _rs = regimes.get(current_date, None)
            _rp = _regime_payload_full(_rs, current_date.date() if hasattr(current_date, 'date') else current_date)
            _aux_ob = {'options': {}}
            if load_aux_data is not None:
                try:
                    _aux_ob = load_aux_data(current_date, strategy_id=strategy_id,
                                            run_stop_history=run_stop_history)
                except Exception:
                    _aux_ob = {'options': {}}
            for _ct in advance_open_book(open_book, current_date, bars_by_ticker, prices_to_date,
                                         _rp, _aux_ob, instance,
                                         dt_priority=_dt_priority, counters=hook_counters):
                if _ct['exit_reason'] == 'stop':
                    _sd = pd.Timestamp(_ct['exit_date'])
                    if run_stop_history.get(_ct['ticker']) is None or _sd > run_stop_history[_ct['ticker']]:
                        run_stop_history[_ct['ticker']] = _sd
                trades.append(_ct if _true_mtm else {**_ct, 'daily_marks': []})
        if len(prices_to_date) < min_lookback + 5:
            continue

        cur_d = current_date.date() if hasattr(current_date, 'date') else current_date
        regime_state = regimes.get(current_date, None)
        if regime_state is None or pd.isna(regime_state):
            continue

        # SP-2 Phase C: when resolver is set, replace the static universe with
        # a point-in-time resolved one for this specific bar.
        if resolver is not None:
            bar_universe = resolver.resolve(strategy_id, as_of=cur_d)
            universe_sizes.append(len(bar_universe))
        else:
            bar_universe = static_universe

        regime_payload = {
            'state':            str(regime_state),
            'date':             cur_d.isoformat(),
            'one_hot':          {r: (1.0 if r == regime_state else 0.0) for r in CANONICAL_REGIMES},
            'transition_probs': {r1: {r2: (1.0 if r1 == r2 else 0.0) for r2 in CANONICAL_REGIMES}
                                 for r1 in CANONICAL_REGIMES},
        }

        # Aux data is point-in-time-safe — loader keys by date.
        # ``run_stop_history`` carries within-run stop exits so per-ticker
        # cooldowns are scoped to THIS run only (prevents cross-run leakage).
        aux = {'options': {}}
        if load_aux_data is not None:
            try:
                aux = load_aux_data(
                    current_date,
                    strategy_id=strategy_id,
                    run_stop_history=run_stop_history,
                )
            except Exception:
                aux = {'options': {}}

        try:
            signals = instance.generate_signals(prices_to_date, regime_payload, bar_universe, aux_data=aux)
        except TypeError:
            try:
                signals = instance.generate_signals(prices_to_date, regime_payload, bar_universe)
            except Exception as _e:
                bars_raised += 1
                if first_raise is None:
                    first_raise = f'{type(_e).__name__}: {_e}'
                continue
        except Exception as _e:
            # Per-bar resilience is deliberate (one bad bar must not kill a
            # multi-hour walk) — but a strategy that raises on EVERY bar used
            # to masquerade as a clean 0-trade run (S_local_global_balance,
            # 2026-07-03: pandas-3 read-only ValueError swallowed for 70 min,
            # rc=0). Count + surface instead of vanishing.
            bars_raised += 1
            if first_raise is None:
                first_raise = f'{type(_e).__name__}: {_e}'
            continue

        days_processed += 1
        if not signals:
            continue
        days_with_signals += 1

        for sig in signals[:instance.MAX_SIGNALS]:
            direction = _signal_to_long_short(sig.direction)
            if direction == 0:
                continue
            ticker = sig.ticker
            if ticker not in bars_by_ticker:
                continue
            if asset_gate is not None:
                _long_ok, _short_ok = asset_gate.get(ticker, (True, True))
                if (direction > 0 and not _long_ok) or (direction < 0 and not _short_ok):
                    entries_asset_gated += 1
                    continue
            ticker_bars = bars_by_ticker[ticker]
            # Need the signal-day bar so we can use its close as the entry price
            if current_date not in ticker_bars.index:
                continue
            # Fill timing (2026-07-29 pivot): 'same_close' fills AT the signal
            # bar's close — signal[t] -> execute[t] — mirroring the live chain
            # (collect+signals ~15:00 ET, submit before the close; the intraday
            # execution search is abandoned, so the old t+1 lag modeled a day of
            # staleness with no live counterpart). 'close'/'open' keep the
            # legacy t+1 semantics for comparison runs. `ref` is the strategy's
            # intended price (signal-day close in practice — 127/140 strategies
            # set entry_price themselves); brackets are shaped around it, then
            # re-anchored to the actual fill (a no-op under same_close when the
            # strategy priced off the same close).
            ref = float(sig.entry_price) if (sig.entry_price and sig.entry_price > 0) \
                  else float(ticker_bars.loc[current_date, 'close'])
            stop_ref = float(sig.stop_loss) if (sig.stop_loss and sig.stop_loss > 0) \
                       else (ref * 0.93 if direction > 0 else ref * 1.07)
            target_ref = float(sig.target_1) if (sig.target_1 and sig.target_1 > 0) \
                         else (ref * 1.08 if direction > 0 else ref * 0.92)
            # The exit walk always starts strictly after the fill bar's close,
            # so ≥1 future bar is required in every model (under same_close a
            # final-bar entry would be a zero-information open trade — skip).
            _future_idx = ticker_bars.index[ticker_bars.index > current_date]
            if len(_future_idx) == 0:
                continue  # signal on the last available bar
            fill_date = current_date if _same_day else _future_idx[0]
            entry_price = float(ticker_bars.loc[fill_date, _price_col])
            # A corrupt price bar (NaN fill price) cannot fill an order at a
            # known price. Skip rather than emit a NaN-entry trade whose NaN
            # pnl_pct would poison the aggregate metrics. (2026-06-15 BRK-B
            # 2026-04-07 incident: one NaN bar nulled 6 strategies' Sharpe.)
            if not math.isfinite(entry_price):
                _log(f'skip {ticker} fill {fill_date.date() if hasattr(fill_date, "date") else fill_date}: '
                     f'non-finite {_price_col} (corrupt price bar)')
                continue
            stop_loss, target_1 = _reanchor_bracket(
                ref=ref, entry_price=entry_price, direction=direction,
                stop_ref=stop_ref, target_ref=target_ref)
            _ov = regime_param_override.resolve_override(
                strategy_id, str(regime_state), injected=param_override)
            if _ov:
                stop_loss, target_1 = regime_param_override.apply_override(
                    entry_price=entry_price, direction=direction,
                    stop_loss=stop_loss, target_1=target_1, override=_ov)
            # Defensive: a bad signal with stop/target on the wrong side of
            # entry will produce a guaranteed-loss trade. Skip rather than
            # carry the bug through.
            if direction > 0 and (stop_loss >= entry_price or target_1 <= entry_price):
                continue
            if direction < 0 and (stop_loss <= entry_price or target_1 >= entry_price):
                continue
            _tkr_bps = (cost_bps_by_ticker.get(ticker, slippage_bps)
                        if cost_bps_by_ticker is not None else slippage_bps)
            if _use_open_book:
                _s = float(_tkr_bps) / 10000.0
                open_book.append(OpenTrade(
                    ticker=ticker, direction=direction, entry_date=fill_date,
                    entry_price=entry_price, entry_fill=entry_price * (1.0 + direction * _s),
                    stop_loss=stop_loss, target_1=target_1,
                    hold_cap=resolve_hold_cap(getattr(sig, 'signal_params', None), max_hold_days),
                    entry_regime=str(regime_state),
                    signal_params=dict(getattr(sig, 'signal_params', None) or {}),
                    slippage=_s, prev_mark=entry_price * (1.0 + direction * _s)))
                continue
            exit_info = simulate_trade(ticker_bars, fill_date, direction,
                                       entry_price, stop_loss, target_1, max_hold_days,
                                       include_entry_bar=_include_fill_bar,
                                       slippage_bps=_tkr_bps)
            # Record stop exits in within-run history so future bars'
            # per-ticker cooldown can suppress same-ticker re-fires. Keep
            # the LATEST stop date per ticker.
            if exit_info.get('exit_reason') == 'stop':
                try:
                    _stop_dt = pd.Timestamp(exit_info['exit_date'])
                    _prev = run_stop_history.get(ticker)
                    if _prev is None or _stop_dt > _prev:
                        run_stop_history[ticker] = _stop_dt
                except Exception:
                    pass
            trades.append({
                'ticker':         ticker,
                'direction':      'long' if direction > 0 else 'short',
                'entry_date':     fill_date.date() if hasattr(fill_date, 'date') else fill_date,
                'entry_price':    entry_price,
                'exit_date':      exit_info['exit_date'].date() if hasattr(exit_info['exit_date'], 'date') else exit_info['exit_date'],
                'exit_price':     exit_info['exit_price'],
                'exit_reason':    exit_info['exit_reason'],
                'holding_days':   exit_info['holding_days'],
                'pnl_pct':        exit_info['pnl_pct'],
                'entry_regime':   str(regime_state),
                'signal_stop':    stop_loss,
                'signal_target':  target_1,
                'daily_marks':    exit_info.get('daily_marks', []) if _true_mtm else [],
            })

    if _use_open_book and open_book:
        for _dt in close_wide.index[close_wide.index > end_dt]:
            if not open_book:
                break
            _rs = regimes.get(_dt, None)
            _rp = _regime_payload_full(_rs, _dt.date())
            _aux_dr = {'options': {}}
            if load_aux_data is not None:
                try:
                    _aux_dr = load_aux_data(_dt, strategy_id=strategy_id,
                                            run_stop_history=run_stop_history)
                except Exception:
                    _aux_dr = {'options': {}}
            for _ct in advance_open_book(open_book, _dt, bars_by_ticker, close_wide.loc[:_dt],
                                         _rp, _aux_dr, instance,
                                         dt_priority=_dt_priority, counters=hook_counters):
                trades.append(_ct if _true_mtm else {**_ct, 'daily_marks': []})
        for _t in open_book:   # ticker has no bar at all after entry
            if _t.holding_days != 0:
                # Reaching here with stepped bars means the invariant
                # "a ticker's bars are a subset of close_wide.index" is broken:
                # the drain loop walks close_wide dates, so a trade that was
                # advanced on some of them and is STILL open has bars the drain
                # cannot see. exit_price/pnl_pct below are then wrong (they
                # assume an unstepped trade), hence the warning rather than a
                # silent flat close.
                print(f'[WARN] open-book flush: {_t.ticker} still open after the drain with '
                      f'holding_days={_t.holding_days} — ticker bars are not a subset of '
                      f'close_wide.index; closing flat at entry', file=sys.stderr)
            trades.append({'ticker': _t.ticker, 'direction': 'long' if _t.direction > 0 else 'short',
                           'entry_date': _t.entry_date.date(), 'entry_price': _t.entry_price,
                           'exit_date': _t.entry_date.date(), 'exit_price': _t.entry_price,
                           'exit_reason': 'end_of_data', 'holding_days': _t.holding_days,
                           'pnl_pct': 0.0,
                           'entry_regime': _t.entry_regime, 'signal_stop': _t.stop_loss,
                           'signal_target': _t.target_1,
                           'daily_marks': list(_t.daily_marks) if _true_mtm else []})
        open_book.clear()
    if hook_counters.get('hook_exits') or hook_counters.get('hook_raised'):
        _log(f'exit hook: {hook_counters.get("hook_exits", 0)} hook exits, '
             f'{hook_counters.get("hook_raised", 0)} hook errors'
             + (f' (first: {hook_counters["first_hook_raise"]})' if hook_counters.get('first_hook_raise') else ''))
    if bars_raised:
        total_bars = days_processed + bars_raised
        print(f'[WARN] generate_signals raised on {bars_raised}/{total_bars} bars '
              f'(first: {first_raise}) — 0-trade results are NOT trustworthy '
              f'if this is a large share', file=sys.stderr)
    if entries_asset_gated:
        _log(f'asset gate: skipped {entries_asset_gated} entries on execution-ineligible '
             f'symbols (non-ETB/non-shortable/non-fractionable per today\'s Alpaca universe)')

    return {
        'trades':           trades,
        'universe_sizes':   universe_sizes,
        'days_processed':   days_processed,
        'days_with_signals': days_with_signals,
        'entries_asset_gated': entries_asset_gated,
        'bars_raised':      bars_raised,
        'static_universe':  static_universe,
        'min_lookback':     min_lookback,
        'hook_exits':       hook_counters.get('hook_exits', 0),
        'hook_raised':      hook_counters.get('hook_raised', 0),
        'first_hook_raise': hook_counters.get('first_hook_raise'),
    }


def _simulate_for(instrument_class: str):
    """SP-4 Phase 0 dispatch: pick the simulate fn for an instrument_class.
    Only 'option' diverges; everything else uses the existing equity path."""
    if instrument_class == 'option':
        return options_backtest.simulate
    return _per_bar_simulate


def _resolve_instrument_class(strategy_id: str, filepath: Optional[str] = None) -> str:
    """Resolve a strategy's instrument_class for backtest dispatch.

    Precedence: (1) manifest ``strategies[strategy_id].instrument_class`` — the
    authoritative source the lifecycle promotion gate reads — accepted only if
    in VALID_INSTRUMENT_CLASSES; (2) a module-level ``INSTRUMENT_CLASS`` const in
    *filepath* (covers a freshly-coded --strategy-file not yet in the manifest),
    via lifecycle._detect_module_instrument_class; (3) 'equity'. Never raises.
    """
    try:
        manifest_path = ROOT / 'src' / 'strategies' / 'manifest.json'
        entry = (json.loads(manifest_path.read_text()).get('strategies', {})
                 .get(strategy_id) or {})
        ic = entry.get('instrument_class')
        if ic in VALID_INSTRUMENT_CLASSES:
            return ic
    except Exception:
        pass
    if filepath:
        detected = _detect_module_instrument_class(filepath)
        if detected:
            return detected
    return 'equity'


# ── Main run ─────────────────────────────────────────────────────────────────

def _configured_max_hold_days(strategy_id: str) -> int:
    """Strategy-configured hold horizon for backtests (2026-07-14 operator
    directive: max_hold is strategy config and is BAKED INTO every backtest —
    curation and adjustment re-tests alike — unless the caller pins one
    explicitly). Reads the per-regime strategy_regime_params rows through the
    live resolver (coupling writes one value across all eligible regimes; MAX
    of the non-null per-regime values decides the single simulate horizon).
    DEFAULT_MAX_HOLD_DAYS when unset, on lookup failure (logged), or when the
    coupling gate is OFF (byte-identical legacy, mirrors the stop/target
    override gating)."""
    from execution import regime_param_resolver as rpr
    return rpr.configured_max_hold_days(strategy_id, default=DEFAULT_MAX_HOLD_DAYS, log=_log)


def _default_fill_model() -> str:
    """Standing fill-timing default for every backtest that doesn't pin one.

    2026-07-29 operator directive: the intraday-execution search is abandoned,
    so signal[t] -> submission[t+1] modeled a day of signal staleness with no
    live counterpart. The live chain returns to same-day execution (collect +
    signals ~15:00 ET, submit on completion, fill by the close), and the
    backtest mirrors it as 'same_close': entry at the signal bar's close.
    OPENCLAW_BT_FILL_MODEL is the escape hatch ('close'/'open' = legacy t+1)."""
    return os.environ.get('OPENCLAW_BT_FILL_MODEL', 'same_close')


def _bounded_resolver(strategy_id: str, *, manifest_path=None, data_dir=None,
                      cap_override: Optional[str] = None):
    """Universe ladder campaign W6: when the manifest sets
    metadata.backtest_universe_cap = <ladder tier>, bound the strategy's
    BACKTEST universe to that tier via the newest frozen membership artifact
    (PrecomputedResolver — point-in-time, zero DB). For strategies whose
    full-universe sim cannot complete on this box (S_ivol: per-bar subsets of
    the 12,536-col panel OOM the 8GB host), the bounded run IS the shrink
    baseline — start at tier_liquid, then shrink down the ladder. Returns
    None when no cap is set (byte-identical legacy behavior)."""
    manifest_path = Path(manifest_path or ROOT / 'src' / 'strategies' / 'manifest.json')
    data_dir = Path(data_dir or ROOT / 'data')
    if cap_override:
        # Explicit cap from the caller (--universe-cap): wins over the manifest
        # and needs no manifest entry — this is how a NEW candidate's FIRST
        # backtest is bounded before it has been registered (operator directive
        # 2026-08-10 after the uncapped 12,548-ticker run OOM-killed the
        # Saturday finisher).
        cap = cap_override
    else:
        try:
            entry = (json.loads(manifest_path.read_text()).get('strategies', {})
                     .get(strategy_id) or {})
            cap = (entry.get('metadata') or {}).get('backtest_universe_cap')
        except Exception:
            return None
    if not cap:
        return None
    arts = (sorted(data_dir.glob('universe_tier_membership_shrink-*.parquet'))
            or sorted(data_dir.glob('universe_tier_membership_*.parquet')))
    if not arts:
        _log(f'WARNING universe cap {cap!r} set for {strategy_id} but no '
             'membership artifact exists — falling back to the static universe')
        return None
    from backtest.precomputed_resolver import PrecomputedResolver
    _log(f'universe cap: {strategy_id} bounded to {cap} via {arts[-1].name}')
    return PrecomputedResolver(arts[-1], cap)


def run_backtest(strategy_id: str, *,
                 filepath: Optional[str] = None,
                 start_date: str = DEFAULT_START_DATE,
                 end_date: Optional[str] = None,
                 max_hold_days: Optional[int] = None,
                 conn: Optional[psycopg2.extensions.connection] = None,
                 commit: bool = True,
                 resolver=None,
                 param_override=None,
                 return_metrics: bool = False,
                 instrument_class: str = 'equity',
                 fill_model: Optional[str] = None,
                 universe_cap: Optional[str] = None,
                 generate_tearsheet: bool = True) -> str:
    """Execute the unified backtest for one strategy. Returns the run_id (UUID).

    Side effect: writes one row to strategy_backtest_runs, up to 4 rows
    to strategy_backtest_regimes (one per regime that produced trades),
    and N rows to strategy_backtest_trades.

    SP-2 Phase C: optional ``resolver`` kwarg. When set (not None), the per-bar
    universe is resolved via ``resolver.resolve(strategy_id, as_of=cur_d)``
    instead of the static universe computed once from close_wide columns.
    The mean of per-bar universe sizes is stored in ``_universe_sizes_out``
    on the resolver object after the run, and also returned via the
    ``run_backtest_grid`` wrapper. When resolver is None the function is
    byte-identical to the pre-resolver implementation.

    max_hold_days=None (the default) resolves the STRATEGY-CONFIGURED horizon
    from strategy_regime_params (2026-07-14 operator directive: config max_hold
    is baked into every backtest); pass an int to pin it (coupling candidates do).

    generate_tearsheet=True is the default VALUE of the parameter, but the
    in-process tearsheet subprocess (see below) is now OPT-IN at the env
    layer: it only fires when generate_tearsheet is True AND env
    OPENCLAW_BT_TEARSHEET == '1' (default unset ⇒ no spawn). Review finding
    2026-08-24 (five-repo-adoptions final fix wave, I1): the nightly fleet
    re-gate (scripts/refresh_backtests_resumable.js, scripts/rebacktest_
    runner.py, scripts/backtest_ids.js) drives this module through the
    SINGLE-STRATEGY --strategy-id CLI path, one subprocess per strategy — the
    pre-existing "--all-live only" suppression never covered that fan-out, so
    a 140-strategy fleet re-gate would have spawned 140 serialized
    matplotlib/quantstats children on this 8GB no-swap box regardless. The
    --all-live CLI path *also* still passes generate_tearsheet=False
    explicitly (belt-and-suspenders — kept so --all-live never fires even if
    an operator sets the env to '1'). Operators generate any strategy's
    tearsheet on demand via `OPENCLAW_BT_TEARSHEET=1` or directly via
    `scripts/generate_tearsheet.py --strategy <sid>` / `--run-id <run_id>`.
    """
    if max_hold_days is None:
        max_hold_days = _configured_max_hold_days(strategy_id)
    filepath = filepath or find_strategy_file(strategy_id)
    if not filepath:
        raise FileNotFoundError(f'no implementation file for {strategy_id}')
    strategy_cls = load_strategy_class(filepath)
    _log(f'loaded {strategy_cls.__name__} from {Path(filepath).resolve().relative_to(ROOT)}')
    _cost_bps = resolve_cost_model_bps(instrument_class)
    # 2026-07-05: always-adverse slippage is now the STANDING DEFAULT — ON
    # unless explicitly disabled with =0 (escape hatch). Was default-OFF
    # (=='1') pre-cutover.
    _slippage_on = os.environ.get('OPENCLAW_BACKTEST_SLIPPAGE', '1') != '0'
    _slippage_bps = _cost_bps if _slippage_on else 0.0
    _log(f'instrument_class={instrument_class} cost_model_bps={_cost_bps} slippage_applied={_slippage_on}')

    # Discovery mode: bypass should_run() by widening active_in_regimes on
    # the instance. Strategies that branch on regime['state'] *inside*
    # generate_signals still see the live regime; they just don't get to
    # early-exit on it.
    instance = strategy_cls()
    instance.active_in_regimes = list(CANONICAL_REGIMES)

    # Universe ladder campaign W6 (2026-07-21): a manifest
    # metadata.backtest_universe_cap bounds this strategy's BACKTEST universe
    # to a ladder tier. Only consulted when the caller passed no resolver —
    # explicit resolvers (grid cells, coupling overrides) always win.
    # universe_cap (2026-08-10) is the caller-supplied override for strategies
    # not yet in the manifest (a new candidate's first backtest).
    if resolver is None:
        resolver = _bounded_resolver(strategy_id, cap_override=universe_cap)

    close_wide, bars_by_ticker = load_prices_panels(calendar=_calendar_for(instrument_class))
    regimes = load_regimes()
    _log(f'prices: {close_wide.shape[0]} dates × {close_wide.shape[1]} tickers; '
         f'regimes: {len(regimes)} days')

    start_dt = pd.Timestamp(start_date)
    end_dt   = pd.Timestamp(end_date) if end_date else close_wide.index.max()

    _sim_fn = _simulate_for(instrument_class)
    _sim_kwargs: dict = dict(
        strategy_id=strategy_id,
        resolver=resolver,
        param_override=param_override,
        max_hold_days=max_hold_days,
    )
    # fill_model is threaded to _per_bar_simulate ONLY — options_backtest.simulate
    # does not accept it (option brackets are priced contracts, not open/close fills).
    fill_model = fill_model or _default_fill_model()
    if _sim_fn is _per_bar_simulate:
        _sim_kwargs['fill_model'] = fill_model
        _sim_kwargs['slippage_bps'] = _slippage_bps
        # Honest cost model (2026-07-27): per-ticker half-spread slippage + the
        # live asset-eligibility gate, equity/etp only. Both fail back to the
        # flat/ungated behavior loudly, never fatally.
        if _slippage_on and instrument_class in ('equity', 'etp'):
            _cost_map = load_ticker_cost_bps()
            if _cost_map:
                _sim_kwargs['cost_bps_by_ticker'] = _cost_map
                _vals = sorted(_cost_map.values())
                _log(f'spread-cost model: {len(_cost_map)} tickers '
                     f'(median {_vals[len(_vals)//2]:.1f}bps, max {_vals[-1]:.1f}bps); '
                     f'fallback flat {_slippage_bps}bps for unmapped')
        if instrument_class in ('equity', 'etp'):
            _gate_map = load_bt_asset_gate()
            if _gate_map:
                _sim_kwargs['asset_gate'] = _gate_map
                _n_no_long = sum(1 for lo, so in _gate_map.values() if not lo)
                _n_no_short = sum(1 for lo, so in _gate_map.values() if not so)
                _log(f'asset gate ({os.environ.get("OPENCLAW_BT_ASSET_GATE", "parity")}): '
                     f'{len(_gate_map)} symbols mapped; {_n_no_long} long-blocked, '
                     f'{_n_no_short} short-blocked')
    sim = _sim_fn(
        instance, close_wide, bars_by_ticker, regimes, start_dt, end_dt,
        **_sim_kwargs,
    )
    trades         = sim['trades']
    universe_sizes = sim['universe_sizes']
    days_processed = sim['days_processed']
    days_with_signals = sim['days_with_signals']
    universe       = sim['static_universe']
    min_lookback   = sim['min_lookback']

    _log(f'simulation: {days_processed} active days, {days_with_signals} signal days, {len(trades)} trades')

    # SP-2 Phase C: store mean universe size on resolver for caller retrieval.
    if resolver is not None and universe_sizes:
        resolver._universe_sizes_out = universe_sizes
    elif resolver is not None:
        resolver._universe_sizes_out = []

    # ── Aggregate metrics ────────────────────────────────────────────────
    total_metrics = aggregate_metrics(trades)
    per_regime    = aggregate_per_regime(trades, regimes.loc[start_dt:end_dt])

    # ── Persist ──────────────────────────────────────────────────────────
    own_conn = False
    if conn is None:
        uri = os.environ.get('POSTGRES_URI')
        if not uri:
            raise RuntimeError('POSTGRES_URI not set')
        conn = psycopg2.connect(uri)
        own_conn = True

    run_id = str(uuid.uuid4())
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO strategy_backtest_runs
              (run_id, strategy_id, code_sha, window_kind, start_date, end_date,
               oos_days, total_sharpe, total_max_dd_pct, total_return_pct,
               total_trades, total_hit_rate, avg_holding_days, primary_window,
               config_json, notes,
               total_sortino, total_calmar, total_avg_pnl_pct)
            VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s, %s,%s,%s)
        """, (
            run_id, strategy_id, _code_sha(filepath), 'full_history',
            start_dt.date(), end_dt.date(), int((end_dt - start_dt).days + 1),
            total_metrics['sharpe'], total_metrics['max_dd_pct'],
            total_metrics['return_pct'], total_metrics['total_trades'],
            total_metrics['hit_rate'], total_metrics['avg_holding_days'],
            True,
            json.dumps({
                'max_hold_days':  max_hold_days,
                'min_lookback':   min_lookback,
                'start_date':     start_dt.date().isoformat(),
                'end_date':       end_dt.date().isoformat(),
                'universe_size':  (round(sum(universe_sizes) / len(universe_sizes), 2)
                                   if resolver is not None and universe_sizes
                                   else len(universe)),
                'methodology':    'discovery',
                # Honest-cost provenance (2026-07-27): distinguishes this epoch
                # from pre-cost canonical rows. spread_v1 = per-ticker half-spread
                # artifact; flat = INSTRUMENT_COST_BPS fallback.
                'cost_model': ('spread_v1' if _sim_kwargs.get('cost_bps_by_ticker')
                               else f'flat_{_slippage_bps}bps'),
                'asset_gate': (os.environ.get('OPENCLAW_BT_ASSET_GATE', 'parity')
                               if _sim_kwargs.get('asset_gate') else 'off'),
                'double_touch': os.environ.get('OPENCLAW_BT_DOUBLE_TOUCH', 'stop'),
                'exit_hook':   bool(getattr(instance, 'exit_hook', False)),
                'hook_exits':  int(sim.get('hook_exits', 0)),
                # Persisted, not just logged: a run whose hook raised on every
                # bar has the same trade list as one whose hook never fired
                # (spec §1: raise => hold), so without this the two are
                # indistinguishable once the journal rolls.
                'hook_raised': int(sim.get('hook_raised', 0)),
                # Fill-timing provenance (2026-07-29 same-day pivot):
                # same_close = signal[t] fills at close[t]; close/open = legacy t+1.
                'fill_model': fill_model,
            }),
            None,
            total_metrics['sortino'], total_metrics['calmar'], total_metrics['avg_pnl_pct'],
        ))
        # Advisory sleeve tail stats (task P3+R3, 2026-08-24 five-repo-
        # adoptions): CVaR(5%) + a raw per-trade Sortino per regime sleeve,
        # computed from that sleeve's own pnl_pct series (the `trades` list
        # already in scope here — no extra query needed). Best-effort: a
        # failure must never fail the backtest, hence the blanket try/except.
        # Written to distinctly-named columns (cvar_5, tail_sortino) rather
        # than overwriting the pre-existing `sortino` column (migration 135,
        # a different annualized/portfolio-based metric already read by the
        # dashboard) — see migration 148's header comment for the full
        # rationale. Never a gate/sizing/promotion input.
        _tail_stats_by_regime: dict[str, dict] = {}
        try:
            from backtest.tail_stats import sleeve_tail_stats
            _by_regime_pnl: dict[str, list] = {r: [] for r in CANONICAL_REGIMES}
            for t in trades:
                _r = t.get('entry_regime')
                _pnl = t.get('pnl_pct')
                # Drop non-finite pnl_pct the same way aggregate_metrics does
                # (2026-06-15 BRK-B incident: one corrupt price bar must not
                # poison a sleeve-wide stat with a NaN/inf).
                if _r in _by_regime_pnl and _pnl is not None and math.isfinite(_pnl):
                    _by_regime_pnl[_r].append(float(_pnl))
            for _regime, _pnl_list in _by_regime_pnl.items():
                _tail_stats_by_regime[_regime] = sleeve_tail_stats(_pnl_list)
        except Exception as _e:
            _log(f'[tail_stats] skipped: {type(_e).__name__}: {_e}')

        # Benchmark-relative promotion criterion (task R1, 2026-08-24
        # five-repo-adoptions): regime-conditioned SPY Sharpe baseline,
        # computed ONCE per run (cached in this dict, not per-regime-row) via
        # src/backtest/benchmark_baseline.py. Feeds
        # strategy_backtest_regimes.benchmark_sharpe (migration 149), read by
        # regime_qualification.qualifies_regime (python) and
        # promotion_service.js judgeRegimeSleeve (JS) as the excess-Sharpe-
        # over-benchmark gate leg. try/except non-fatal — a missing/broken
        # benchmark must never fail a backtest run (fail-open contract; see
        # benchmark_baseline module docstring). regime_benchmark_sharpe
        # itself also fails open (returns {} / per-regime None), so this
        # try/except only guards the import + call plumbing.
        _benchmark_sharpe_by_regime: dict[str, float] = {}
        try:
            from backtest.benchmark_baseline import regime_benchmark_sharpe
            _benchmark_sharpe_by_regime = regime_benchmark_sharpe(start_dt, end_dt) or {}
        except Exception as _e:
            _log(f'[bench_baseline] persist skipped: {_e}')

        # 2026-05-19: always write a row per canonical regime, even when
        # the strategy produced 0 trades in that regime. The dashboard's
        # per-regime BT Sharpe view (renderBacktestRegimeBreakdown) reads
        # one breakdown row per (strategy, regime) — pre-fix we only wrote
        # regimes with trades, so candidates whose strategy only fires in
        # 1-2 regimes silently lost their "By Regime" cell coverage on the
        # rest. Matches the auto_backtest.py behaviour shipped in ea5fafa.
        # Zero-trade regimes get trade_count=0 + NULLs for the metric
        # columns (schema allows it; only trade_count is NOT NULL).
        regime_rows = []
        for regime in CANONICAL_REGIMES:
            agg = per_regime.get(regime, {})
            n_trades = int(agg.get('trade_count', 0) or 0)
            _tail = _tail_stats_by_regime.get(regime) or {}
            _cvar_5 = _tail.get('cvar_5')
            _tail_sortino = _tail.get('sortino')
            _bench_sharpe = _benchmark_sharpe_by_regime.get(regime)
            if n_trades == 0:
                regime_rows.append((
                    run_id, regime,
                    0, None, None, None, None, None, None,
                    int(agg.get('oos_days_in_regime') or 0),
                    None, None,
                    _cvar_5, _tail_sortino, _bench_sharpe,
                ))
                continue
            regime_rows.append((
                run_id, regime,
                agg['trade_count'], agg['sharpe'], agg['max_dd_pct'],
                agg['return_pct'], agg['hit_rate'], agg['avg_pnl_pct'],
                agg['avg_holding_days'], agg['oos_days_in_regime'],
                agg.get('sortino'), agg.get('calmar'),
                _cvar_5, _tail_sortino, _bench_sharpe,
            ))
        if regime_rows:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO strategy_backtest_regimes
                  (run_id, regime_state, trade_count, sharpe, max_dd_pct,
                   return_pct, hit_rate, avg_pnl_pct, avg_holding_days,
                   oos_days_in_regime, sortino, calmar, cvar_5, tail_sortino,
                   benchmark_sharpe)
                VALUES %s
            """, regime_rows)
        if trades:
            trade_rows = [(
                run_id, i + 1, strategy_id, t['ticker'], t['direction'],
                t['entry_date'], t['entry_price'], t['exit_date'],
                t['exit_price'], t['exit_reason'], t['pnl_pct'],
                t['holding_days'], t['entry_regime'], t['signal_stop'],
                t['signal_target'],
            ) for i, t in enumerate(trades)]
            psycopg2.extras.execute_values(cur, """
                INSERT INTO strategy_backtest_trades
                  (run_id, trade_seq, strategy_id, ticker, direction,
                   entry_date, entry_price, exit_date, exit_price,
                   exit_reason, pnl_pct, holding_days, entry_regime,
                   signal_stop, signal_target)
                VALUES %s
            """, trade_rows, page_size=500)

        # Demote prior primary_window=true for this strategy so dashboard
        # / sizer / eligibility reads converge on the new run as the single
        # source of truth. The prior rows stay (audit history) but get
        # primary_window=false.
        cur.execute("""
            UPDATE strategy_backtest_runs
            SET primary_window = FALSE
            WHERE strategy_id = %s AND run_id <> %s
        """, (strategy_id, run_id))

        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()

    _log(f'wrote run_id={run_id}  total_sharpe={total_metrics["sharpe"]}  '
         f'trades={total_metrics["total_trades"]}  '
         f'regimes={list(per_regime.keys())}')
    # Refresh the dashboard backtest panel for this strategy (best-effort;
    # a panel build failure must never fail the backtest itself). Skipped for
    # ephemeral (commit=False) runs — the persist block rolled back, so there
    # is no new run for the panel to read.
    if commit:
        try:
            from backtest.backtest_panel import rebuild as _rebuild_panel
            _rebuild_panel(strategy_id)
        except Exception as _e:
            print(f'[unified_backtest] panel rebuild skipped: {_e}')
        # Best-effort per-run tearsheet (task P3+R3, 2026-08-24). A subprocess
        # with a hard timeout so a hung/slow render can never wedge the
        # backtest (2-core box).
        #
        # OPT-IN as of the 2026-08-24 final fix wave (review finding I1): this
        # in-process hook now fires ONLY when OPENCLAW_BT_TEARSHEET == '1'
        # (default unset ⇒ no spawn), AND ONLY when generate_tearsheet is
        # True. The original design gated solely on generate_tearsheet
        # (default True, --all-live explicitly passes False) on the theory
        # that only the --all-live fleet CLI path ran at fleet scale — but the
        # ACTUAL nightly fleet re-gate (scripts/refresh_backtests_resumable.js
        # -> `-m backtest.unified_backtest --strategy-id <sid>` per strategy;
        # also scripts/rebacktest_runner.py, scripts/backtest_ids.js) drives
        # the SINGLE-STRATEGY path, where generate_tearsheet still defaults
        # True. That would have serialized ~140 5-180s matplotlib/quantstats
        # children across a 140-strategy re-gate regardless of the
        # --all-live suppression. Flipping the env default to OFF closes that
        # gap for every caller at once, single-strategy or fleet, without
        # touching the fleet runner scripts themselves. research-orchestrator.
        # js now fires its OWN out-of-band tearsheet subprocess after this
        # call returns (see I2 below) instead of relying on this in-process
        # hook, so the two never double-fire under normal operation.
        #
        # I2 (2026-08-24, also review): this in-process hook runs BEFORE
        # run_backtest returns, i.e. inside a caller's own timeout budget for
        # the whole run_backtest() call (research-orchestrator.js bounds its
        # single `python3 -m backtest.unified_backtest ...` spawn to 900s). A
        # SIGTERM landing during tearsheet render would make such a caller
        # record the run as failed even though the run row was already
        # committed durably. Opt-in-only removes this risk for the
        # orchestrator's normal path (env unset there); an operator who sets
        # OPENCLAW_BT_TEARSHEET=1 to render inline accepts that the tearsheet
        # subprocess's own 180s timeout now shares whatever timeout budget
        # wraps run_backtest() in their calling context.
        #
        # The --all-live CLI path still ALSO passes generate_tearsheet=False
        # explicitly (kept, belt-and-suspenders) — at ~140 strategies, a
        # serialized 5-180s matplotlib/quantstats child per strategy is
        # memory-unbounded and risks the nightly fleet window's real bound
        # (RuntimeMaxSec SIGKILL) on this 8GB no-swap box even if an operator
        # sets the env to '1'. Operators can generate a fleet strategy's
        # tearsheet on demand via `scripts/generate_tearsheet.py --strategy
        # <sid>` (no env needed — that script is a standalone invocation, not
        # this in-process hook).
        #
        # commit=False runs (this whole block is inside `if commit:`) are
        # skipped here, period — this is NOT the same claim as "there is no
        # persisted run for the tearsheet to read": some commit=False callers
        # (e.g. src/execution/backtest_coupled_recs.py's apply path) commit
        # the run externally at their own call site and even redo the panel
        # rebuild for exactly that reason. Those callers get no tearsheet
        # from this in-process hook either way — a known follow-up gap, not
        # a case of "nothing was persisted".
        if generate_tearsheet and os.environ.get('OPENCLAW_BT_TEARSHEET') == '1':
            try:
                _tear = subprocess.run(
                    [sys.executable or 'python3',
                     str(ROOT / 'scripts' / 'generate_tearsheet.py'),
                     '--run-id', run_id],
                    cwd=str(ROOT), capture_output=True, text=True, timeout=180,
                )
                for _line in (_tear.stdout or '').splitlines():
                    _log(f'[tearsheet] {_line}')
                if _tear.returncode != 0:
                    _log(f'[tearsheet] exit={_tear.returncode} '
                         f'stderr={(_tear.stderr or "")[-500:]}')
            except Exception as _e:
                _log(f'[tearsheet] skipped: {type(_e).__name__}: {_e}')
    if return_metrics:
        import statistics
        sd = [abs(t['entry_price'] - t['signal_stop']) / t['entry_price']
              for t in trades if t.get('signal_stop') and t.get('entry_price')]
        td = [abs(t['signal_target'] - t['entry_price']) / t['entry_price']
              for t in trades if t.get('signal_target') and t.get('entry_price')]
        total_metrics = {**total_metrics,
                         'median_stop_pct':   (statistics.median(sd) if sd else None),
                         'median_target_pct': (statistics.median(td) if td else None)}
        return run_id, total_metrics
    return run_id


# ── CLI ──────────────────────────────────────────────────────────────────────

def _all_live_strategies() -> list[str]:
    manifest_path = ROOT / 'src' / 'strategies' / 'manifest.json'
    m = json.loads(manifest_path.read_text())
    return [sid for sid, entry in (m.get('strategies') or {}).items()
            if entry.get('state') in ('live', 'candidate', 'staging')]


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--strategy-id', help='Run backtest for one strategy_id')
    g.add_argument('--strategy-file', help='Run backtest for a .py file directly')
    g.add_argument('--all-live', action='store_true',
                   help='Run for all live + candidate + staging strategies')
    ap.add_argument('--start-date', default=DEFAULT_START_DATE)
    ap.add_argument('--end-date',   default=None)
    ap.add_argument('--max-hold-days', type=int, default=None,
                    help='Pin the hold horizon; default resolves each strategy\'s '
                         'configured max_hold from strategy_regime_params '
                         f'(falls back to {DEFAULT_MAX_HOLD_DAYS}).')
    ap.add_argument('--universe-cap', default=None,
                    help='Bound the backtest universe to a ladder tier (e.g. '
                         'tier_liquid) regardless of manifest metadata — the '
                         'way a NEW candidate\'s first backtest is bounded '
                         'before registration. Manifest metadata.backtest_'
                         'universe_cap still applies when this is unset.')
    args = ap.parse_args()

    if args.all_live:
        sids = _all_live_strategies()
        _log(f'running {len(sids)} strategies')
        _log('[tearsheet] skipped for --all-live fleet run (per-strategy '
             'generation would serialize ~140 5-180s matplotlib/quantstats '
             'children on this 8GB no-swap box); generate on demand via '
             '`scripts/generate_tearsheet.py --strategy <sid>`')
        ok = 0; fail = 0
        for sid in sids:
            try:
                run_backtest(sid,
                             start_date=args.start_date, end_date=args.end_date,
                             max_hold_days=args.max_hold_days,
                             instrument_class=_resolve_instrument_class(sid),
                             generate_tearsheet=False)
                ok += 1
            except Exception as e:
                _log(f'FAIL {sid}: {type(e).__name__}: {e}')
                fail += 1
        _log(f'done: ok={ok} fail={fail}')
        return 0 if fail == 0 else 1

    if args.strategy_id:
        try:
            run_backtest(args.strategy_id,
                         start_date=args.start_date, end_date=args.end_date,
                         max_hold_days=args.max_hold_days,
                         universe_cap=args.universe_cap,
                         instrument_class=_resolve_instrument_class(args.strategy_id))
            return 0
        except FileNotFoundError as e:
            _log(f'FAIL: {e}'); return 1
        except Exception as e:
            _log(f'FAIL: {type(e).__name__}: {e}')
            return 1

    if args.strategy_file:
        sid = Path(args.strategy_file).stem
        try:
            run_backtest(sid, filepath=args.strategy_file,
                         start_date=args.start_date, end_date=args.end_date,
                         max_hold_days=args.max_hold_days,
                         universe_cap=args.universe_cap,
                         instrument_class=_resolve_instrument_class(sid, filepath=args.strategy_file))
            return 0
        except Exception as e:
            _log(f'FAIL: {type(e).__name__}: {e}')
            return 1
    return 1


def run_backtest_with_resolver(strategy, start, end, resolver, **kwargs):
    """SP-2 Phase A: per-bar universe resolution.

    Strategies invoked via this entry point receive a fresh `universe` list
    each bar from `resolver.resolve(strategy.id, as_of=bar_date)`. Existing
    `run_backtest` keeps its current signature for backward compat.
    Returns list[{"date": d, "signals": [...]}].
    """
    from src.backtest._trading_calendar import trading_days
    results = []
    for bar_date in trading_days(start, end):
        universe = resolver.resolve(strategy.id, as_of=bar_date)
        signals = strategy.generate(bar_date, universe)
        results.append({"date": bar_date, "signals": signals})
    return results


if __name__ == '__main__':
    sys.exit(main())
