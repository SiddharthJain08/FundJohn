#!/usr/bin/env python3
"""factor_prescreen.py — cheap pre-backtest factor screen (Task R2).

Runs a strategy's generate_signals() over a short recent window (a sliced
read of prices.parquet — never the full panel) and flags ONLY provably
degenerate output: zero signals anywhere in the sample window, or 100%
constant output (one ticker, one direction, every single driven day).
Everything else annotates with stats and passes.

CONSERVATIVE BY DESIGN (Task R2 brief, 2026-08-24): this is a cheap filter
to skip the ~900s unified_backtest run where it is provably pointless, NOT
a quality gate. Orchestrator wiring lives in research-orchestrator.js,
inserted after the redteam stage and before backtest.

CLI:
    python3 -m backtest.factor_prescreen --strategy-file <path> \
        [--days 60] [--max-tickers 300] [--lookback 300]

HISTORY vs DECISION WINDOW (fix, 2026-08-24 — the S24_52wk_high_proximity
false zero-signal): load_price_window() loads `--days` + `--lookback`
trading days of price history (default lookback=300, env
OPENCLAW_PRESCREEN_LOOKBACK overrides the default), but generate_signals()
is still DRIVEN only over the last `--days` bars — the extra lookback
exists purely so each driven call sees enough trailing history to compute
a long-window indicator (a 52-week high, a 200-day moving average, ...),
mirroring how unified_backtest.py's per-bar loop hands generate_signals()
`close_wide.loc[:current_date]` (the FULL history up to that bar, not a
`--days`-sized slice). Before this fix, load_price_window only loaded
`--days` trading days of history total, so a >60-bar-lookback strategy saw
a truncated panel on every one of the `--days` expanding-window calls and
always returned [] — mis-annotated as `zero_signals_on_fallback_universe`
(a screen artifact) rather than the load-window bug it actually was.
`stats.history_bars` records how many total rows load_price_window
actually returned — AT LEAST `days + lookback_used` trading days when the
underlying data covers that far back (the calendar-day over-fetch buffer
that compensates for weekends/holidays routinely returns a few dozen extra
trading days on top; this function never truncates down to the exact
requested count), fewer only when the ticker's/parquet's own history is
shorter than requested.

MIN_LOOKBACK-AWARE FLOOR (extension, 2026-08-24): a flat `--lookback`
default doesn't cover every strategy — a repo-wide grep of
`min_lookback = <N>` across src/strategies/implementations/ found values
up to 1260 (well above the 300 default). run_prescreen() now reads the
loaded strategy's own `min_lookback` class attribute (same one
unified_backtest.py reads via `getattr(instance, 'min_lookback', 20)`) and
raises the effective lookback to `declared_min_lookback + 10` when that
exceeds `--lookback`, capped at `MAX_LOOKBACK_BARS` (1300). Recorded as
`stats.lookback_used` (the value actually applied) and
`stats.declared_min_lookback` (the raw declared value, None if unreadable).

Prints ONE JSON line to stdout and exits 0 whenever the screen completes
(whether pass=true or pass=false — a hard-fail verdict is still a completed
screen, not an infra failure). Any infra problem (strategy file not found /
import failure, price-load failure, an uncaught exception inside
generate_signals) prints nothing to stdout and exits 1 — the orchestrator
treats a non-zero exit / timeout / unparseable line as `prescreen_infra_fail`
and passes the candidate through to backtest unconditionally.

Two soft-pass annotations (controller ruling, 2026-08-24 — both fix a
verified false-positive risk, never a new way to block a candidate):
  - `prescreen_skipped_aux_dependent`: this screen only ever drives
    generate_signals() with aux_data=None (or an empty stand-in) — it never
    populates real per-ticker options data. A strategy that structurally
    needs aux_data to emit anything would therefore ALWAYS see zero signals
    here regardless of whether it's a legitimate strategy, which would
    violate "block ONLY where provably pointless". Triggered by either of
    two AST signals (widened same day, 2026-08-24): instrument_class ==
    'option' (see _resolve_instrument_class), OR the module's source reads
    a variable named `aux_data` anywhere — subscript or `.get(`, including
    the `(aux_data or {}).get(...)` idiom (see _module_reads_aux_data) —
    which is the real pattern 11 manifest-instrument_class='equity'
    strategies use to consume aux_data['options']
    (e.g. S21_iv_hv_spread.py), a gap the instrument_class check alone
    missed. Such strategies skip the screen entirely, universe never
    loaded: pass=true, stats=null.
  - `zero_signals_on_fallback_universe`: when the universe driving this
    screen came from the most-liquid fallback (see load_price_window)
    rather than the strategy's own resolved universe, a zero-signal result
    doesn't prove the strategy is dead — it may just mean its real edge
    lives outside the top-N liquid names this screen happened to feed it.
    Downgraded to an annotate-only pass; `stats.universe_source` records
    which universe was actually used. zero_signals is still a hard block
    when the strategy's own declared universe was used (currently never —
    see load_price_window). constant_output is ALWAYS a hard block,
    regardless of universe source.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import inspect
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = str(ROOT / 'src')
sys.path.insert(0, str(ROOT))
sys.path.insert(0, SRC_DIR)

PRICES_PARQUET = ROOT / 'data' / 'master' / 'prices.parquet'

DEFAULT_DAYS = 60
DEFAULT_MAX_TICKERS = 300
DEFAULT_LOOKBACK = 300
# min_lookback-aware floor/cap (extension, 2026-08-24): a strategy that
# DECLARES a longer min_lookback than the --lookback default gets bumped up
# to cover it (plus this pad, so its own `len(prices) >= min_lookback`
# check clears with margin on every driven bar, not right at the boundary),
# but never past this cap — 1300 bars (~5 trading years) comfortably covers
# every min_lookback found in a repo-wide grep of
# src/strategies/implementations/ (max observed: 1260) while keeping a
# pathological/typo'd declaration (e.g. min_lookback=999999) from blowing
# up the load.
MIN_LOOKBACK_PAD = 10
MAX_LOOKBACK_BARS = 1300


def _default_lookback() -> int:
    """OPENCLAW_PRESCREEN_LOOKBACK env override, else DEFAULT_LOOKBACK.
    Invalid/unparseable values fall back to the default rather than raising —
    this is a cheap screen, not a place to hard-fail on a bad env var."""
    raw = os.environ.get('OPENCLAW_PRESCREEN_LOOKBACK')
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_LOOKBACK


def _benign_regime() -> dict:
    """Fixed benign regime state (LOW_VOL). Same shape as the synthetic
    regime validate_strategy.py drives strategies with, so strategies that
    read extra regime fields (stress_score, features, ...) don't crash."""
    return {
        'state':               'LOW_VOL',
        'state_probabilities': {'LOW_VOL': 1.0, 'TRANSITIONING': 0.0, 'HIGH_VOL': 0.0, 'CRISIS': 0.0},
        'confidence':          1.0,
        'transition_probs_tomorrow': {'LOW_VOL': 0.9, 'TRANSITIONING': 0.1, 'HIGH_VOL': 0.0, 'CRISIS': 0.0},
        'stress_score':        15,
        'roro_score':          40.0,
        'features':            {'vix': 14.0, 'vix_5d_chg': -0.5, 'vix_term_slope': 1.2,
                                 'spx_rv_20d': 10.0, 'hy_ig_spread': 0.01, 'spx_5d_return': 0.02},
        'regime_change_alert': False,
        'days_in_current_state': 20,
        'position_scale':      1.0,
    }


def _load_strategy_class(filepath: str):
    """Registry/spec-free dynamic import of a strategy file — the same
    approach src/strategies/validate_strategy.py uses: import as a package
    module when the file lives under SRC_DIR (so relative imports resolve
    correctly), else load it directly via spec_from_file_location (tmp
    files / freshly generated strategies with absolute imports)."""
    if not Path(filepath).is_file():
        raise RuntimeError(f'strategy file not found: {filepath}')

    abs_path = str(Path(filepath).resolve())
    module_name = None
    if SRC_DIR in abs_path:
        rel = str(Path(abs_path).relative_to(SRC_DIR))
        rel = rel.replace('/', '.').replace('\\', '.')
        if rel.endswith('.py'):
            module_name = rel[:-3]

    if module_name:
        sys.modules.pop(module_name, None)
        module = importlib.import_module(module_name)
    else:
        spec = importlib.util.spec_from_file_location('_prescreen_strat_under_test', filepath)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    from strategies.base import BaseStrategy

    def _is_strategy_class(obj) -> bool:
        if not inspect.isclass(obj) or obj.__name__ == 'BaseStrategy':
            return False
        # Skip abstract adapter classes (e.g. CohortBaseStrategy shims).
        if getattr(obj, '__abstractmethods__', None):
            return False
        try:
            if issubclass(obj, BaseStrategy):
                return True
        except TypeError:
            pass
        return any(b.__name__ == 'BaseStrategy' for b in obj.__mro__[1:])

    strategy_classes = [
        obj for _, obj in inspect.getmembers(module, inspect.isclass)
        if _is_strategy_class(obj)
    ]
    if not strategy_classes:
        raise RuntimeError('No BaseStrategy subclass found in file')
    return strategy_classes[0]


def _resolve_instrument_class(strategy_id: Optional[str], filepath: str) -> str:
    """Resolve a strategy's instrument_class — the SAME precedence
    src/backtest/unified_backtest.py's `_resolve_instrument_class()` uses:
    (1) manifest.json `strategies[strategy_id].instrument_class`, accepted
    only if it's a VALID_INSTRUMENT_CLASSES value; (2) a module-level
    `INSTRUMENT_CLASS` constant detected via AST
    (strategies.lifecycle._detect_module_instrument_class — covers a
    freshly-coded --strategy-file not yet in the manifest, which is the
    common case here since this screen runs BEFORE promotion); (3)
    'equity'. Never raises.

    Duplicated locally rather than imported from unified_backtest.py: that
    module's top-level imports pull in psycopg2 + a DB connection pool this
    deliberately DB-free, subprocess-isolated, 120s-budget screen has no
    other reason to touch.

    base.py was checked for any "requires aux_data" attribute/hook on
    BaseStrategy — none exists (grep-confirmed repo-wide) — so
    instrument_class is one of two aux-dependence signals available; see
    _module_reads_aux_data() for the other, and run_prescreen()'s bypass
    check for how they combine.
    """
    from strategies.lifecycle import VALID_INSTRUMENT_CLASSES, _detect_module_instrument_class

    try:
        manifest_path = ROOT / 'src' / 'strategies' / 'manifest.json'
        entry = (json.loads(manifest_path.read_text())
                 .get('strategies', {}).get(strategy_id or '') or {})
        ic = entry.get('instrument_class')
        if ic in VALID_INSTRUMENT_CLASSES:
            return ic
    except Exception:
        pass

    try:
        detected = _detect_module_instrument_class(filepath)
        if detected:
            return detected
    except Exception:
        pass

    return 'equity'


def _module_reads_aux_data(filepath: str) -> bool:
    """AST HEURISTIC (controller ruling 2026-08-24, widening the Ruling-1
    bypass): True if the strategy file's source appears to read from a
    variable literally named `aux_data` anywhere in the module — via
    subscript (`aux_data['options']`) or a `.get(` call
    (`aux_data.get('options', {})`, including the `(aux_data or {}).get(...)`
    / conditional-expression idiom, where the immediate receiver of `.get(`
    is a BoolOp or IfExp that itself references a bare `aux_data` Name).
    This is exactly the pattern src/strategies/implementations/
    S21_iv_hv_spread.py and 10 other real strategies use
    (`opts_map = (aux_data or {}).get('options', {})`) — grep-confirmed
    2026-08-24. Uses ast.walk() (the whole module, not just top-level
    statements — unlike _detect_module_instrument_class/
    _detect_module_predicate, which only need a top-level assignment/import)
    because this access pattern lives inside generate_signals()'s method
    body, several nesting levels deep.

    THIS IS A HEURISTIC, NOT A DATA-FLOW ANALYSIS:
      - Over-matches a strategy that reads a variable named `aux_data` for
        an unrelated reason (not observed anywhere in this codebase — the
        `aux_data` name is used exclusively for the BaseStrategy contract's
        optional aux slice).
      - Under-matches a strategy that renames the parameter, destructures it
        before subscripting/`.get`-ing, or accesses it through some other
        indirection (e.g. `getattr(self, '_aux', aux_data)` first).
    Given the false-positive this bypass exists to prevent (hard-blocking a
    legitimate aux-dependent strategy as 'zero_signals') is worse than the
    false-negative (a strategy this heuristic misses is simply screened
    normally — and would still land on the ordinary fallback-universe
    zero-signals soft-pass rather than a hard block, per Ruling 2), erring
    toward over-matching is the correct failure direction.

    A bare `def generate_signals(..., aux_data=None): ...` that never reads
    `aux_data` in its body does NOT match — only accepting the parameter
    is not aux-dependence.

    Returns False on any parse error — never raises, matching
    _detect_module_instrument_class's contract.
    """
    try:
        tree = ast.parse(Path(filepath).read_text())
    except (FileNotFoundError, SyntaxError, OSError):
        return False

    def _is_aux_data_name(node) -> bool:
        return isinstance(node, ast.Name) and node.id == 'aux_data'

    def _unwraps_to_aux_data(node) -> bool:
        # Bare `aux_data`, or the `(aux_data or {})` / `(aux_data if ... else
        # {})` idioms, which still resolve to aux_data on the truthy branch.
        if _is_aux_data_name(node):
            return True
        if isinstance(node, ast.BoolOp) and any(_is_aux_data_name(v) for v in node.values):
            return True
        if isinstance(node, ast.IfExp) and (_is_aux_data_name(node.body) or _is_aux_data_name(node.orelse)):
            return True
        return False

    for node in ast.walk(tree):
        # aux_data['options'] / aux_data[...]
        if isinstance(node, ast.Subscript) and _is_aux_data_name(node.value):
            return True
        # aux_data.get(...) / (aux_data or {}).get(...)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'get' and _unwraps_to_aux_data(node.func.value)):
            return True
    return False


def load_price_window(days: int, max_tickers: int,
                       lookback: int = DEFAULT_LOOKBACK) -> Tuple[pd.DataFrame, List[str], str]:
    """Sliced pyarrow read of prices.parquet — never loads the full panel,
    and (history/decision-window fix, 2026-08-24) never materializes the
    FULL ticker universe over the longer `days + lookback` window either —
    see the two-pass note below, added after the naive single-pass version
    of this fix nearly doubled RSS (544MB -> ~1GB on the real S24 CLI run)
    by reading every ticker in prices.parquet across the larger window
    before ranking. This box is 2-core/8GB with no swap (see
    reference_vps_two_core_cpu memory) — a "cheap screen" doubling its
    footprint is exactly the kind of regression that class of incident
    describes.

    TWO PASSES, each predicate-pushed and column-pruned:
      1. RANK pass — reads ticker/close/volume for ALL tickers, but only
         over the RECENT `days` window (the pre-lookback-fix window size,
         unchanged) — never `days + lookback`. Ranks by mean dollar volume
         (close * volume) entirely in Arrow (pc.multiply +
         Table.group_by().aggregate()) and keeps only the top `max_tickers`
         ticker strings; nothing here is converted to pandas except the
         final small ranked-ticker list. This preserves the ORIGINAL
         ranking contract (liquidity computed over the recent `days`
         window, not diluted by a year of lookback history) as a side
         effect of being the cheap pass.
      2. HISTORY pass — reads ticker/date/close/volume for the
         `days + lookback` window, with the ticker filter from pass 1
         PUSHED DOWN into the dataset predicate
         (`ds.field('ticker').isin(universe)`) so pyarrow never
         materializes rows for tickers outside the kept universe. This is
         the only pass that gets `.to_pandas()`'d, and it's bounded by
         `max_tickers x (days + lookback)` rows, not
         `all_tickers x (days + lookback)`.

    Both passes find the latest date in the parquet from ROW-GROUP
    STATISTICS ONLY (parquet column min/max metadata — no row data read)
    so the cutoff dates can be computed without touching a single row.

    HISTORY vs DECISION WINDOW: `days` is the number of DECISION bars
    run_prescreen() will actually drive generate_signals() over (unchanged
    from before this fix); `lookback` is EXTRA leading history loaded so
    each of those decision bars sees a realistic amount of trailing history
    (mirrors unified_backtest.py's per-bar close_wide.loc[:current_date],
    which always exposes full history, not a `days`-sized slice) — a
    long-lookback strategy (e.g. a 52-week-high indicator) needs history
    older than the decision window itself to compute anything. The HISTORY
    pass loads `days + lookback` trading days total; run_prescreen() is
    responsible for slicing the last `days` rows back out as the driven
    decision bars.

    Returns (close_wide, universe, universe_source): close_wide is a
    date-ascending x ticker close-price panel covering enough calendar days
    to have >= `days + lookback` trading days of history for the kept
    universe; universe is the ranked ticker list (most liquid first, per
    the RECENT `days` window... order doesn't matter downstream);
    universe_source is always the literal string 'fallback' here — there is
    no DB-free, trivially reusable way for this isolated, subprocess-only
    screen to resolve a strategy's OWN declared universe (the SP-2
    universe_filter_ref predicate mechanism needs a live metadata table +
    an as_of-aware resolver, which is exactly the DB dependency this screen
    is designed to avoid). Kept as an explicit return value (not hardcoded
    at the call site) so a future, genuinely trivial declared-universe
    resolution path can flip it to 'declared' without any caller changes —
    see compute_stats()'s zero_signals handling, which branches on this
    value.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.dataset as ds
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(PRICES_PARQUET)
    date_idx = pf.schema_arrow.get_field_index('date')
    max_date_str = None
    for i in range(pf.num_row_groups):
        stats = pf.metadata.row_group(i).column(date_idx).statistics
        if stats is not None and stats.has_min_max and stats.max is not None:
            if max_date_str is None or stats.max > max_date_str:
                max_date_str = stats.max
    if max_date_str is None:
        raise RuntimeError('could not determine latest date in prices.parquet (no row-group stats)')

    max_date = date.fromisoformat(max_date_str)
    dataset = ds.dataset(str(PRICES_PARQUET), format='parquet')

    # Equity/ETF only (matches unified_backtest.py's static_universe filter,
    # lib.price_panel.is_equity_ticker) — excludes indices (^…), crypto
    # (…-USD), futures (…=F) and forex (…=X), which otherwise dominate the
    # dollar-volume ranking below with unit-inconsistent "volume" figures
    # and are never what a backtest actually trades.
    from lib.price_panel import is_equity_ticker

    # ---- Pass 1: RANK — recent `days` window only, ALL tickers, Arrow-only ----
    rank_calendar_days_needed = int(days * 1.6) + 15
    rank_cutoff = max_date - timedelta(days=rank_calendar_days_needed)
    rank_table = dataset.to_table(
        columns=['ticker', 'close', 'volume'],
        filter=(ds.field('date') >= rank_cutoff.isoformat()),
    )
    if rank_table.num_rows == 0:
        raise RuntimeError('no price rows found in the prescreen ranking window')

    distinct_tickers = rank_table.column('ticker').unique().to_pylist()
    equity_tickers = [t for t in distinct_tickers if is_equity_ticker(t)]
    if not equity_tickers:
        raise RuntimeError('no equity/ETF price rows found in the prescreen window')
    rank_table = rank_table.filter(pc.field('ticker').isin(equity_tickers))

    dollar_vol_col = pc.multiply(
        pc.cast(rank_table.column('close'), pa.float64()),
        pc.cast(rank_table.column('volume'), pa.float64()),
    )
    rank_table = rank_table.append_column('dollar_vol', dollar_vol_col)
    ranked = (rank_table.group_by('ticker')
              .aggregate([('dollar_vol', 'mean')])
              .sort_by([('dollar_vol_mean', 'descending')]))
    universe = ranked.column('ticker').to_pylist()[:max_tickers]

    # ---- Pass 2: HISTORY — `days + lookback` window, ticker filter PUSHED
    # DOWN to the kept universe only ----
    total_trading_days = days + max(0, lookback)
    calendar_days_needed = int(total_trading_days * 1.6) + 15
    cutoff = max_date - timedelta(days=calendar_days_needed)

    table = dataset.to_table(
        columns=['ticker', 'date', 'close', 'volume'],
        filter=(ds.field('date') >= cutoff.isoformat()) & (ds.field('ticker').isin(universe)),
    )
    df = table.to_pandas()
    if df.empty:
        raise RuntimeError('no price rows found in the prescreen window')
    df['date'] = pd.to_datetime(df['date'])

    close_wide = df.pivot(index='date', columns='ticker', values='close').sort_index()
    return close_wide, universe, 'fallback'


def _signal_sets_and_counts(daily_signals: List[list]):
    all_tickers = set()
    directions_seen = set()
    long_count = 0
    short_count = 0
    active_ticker_sets: List[set] = []
    for day_signals in daily_signals:
        if not day_signals:
            continue
        today_tickers = set()
        for sig in day_signals:
            ticker = getattr(sig, 'ticker', None)
            direction = getattr(sig, 'direction', None)
            if ticker is not None:
                all_tickers.add(ticker)
                today_tickers.add(ticker)
            directions_seen.add(direction)
            if direction == 'LONG':
                long_count += 1
            elif direction == 'SHORT':
                short_count += 1
        active_ticker_sets.append(today_tickers)
    return all_tickers, directions_seen, long_count, short_count, active_ticker_sets


def compute_stats(daily_signals: List[list],
                   universe_source: str = 'fallback',
                   history_bars: Optional[int] = None,
                   lookback_used: Optional[int] = None,
                   declared_min_lookback: Optional[int] = None) -> Tuple[bool, Optional[str], dict]:
    """Aggregate stats + hard-fail verdict from per-day signal lists.

    `history_bars` (added with the history/decision-window fix, 2026-08-24)
    is the total row count load_price_window() returned — AT LEAST
    days + lookback_used trading days (see load_price_window's docstring
    for why it's routinely a bit more, never less unless the underlying
    data is shorter) — None when the caller has no such figure (e.g. a
    unit test driving compute_stats() directly).

    `lookback_used` / `declared_min_lookback` (min_lookback-aware floor/cap
    extension, 2026-08-24 — see run_prescreen()'s docstring for the full
    rule): `lookback_used` is the extra-history figure actually passed to
    load_price_window after applying the strategy's own declared
    `min_lookback` as a floor (plus a pad) and the MAX_LOOKBACK_BARS cap;
    `declared_min_lookback` is the raw value read off the strategy instance
    (None when absent/unreadable). Both None when the caller has no such
    figures (same unit-test case as `history_bars`).

    HARD-FAIL (pass=False) ONLY on provably degenerate output:
      - zero_signals:    no signals anywhere in the sample window, AND the
                         universe driving the screen was the strategy's OWN
                         resolved universe (universe_source == 'declared').
                         When universe_source == 'fallback' (currently
                         always — see load_price_window), this is instead a
                         soft pass, reason='zero_signals_on_fallback_universe'
                         (controller ruling 2026-08-24 — a most-liquid
                         fallback universe not matching the strategy's real
                         edge is a screen artifact, not proof of a dead
                         strategy).
      - constant_output: exactly one unique ticker traded across the whole
                          window, EVERY driven day had >=1 signal, and only
                          one direction was ever emitted (100% constant).
                          ALWAYS a hard block, regardless of universe_source.
    Everything else passes — this is a cheap screen, not a quality gate.

    turnover_proxy is the mean, over consecutive ACTIVE days (i.e.
    consecutive entries in the day-ordered subsequence of days that had
    >=1 signal — inactive days in between don't break the "consecutive"
    pairing), of 1 - |intersection|/|union| of that day's and the next
    active day's signal-ticker sets. None when fewer than 2 active days.
    """
    signals_total = sum(len(d) for d in daily_signals)
    active_days = sum(1 for d in daily_signals if d)
    all_tickers, directions_seen, long_count, short_count, active_ticker_sets = \
        _signal_sets_and_counts(daily_signals)
    unique_tickers = len(all_tickers)

    total_directional = long_count + short_count
    direction_balance = (long_count / total_directional) if total_directional > 0 else None

    if len(active_ticker_sets) >= 2:
        turnovers = []
        for a, b in zip(active_ticker_sets, active_ticker_sets[1:]):
            union = a | b
            inter = a & b
            turnovers.append(1.0 - (len(inter) / len(union) if union else 0.0))
        turnover_proxy = sum(turnovers) / len(turnovers)
    else:
        turnover_proxy = None

    stats = {
        'signals_total':         signals_total,
        'active_days':           active_days,
        'direction_balance':     direction_balance,
        'unique_tickers':        unique_tickers,
        'turnover_proxy':        turnover_proxy,
        'universe_source':       universe_source,
        'history_bars':          history_bars,
        'lookback_used':         lookback_used,
        'declared_min_lookback': declared_min_lookback,
    }

    n_driven_days = len(daily_signals)
    if signals_total == 0:
        if universe_source == 'fallback':
            return True, 'zero_signals_on_fallback_universe', stats
        return False, 'zero_signals', stats
    if unique_tickers == 1 and active_days == n_driven_days and len(directions_seen) == 1:
        return False, 'constant_output', stats
    return True, None, stats


def run_prescreen(strategy_file: str, days: int = DEFAULT_DAYS,
                   max_tickers: int = DEFAULT_MAX_TICKERS,
                   lookback: Optional[int] = None) -> dict:
    """Drive generate_signals() over the last `days` trading days and return
    the {"pass", "reason", "stats"} verdict dict.

    `lookback` (history/decision-window fix, 2026-08-24): extra trading days
    of history loaded ahead of the `days` decision window so long-lookback
    strategies (52-week high, 200-day MA, ...) see realistic history on
    every driven call, same as unified_backtest.py's per-bar full-history
    slice. None resolves to _default_lookback() (DEFAULT_LOOKBACK, or
    OPENCLAW_PRESCREEN_LOOKBACK when set) — never a hardcoded default value
    on the signature itself, so an env-var change is picked up even by
    callers that already froze `lookback=None` at import time.

    MIN_LOOKBACK-AWARE FLOOR (extension, 2026-08-24): once the strategy
    class is loaded, if it DECLARES `min_lookback` (the same class
    attribute unified_backtest.py reads via
    `getattr(instance, 'min_lookback', 20)` — confirmed via base.py's
    `BaseStrategy.min_lookback: int = 20` default and a repo-wide grep of
    src/strategies/implementations/, e.g. `min_lookback = 504` on
    s_idiosyncratic_vol_puzzle.py, `= 1260` on
    S_fama_french_anomaly_dissection.py), the ACTUAL lookback fed to
    load_price_window is `max(lookback, declared_min_lookback +
    MIN_LOOKBACK_PAD)`, capped at `MAX_LOOKBACK_BARS` (1300) — so a
    strategy needing more history than the flat `--lookback` default
    still clears its own bar with margin (the +10 pad), without a
    pathological declaration blowing up the load. `stats.lookback_used`
    records the value actually used; `stats.declared_min_lookback` records
    the raw declared value (None when the loaded class has no readable
    `min_lookback` at all — in practice every BaseStrategy subclass has
    one via the base-class default, so this is a defensive fallback, not
    an expected path).

    Raises on any infra problem (strategy load failure, price load failure,
    an exception raised inside generate_signals) — main() turns any raise
    here into exit 1, which the orchestrator treats as prescreen_infra_fail.
    """
    if lookback is None:
        lookback = _default_lookback()

    cls = _load_strategy_class(strategy_file)

    # Aux-dependent bypass (controller ruling 2026-08-24, widened same day —
    # fixes a verified false positive): this screen never populates real
    # aux_data (options chains, financials, etc.), so a strategy that
    # structurally needs it would always see [] here regardless of
    # legitimacy. Two independent signals, either one triggers the bypass:
    #   (a) instrument_class == 'option' (manifest / module-level constant);
    #   (b) the module's source reads a variable named `aux_data` anywhere
    #       (subscript or `.get(`, including `(aux_data or {}).get(...)`) —
    #       an AST heuristic (_module_reads_aux_data) covering the real gap
    #       (a) misses: 11 manifest-instrument_class='equity' strategies
    #       (e.g. S21_iv_hv_spread.py) that consume aux_data['options'] to
    #       build a signal on the underlying equity, not an option contract.
    # base.py has no separate "requires aux_data" attribute/hook (checked —
    # none exists), so these two AST signals are what's available; skip
    # entirely — never load prices, never drive generate_signals — since the
    # whole point is avoiding a screen result that can't mean anything.
    instrument_class = _resolve_instrument_class(getattr(cls, 'id', None), strategy_file)
    if instrument_class == 'option' or _module_reads_aux_data(strategy_file):
        return {'pass': True, 'reason': 'prescreen_skipped_aux_dependent', 'stats': None}

    instance = cls()

    # min_lookback-aware floor + cap (see docstring). Read off the
    # INSTANCE (matches unified_backtest.py's getattr(instance,
    # 'min_lookback', 20) precedent) so an instance-level override in
    # __init__ is honored, not just the class attribute.
    declared_min_lookback = getattr(instance, 'min_lookback', None)
    try:
        declared_min_lookback = int(declared_min_lookback) if declared_min_lookback is not None else None
    except (TypeError, ValueError):
        declared_min_lookback = None

    lookback_used = lookback
    if declared_min_lookback is not None:
        lookback_used = max(lookback_used, declared_min_lookback + MIN_LOOKBACK_PAD)
    lookback_used = min(lookback_used, MAX_LOOKBACK_BARS)

    close_wide, universe, universe_source = load_price_window(days, max_tickers, lookback_used)
    n_rows = len(close_wide.index)
    if n_rows < 1:
        raise RuntimeError('empty price panel for prescreen window')

    # DECISION window stays the last `days` rows — unchanged from before this
    # fix. `close_wide` now carries up to `lookback_used` EXTRA leading rows
    # of history ahead of that window (see load_price_window), so each
    # `prices_to_date` slice below already carries that history without any
    # change to this indexing.
    start_idx = max(0, n_rows - days)
    regime = _benign_regime()

    daily_signals = []
    for i in range(start_idx, n_rows):
        # Full history up to and including bar i — mirrors
        # unified_backtest.py's per-bar `close_wide.loc[:current_date]`
        # (never a `days`-sized slice), so a long-lookback strategy sees the
        # same panel shape here that it would see in the real backtest.
        prices_to_date = close_wide.iloc[:i + 1]
        # Same aux_data convention validate_strategy.py uses: try the
        # aux_data kwarg, fall back to the 3-arg call for strategies whose
        # signature doesn't accept it.
        try:
            signals = instance.generate_signals(prices_to_date, regime, universe, aux_data=None)
        except TypeError:
            signals = instance.generate_signals(prices_to_date, regime, universe)
        if signals is None:
            signals = []
        daily_signals.append(signals)

    passed, reason, stats = compute_stats(daily_signals, universe_source=universe_source,
                                           history_bars=n_rows, lookback_used=lookback_used,
                                           declared_min_lookback=declared_min_lookback)
    return {'pass': passed, 'reason': reason, 'stats': stats}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Cheap pre-backtest factor screen (Task R2)')
    ap.add_argument('--strategy-file', required=True)
    ap.add_argument('--days', type=int, default=DEFAULT_DAYS)
    ap.add_argument('--max-tickers', type=int, default=DEFAULT_MAX_TICKERS)
    ap.add_argument('--lookback', type=int, default=_default_lookback(),
                     help='Extra trading days of leading history loaded ahead of --days '
                          '(default 300; env OPENCLAW_PRESCREEN_LOOKBACK overrides the default)')
    args = ap.parse_args(argv)

    try:
        result = run_prescreen(args.strategy_file, days=args.days, max_tickers=args.max_tickers,
                                lookback=args.lookback)
    except Exception as e:  # noqa: BLE001 — any infra failure -> exit 1, JS side passes-through
        print(f'prescreen infra error: {e}', file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
