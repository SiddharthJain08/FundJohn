#!/usr/bin/env python3
"""
OpenClaw Execution Engine — zero-token, zero-LLM.

Daily run sequence:
  1. Load regime state from DB
  2. Load approved strategies from strategy_registry
  3. Load prices + aux_data
  4. Run each strategy → collect signals
  5. Write signals to execution_signals (ON CONFLICT DO NOTHING)
  6. Detect confluence (≥2 strategies agree on same ticker/direction)
  7. Update P&L on open signals
  8. Fire report triggers (stop hit, target hit, 10% drawdown, etc.)
  9. Log execution run metrics
"""

import os
import sys
import gc as _gc
import json
import logging
import traceback
import decimal
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import pandas as pd
import numpy as np

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from strategies.registry import get_approved_strategies
from strategies.regime_gate import is_eligible
from strategies.instrument_class import instrument_class_for
from regime.crypto_regime import load_crypto_regime_state
from execution import regime_param_override
from lib.price_panel import apply_equity_calendar, calendar_for

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [ENGINE] %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

WORKSPACE = os.environ.get('WORKSPACE_ID', 'default')
DB_URI    = os.environ.get('POSTGRES_URI')

# Trigger thresholds
STOP_TRIGGER_PCT        = -0.02   # -2% below stop = close signal
TARGET1_TRIGGER_PCT     =  0.005  # within 0.5% of target_1
DRAWDOWN_REPORT_PCT     = -0.10   # -10% unrealized triggers review
DAYS_HELD_REPORT        = 30      # flag if held > 30 days with no target hit
CONFLUENCE_MIN          = 2       # min strategies agreeing for confluence


_ALPACA_BIN = '/root/go/bin/alpaca'


def _next_trading_day(run_date: date) -> date:
    """Derive the next trading session date after run_date, respecting US market holidays.

    Uses the Alpaca market calendar CLI (holiday-aware) as the primary source.
    Queries --start run_date+1 --end run_date+7 and returns the first calendar
    session date.  Falls back to plain weekday-skip math ONLY if the CLI call
    fails, and logs a warning in that case.

    The holiday-aware approach is required because the next-session reconcile
    (Task 3 onwards) queries `target_date = today`.  If target_date were set to
    a market holiday, the signal would be silently orphaned.

    Example: run_date=2026-07-02 (Thursday before observed July 4)
      - weekday math → 2026-07-03 (Friday) — WRONG; market is closed
      - calendar     → 2026-07-06 (Monday) — correct next session

    Args:
        run_date: The current EOD run date.

    Returns:
        The next trading session date after run_date.
    """
    start = run_date + timedelta(days=1)
    end = run_date + timedelta(days=7)
    try:
        result = subprocess.run(
            [_ALPACA_BIN, 'calendar',
             '--start', start.isoformat(),
             '--end', end.isoformat()],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.warning(
                '_next_trading_day: alpaca calendar call failed (rc=%s); '
                'falling back to weekday-skip math for run_date=%s',
                result.returncode, run_date,
            )
        else:
            sessions = json.loads(result.stdout) if result.stdout.strip() else []
            if sessions:
                return date.fromisoformat(sessions[0]['date'])
            logger.warning(
                '_next_trading_day: alpaca calendar returned empty session list; '
                'falling back to weekday-skip math for run_date=%s',
                run_date,
            )
    except Exception as exc:
        logger.warning(
            '_next_trading_day: alpaca calendar exception (%s); '
            'falling back to weekday-skip math for run_date=%s',
            exc, run_date,
        )
    # Fallback: skip weekends only (no holiday awareness)
    d = start
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d += timedelta(days=1)
    return d


def _eod_signal_register_gate_on() -> bool:
    """True ⇒ T+1 EOD-register signal semantics (target_date=T+1 via
    _next_trading_day); False ⇒ same-day target_date=T (the live mode).

    §8 (2026-08-06): mode resolution moved to signal_target_mode — the
    positively-named OPENCLAW_SAMEDAY_SIGNAL_TARGET wins when set, the
    legacy OPENCLAW_EOD_SIGNAL_REGISTER is honoured for one epoch when
    not. Lifecycle side-effects that must survive in BOTH executing modes
    live behind _signal_lifecycle_pass_on() instead."""
    from execution.signal_target_mode import eod_register_on
    return eod_register_on()


def _signal_lifecycle_pass_on() -> bool:
    """True when a routed-execution mode is driving the book (EOD flow OR
    same-day exec, 2026-07-29 pivot). Gates the compute-health sentinel and
    the parity-mark / ledger-finalize / live-measurement pass — those must
    run whenever signals feed real submissions, regardless of the timing
    model. (The sentinel also arms the premarket reconcile's deliberate-
    flatten guard, which stays active in same-day mode.)"""
    from execution.signal_target_mode import eod_register_on
    return eod_register_on() or os.environ.get('OPENCLAW_SAMEDAY_EXEC') == '1'


def _is_trading_session(d: date) -> bool | None:
    """True/False via `alpaca calendar --start d --end d`; None on probe failure.

    Mirrors _next_trading_day's CLI contract (5s timeout, JSON session list).
    """
    try:
        result = subprocess.run(
            [_ALPACA_BIN, 'calendar', '--start', d.isoformat(), '--end', d.isoformat()],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        sessions = json.loads(result.stdout) if result.stdout.strip() else []
        if not isinstance(sessions, list):
            return None
        return any(s.get('date') == d.isoformat() for s in sessions if isinstance(s, dict))
    except Exception:
        return None


def _panel_fresh_required(run_date: date) -> bool:
    """Fix C: does THIS run require the panel to hold close[run_date]?

    True only for the post-close EOD-compute shape: now is ≥16:05 ET on the
    run_date itself AND run_date is a trading session. Intraday redeploys
    (pre-close) and historical re-runs (run_date != today-ET) correctly use
    the latest available session — exempt. Calendar-probe failure on the
    post-close shape fails LOUD (required) — silently blessing a stale panel
    is the 06-02/06-03 failure this exists to detect.
    """
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo('America/New_York'))
    if now_et.date() != run_date:
        return False
    if now_et.weekday() >= 5 or now_et.strftime('%H:%M') < '16:05':
        return False
    trading = _is_trading_session(run_date)
    return trading is not False


def get_db():
    return psycopg2.connect(DB_URI, cursor_factory=psycopg2.extras.DictCursor)


# ──────────────────────────────────────────────────────────
# SP-1: options EOD parquet helpers
# ──────────────────────────────────────────────────────────

def _drop_zero_greeks(df: 'pd.DataFrame') -> 'pd.DataFrame':
    """Drop rows where ALL four greeks are zero (degenerate contracts).

    options_eod.parquet is kept as an immutable record by the archive job
    (Task 6/alpaca_options.py) — it intentionally preserves zero-greek rows
    so that the parquet reflects the raw chain snapshot (0-DTE expired
    contracts, deep-ITM no-flow rows, etc.).  Consumers that aggregate the
    chain (like this engine) must filter them out before aggregation to avoid
    contaminating IV-rank, ATM-greek, and put/call ratio calculations.

    This is the SECOND line of defence:
      1st — alpaca_options.js (Task 13) drops zero-greek rows during the live
            intraday snapshot before they ever reach the signal stage.
      2nd — this function, applied immediately after pd.read_parquet(), for
            rows that may exist in the historical archive.
    """
    if df.empty:
        return df
    greek_cols = ('delta', 'gamma', 'theta', 'vega')
    present = [c for c in greek_cols if c in df.columns]
    if not present:
        # No greek columns at all — nothing to filter on; return unchanged.
        return df
    zero_mask = pd.Series(True, index=df.index)
    for col in present:
        zero_mask = zero_mask & (df[col].fillna(0) == 0)
    # Columns that are absent are treated as 0 (conservative: only drop if
    # ALL present columns are also zero).
    dropped = int(zero_mask.sum())
    if dropped > 0:
        logger.info('sp1.engine.options_filter dropped=%d degenerate rows (of %d total)', dropped, len(df))
    return df[~zero_mask].copy()


# Memory-bounded options read for the signals step (2026-06-24 OOM fix).
# run_strategies references only these 14 of options_eod.parquet's 27 columns and looks
# back at most 30 calendar days (IV-rank), so loading the full multi-million-row file
# each cycle (which OOM-killed the step once it grew past ~6M rows) is wasteful. Project
# these columns + a trailing date window via pyarrow predicate pushdown instead.
# `theta`/`vega` are kept because `_drop_zero_greeks` checks all four greeks.
_OPTIONS_SIGNAL_COLS = [
    'ticker', 'date', 'expiry', 'strike', 'option_type', 'implied_volatility',
    'delta', 'gamma', 'theta', 'vega', 'open_interest', 'volume', 'open', 'close',
]
# Trailing read windows (calendar days) for the signals step's master-parquet reads.
# Each MUST exceed the longest lookback its consumer needs; they cap memory as the
# archives deepen. Options: 60 >= the 30d IV-rank lookback. HV20 prices: 120 — the
# HV20 calc only needs ~28 trading days, but the `len(_g) < 22` skip below means a
# too-tight window would silently drop sparse/halted tickers, so keep generous headroom.
# 60 -> 14 (2026-07-29): options_eod reached 18M rows, 17M of them in July
# alone (the collector now pulls full chains for 551 tickers daily), so a
# 60-day window read essentially the WHOLE file — 5.4GB in pandas, rc=137 at
# the signals step. This killed the 20:15 EOD compute every night since 07-27
# and the first same-day 15:00 compute. 14 days still covers the aux's 8-day
# history arrays and most of iv_rank's 30-day range (which degrades gracefully
# on a short history). PROPER FIX (owed): serve the live options aux from the
# pre-aggregated panel + intraday overlay instead of re-aggregating raw chains
# on every run — same math, ~225k rows instead of 18M, and it puts live and
# backtest on ONE source.
_OPTIONS_READ_WINDOW_DAYS = int(os.environ.get('OPENCLAW_OPTIONS_READ_WINDOW_DAYS', '14'))
_HV20_PRICES_WINDOW_DAYS  = int(os.environ.get('OPENCLAW_HV20_PRICES_WINDOW_DAYS', '120'))


def _read_parquet_window(path, columns, window_days, today=None, date_col='date'):
    """Read a master parquet projected to `columns` and limited to the trailing
    `window_days`, via pyarrow predicate pushdown — avoids materializing the full file
    in pandas (the 2026-06-24 signals-step OOM). The date column is an ISO 'YYYY-MM-DD'
    string, so the filter compares strings (lexicographic == chronological for ISO)."""
    if today is None:
        today = pd.Timestamp.today().normalize()
    cutoff = (today - pd.Timedelta(days=window_days)).strftime('%Y-%m-%d')
    return pd.read_parquet(path, columns=list(columns), filters=[(date_col, '>=', cutoff)])


def _cat_to_datetime(s: 'pd.Series') -> 'pd.Series':
    """Category-of-ISO-strings → datetime64, converting only the (few) unique
    categories instead of every row. Equivalent to pd.to_datetime(s,
    errors='coerce') on the object column, at ~1/1e6 the work for a
    16M-row/10-unique-date column."""
    cats = pd.to_datetime(s.cat.categories, errors='coerce')
    codes = s.cat.codes.to_numpy()
    out = cats.take(np.where(codes >= 0, codes, 0)).to_numpy(copy=True)
    out[codes < 0] = np.datetime64('NaT')
    return pd.Series(out, index=s.index)


def _load_options_window(path, columns, window_days, today):
    """Memory-lean options_eod window read (2026-08-06 OOM fix, 2nd wave).

    The plain _read_parquet_window at 1.8-1.9M rows/day (full daily chains for
    the tradable universe since the three-tier ingestion, 2026-07-30) hands
    pandas ~16M rows where ticker/date/expiry/option_type are python-object
    strings: measured 5.2GB peak on 2026-08-06 — over the whole box. Same
    step, same rc=137 class as the prices-panel fix (9bccab0), different
    frame. Three changes, output-equivalent:

      1. dictionary-encode the four string columns at the arrow layer
         (ticker/option_type stay pandas category downstream — groupby and
         .str.upper() behave identically; date/expiry are decoded per-CATEGORY
         to datetime64, which the old path produced via row-wise to_datetime);
      2. drop degenerate all-zero-greek rows with arrow compute BEFORE the
         pandas conversion (same fillna(0)==0 semantics and same log line as
         _drop_zero_greeks, which stays for other callers);
      3. numeric columns remain float64 — no value changes anywhere.

    The loader also applies the 0 < DTE <= MAX_DTE expiry band that
    _inject_intraday_options applies when a tier-1 overlay exists — now on
    EVERY run. Before this the panel was bimodal: banded on overlay days
    (the 15:00 ET same-day compute, the semantics of record for execution)
    and unbanded on overlay-less runs (the 10:00 ET base cycle), where
    expired weeklies and LEAPS both inflated the per-ticker IV history by a
    median +4.5 vol points (per the injection's own 2026-07-29 measurement)
    AND kept ~9M rows alive through the aggregation loop — the second half
    of the 5.2GB OOM. With-overlay behavior is unchanged (the band is
    idempotent); overlay-less runs now match it. Most of the band is pushed
    into the parquet read itself (expiry > today, expiry <= today+MAX_DTE is
    a superset of dte_own <= MAX_DTE since date <= today); the exact
    per-observation cut follows in pandas.
    """
    import pyarrow.compute as _pc
    import pyarrow.parquet as _pq
    try:
        from ingestion.intraday_options import MAX_DTE as _max_dte
    except Exception:  # noqa: BLE001 — same default as intraday_options.py
        _max_dte = int(os.environ.get('OPENCLAW_INTRADAY_OPTIONS_MAX_DTE', '100'))
    cutoff = (today - pd.Timedelta(days=window_days)).strftime('%Y-%m-%d')
    tbl = _pq.read_table(
        path, columns=list(columns),
        filters=[('date', '>=', cutoff),
                 ('expiry', '>', today.strftime('%Y-%m-%d')),
                 ('expiry', '<=', (today + pd.Timedelta(days=_max_dte)).strftime('%Y-%m-%d'))],
        read_dictionary=['ticker', 'date', 'expiry', 'option_type'])
    total = tbl.num_rows
    greeks = [c for c in ('delta', 'gamma', 'theta', 'vega')
              if c in tbl.column_names]
    if greeks and total:
        zero = None
        for c in greeks:
            m = _pc.equal(_pc.fill_null(tbl[c], 0.0), 0.0)
            zero = m if zero is None else _pc.and_(zero, m)
        tbl = tbl.filter(_pc.invert(zero))
        dropped = total - tbl.num_rows
        if dropped > 0:
            logger.info('sp1.engine.options_filter dropped=%d degenerate rows (of %d total)',
                        dropped, total)
    df = tbl.to_pandas(self_destruct=True, split_blocks=True)
    del tbl
    for col in ('date', 'expiry'):
        if col in df.columns and isinstance(df[col].dtype, pd.CategoricalDtype):
            df[col] = _cat_to_datetime(df[col])
    # Exact per-observation band (the arrow expiry filters above are its
    # superset). Mirrors _inject_intraday_options.in_band precisely.
    if 'expiry' in df.columns and 'date' in df.columns and not df.empty:
        _dte_today = (df['expiry'] - today).dt.days
        _dte_own = (df['expiry'] - df['date']).dt.days
        df = df[(_dte_today > 0) & (_dte_own <= _max_dte)]
    return df


# ──────────────────────────────────────────────────────────
# 1. LOAD REGIME
# ──────────────────────────────────────────────────────────

def resolve_workspace(cur, name_or_id: str) -> str:
    """Resolve workspace name ('default') to UUID."""
    if len(name_or_id) == 36 and '-' in name_or_id:
        return name_or_id  # already a UUID
    cur.execute("SELECT id FROM workspaces WHERE name=%s LIMIT 1", (name_or_id,))
    row = cur.fetchone()
    if row:
        return str(row['id'])
    cur.execute("SELECT id FROM workspaces ORDER BY created_at ASC LIMIT 1")
    row = cur.fetchone()
    return str(row['id']) if row else name_or_id


_INTRADAY_AUX = os.environ.get('OPENCLAW_INTRADAY_AUX', '0') == '1'
_INTRADAY_AUX_MAX_AGE_H = float(os.environ.get('OPENCLAW_INTRADAY_AUX_MAX_AGE_H', '6'))


def _inject_intraday_options(opts, today, universe):
    """Splice the tier-1 (14:30 ET) options overlay into the EOD panel.

    WHY (three-tier ingestion, operator directive 2026-07-29): an ACTING
    strategy must never decide on the previous day's EOD collect. Under
    same-day execution the signals step runs at 15:00 ET, hours before the
    16:15 collect, so without this every options field the engine derives is
    yesterday's surface.

    WHERE: raw contract rows, not aggregates. Every derived field below keys
    off ``chain['date'].max()`` (iv30, pc_ratio, gamma_atm, theta_atm,
    iv_spread, skew_20d, term structure, gex, iv_centroid_delta), so injecting
    today-dated raw rows makes the whole surface same-day with NO change to
    the field math.

    Two invariants:
      * PRECEDENCE — panel rows dated today win. load_aux_data also runs from
        redeploy_pipeline on a regime change, which can fire after the 16:15
        collect has already appended real closes; the official record beats
        the snapshot whenever both exist.
      * SHARED EXPIRY BAND — the overlay carries only 0 < DTE <= MAX_DTE, so
        the panel side is cut to the same band. Measured on 2026-07-29, not
        equalizing shifts the per-ticker mean IV by a median +4.5 vol points
        (p95 +19) purely from contract population, which would masquerade as
        a real one-day move. It also closes the expiry-day hole: without it
        `nearest_expiry` on a Friday resolves to a same-day expiry the
        overlay has no rows for, silently reverting those tickers to
        yesterday.
    """
    if not _INTRADAY_AUX or opts is None or 'date' not in opts.columns:
        return opts
    try:
        from ingestion.intraday_options import load_overlay, overlay_path, MAX_DTE
        path = overlay_path(today, 'options_raw')
        # An overlay is a SNAPSHOT of a moment. A file left from an earlier run
        # (or a tier-1 job that failed and never refreshed it) is date-stamped
        # today while holding a stale surface — precisely the staleness this
        # pivot removes. Age it out rather than trust the filename.
        if path.exists():
            age_h = (time.time() - path.stat().st_mtime) / 3600.0
            if age_h > _INTRADAY_AUX_MAX_AGE_H:
                logger.warning("intraday options overlay is %.1fh old (max %.1fh) "
                               "— IGNORED, options aux served from the EOD panel",
                               age_h, _INTRADAY_AUX_MAX_AGE_H)
                return opts
        overlay = load_overlay(today, category='options_raw')
        if overlay is None or overlay.empty:
            logger.info("intraday options overlay absent for %s — options aux "
                        "served from the EOD panel", today.date())
            return opts
        overlay['date']   = pd.to_datetime(overlay['date'], errors='coerce')
        overlay['expiry'] = pd.to_datetime(overlay['expiry'], errors='coerce')
        overlay = overlay[overlay['ticker'].isin(set(universe))]

        already = set(opts.loc[opts['date'] == today, 'ticker'].unique())
        if already:
            overlay = overlay[~overlay['ticker'].isin(already)]
        if overlay.empty:
            logger.info("intraday options overlay fully superseded by today's "
                        "EOD rows (%d tickers)", len(already))
            return opts

        overlay = _drop_zero_greeks(overlay)
        n_over, n_tk, n_before = len(overlay), overlay['ticker'].nunique(), len(opts)

        def in_band(df):
            # `expiry > today` is TODAY-relative on purpose: it is what closes
            # the expiry-day hole. The MAX_DTE cap is per OBSERVATION date, so
            # every date keeps the same contract population — a today-relative
            # cap would strip short-dated contracts from older dates only,
            # skewing the history arrays load_aux_data builds from `grp`.
            dte_today = (df['expiry'] - today).dt.days
            dte_own = (df['expiry'] - df['date']).dt.days
            return df[(dte_today > 0) & (dte_own <= MAX_DTE)]

        # Rebind so the unfiltered panel is freed before the concat: this is
        # the step that OOM'd (rc=137) on 06-24 and 07-29, on 8GB and no swap.
        opts = in_band(opts)
        # The panel carries columns the chain feed does not (`open`); reindex
        # rather than select, so a schema gap widens the frame with NaN instead
        # of raising and costing the whole overlay.
        aligned = in_band(overlay).reindex(columns=opts.columns)
        del overlay
        # Keep the panel's dictionary encoding through the concat: a mixed
        # category/object concat silently upcasts ticker+option_type back to
        # per-row python objects for every panel row (~GB-scale at 1.8M
        # rows/day — the dtype half of the 2026-08-06 OOM fix).
        for _c in ('ticker', 'option_type'):
            if _c in opts.columns and isinstance(opts[_c].dtype, pd.CategoricalDtype):
                _cats = opts[_c].cat.categories.union(
                    pd.Index(aligned[_c].dropna().unique()))
                opts[_c] = opts[_c].cat.set_categories(_cats)
                aligned[_c] = pd.Categorical(aligned[_c], categories=_cats)
        merged = pd.concat([opts, aligned], ignore_index=True)
        logger.info("intraday options overlay: +%d rows / %d tickers "
                    "(band 0<DTE, per-date cap %d: %d -> %d rows)",
                    n_over, n_tk, MAX_DTE, n_before, len(merged))
        return merged
    except Exception as e:  # noqa: BLE001 — never lose the EOD panel over an overlay
        logger.warning("intraday options overlay skipped (%s) — EOD panel kept", e)
        return opts


def _intraday_overlay_exists(category: str) -> bool:
    if not _INTRADAY_AUX:
        return False
    try:
        from ingestion.intraday_options import overlay_path
        return overlay_path(pd.Timestamp.today().normalize(), category).exists()
    except Exception:  # noqa: BLE001
        return False


def _inject_intraday_rows(master, category: str, universe, *, dedup_keys):
    """Splice a tier-1 row overlay into an aux master frame.

    Used by the row-stream categories (financials, insider) — the options path
    has its own helper because it also has to equalize an expiry band.

    Three shared properties with _inject_intraday_options:
      * a 6h age guard, so a file left by an earlier run cannot pass itself off
        as today's snapshot;
      * scoped to `universe`;
      * fail-open — the master is returned unchanged on any error, because a
        stale-but-present panel beats no panel.

    The MASTER WINS on a key collision. The overlay is a snapshot taken before
    the 16:15 collect; once the official record carries the same row, it is
    authoritative. For insider this is also what keeps the transaction list
    from double-counting a filing that both sources saw."""
    if not _INTRADAY_AUX or master is None:
        return master
    try:
        from ingestion.intraday_options import load_overlay, overlay_path
        today = pd.Timestamp.today().normalize()
        path = overlay_path(today, category)
        if not path.exists():
            return master
        age_h = (time.time() - path.stat().st_mtime) / 3600.0
        if age_h > _INTRADAY_AUX_MAX_AGE_H:
            logger.warning('intraday %s overlay is %.1fh old (max %.1fh) — '
                           'IGNORED, serving the EOD master', category, age_h,
                           _INTRADAY_AUX_MAX_AGE_H)
            return master
        overlay = load_overlay(today, category)
        if overlay is None or overlay.empty:
            return master
        overlay = overlay[overlay['ticker'].isin(set(universe))]
        if overlay.empty:
            return master
        keys = [k for k in dedup_keys if k in master.columns and k in overlay.columns]
        if keys and not master.empty:
            def _norm(df):
                # Both masters store `date` as a STRING today and the adapters
                # emit strings, so a plain astype(str) matches. Normalizing
                # date-ish columns to 10-char ISO anyway: if a future writer
                # switches to datetime64, str() would yield '... 00:00:00',
                # the anti-join would silently miss EVERY row, and insider
                # transactions would start double-counting with no error.
                out = df[keys].astype(str)
                for c in out.columns:
                    if 'date' in c:
                        out[c] = out[c].str.slice(0, 10)
                return out
            known = set(_norm(master).itertuples(index=False, name=None))
            keep = [t not in known for t in
                    _norm(overlay).itertuples(index=False, name=None)]
            n_dup = len(overlay) - sum(keep)
            overlay = overlay[keep]
        else:
            n_dup = 0
        if overlay.empty:
            logger.info('intraday %s overlay fully superseded by the master '
                        '(%d duplicate row(s))', category, n_dup)
            return master
        merged = pd.concat([master, overlay.reindex(columns=master.columns)],
                           ignore_index=True)
        logger.info('intraday %s overlay: +%d row(s) / %d ticker(s) '
                    '(%d already in master)', category, len(overlay),
                    overlay['ticker'].nunique(), n_dup)
        return merged
    except Exception as e:  # noqa: BLE001
        logger.warning('intraday %s overlay skipped (%s) — master kept',
                       category, e)
        return master


REGIME_LATEST_FILE = ROOT / '.agents' / 'market-state' / 'regime_latest.json'


def load_regime(cur=None) -> dict:
    """Load the latest regime from `regime_latest.json` (file-primary).

    File is the producer's single atomic write; the DB row is an
    append-only history copy now (2026-04-29). Reading the file
    eliminates the file/DB drift class of bug entirely — the
    2026-04-28 LOW_VOL miss specifically came from the DB write
    silently failing while the file stayed fresh, and the engine
    consuming the stale DB.

    Hard fail (exit 2 via SystemExit) when the file is older than
    ENGINE_REGIME_FAIL_HOURS or missing entirely — the orchestrator's
    CycleAbort path then pages the operator instead of silently
    producing TRANSITIONING signals on a LOW_VOL day. Override with
    OPENCLAW_ALLOW_STALE_REGIME=1 only for offline backtest runs.

    `cur` arg is kept for backwards-compat (existing callers pass a
    Postgres cursor) but is unused.
    """
    import os, json
    from datetime import datetime, timezone

    fail_hours  = float(os.environ.get('ENGINE_REGIME_FAIL_HOURS', '80'))
    allow_stale = os.environ.get('OPENCLAW_ALLOW_STALE_REGIME') == '1'

    if not REGIME_LATEST_FILE.exists():
        msg = (f'regime_latest.json missing at {REGIME_LATEST_FILE}. '
               f'Run scripts/run_market_state.py first.')
        logger.error(f'[engine] {msg}')
        print(f'[engine] FATAL: {msg}', flush=True)
        import sys; sys.exit(2)

    mtime = datetime.fromtimestamp(REGIME_LATEST_FILE.stat().st_mtime, tz=timezone.utc)
    age_h = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600.0
    if age_h > fail_hours and not allow_stale:
        msg = (f'regime_latest.json is stale: mtime={mtime.isoformat()} '
               f'({age_h:.1f}h ago, limit={fail_hours}h). Refusing to '
               f'generate signals. Run scripts/run_market_state.py or set '
               f'OPENCLAW_ALLOW_STALE_REGIME=1 for backtests.')
        logger.error(f'[engine] {msg}')
        print(f'[engine] FATAL: {msg}', flush=True)
        import sys; sys.exit(2)

    j = json.loads(REGIME_LATEST_FILE.read_text())
    state    = j.get('state') or 'HIGH_VOL'
    vix      = float(j.get('vix_level') or (j.get('features') or {}).get('vix') or 25.0)
    vix_pct  = float(j.get('vix_percentile') or 50.0)
    return {
        'state':           state,
        'vix_level':       vix,
        'vix_percentile':  vix_pct,
        'regime_data':     j,                # whole file as regime_data
        'updated_at':      mtime.isoformat(),
    }


# ──────────────────────────────────────────────────────────
# 2. LOAD STRATEGIES
# ──────────────────────────────────────────────────────────

def load_approved_strategies(cur):
    cur.execute("""
        SELECT id, name, parameters, status, regime_conditions, universe
        FROM strategy_registry
        WHERE status = 'approved'
    """)
    rows = [dict(r) for r in cur.fetchall()]
    logger.info(f"Found {len(rows)} approved strategies in DB")
    return get_approved_strategies(rows)


# ──────────────────────────────────────────────────────────
# 3. LOAD PRICES
# ──────────────────────────────────────────────────────────

def _memory_footprint() -> str:
    """' peak_rss=1.9GB avail=3.1GB | co-tenants: node 217MB, uvicorn 487MB'.

    Best-effort and never raises — this is a diagnostic tail on a log line, and
    must not be able to fail a run that has already done its work.
    """
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
        avail = None
        try:
            for line in open('/proc/meminfo'):
                if line.startswith('MemAvailable:'):
                    avail = int(line.split()[1]) / 1024 / 1024
                    break
        except OSError:
            pass
        others = []
        me = os.getpid()
        for pid_dir in os.listdir('/proc'):
            if not pid_dir.isdigit() or int(pid_dir) == me:
                continue
            try:
                with open(f'/proc/{pid_dir}/statm') as f:
                    rss_mb = int(f.read().split()[1]) * 4096 / 1048576
                if rss_mb < 150:          # only things big enough to matter
                    continue
                with open(f'/proc/{pid_dir}/comm') as f:
                    others.append((rss_mb, f.read().strip()))
            except (OSError, ValueError, IndexError):
                continue
        others.sort(reverse=True)
        tail = ', '.join(f'{n} {r:.0f}MB' for r, n in others[:5]) or 'none >150MB'
        av = f' avail={avail:.1f}GB' if avail is not None else ''
        return f' peak_rss={peak:.1f}GB{av} | co-tenants: {tail}'
    except Exception:
        return ''


def _parquet_date_axis(path, date_col: str = 'date'):
    """Every distinct value of `date_col` in the parquet, as a sorted DatetimeIndex.

    Reads ONE column via Arrow and dedupes in Arrow (no pandas materialisation):
    ~1.6s and ~600MB transient on the 18.9M-row master, taken and released
    BEFORE the main panel read, so it does not raise the process peak.

    Load-bearing, not cosmetic. Once load_prices pushes the universe filter into
    the parquet read, a date on which only OUT-OF-universe tickers traded
    vanishes from the panel entirely — whereas the old full read kept it as an
    all-NaN row. Reindexing onto this axis preserves the old frame exactly.
    That row is not always harmless downstream: apply_equity_calendar drops
    all-NaN rows, but it is gated (OPENCLAW_EQUITY_TRADING_CALENDAR) and crypto
    ALWAYS gets the full union calendar, so the difference would be live-visible.

    Returns None on any structural surprise; the caller then keeps the filtered
    frame's own index rather than guessing.
    """
    try:
        import pyarrow.parquet as _pq
        import pyarrow.compute as _pc
        tab = _pq.read_table(path, columns=[date_col])
        vals = _pc.unique(tab.column(date_col).combine_chunks()).to_pylist()
        del tab
        _gc.collect()
        return pd.DatetimeIndex(sorted(pd.to_datetime(v) for v in vals))
    except Exception:      # an optimisation/fidelity aid, never a hard dependency
        return None


def load_prices(universe: list) -> pd.DataFrame:
    """Load master price parquet; pivot to wide format (date index × ticker columns, close prices).

    When OPENCLAW_CLOSE_PROXY_SNAPSHOT=1, append a today-dated (ET) row from a
    live close[t]-proxy snapshot so live signal generation mirrors the backtests'
    close[t] decision. The proxy fetch is deliberately OUTSIDE the parquet
    try/except: a CloseProxyError MUST propagate (abort the signals step), never
    be swallowed into an empty frame that would orphan-close the whole book.
    """
    master_path = ROOT / 'data' / 'master' / 'prices.parquet'
    # Fix C: record the PRE-proxy parquet panel max date for eod_compute_health
    # (function-attribute pattern, mirrors run_strategies.last_run_stats). Must
    # be the parquet's own max — a proxy-injected today-row would otherwise
    # mask a failed close-capture from the freshness detector.
    load_prices.last_parquet_max_date = None
    if not master_path.exists():
        logger.warning(f"Master prices not found at {master_path}")
        return pd.DataFrame()
    try:
        # ── OOM hardening (2026-08-06) ──────────────────────────────────────
        # This used to read ALL 18.86M rows / 12,547 tickers / 15 years and then
        # pandas-.pivot() the lot, only to throw ~58% of the columns away on the
        # next line. Measured peak RSS 2608MB for a 3831x5231 result that needs
        # ~160MB. The signals step has been OOM-killed 5 times in 10 days
        # (rc=137: 07-27/28/29, 08-03, 08-05) on an 8GB no-swap box where the
        # kernel's own tally at kill time was 8134MB across 59 tasks — i.e. it
        # died by a couple of hundred MB.
        #
        # Two changes, both provably output-identical (verified 08-06 against
        # the old path: same shape/columns/index/dtypes/NaN-mask, max abs
        # diff 0.0):
        #   1. push the universe filter into the parquet read — the universe is
        #      already resolved before this call (engine logs "live-universe ON:
        #      union N tickers" immediately before "Prices loaded"), so nothing
        #      downstream sees a different frame.
        #   2. build the wide frame by direct numpy scatter. pandas .pivot()
        #      transiently allocates ~1.4GB above the frames it returns.
        # Peak 2608MB -> 1969MB (-639MB, -25%), which is larger than the margin
        # by which both the 08-03 and 08-05 kills overshot.
        _read_kwargs = {'columns': ['ticker', 'date', 'close']}
        _uni = sorted({t for t in (universe or []) if isinstance(t, str)})
        # Take the full date axis BEFORE the panel read (and release it) so the
        # transient does not stack onto the peak; needed to restore any date the
        # universe filter drops. Skipped entirely when we aren't filtering.
        _date_axis = _parquet_date_axis(master_path) if _uni else None
        if _uni:
            _read_kwargs['filters'] = [('ticker', 'in', _uni)]
        df = pd.read_parquet(master_path, **_read_kwargs)

        if df.empty:
            wide = pd.DataFrame()
        else:
            _dates = np.sort(df['date'].unique())
            _tick  = pd.Categorical(df['ticker'])
            _di    = pd.Index(_dates).get_indexer(df['date'])
            _ti    = _tick.codes.astype(np.int64)
            _out   = np.full((len(_dates), len(_tick.categories)), np.nan, dtype=np.float64)
            _out[_di, _ti] = df['close'].to_numpy()
            wide = pd.DataFrame(_out, index=pd.to_datetime(_dates),
                                columns=list(_tick.categories))
            # Drop the long frame and the scatter scratch before anything else
            # allocates — this is the whole point of the rewrite.
            del df, _dates, _tick, _di, _ti, _out
            _gc.collect()
            wide.sort_index(inplace=True)

        cols = [c for c in universe if c in wide.columns]
        if cols:
            wide = wide[cols]
        # Restore any date the universe filter dropped, as the all-NaN row the
        # old full read would have produced. No-op on real data (every date in
        # the master carries at least one universe ticker) but it keeps the
        # rewrite exactly equivalent instead of nearly so.
        if _date_axis is not None and len(_date_axis) and not wide.empty:
            wide = wide.reindex(_date_axis)
        # Fix C's invariant: last_parquet_max_date must be the PARQUET's own max,
        # not the filtered frame's.
        if _date_axis is not None and len(_date_axis):
            load_prices.last_parquet_max_date = _date_axis.max().date()
        elif len(wide.index):
            load_prices.last_parquet_max_date = wide.index.max().date()
    except (OSError, ValueError, KeyError) as e:   # narrow: parquet read/pivot only
        logger.error(f"Failed to load prices: {e}")
        return pd.DataFrame()

    # close[t]-proxy injection — OUTSIDE the try so CloseProxyError propagates.
    if os.environ.get('OPENCLAW_CLOSE_PROXY_SNAPSHOT') == '1':
        from ingestion.close_proxy_snapshot import fetch_close_proxy
        today = pd.Timestamp.now(tz='America/New_York').normalize().tz_localize(None)
        proxy = fetch_close_proxy(list(wide.columns), today)
        if today not in wide.index:
            wide.loc[today] = pd.Series(proxy).reindex(wide.columns)
            wide.sort_index(inplace=True)

    logger.info(f"Prices loaded: {wide.shape[1]} tickers × {wide.shape[0]} dates")
    return wide


_SENTIMENT_COLS = ['ticker', 'date', 'alpaca_news_count_24h', 'alpaca_news_mean_score',
                   'alpaca_news_finbert_pos', 'alpaca_news_finbert_neu', 'alpaca_news_finbert_neg']


def _sentiment_slice(universe: list, as_of=None) -> dict:
    """Live per-ticker news sentiment for aux_data['sentiment'].

    Reads ticker_sentiment_daily.alpaca_news_* — the live home of Alpaca-news FinBERT
    scores — and remaps to the news_* keys S_news_sentiment_long_short expects, applying
    the same point-in-time forward-fill + 7-day staleness cap the backtest aux loader uses.
    (The legacy live news_* columns are a dead RSS source, ~0% covered since 2026-05-22;
    backtest parity is news_* ↔ alpaca_news_*. See src/execution/sentiment_aux.py.)

    Fail-open: any DB error → {} (sentiment feeds one strategy, not the whole cycle).
    """
    if not universe:
        return {}
    try:
        from execution.sentiment_aux import build_sentiment_aux, SENTIMENT_MAX_AGE_DAYS
        from datetime import date as _date
        if as_of is None:
            as_of = _date.today()
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {', '.join(_SENTIMENT_COLS)}
                      FROM ticker_sentiment_daily
                     WHERE ticker = ANY(%s)
                       AND date BETWEEN (%s::date - %s) AND %s::date
                       AND alpaca_news_count_24h > 0
                    """,
                    (list(universe), as_of, SENTIMENT_MAX_AGE_DAYS, as_of),
                )
                rows = [dict(zip(_SENTIMENT_COLS, r)) for r in cur.fetchall()]
        finally:
            conn.close()
        return build_sentiment_aux(rows, as_of=as_of, max_age_days=SENTIMENT_MAX_AGE_DAYS)
    except Exception as e:
        logger.warning(f"Could not load sentiment: {e}")
        return {}


def load_aux_data(universe: list, as_of=None) -> dict:
    """Load financials + insider from master Parquets; convert to dict formats the strategies expect.

    as_of anchors every date-relative aux computation (options DTE / iv_rank
    window, upcoming-earnings split, sentiment slice). Parity fix 2026-08-07:
    these used wall-clock today, so a --date re-run on a later calendar day
    produced DIFFERENT aux → different signals (the engine's half of the
    backtest≡live reproducibility contract). None → today (the daily case,
    behaviour unchanged)."""
    aux = {}
    master_dir = ROOT / 'data' / 'master'
    _as_of_ts = (pd.Timestamp(as_of).normalize() if as_of is not None
                 else pd.Timestamp.today().normalize())

    # Financials: {ticker: {gross_margin, net_margin, ev_ebitda, pe_ratio, ...}}
    # Also provides camelCase aliases for S10_quality_value (FMP field name convention):
    #   roe → returnOnEquity, roic → returnOnInvestedCapital,
    #   gross_margin → grossProfitMargin, debt_equity_ratio → debtEquityRatio,
    #   ev_ebitda → enterpriseValueMultiple, p_fcf_ratio → priceToFreeCashFlowsRatio
    fin_path = master_dir / 'financials.parquet'
    # NOT gated on fin_path.exists(): a present overlay must still be served if
    # the master is missing, or a fresh clone would silently discard tier-1.
    if fin_path.exists() or _intraday_overlay_exists('financials'):
        try:
            fin = (pd.read_parquet(fin_path) if fin_path.exists()
                   else pd.DataFrame(columns=['ticker', 'date']))
            fin = _inject_intraday_rows(fin, 'financials', universe,
                                        dedup_keys=['ticker', 'date'])
            # Use most recent row per ticker
            fin = fin.sort_values('date')
            fin_latest = fin.groupby('ticker').last().reset_index()
            # Prior-period rows, SELF-relative to each ticker's latest filing
            # (a stale ticker's only row must not serve as both "current" and
            # "prior" — that fabricates 0% growth). 250d ≈ ≥3 quarters older ⇒
            # .last() is the ~1-year-back filing; 60d ⇒ the prior quarter.
            # Mirrors aux_data_loader._financials_slice (the backtest twin).
            _fin_dt = pd.to_datetime(fin['date'])
            _fin_latest_dt = _fin_dt.groupby(fin['ticker']).transform('max')
            _fin_py = fin[_fin_dt <= _fin_latest_dt - pd.Timedelta(days=250)] \
                .groupby('ticker')[['total_assets', 'working_capital']].last().to_dict('index')
            _fin_pq = fin[_fin_dt <= _fin_latest_dt - pd.Timedelta(days=60)] \
                .groupby('ticker')[['total_assets']].last().to_dict('index')
            fin_dict = {}
            for _, row in fin_latest.iterrows():
                ticker = row.get('ticker')
                if ticker and ticker in universe:
                    d = {
                        k: (float(v) if pd.notna(v) else None)
                        for k, v in row.items()
                        if k not in ('ticker', 'date', 'period')
                    }
                    # camelCase aliases so S10 can use FMP field names directly
                    d['returnOnEquity']             = d.get('roe')
                    d['returnOnInvestedCapital']     = d.get('roic')
                    d['grossProfitMargin']           = d.get('gross_margin')
                    d['debtEquityRatio']             = d.get('debt_equity_ratio')
                    d['enterpriseValueMultiple']     = d.get('ev_ebitda')
                    d['priceToFreeCashFlowsRatio']   = d.get('p_fcf_ratio')
                    # Altman-Z aliases (S_bankruptcy_risk_anomaly, 2026-07-03)
                    d['totalAssets']                 = d.get('total_assets')
                    d['totalLiabilities']            = d.get('total_liabilities')
                    d['retainedEarnings']            = d.get('retained_earnings')
                    d['workingCapital']              = d.get('working_capital')
                    d['operatingIncome']             = d.get('operating_income')
                    d['marketCap']                   = d.get('market_cap')
                    d['netIncome']                   = d.get('net_income')
                    # Prior-period aliases (2026-08-10; asset-growth / ROA /
                    # accrual candidates). None when the panel lacks history.
                    _py = _fin_py.get(ticker)
                    _pq = _fin_pq.get(ticker)
                    d['totalAssetsPriorYear'] = (
                        float(_py['total_assets'])
                        if _py is not None and pd.notna(_py['total_assets']) else None)
                    d['workingCapitalPriorYear'] = (
                        float(_py['working_capital'])
                        if _py is not None and pd.notna(_py['working_capital']) else None)
                    d['totalAssetsPriorQuarter'] = (
                        float(_pq['total_assets'])
                        if _pq is not None and pd.notna(_pq['total_assets']) else None)
                    fin_dict[ticker] = d
            aux['financials'] = fin_dict
            logger.info(f"Financials loaded: {len(fin_dict)} tickers")
        except Exception as e:
            logger.warning(f"Could not load financials: {e}")

    # Insider transactions: {ticker: [{transactionDate, transactionType, reportingName, value, shares}]}
    insider_path = master_dir / 'insider.parquet'
    if insider_path.exists() or _intraday_overlay_exists('insider'):
        try:
            ins = (pd.read_parquet(insider_path) if insider_path.exists()
                   else pd.DataFrame(columns=['ticker', 'date']))
            # Dedup is load-bearing here: the loop below builds a LIST of every
            # transaction per ticker, so a row present in both the master and
            # the overlay would be COUNTED TWICE by the consuming strategy.
            ins = _inject_intraday_rows(
                ins, 'insider', universe,
                dedup_keys=['ticker', 'date', 'insider_name',
                            'transaction_type', 'shares'])
            ins_dict = {}
            for ticker, grp in ins.groupby('ticker'):
                if ticker not in universe:
                    continue
                txns = []
                for _, row in grp.iterrows():
                    txns.append({
                        'transactionDate': str(row.get('date', '')),
                        'transactionType': str(row.get('transaction_type', '')),
                        'reportingName':   str(row.get('insider_name', '')),
                        'value':           float(row.get('net_value', 0) or 0),
                        'shares':          float(row.get('shares', 0) or 0),
                    })
                ins_dict[ticker] = txns
            aux['insider_txns'] = ins_dict
            logger.info(f"Insider data loaded: {len(ins_dict)} tickers")
        except Exception as e:
            logger.warning(f"Could not load insider: {e}")

    # Options: {ticker: {iv_rank, open_interest_by_strike: {strike: oi}, expiry_date}}
    # Picks nearest future expiry per ticker; sums call+put OI per strike.
    # iv_rank = percentile of current ATM IV vs trailing 30-day history of that ticker's ATM IV.
    opts_path = master_dir / 'options_eod.parquet'
    if opts_path.exists():
        try:
            today = _as_of_ts
            # Pre-compute HV20 per ticker from master prices (options_eod has no hv20 column)
            # Used for rv_20 and vrp fields consumed by S_HV9, S_HV11, S_HV12, S_HV14, S_HV20.
            _hv20_by_ticker = {}
            _hv20_history_by_ticker = {}
            try:
                _px_path = master_dir / 'prices.parquet'
                if _px_path.exists():
                    # Memory-bounded: HV20 needs only the recent window, not 10y of prices.
                    _px = _read_parquet_window(_px_path, ['ticker', 'date', 'close'],
                                               _HV20_PRICES_WINDOW_DAYS, today)
                    _px['date'] = pd.to_datetime(_px['date'])
                    _px = _px.sort_values(['ticker', 'date'])
                    for _t, _g in _px.groupby('ticker'):
                        if _t not in universe or len(_g) < 22:
                            continue
                        _rets = _g['close'].pct_change()
                        # Current HV20 (annualized, fraction)
                        _hv20_now = float(_rets.iloc[-20:].std() * (252 ** 0.5))
                        if _hv20_now > 0:
                            _hv20_by_ticker[_t] = round(_hv20_now, 4)
                        # Last 8 trading days of HV20 history (rolling)
                        _hv_roll = _rets.rolling(20).std() * (252 ** 0.5)
                        _hist = [round(float(v), 4) for v in _hv_roll.dropna().tail(8).tolist()]
                        _hv20_history_by_ticker[_t] = _hist
                    logger.info(f"HV20 pre-computed: {len(_hv20_by_ticker)} tickers")
            except Exception as _e:
                logger.warning(f"HV20 pre-compute failed: {_e}")

            # Memory-bounded: project the 14 referenced columns + a trailing date window
            # via pyarrow pushdown instead of full-loading the multi-million-row
            # options_eod.parquet (the 2026-06-24 signals-step OOM, rc=137). `today` is
            # computed once at the top of this try block (also used by the HV20 read).
            # SP-1 degenerate-row drop happens inside the loader, at the arrow
            # layer, before pandas ever materializes the frame.
            opts = _load_options_window(opts_path, _OPTIONS_SIGNAL_COLS, _OPTIONS_READ_WINDOW_DAYS, today)

            # Ensure expiry is datetime
            if 'expiry' in opts.columns:
                opts['expiry'] = pd.to_datetime(opts['expiry'], errors='coerce')
            elif 'expiration_date' in opts.columns:
                opts = opts.rename(columns={'expiration_date': 'expiry'})
                opts['expiry'] = pd.to_datetime(opts['expiry'], errors='coerce')

            if 'date' in opts.columns:
                opts['date'] = pd.to_datetime(opts['date'], errors='coerce')

            # Tier-1 intraday overlay (three-tier ingestion, 2026-07-30).
            opts = _inject_intraday_options(opts, today, universe)

            # Load earnings calendar for earnings_dte (S-HV17)
            _earnings_path = Path(__file__).resolve().parent.parent.parent / 'data' / 'master' / 'earnings.parquet'
            _earnings_df   = None
            _upcoming_earnings = None
            if _earnings_path.exists():
                try:
                    _earnings_df = pd.read_parquet(_earnings_path)
                    _earnings_df['date'] = pd.to_datetime(_earnings_df['date'], errors='coerce')
                    _today_ts = _as_of_ts
                    _upcoming_earnings = _earnings_df[_earnings_df['date'] >= _today_ts].copy()
                    logger.info(f"Earnings calendar loaded: {len(_upcoming_earnings)} upcoming events")
                except Exception as _e:
                    logger.warning(f"Could not load earnings.parquet: {_e}")
            opts_dict = {}
            _oi_missing_tickers: list = []
            for ticker, grp in opts.groupby('ticker'):
                if ticker not in universe:
                    continue

                # Nearest future expiry with DTE ≤ 45
                future = grp[grp['expiry'] >= today].copy()
                if future.empty:
                    continue
                future['dte'] = (future['expiry'] - today).dt.days
                near = future[future['dte'] <= 45]
                if near.empty:
                    near = future
                nearest_expiry = near['expiry'].min()
                chain = near[near['expiry'] == nearest_expiry]

                # Sum call + put OI per strike
                oi_by_strike = (
                    chain.groupby('strike')['open_interest']
                    .sum()
                    .to_dict()
                )
                # Filter zero-OI strikes
                oi_by_strike = {float(k): float(v) for k, v in oi_by_strike.items() if v and v > 0}

                # IV rank: percentile of today's ATM IV vs trailing 30 days of ATM IV
                iv_rank = 50.0  # default
                if 'implied_volatility' in grp.columns and 'date' in grp.columns:
                    # ATM = strike closest to the most recent close price
                    # Use the current chain to get current ATM IV
                    if 'close' in chain.columns:
                        current_price = chain['close'].iloc[0]
                    else:
                        # Estimate ATM as midpoint of strikes
                        current_price = float(np.median(list(oi_by_strike.keys()))) if oi_by_strike else None

                    if current_price and oi_by_strike:
                        closest_strike = min(oi_by_strike.keys(), key=lambda k: abs(k - current_price))
                        atm_today = chain[chain['strike'].apply(lambda s: abs(float(s) - closest_strike) < 0.01)]
                        current_iv = float(atm_today['implied_volatility'].mean()) if not atm_today.empty else None

                        if current_iv is not None:
                            # 30-day history of this ticker's avg IV
                            cutoff = today - pd.Timedelta(days=30)
                            hist = grp[grp['date'] >= cutoff]
                            daily_iv = hist.groupby('date')['implied_volatility'].mean().dropna()
                            if len(daily_iv) >= 5:
                                lo, hi = daily_iv.min(), daily_iv.max()
                                iv_rank = float(round((current_iv - lo) / (hi - lo) * 100, 1)) if hi > lo else 50.0

                # iv30: mean implied_volatility across the nearest-expiry chain (raw IV, not percentile)
                iv30 = None
                if 'implied_volatility' in chain.columns:
                    iv_vals = chain['implied_volatility'].dropna()
                    if not iv_vals.empty:
                        iv30 = float(iv_vals.mean())

                # volume: total options volume for this ticker's nearest-expiry chain
                chain_volume = 0.0
                if 'volume' in chain.columns:
                    chain_volume = float(chain['volume'].fillna(0).sum())

                #  HV-strategy enrichments 
                # pc_ratio: put/call volume ratio (most recent date)
                pc_ratio = None
                if 'option_type' in chain.columns and 'volume' in chain.columns:
                    if 'date' in chain.columns:
                        latest_dt = chain['date'].max()
                        today_chain = chain[chain['date'] == latest_dt]
                    else:
                        today_chain = chain
                    c_v = float(today_chain[today_chain['option_type'].str.upper() == 'CALL']['volume'].fillna(0).sum())
                    p_v = float(today_chain[today_chain['option_type'].str.upper() == 'PUT']['volume'].fillna(0).sum())
                    pc_ratio = round(p_v / c_v, 4) if c_v > 0 else None

                # gamma_atm: mean gamma of near-ATM options (|delta| 0.40-0.60)
                gamma_atm = None
                if 'delta' in chain.columns and 'gamma' in chain.columns:
                    atm_src = chain[chain['date'] == chain['date'].max()] if 'date' in chain.columns else chain
                    atm_opts = atm_src[atm_src['delta'].abs().between(0.40, 0.60)]
                    if not atm_opts.empty:
                        gamma_atm = round(float(atm_opts['gamma'].mean()), 6)


                # theta_atm: mean theta of near-ATM options (|delta| 0.40-0.60)
                theta_atm = None
                if 'delta' in chain.columns and 'theta' in chain.columns:
                    atm_src2 = chain[chain['date'] == chain['date'].max()] if 'date' in chain.columns else chain
                    atm_opts2 = atm_src2[atm_src2['delta'].abs().between(0.40, 0.60)]
                    if not atm_opts2.empty:
                        theta_atm = round(float(atm_opts2['theta'].mean()), 6)
                # rv_20: current HV20 (computed from master prices above);
                # vrp: implied vol premium over realized vol.
                rv_20 = _hv20_by_ticker.get(ticker)
                vrp = round(iv30 - rv_20, 4) if (iv30 is not None and rv_20 is not None) else None

                # History arrays (last 8 trading days)
                iv_rank_history = []; pc_ratio_history = []; vrp_history = []; hv20_history = []
                if 'date' in grp.columns:
                    for d in sorted(grp['date'].unique())[-8:]:
                        day = grp[grp['date'] == d]
                        if 'implied_volatility' in day.columns:
                            day_iv = float(day['implied_volatility'].mean())
                            hist_iv = grp[grp['date'] <= d].groupby('date')['implied_volatility'].mean().dropna()
                            if len(hist_iv) >= 5:
                                lo_d, hi_d = float(hist_iv.min()), float(hist_iv.max())
                                iv_rank_history.append(round((day_iv-lo_d)/(hi_d-lo_d)*100,1) if hi_d>lo_d else 50.0)
                        if 'option_type' in day.columns and 'volume' in day.columns:
                            c_dv = float(day[day['option_type'].str.upper()=='CALL']['volume'].fillna(0).sum())
                            p_dv = float(day[day['option_type'].str.upper()=='PUT']['volume'].fillna(0).sum())
                            pc_ratio_history.append(round(p_dv/c_dv,4) if c_dv>0 else None)
                # hv20_history: from pre-computed price-based HV20 (options_eod has no hv20 col).
                hv20_history = _hv20_history_by_ticker.get(ticker, [])
                # vrp_history: zip IV30 history with HV20 history where both exist.
                if 'date' in grp.columns and hv20_history:
                    _iv_by_date = grp.groupby('date')['implied_volatility'].mean().dropna().sort_index()
                    _last_iv = [round(float(v), 4) for v in _iv_by_date.tail(len(hv20_history)).tolist()]
                    vrp_history = [round(iv - hv, 4) for iv, hv in zip(_last_iv, hv20_history) if iv is not None and hv is not None]
                # 


                #  S-HV13: iv_spread (call_iv - put_iv, ATM, front-month) 
                iv_spread = None
                if 'date' in chain.columns and 'option_type' in chain.columns and 'delta' in chain.columns:
                    import pandas as _pd
                    _ld = chain['date'].max()
                    _td = chain[chain['date'] == _ld].copy()
                    _td['_dte'] = (_pd.to_datetime(_td['expiry']) - _pd.to_datetime(_ld)).dt.days if 'expiry' in _td.columns else 999
                    _fm = _td[_td['_dte'].between(5, 40)]
                    _calls_atm = _fm[(_fm['option_type'].str.upper()=='CALL') & (_fm['delta'].between(0.40,0.60))]
                    _puts_atm  = _fm[(_fm['option_type'].str.upper()=='PUT')  & (_fm['delta'].abs().between(0.40,0.60))]
                    if not _calls_atm.empty and not _puts_atm.empty:
                        iv_spread = round(float(_calls_atm['implied_volatility'].mean()) - float(_puts_atm['implied_volatility'].mean()), 4)

                #  S-HV14: skew_20d (20-delta put IV - 50-delta call IV, smirk) 
                skew_20d = None
                if 'date' in chain.columns and 'delta' in chain.columns and 'option_type' in chain.columns:
                    _ld2 = chain['date'].max()
                    _td2 = chain[chain['date'] == _ld2]
                    _otm_puts = _td2[(_td2['option_type'].str.upper()=='PUT') & (_td2['delta'].between(-0.25,-0.15))]
                    _atm_calls = _td2[(_td2['option_type'].str.upper()=='CALL') & (_td2['delta'].between(0.45,0.55))]
                    if not _otm_puts.empty and not _atm_calls.empty:
                        skew_20d = round(float(_otm_puts['implied_volatility'].mean()) - float(_atm_calls['implied_volatility'].mean()), 4)

                #  S-HV15: term structure (near_iv / far_iv) 
                near_iv_ts = None; far_iv_ts = None; ts_ratio = None
                if 'date' in chain.columns and 'delta' in chain.columns and 'expiry' in chain.columns:
                    import pandas as _pd2
                    _ld3 = chain['date'].max()
                    _td3 = chain[chain['date'] == _ld3].copy()
                    _td3['_dte3'] = (_pd2.to_datetime(_td3['expiry']) - _pd2.to_datetime(_ld3)).dt.days
                    _atm3 = _td3[_td3['delta'].abs().between(0.40, 0.60)]
                    _near = _atm3[_atm3['_dte3'].between(5, 35)]
                    _far  = _atm3[_atm3['_dte3'].between(55, 95)]
                    if not _near.empty and not _far.empty:
                        near_iv_ts = round(float(_near['implied_volatility'].mean()), 4)
                        far_iv_ts  = round(float(_far['implied_volatility'].mean()), 4)
                        ts_ratio   = round(near_iv_ts / far_iv_ts, 4) if far_iv_ts > 0 else None

                #  S-HV16: gex (net dealer gamma exposure) 
                gex = None
                if 'gamma' in chain.columns and 'open_interest' in chain.columns and 'option_type' in chain.columns:
                    _ld4 = chain['date'].max() if 'date' in chain.columns else None
                    _td4 = chain[chain['date'] == _ld4] if _ld4 is not None else chain
                    # open_interest is 100% NULL in the current provider feed
                    # (measured 2026-07-29 across 3.3M contract rows). Without
                    # this guard the NaN products sum to 0.0 and gex reports a
                    # confident 0.0 — "no dealer gamma imbalance" — to S_HV16,
                    # which is a fabricated fact rather than missing data.
                    # None makes the absence visible so the strategy stands down.
                    _oi4 = pd.to_numeric(_td4['open_interest'], errors='coerce')
                    if _oi4.notna().any() and (_oi4.fillna(0) > 0).any():
                        _c4  = _td4[_td4['option_type'].str.upper() == 'CALL']
                        _p4  = _td4[_td4['option_type'].str.upper() == 'PUT']
                        _gc  = float((_c4['gamma'] * _c4['open_interest']).sum())
                        _gp  = float((_p4['gamma'] * _p4['open_interest']).sum())
                        gex  = round((_gc - _gp) * 100, 2)   # per 1-point move, scaled
                    else:
                        _oi_missing_tickers.append(ticker)

                #  S-HV19: iv_centroid_delta + surface_premium 
                iv_centroid_delta = None; surface_premium = None
                if all(c in chain.columns for c in ['vega','delta','open_interest','implied_volatility']):
                    _ld5 = chain['date'].max() if 'date' in chain.columns else None
                    _td5 = chain[chain['date'] == _ld5] if _ld5 is not None else chain
                    _td5 = _td5.copy()
                    _td5['_w'] = _td5['vega'].abs() * _td5['open_interest']
                    _tw = float(_td5['_w'].sum())
                    if _tw > 0:
                        iv_centroid_delta = round(float((_td5['delta'] * _td5['_w']).sum() / _tw), 4)
                        _vwiv = float((_td5['implied_volatility'] * _td5['_w']).sum() / _tw)
                        _atm5 = _td5[_td5['delta'].abs().between(0.45, 0.55)]
                        _atm_iv5 = float(_atm5['implied_volatility'].mean()) if not _atm5.empty else _vwiv
                        surface_premium = round(_vwiv - _atm_iv5, 4)

                
                # earnings_dte (S-HV17): days to next earnings announcement
                earnings_dte = None
                if _upcoming_earnings is not None and not _upcoming_earnings.empty:
                    _t_earn = _upcoming_earnings[_upcoming_earnings['ticker'] == ticker]
                    if not _t_earn.empty:
                        _next_earn = _t_earn['date'].min()
                        earnings_dte = int((_next_earn - _today_ts).days)

                opts_dict[ticker] = {
                    'iv_rank':                 iv_rank,
                    'iv30':                    iv30,
                    'volume':                  chain_volume,
                    'open_interest_by_strike': oi_by_strike,
                    'expiry_date':             nearest_expiry.date().isoformat(),
                    'pc_ratio':               pc_ratio,
                    'gamma_atm':              gamma_atm,
                    'theta_atm':              theta_atm,
                    'iv_spread':              iv_spread,
                    'skew_20d':               skew_20d,
                    'ts_ratio':               ts_ratio,
                    'near_iv':                near_iv_ts,
                    'far_iv':                 far_iv_ts,
                    'gex':                    gex,
                    'iv_centroid_delta':      iv_centroid_delta,
                    'surface_premium':        surface_premium,
                    'earnings_dte':           earnings_dte,
                    'rv_20':                  rv_20,
                    'vrp':                    vrp,
                    'iv_rank_history':        iv_rank_history,
                    'pc_ratio_history':       pc_ratio_history,
                    'vrp_history':            vrp_history,
                    'hv20_history':           hv20_history,
                }

            # Inject last_price from prices.parquet (long-format, always available)
            try:
                _px_path = master_dir / 'prices.parquet'
                if _px_path.exists():
                    import pandas as _pd2
                    _px = _pd2.read_parquet(_px_path, columns=['ticker','close'])
                    _lp = _px.groupby('ticker')['close'].last().to_dict()
                    for _tk, _od in opts_dict.items():
                        _od['last_price'] = _lp.get(_tk)
            except Exception as _lpe:
                logger.warning(f'last_price load failed: {_lpe}')
            aux['options'] = opts_dict
            logger.info(f"Options loaded: {len(opts_dict)} tickers")
            if _oi_missing_tickers:
                # LOUD, not silent: OI-derived fields are unavailable, so any
                # strategy gated on them (S_HV16 gex, s5_max_pain strike OI)
                # emits nothing rather than acting on fabricated zeros.
                logger.warning(
                    "options open_interest ABSENT for %d/%d tickers — gex reported "
                    "as None (was a false 0.0 before 2026-07-29); OI-gated "
                    "strategies will stand down until a provider supplies it",
                    len(_oi_missing_tickers), len(opts_dict))
        except Exception as e:
            logger.warning(f"Could not load options: {e}")

    # 30-min intraday bars: full DataFrame (date, datetime, ticker, o/h/l/c/v/vwap)
    # Used by S-TR-04 (Zarattini) and future intraday strategies.
    bars_30m_path = master_dir / 'prices_30m.parquet'
    if bars_30m_path.exists():
        try:
            bars_30m = pd.read_parquet(bars_30m_path)
            if not bars_30m.empty:
                aux['prices_30m'] = bars_30m
                logger.info(f"30m bars loaded: {len(bars_30m):,} rows, {bars_30m['ticker'].nunique()} tickers")
        except Exception as e:
            logger.warning(f"Could not load prices_30m: {e}")

    # Macro time series: {series_name: pd.Series(date_index → value)}
    # Provides VIX, VVIX, VIX3M, etc. for regime-aware strategies (S-TR-01 etc.)
    macro_path = master_dir / 'macro.parquet'
    if macro_path.exists():
        try:
            mac = pd.read_parquet(macro_path)
            if not mac.empty and {'date', 'series', 'value'}.issubset(mac.columns):
                mac['date'] = pd.to_datetime(mac['date'])
                mac_dict: dict[str, pd.Series] = {}
                for series_name, grp in mac.groupby('series'):
                    mac_dict[series_name] = (
                        grp.set_index('date')['value']
                        .sort_index()
                        .dropna()
                    )
                aux['macro'] = mac_dict
                logger.info(f"Macro loaded: {list(mac_dict.keys())} series")
        except Exception as e:
            logger.warning(f"Could not load macro: {e}")

    # Sentiment: {ticker: {news_count_24h, news_mean_score, news_finbert_pos/neu/neg}}
    # Read from ticker_sentiment_daily.alpaca_news_* (live Alpaca-news FinBERT scores),
    # remapped to the news_* keys S_news_sentiment_long_short expects — backtest parity.
    aux['sentiment'] = _sentiment_slice(universe, as_of=_as_of_ts.date())
    logger.info(f"Sentiment loaded: {len(aux['sentiment'])} tickers")

    return aux


# ──────────────────────────────────────────────────────────
# 4. RUN STRATEGIES
# ──────────────────────────────────────────────────────────

def _apply_regime_overrides_to_signals(strategy_id, signals, regime_state):
    """Mutate each Signal's stop_loss/target_1 with the per-(strategy, regime)
    override (gated; no-op when OPENCLAW_BACKTEST_COUPLED_RECS is unset). Mirrors
    the backtest's simulate-time application so live and backtest agree."""
    ov = regime_param_override.resolve_override(strategy_id, str(regime_state))
    if not ov:
        return
    for sig in signals or []:
        # Classify via the shared normalizer (mirrors the backtest's
        # _signal_to_long_short): LONG/BUY/BUY_VOL → +1, SHORT/SELL/SELL_VOL → -1,
        # FLAT/unknown → 0. Skip 0 so FLAT/unknown directions get NO override —
        # the backtest skips those trades, so live must too (parity + safety:
        # never persist an inverted bracket for a non-long/short signal).
        d = regime_param_override.direction_sign(sig.direction)
        if d == 0:
            continue
        ep = float(sig.entry_price) if getattr(sig, 'entry_price', 0) else 0.0
        if ep <= 0:
            continue
        sig.stop_loss, sig.target_1 = regime_param_override.apply_override(
            entry_price=ep, direction=d,
            stop_loss=float(sig.stop_loss or 0), target_1=float(sig.target_1 or 0),
            override=ov)


_TICKER_KEYED_AUX = ('financials', 'insider_txns', 'options', 'sentiment')


def _slice_aux(aux_data: dict, universe_set: set) -> dict:
    """SP-7 C1: per-strategy aux slice. Ticker-keyed dicts are filtered;
    prices_30m (long DataFrame with a ticker column) is row-filtered; macro
    (series-name keyed) passes through whole. Slicing aux alongside the price
    panel makes identical-universe ⇒ identical-signals airtight even for
    strategies that iterate aux keys instead of the universe param.

    NOTE (latent no-op, do NOT fix here): the last_price inject at engine.py
    ~line 1546-1552 calls prices.groupby('ticker') on the WIDE pivoted frame
    (no ticker column) → its except swallows it → the inject is already a
    silent no-op live. _slice_aux neither worsens nor masks this."""
    out = dict(aux_data)
    for k in _TICKER_KEYED_AUX:
        v = aux_data.get(k)
        if isinstance(v, dict):
            out[k] = {t: d for t, d in v.items() if t in universe_set}
    p30 = aux_data.get('prices_30m')
    if p30 is not None and hasattr(p30, 'columns') and 'ticker' in p30.columns:
        out['prices_30m'] = p30[p30['ticker'].isin(universe_set)]
    return out


def _stamp_cadence_reset_on_flip(cur, regime) -> None:
    """Regime-flip day detection (operator directive 2026-08-13).

    On the day the regime-of-record changes, the new regime's book must be
    built same-day by EVERY eligible strategy — not just the ones whose
    rebalance calendar happens to land on the flip. Compare today's regime
    against the regime the most recent persisted signal set was built under;
    on mismatch stamp regime['cadence_reset']=True, which rebalance-calendar
    strategies OR into their boundary gates (StrategyBase.cadence_reset).
    Cadence persistence then restarts naturally from the flip-day signal_date.

    Covers both lanes (EOD signals step and the intraday-redeploy signals
    fragment) because both run this engine. Same-day second runs see the
    flip-day signals already tagged with the new regime → no re-stamp, so
    the forced emission fires exactly once per flip.

    Fail-quiet on DB errors (no stamp): a transient DB hiccup must degrade to
    the historical calendar-gated behavior, never force a mass emission.
    Kill switch OPENCLAW_CADENCE_RESET_ON_FLIP=0; manual force
    OPENCLAW_FORCE_CADENCE_RESET=1 (e.g. re-running an aborted flip day)."""
    if not isinstance(regime, dict) or not regime.get('state'):
        return
    if os.environ.get('OPENCLAW_FORCE_CADENCE_RESET') == '1':
        regime['cadence_reset'] = True
        logger.warning('[engine] cadence reset FORCED via OPENCLAW_FORCE_CADENCE_RESET=1 '
                       '— all rebalance-cadence strategies emit this run')
        return
    if os.environ.get('OPENCLAW_CADENCE_RESET_ON_FLIP', '1') != '1':
        return
    try:
        # created_at tiebreak matters on a double-flip day: same signal_date can
        # carry rows from two regimes; the NEWEST mint must win or the probe
        # would re-force emission against an already-current book.
        cur.execute("""SELECT regime_state FROM execution_signals
                       WHERE regime_state IS NOT NULL
                       ORDER BY signal_date DESC, created_at DESC NULLS LAST LIMIT 1""")
        row = cur.fetchone()
    except Exception as e:
        logger.warning('[engine] regime-flip probe failed (%s) — cadence gates unchanged', e)
        return
    prev = row[0] if row else None
    if prev is not None and prev == regime['state']:
        return
    regime['cadence_reset'] = True
    logger.warning('[engine] 🔁 REGIME FLIP DAY: %s → %s — cadence-gate bypass armed: '
                   'all eligible rebalance-cadence strategies emit today; cadence '
                   'windows restart from today (operator directive 2026-08-13)',
                   prev or '(no prior signals)', regime['state'])


def run_strategies(strategies, prices, regime, universe, aux_data,
                   strategy_universes=None) -> dict:
    """
    Returns: {strategy_id: [Signal, ...]}

    Side-effect: sets run_strategies.last_run_stats after every call:
        {'total': <len(strategies)>, 'errored': <count of exception paths>,
         'ran_ok': total - errored}
    Strategies that were skipped (regime-ineligible, no crypto regime) are NOT
    counted as errored — they didn't raise.  Callers that need to distinguish
    "ran fine but emitted zero signals" from "raised an exception" should read
    last_run_stats rather than checking `if signals` on the results dict (which
    cannot distinguish the two cases since both store []).
    """
    # regime is the full dict from load_regime(); the eligibility gate takes
    # the regime-state string. Strategies still get the full dict.
    equity_regime_str = regime['state'] if isinstance(regime, dict) else regime
    _crypto_regime = None  # lazily loaded only if a crypto strategy is present
    results = {}
    errored_ids: set = set()
    for strat in strategies:
        try:
            # SP-3.1 Phase C: crypto strategies gate on the CRYPTO regime, not equity.
            ic = instrument_class_for(strat.id)
            if ic == 'crypto':
                if _crypto_regime is None:
                    _crypto_regime = load_crypto_regime_state()
                strat_regime = _crypto_regime
                strat_regime_str = _crypto_regime.get('state')
                if not strat_regime_str:
                    logger.info('[engine] %s skipped — no crypto regime available', strat.id)
                    continue
            else:
                strat_regime = regime
                strat_regime_str = equity_regime_str
            if not is_eligible(strat.id, strat_regime_str):
                logger.info('[engine] %s skipped — regime %s not eligible (strategy_regime_params)', strat.id, strat_regime_str)
                continue
            # Eligibility is decided SOLELY by the DB gate above — the activation
            # slider derived from backtest performance (strategy_regime_params).
            # The strategy's own should_run(active_in_regimes) is a stale SECOND
            # gate that would silently drop signals for a regime the backtest
            # qualified but the author's active_in_regimes omits (the
            # "silent-dead" class — ERR-20260721-002). Mirror the backtest's
            # discovery-mode override (unified_backtest.py ~949): once the DB says
            # eligible for this regime, widen THIS INSTANCE's active_in_regimes so
            # should_run cannot veto the current regime. Strategies that branch on
            # regime['state'] INSIDE generate_signals still see the live regime.
            _air = getattr(strat, 'active_in_regimes', None)
            if strat_regime_str and _air is not None and strat_regime_str not in _air:
                strat.active_in_regimes = [*_air, strat_regime_str]
            # SP-7 Phase C (C1): per-strategy universe slice. None (gate OFF)
            # → byte-identical legacy behavior: shared panel/universe/aux.
            if strategy_universes is not None and strat.id in strategy_universes:
                _su = strategy_universes[strat.id]
                _su_set = set(_su)
                strat_prices = prices[[c for c in prices.columns if c in _su_set]]
                strat_aux = _slice_aux(aux_data, _su_set)
                strat_universe = _su
            else:
                strat_prices, strat_aux, strat_universe = prices, aux_data, universe
            if calendar_for(ic) == 'equity':
                strat_prices = apply_equity_calendar(strat_prices)
            _t_gen = time.perf_counter()
            signals = strat.generate_signals(strat_prices, strat_regime, strat_universe, strat_aux)
            _gen_s = time.perf_counter() - _t_gen
            _apply_regime_overrides_to_signals(strat.id, signals, strat_regime_str)
            if signals and getattr(strat, 'calendar_edge', False):
                # Stamp travels in signal_params (jsonb) so the sizer's loaders
                # can port this signal across regime flips for the rest of its
                # cadence window, gated on eligibility (operator 2026-08-13).
                for _s in signals:
                    _s.signal_params = {**(getattr(_s, 'signal_params', None) or {}),
                                        'calendar_edge': True}
            results[strat.id] = signals or []
            # Per-strategy input diagnostics (§2 Phase A, 2026-08-06 spec): 36
            # approved strategies ran clean and returned [] on every live day
            # while backtesting thousands of trades, and nothing recorded WHAT
            # each strategy was actually handed. panel is the post-slice,
            # post-calendar frame generate_signals received.
            _aux_desc = ','.join(
                f"{k}:{len(v) if hasattr(v, '__len__') else '?'}"
                for k, v in sorted((strat_aux or {}).items()))
            _pshape = getattr(strat_prices, 'shape', None)
            logger.info(
                f"  {strat.id}: {len(results[strat.id])} signals "
                f"[universe={len(strat_universe or [])} "
                f"panel={f'{_pshape[0]}x{_pshape[1]}' if _pshape is not None else 'none'} "
                f"aux={{{_aux_desc}}} t={_gen_s:.2f}s]")
        except Exception as e:
            logger.error(f"  {strat.id} FAILED: {e}\n{traceback.format_exc()}")
            errored_ids.add(strat.id)
            results[strat.id] = []
    total = len(strategies)
    run_strategies.last_run_stats = {
        'total': total,
        'errored': len(errored_ids),
        'ran_ok': total - len(errored_ids),
    }
    return results


# ──────────────────────────────────────────────────────────
# 4.5. WRITE EOD COMPUTE HEALTH SENTINEL
# ──────────────────────────────────────────────────────────

def write_eod_health(cur, run_date: date, *, rc: int, n_strategies_ok: int,
                     n_strategies_total: int, regime_ok: bool, universe_size: int,
                     panel_max_date: date | None = None,
                     panel_fresh_required: bool | None = None) -> None:
    """
    INSERT one eod_compute_health row after run_strategies.

    Gated by _eod_signal_register_gate_on(): caller is responsible for the gate
    check so this function can also be called directly in tests with any cursor.

    n_strategies_ok: strategies that executed WITHOUT raising an exception,
        regardless of whether they emitted signals.  Read from
        run_strategies.last_run_stats['ran_ok'] at the call-site.
        An all-flat successful day (every strategy ran fine, none emitted
        signals) → ran_ok == total > 0 → healthy=True → Task 8 flatten fires.
        An all-errored day → ran_ok=0 → healthy=False → flatten refused.
        (Cannot infer this from strategy_results.values() because both the
        exception path and the genuine-zero-signal path store [].)

    healthy = (rc == 0 AND regime_ok AND universe_size > 0 AND n_strategies_ok > 0)
    """
    # Fix C (2026-06-04, sp6 diagnosis §10): a POST-CLOSE compute deciding on a
    # close[T−1] panel must not report healthy — that exact failure ran silent
    # on 06-02/06-03. panel_fresh_required=True only for the 16:15 EOD compute
    # shape (intraday redeploys correctly use close[T−1] and stay exempt).
    # Required-but-unknown panel date fails CLOSED.
    panel_ok = True
    if panel_fresh_required:
        panel_ok = panel_max_date is not None and panel_max_date >= run_date
    healthy = (rc == 0 and regime_ok and universe_size > 0 and n_strategies_ok > 0
               and panel_ok)
    detail = {
        'rc': rc,
        'n_strategies_ok': n_strategies_ok,
        'n_strategies_total': n_strategies_total,
        'regime_ok': regime_ok,
        'universe_size': universe_size,
        'panel_max_date': str(panel_max_date) if panel_max_date else None,
        'panel_fresh_required': bool(panel_fresh_required),
        'panel_ok': panel_ok,
        'healthy': healthy,
    }

    cur.execute(
        """
        INSERT INTO eod_compute_health
            (run_date, rc, n_strategies_ok, n_strategies_total,
             regime_ok, universe_size, healthy, detail, panel_max_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (run_date, rc, n_strategies_ok, n_strategies_total,
         regime_ok, universe_size, healthy, json.dumps(detail), panel_max_date),
    )

    logger.info(
        "[engine] eod_compute_health: rc=%s ok=%s/%s regime_ok=%s "
        "universe_size=%s panel_max_date=%s panel_ok=%s healthy=%s",
        rc, n_strategies_ok, n_strategies_total, regime_ok, universe_size,
        panel_max_date, panel_ok, healthy,
    )


# ──────────────────────────────────────────────────────────
# 5. WRITE SIGNALS
# ──────────────────────────────────────────────────────────

def _params_with_option_spec(sig):
    """Build the signal_params dict for an execution_signals row, folding in
    Signal.features and Signal.option_spec (SP-5.1c). option_spec is additive:
    equity/crypto/etp signals leave it None -> key absent -> byte-identical."""
    import math as _math, dataclasses as _dc

    def _to_native(v):
        if hasattr(v, 'item'):
            v = v.item()   # numpy scalar → Python scalar
        if isinstance(v, float) and not _math.isfinite(v):
            return None    # Postgres rejects NaN/Inf in jsonb
        if isinstance(v, dict):
            return {k: _to_native(vv) for k, vv in v.items()}
        if isinstance(v, (list, tuple)):
            return [_to_native(vv) for vv in v]
        return v

    params = {k: _to_native(v) for k, v in (sig.signal_params or {}).items()}
    feats = getattr(sig, 'features', None)
    if feats:
        params['features'] = {k: _to_native(v) for k, v in feats.items()}
    spec = getattr(sig, 'option_spec', None)
    if spec is not None:
        params['option_spec'] = {k: _to_native(v) for k, v in _dc.asdict(spec).items()}
    return params



def write_signals(cur, strategy_results: dict, regime_state: str, run_date: date) -> int:
    total = 0
    # Hoist gate check and next-trading-day lookup ONCE per call (not per signal).
    # When gate is OFF, next_td stays None — no CLI subprocess spawned.
    _gate_on = _eod_signal_register_gate_on()
    # Same-day mode (2026-07-29 pivot): signals carry the FULL SP-6 lifecycle
    # (lifecycle_state/computed_at/target_date + continuation mints) with
    # target_date = run_date (T) — execute-today semantics. Legacy NULL-
    # lifecycle rows would leave tomorrow's premarket reconcile blind to
    # today's book (zero-APPROVED + sentinel ⇒ deliberate flatten).
    _sameday = os.environ.get('OPENCLAW_SAMEDAY_EXEC') == '1'
    _lifecycle_rows = _gate_on or _sameday
    _next_td = (_next_trading_day(run_date) if _gate_on
                else (run_date if _sameday else None))
    for strategy_id, signals in strategy_results.items():
        for sig in signals:
            try:
                # Guard: skip signals with NaN/Inf in core price fields — they
                # corrupt the sizer and break the no_new_nan_signals_after_fix check.
                import math as _math
                _price_fields = [sig.entry_price, sig.stop_loss, sig.target_1]
                if any(v is not None and not _math.isfinite(float(v)) for v in _price_fields):
                    logger.warning(
                        f'[engine] Dropping NaN signal: {strategy_id}/{sig.ticker} '
                        f'(entry={sig.entry_price}, stop={sig.stop_loss}, t1={sig.target_1})'
                    )
                    continue
                # Guard: skip signals with degenerate geometry (stop == entry or inverted).
                # Root cause: ATR=0 from stale/ffilled constant price series.
                _ep, _sl, _t1 = float(sig.entry_price), float(sig.stop_loss), float(sig.target_1)
                _dirn = (sig.direction or '').upper()
                if _dirn in ('LONG', 'SHORT') and all(_math.isfinite(v) for v in [_ep, _sl, _t1]):
                    _bad = ((_dirn == 'LONG' and not (_t1 > _ep > _sl)) or
                            (_dirn == 'SHORT' and not (_t1 < _ep < _sl)))
                    if _bad:
                        logger.warning(
                            f'[engine] Dropping degenerate-geometry signal: {strategy_id}/{sig.ticker} '
                            f'(direction={_dirn} entry={_ep:.4f} stop={_sl:.4f} t1={_t1:.4f})'
                        )
                        continue
                # Serialize signal_params (SP-5.1c): delegate to module-level helper
                # which reproduces the original _to_native + features-fold behaviour
                # and additionally folds in option_spec when present (additive:
                # equity/crypto/etp leave option_spec=None → key absent → byte-identical).
                params_clean = _params_with_option_spec(sig)

                cur.execute("SAVEPOINT sp_signal")
                # 2026-05-19 Phase A: bracket-upsert per (strategy, ticker).
                # Invariant: at most ONE open execution_signals row per
                # (strategy_id, ticker). Re-emissions don't insert a new row;
                # they UPDATE the existing one's bracket fields so the
                # strategy's current intent (stop/target/size/signal_params)
                # is reflected in DB, dashboard, and sizer reads on every
                # cycle. entry_price + signal_date stay at the original
                # entry (audit trail of when the position was first taken).
                #
                # Live broker bracket leg is NOT touched here — that's
                # Phase B (scripts/sync_brackets.py, gated by
                # OPENCLAW_DAILY_BRACKET_REPLACE=1). Without B, a held
                # position's broker stop-leg keeps its day-0 values until
                # natural exit + re-entry; the DB and dashboard always
                # show the strategy's current intent.
                if _lifecycle_rows:
                    # Lifecycle rows: continuation mints create MULTIPLE open rows per
                    # (strategy_id, ticker) over time, so the match must be
                    # DETERMINISTIC — pick the NEWEST target_date so the spent-
                    # check below compares against the latest intent.  Also fetch
                    # target_date for that spent-check.
                    cur.execute("""
                        SELECT id, target_date FROM execution_signals
                         WHERE strategy_id = %s AND ticker = %s AND status = 'open'
                         ORDER BY target_date DESC NULLS LAST
                         LIMIT 1
                    """, (strategy_id, sig.ticker))
                else:
                    # Gate OFF: byte-identical legacy SELECT.
                    cur.execute("""
                        SELECT id FROM execution_signals
                         WHERE strategy_id = %s AND ticker = %s AND status = 'open'
                         LIMIT 1
                    """, (strategy_id, sig.ticker))
                existing_row = cur.fetchone()
                # SP-6 Phase C (§13 durable fix): a held position's row keeps
                # status='open' until pnl closes it; the bracket-refresh below
                # used to match-and-refresh that SPENT row (target_date already
                # consumed), so re-emissions never minted a row for the NEXT
                # target_date — held tickers were locked out of subsequent target
                # sets ⇒ structural 1-day max-hold.  Fix: when gate ON and the
                # matched row is spent (its target_date < _next_td computed from
                # THIS run_date), do NOT refresh — INSERT a fresh COMPUTED row for
                # the next target_date (new signal_date ⇒ no unique-key collision).
                # The spent row keeps tracking the live position (pnl continuity);
                # parity_mark.finalize_parity_marks closes it as the CONTINUATION
                # fills (D1: close_reason='rolled_continuation').
                _continuation_mint = False
                if existing_row and _lifecycle_rows:
                    _existing_td = existing_row[1]
                    if _existing_td is not None and _next_td is not None \
                            and _existing_td < _next_td:
                        _continuation_mint = True
                if existing_row and not _continuation_mint:
                    cur.execute("""
                        UPDATE execution_signals
                           SET stop_loss        = %s,
                               target_1         = %s,
                               target_2         = %s,
                               target_3         = %s,
                               position_size_pct = %s,
                               regime_state     = %s,
                               signal_params    = %s::jsonb
                         WHERE id = %s
                    """, (
                        sig.stop_loss, sig.target_1, sig.target_2, sig.target_3,
                        sig.position_size_pct, regime_state,
                        json.dumps(params_clean), existing_row[0],
                    ))
                    # Post-update geometry guard: when entry_price is frozen
                    # (audit-trail invariant) but the new stop/target were
                    # computed from a different current price (e.g. intraday
                    # redeploy vs main pipeline running at different times),
                    # the update can produce stop > entry for LONG or stop <
                    # entry for SHORT.  Roll back the UPDATE in that case —
                    # the pre-existing clean geometry is safer than an inverted
                    # bracket.
                    cur.execute("""
                        SELECT entry_price, stop_loss, target_1, direction
                          FROM execution_signals WHERE id = %s
                    """, (existing_row[0],))
                    _chk = cur.fetchone()
                    if _chk:
                        _ep, _sl, _t1, _dirn = (float(_chk[0]), float(_chk[1]),
                                                 float(_chk[2]), _chk[3])
                        _bad_geo = (
                            all(_math.isfinite(v) for v in [_ep, _sl, _t1]) and (
                                (_dirn.upper() == 'LONG'  and not (_t1 > _ep > _sl)) or
                                (_dirn.upper() == 'SHORT' and not (_t1 < _ep < _sl))
                            )
                        )
                        if _bad_geo:
                            cur.execute("ROLLBACK TO SAVEPOINT sp_signal")
                            cur.execute("RELEASE SAVEPOINT sp_signal")
                            logger.warning(
                                f'[engine] Skipping bracket-update: {strategy_id}/{sig.ticker} '
                                f'would create inverted geometry '
                                f'(entry={_ep:.4f} stop={_sl:.4f} t1={_t1:.4f})'
                            )
                            continue
                    rows_inserted = 0   # not a new emit; bracket refresh only
                else:
                    # Reached when EITHER no open row matched (first emit) OR the
                    # matched row is spent and we are minting a CONTINUATION row
                    # for the next target_date (_continuation_mint, gate-ON only).
                    if _lifecycle_rows:
                        # Lifecycle insert: lifecycle_state, computed_at, target_date
                        cur.execute("""
                            INSERT INTO execution_signals
                                (strategy_id, workspace_id, signal_date, ticker, direction,
                                 entry_price, stop_loss, target_1, target_2, target_3,
                                 position_size_pct, regime_state, signal_params, status,
                                 lifecycle_state, computed_at, target_date)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'open',
                                    %s,%s,%s)
                            ON CONFLICT (strategy_id, signal_date, ticker, direction) DO NOTHING
                        """, (
                            strategy_id, WORKSPACE, run_date,
                            sig.ticker, sig.direction,
                            sig.entry_price, sig.stop_loss,
                            sig.target_1, sig.target_2, sig.target_3,
                            sig.position_size_pct, regime_state,
                            json.dumps(params_clean),
                            'COMPUTED', datetime.now(timezone.utc), _next_td,
                        ))
                    else:
                        # Gate OFF: legacy INSERT — byte-identical to pre-SP-6 behavior
                        cur.execute("""
                            INSERT INTO execution_signals
                                (strategy_id, workspace_id, signal_date, ticker, direction,
                                 entry_price, stop_loss, target_1, target_2, target_3,
                                 position_size_pct, regime_state, signal_params, status)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'open')
                            ON CONFLICT (strategy_id, signal_date, ticker, direction) DO NOTHING
                        """, (
                            strategy_id, WORKSPACE, run_date,
                            sig.ticker, sig.direction,
                            sig.entry_price, sig.stop_loss,
                            sig.target_1, sig.target_2, sig.target_3,
                            sig.position_size_pct, regime_state,
                            json.dumps(params_clean),
                        ))
                    rows_inserted = max(cur.rowcount, 0)  # ON CONFLICT DO NOTHING returns -1
                cur.execute("RELEASE SAVEPOINT sp_signal")
                total += rows_inserted
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT sp_signal")
                cur.execute("RELEASE SAVEPOINT sp_signal")
                logger.error(f"write_signals error for {strategy_id}/{sig.ticker}: {e}")
    return total


# ──────────────────────────────────────────────────────────
# 6. CONFLUENCE
# ──────────────────────────────────────────────────────────

def detect_confluence(cur, strategy_results: dict, regime_state: str, run_date: date) -> int:
    """Identify tickers where ≥2 strategies agree on direction."""
    # Build ticker → {direction → [strategy_ids]}
    agree: dict = {}
    for strat_id, signals in strategy_results.items():
        for sig in signals:
            key = (sig.ticker, sig.direction)
            agree.setdefault(key, []).append(strat_id)

    count = 0
    for (ticker, direction), strats in agree.items():
        if len(strats) < CONFLUENCE_MIN:
            continue

        # Sum position sizes for combined sizing
        all_sigs = [s for sid in strats for s in strategy_results[sid]
                    if s.ticker == ticker and s.direction == direction]
        combined = sum(s.position_size_pct for s in all_sigs)

        try:
            cur.execute("""
                INSERT INTO confluence_signals
                    (workspace_id, signal_date, ticker, direction,
                     agreeing_strategies, confluence_count, regime_state, combined_size_pct)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (workspace_id, signal_date, ticker, direction) DO NOTHING
            """, (
                WORKSPACE, run_date, ticker, direction,
                strats, len(strats), regime_state, round(combined, 4),
            ))
            count += cur.rowcount
        except Exception as e:
            logger.error(f"detect_confluence error {ticker}: {e}")

    return count


# ──────────────────────────────────────────────────────────
# 7. UPDATE P&L
# ──────────────────────────────────────────────────────────

def update_pnl(cur, prices: pd.DataFrame, run_date: date) -> tuple[int, list]:
    """Update unrealized P&L for all open signals. Close if stop/target hit.

    Returns (n_updates, newly_closed_signal_ids). The ids are classified by
    the caller AFTER it commits — see the note at the end of this function."""
    cur.execute("""
        SELECT id, strategy_id, ticker, direction, entry_price,
               mark_entry_price, target_date, lifecycle_state,
               stop_loss, target_1, signal_date
        FROM execution_signals
        WHERE workspace_id = %s AND status = 'open'
          AND (lifecycle_state IS NULL OR lifecycle_state = 'FILLED')
    """, (WORKSPACE,))
    open_signals = cur.fetchall()

    updates = 0
    _newly_closed_signal_ids: list[int] = []
    for row in open_signals:
        sig_id     = row['id']
        strat_id   = row['strategy_id']
        ticker     = row['ticker']
        direction  = row['direction']
        stop_loss  = float(row['stop_loss'])
        target_1   = float(row['target_1'])
        sig_date   = row['signal_date']

        # SP-6: skip CLOSED_AT_OPEN signals (belt-and-suspenders; normally
        # already excluded by status='open', but a flattened/dropped position
        # that retained status='open' must never be re-marked).
        import math as _math
        if row.get('lifecycle_state') == 'CLOSED_AT_OPEN':
            continue

        # SP-6: prefer mark_entry_price (close[T+1] fill mark) over
        # entry_price; fall back to entry_price for legacy NULL rows.
        _raw_mark = row.get('mark_entry_price')
        _raw_entry = row['entry_price']
        if _raw_mark is not None:
            try:
                _mark_f = float(_raw_mark)
                entry = _mark_f if _math.isfinite(_mark_f) else float(_raw_entry)
            except (ValueError, TypeError):
                entry = float(_raw_entry)
        else:
            entry = float(_raw_entry)

        if ticker not in prices.columns:
            continue

        ts = prices[ticker].dropna()
        if ts.empty:
            continue

        current = float(ts.iloc[-1])
        # SP-6: use target_date for days_held when present (aligns with backtest
        # horizon); fall back to signal_date for legacy NULL rows.
        _tgt_dt = row.get('target_date')
        if _tgt_dt is not None and isinstance(_tgt_dt, date):
            days_held = (run_date - _tgt_dt).days if isinstance(run_date, date) else 0
        else:
            days_held = (run_date - sig_date).days if isinstance(sig_date, date) else 0

        # Compute unrealized P&L; guard against zero/NaN entries.
        if not entry or not _math.isfinite(entry):
            unrealized_pct = 0.0
        elif direction == 'LONG':
            unrealized_pct = (current - entry) / entry
        elif direction == 'SHORT':
            unrealized_pct = (entry - current) / entry
        else:  # SELL_VOL, BUY_VOL, FLAT — mark as neutral
            unrealized_pct = 0.0
        if not _math.isfinite(unrealized_pct):
            unrealized_pct = 0.0

        # Determine if signal should close
        close_reason = None
        close_status = 'open'
        realized_pct = None

        if direction == 'LONG' and current <= stop_loss * (1 + STOP_TRIGGER_PCT):
            close_reason = 'stop_loss'
            close_status = 'closed'
            realized_pct = unrealized_pct
        elif direction == 'SHORT' and current >= stop_loss * (1 - STOP_TRIGGER_PCT):
            close_reason = 'stop_loss'
            close_status = 'closed'
            realized_pct = unrealized_pct
        elif direction == 'LONG' and current >= target_1 * (1 - TARGET1_TRIGGER_PCT):
            close_reason = 'target_1'
            close_status = 'closed'
            realized_pct = unrealized_pct
        elif direction == 'SHORT' and current <= target_1 * (1 + TARGET1_TRIGGER_PCT):
            close_reason = 'target_1'
            close_status = 'closed'
            realized_pct = unrealized_pct

        try:
            # Upsert P&L row
            cur.execute("""
                INSERT INTO signal_pnl
                    (signal_id, strategy_id, workspace_id, pnl_date,
                     close_price, unrealized_pnl_pct, days_held, status,
                     closed_price, closed_at, close_reason, realized_pnl_pct)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (signal_id, pnl_date) DO UPDATE SET
                    close_price       = EXCLUDED.close_price,
                    unrealized_pnl_pct= EXCLUDED.unrealized_pnl_pct,
                    days_held         = EXCLUDED.days_held,
                    status            = EXCLUDED.status,
                    closed_price      = EXCLUDED.closed_price,
                    closed_at         = EXCLUDED.closed_at,
                    close_reason      = EXCLUDED.close_reason,
                    realized_pnl_pct  = EXCLUDED.realized_pnl_pct
            """, (
                sig_id, strat_id, WORKSPACE, run_date,
                current, round(unrealized_pct, 6), days_held,
                close_status,
                current if close_status == 'closed' else None,
                run_date if close_status == 'closed' else None,
                close_reason,
                round(realized_pct, 6) if realized_pct is not None else None,
            ))

            if close_status == 'closed':
                cur.execute(
                    "UPDATE execution_signals SET status='closed' WHERE id=%s",
                    (sig_id,)
                )
                # OUE classification (over/under/expected vs the signal's
                # GBM expectation captured at handoff time). Deferred to
                # after this loop so a single failed lookup doesn't roll
                # back the close itself.
                _newly_closed_signal_ids.append(sig_id)

            updates += 1
        except Exception as e:
            logger.error(f"update_pnl error {sig_id}: {e}")

    # OUE classification is intentionally NOT done here. classify_batch
    # reads on its own connection, so it must run AFTER the caller commits
    # these closes — otherwise it sees uncommitted rows, finds no realized
    # P&L, and skips every signal (the 2026-05-16→2026-05-29 bug where
    # oue_kind stayed NULL: logs showed 'skipped': N for every close).
    # Return the newly-closed ids so run() can classify them post-commit.
    return updates, _newly_closed_signal_ids


# ──────────────────────────────────────────────────────────
# 8. REPORT TRIGGERS
# ──────────────────────────────────────────────────────────

def fire_report_triggers(cur, prices: pd.DataFrame, run_date: date) -> int:
    """Queue report triggers for significant P&L events."""
    cur.execute("""
        SELECT sp.signal_id, sp.strategy_id, sp.unrealized_pnl_pct,
               sp.days_held, sp.close_reason, es.ticker, es.direction
        FROM signal_pnl sp
        JOIN execution_signals es ON es.id = sp.signal_id
        WHERE sp.pnl_date = %s AND sp.workspace_id = %s
    """, (run_date, WORKSPACE))

    fired = 0
    for row in cur.fetchall():
        trigger_type   = None
        trigger_reason = None

        # Coerce Decimal/None/NaN from the DB into a plain float once; the
        # raw Decimal('NaN') that occasionally comes back from NUMERIC
        # columns can't be compared against a Python float and raises
        # decimal.InvalidOperation.
        try:
            _pnl_pct = float(row['unrealized_pnl_pct']) if row['unrealized_pnl_pct'] is not None else 0.0
        except (TypeError, ValueError, decimal.InvalidOperation):
            _pnl_pct = 0.0
        if _pnl_pct != _pnl_pct:   # NaN check
            _pnl_pct = 0.0

        if row['close_reason'] == 'stop_loss':
            trigger_type   = 'STOP_HIT'
            trigger_reason = f"{row['ticker']} {row['direction']} stopped out at {_pnl_pct:.1%}"
        elif row['close_reason'] == 'target_1':
            trigger_type   = 'TARGET_HIT'
            trigger_reason = f"{row['ticker']} {row['direction']} hit T1 at {_pnl_pct:.1%}"
        elif _pnl_pct < DRAWDOWN_REPORT_PCT:
            trigger_type   = 'DRAWDOWN'
            trigger_reason = f"{row['ticker']} {row['direction']} drawdown {_pnl_pct:.1%}"
        elif (row['days_held'] or 0) >= DAYS_HELD_REPORT:
            trigger_type   = 'AGED'
            trigger_reason = f"{row['ticker']} {row['direction']} held {row['days_held']} days — review"

        if trigger_type:
            try:
                cur.execute("""
                    INSERT INTO report_triggers
                        (strategy_id, workspace_id, trigger_type, trigger_reason)
                    VALUES (%s,%s,%s,%s)
                """, (row['strategy_id'], WORKSPACE, trigger_type, trigger_reason))
                fired += 1
            except Exception as e:
                logger.error(f"fire_report_triggers error: {e}")

    return fired


# ──────────────────────────────────────────────────────────
# 9. LOG EXECUTION RUN
# ──────────────────────────────────────────────────────────

def log_run(cur, run_date, regime_state, metrics: dict):
    cur.execute("""
        INSERT INTO execution_runs
            (workspace_id, run_date, regime_state, strategies_run,
             signals_generated, high_confluence_signals, pnl_updates,
             report_triggers_fired, duration_seconds, errors)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        WORKSPACE, run_date, regime_state,
        metrics.get('strategies_run', 0),
        metrics.get('signals_generated', 0),
        metrics.get('confluence_count', 0),
        metrics.get('pnl_updates', 0),
        metrics.get('report_triggers', 0),
        metrics.get('duration_s', 0),
        json.dumps(metrics.get('errors', [])),
    ))


# ──────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────

def _fatal_exit_code(exc) -> int:
    """Exit code for a fatal engine error.

    CloseProxyError (the close[t]-proxy snapshot fetch failed entirely) is a
    data-availability FATAL: return 2 so the LangGraph step node aborts the
    whole cycle regardless of strict mode. rc=1 is treated as a soft 'warn'
    that lets the chain continue to handoff/trade — on an empty signal set that
    would orphan-close the book (the 2026-05-22 failure mode). All other errors
    keep the legacy rc=1.
    """
    return 2 if type(exc).__name__ == 'CloseProxyError' else 1


def _parse_run_date(argv=None) -> date:
    """Parse `--date YYYY-MM-DD` (optional; default today).

    The daily-cycle orchestrators (resolve_script.js, pipeline_orchestrator.py)
    have ALWAYS appended `--date <runDate>` to the engine invocation, but
    main() ignored argv and used date.today() — harmless while the two
    coincide, wrong for any historical re-run (e.g. recomputing an EOD target
    set after a data fix; see sp6 diagnosis 2026-06-04 §10). parse_known_args
    so orchestrator-injected flags the engine doesn't implement never crash
    the signals step. --dry-run is real since 2026-08-06 (_parse_dry_run).
    """
    import argparse
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument('--date', default=None)
    args, _unknown = p.parse_known_args(argv if argv is not None else sys.argv[1:])
    if args.date:
        try:
            return datetime.strptime(args.date, '%Y-%m-%d').date()
        except ValueError:
            logger.error(f"--date must be YYYY-MM-DD, got {args.date!r}")
            sys.exit(2)
    return date.today()


def _parse_dry_run(argv=None) -> bool:
    """True when --dry-run is on the argv (PIPELINE_DRY_RUN=1 appends it).

    The engine's half of the contract at pipeline_orchestrator.py:481: compute
    everything (regime, universe, panel load, run_strategies) so the cycle's
    plumbing and memory profile are exercised for real, then skip every
    external write. Kept separate from _parse_run_date so existing callers of
    that function keep their exact signature.
    """
    import argparse
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument('--dry-run', action='store_true')
    args, _unknown = p.parse_known_args(argv if argv is not None else sys.argv[1:])
    return bool(args.dry_run)


def main():
    import time
    t0       = time.time()
    run_date = _parse_run_date()
    dry_run  = _parse_dry_run()
    errors   = []

    logger.info(f"=== Execution Engine START {run_date} ==="
                + (" [DRY-RUN — no external writes]" if dry_run else ""))

    if not DB_URI:
        logger.error("POSTGRES_URI not set — aborting")
        sys.exit(1)

    conn = get_db()
    conn.autocommit = False
    cur  = conn.cursor()

    # Resolve workspace name → UUID once at startup
    global WORKSPACE
    WORKSPACE = resolve_workspace(cur, WORKSPACE)
    logger.info(f"Workspace: {WORKSPACE}")

    try:
        # 1. Regime
        regime = load_regime(cur)
        regime_state = regime['state']
        logger.info(f"Regime: {regime_state} (VIX={regime.get('vix_level')})")
        _stamp_cadence_reset_on_flip(cur, regime)

        # 2. Strategies
        strategies = load_approved_strategies(cur)
        if not strategies:
            logger.info("No approved strategies — nothing to do")
            if not dry_run:
                log_run(cur, run_date, regime_state, {'strategies_run': 0, 'errors': []})
                conn.commit()
            return

        # Build combined universe
        universe = []
        for s in strategies:
            # Strategy universe comes from DB row; fallback to SP100 proxy
            universe.extend(getattr(s, '_universe', []))
        if not universe:
            # Build universe from active tickers in master prices parquet
            prices_path = ROOT / 'data' / 'master' / 'prices.parquet'
            if prices_path.exists():
                try:
                    import pyarrow.parquet as pq
                    tickers = pq.read_table(prices_path, columns=['ticker']).to_pandas()['ticker'].unique().tolist()
                    universe = sorted(tickers)
                    logger.info(f"Universe from master prices: {len(universe)} tickers")
                except Exception:
                    universe = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'AMZN', 'NVDA', 'GOOGL', 'META']
            else:
                universe = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'AMZN', 'NVDA', 'GOOGL', 'META']
        universe = list(dict.fromkeys(universe))  # dedupe preserving order

        # SP-7 Phase C §6 (2026-06-28): the Phase-A4 sp500 clamp was DELETED here
        # once C1/C2/C3 were live — the per-strategy resolver below is now the sole
        # authority on the live universe (adopted tiers widen past sp500). The
        # Phase-A4 clamp module and its gate env var were removed in the same commit.

        # SP-7 Phase C (C1): per-strategy universes via UniverseResolver.
        # When ON: the union of per-strategy sets replaces the shared fallback
        # universe for the ONE panel load (memory invariant), and run_strategies
        # slices prices/aux per strategy. Whole-build failure fails open to the
        # legacy shared universe.
        strategy_universes = None
        if os.environ.get('OPENCLAW_LIVE_UNIVERSE_RESOLVER') == '1':
            try:
                from execution.live_universe import build_strategy_universes
                _built = build_strategy_universes(
                    [s.id for s in strategies], run_date, list(universe))
                strategy_universes = {sid: info['universe']
                                      for sid, info in _built.items()}
                universe = sorted(set().union(
                    *[set(u) for u in strategy_universes.values()]))
                _n_err = sum(1 for i in _built.values() if i['error'])
                logger.info(f"live-universe ON: union {len(universe)} tickers, "
                            f"{len(strategy_universes)} strategies, {_n_err} fail-open")
            except Exception as e:
                logger.error(f"live-universe build failed — fail-open to shared "
                             f"fallback universe: {e}")
                strategy_universes = None

        # 3. Load data
        prices   = load_prices(universe)
        aux_data = load_aux_data(universe, as_of=run_date)

        if prices.empty:
            logger.warning("Prices DataFrame empty — signals will be minimal")

        # 4. Run strategies

        # Inject last_price from prices DataFrame into each ticker's opts entry
        if 'options' in aux_data and prices is not None:
            try:
                latest_px = prices.groupby('ticker')['close'].last().to_dict()
                for _tk, _opts in aux_data['options'].items():
                    _opts['last_price'] = latest_px.get(_tk)
            except Exception as _e:
                logger.warning(f'last_price inject failed: {_e}')
        strategy_results = run_strategies(strategies, prices, regime, universe,
                                          aux_data, strategy_universes=strategy_universes)

        if dry_run:
            # Everything above (regime, universe resolution, panel load,
            # generate_signals) ran for real — memory profile and per-strategy
            # behavior are the production ones. Everything below is writes
            # (W1-W19 in the 2026-08-06 spec map: eod_compute_health,
            # execution_signals, parity/lifecycle passes, confluence, pnl,
            # report triggers, execution_runs, OUE) — skip it all wholesale
            # rather than threading a flag through every helper; the three
            # commit sites stay unreached and the txn is rolled back.
            would_signals = sum(len(v) for v in strategy_results.values())
            per_strat = {sid: len(v) for sid, v in strategy_results.items() if v}
            conn.rollback()
            duration_s = round(time.time() - t0, 2)
            logger.info(f"DRY-RUN: would write {would_signals} signals across "
                        f"{len(per_strat)} emitting strategies: {per_strat}")
            logger.info(f"=== Execution Engine DRY-RUN DONE in {duration_s}s ==="
                        f"{_memory_footprint()}")
            print(json.dumps({
                'status':            'ok',
                'dry_run':           True,
                'run_date':          str(run_date),
                'regime':            regime_state,
                'strategies_run':    len(strategies),
                'signals_generated': would_signals,
                'confluence_count':  0,
                'pnl_updates':       0,
                'report_triggers':   0,
                'duration_s':        duration_s,
            }))
            return

        # 4.5 Write eod_compute_health sentinel (gated: any routed-execution
        # mode — EOD flow or same-day exec; arms the premarket flatten guard)
        if _signal_lifecycle_pass_on():
            _stats = run_strategies.last_run_stats
            n_strategies_total = _stats['total']
            n_strategies_ok = _stats['ran_ok']
            regime_ok = bool(
                regime_state and
                regime_state in ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
            )
            write_eod_health(
                cur,
                run_date,
                rc=0,
                n_strategies_ok=n_strategies_ok,
                n_strategies_total=n_strategies_total,
                regime_ok=regime_ok,
                universe_size=len(universe),
                panel_max_date=getattr(load_prices, 'last_parquet_max_date', None),
                panel_fresh_required=_panel_fresh_required(run_date),
            )

        # 5. Write signals
        total_signals = write_signals(cur, strategy_results, regime_state, run_date)
        logger.info(f"Signals written: {total_signals}")

        # 5a. Parity marks — mark entry at close[T+1] and re-anchor brackets.
        #     Gated by OPENCLAW_EOD_SIGNAL_REGISTER==1 (same gate as write_signals).
        #     Builds closes dict from prices (wide: date-index × ticker-columns)
        #     using last non-NaN close per ticker — mirrors update_pnl's proven
        #     pattern (prices[ticker].dropna().iloc[-1]).
        parity_mark_count = 0
        # Any routed-execution mode: parity marks / ledger finalize /
        # live-measurement must run whenever signals feed real submissions
        # (2026-07-29: was EOD-flag-only, which would have silently killed
        # fix-7 measurement on the same-day flip).
        if _signal_lifecycle_pass_on():
            try:
                from execution.parity_mark import (
                    finalize_parity_marks, finalize_execution_ledger)
                _closes: dict = {}
                if not prices.empty:
                    for _tk in prices.columns:
                        _ts = prices[_tk].dropna()
                        if not _ts.empty:
                            _closes[_tk] = float(_ts.iloc[-1])
                parity_mark_count = finalize_parity_marks(cur, _closes, run_date, WORKSPACE)
                logger.info(f"Parity marks finalized: {parity_mark_count}")
                ledger_count = finalize_execution_ledger(cur, _closes, run_date)
                logger.info(f"Execution ledger finalized: {ledger_count}")
                # Fix 7 (2026-07-27): record what execution ACTUALLY paid next
                # to the parity mark, keep registry live_days honest, and retire
                # abandoned signal trackers (metrics pollution). All savepoint-
                # isolated internally — never poison this transaction. The
                # stale-tracker pass requires broker evidence and skips itself
                # when the book can't be read.
                from execution.parity_mark import (backfill_broker_fill_truth,
                                                   refresh_live_days,
                                                   close_stale_trackers)
                backfill_broker_fill_truth(cur, run_date, WORKSPACE)
                refresh_live_days(cur)
                try:
                    from execution.regime_blended_sizer import _load_broker_positions_usd
                    _held = _load_broker_positions_usd()
                    close_stale_trackers(
                        cur, held_tickers=(set(_held) if _held is not None else None))
                except Exception as _st_err:
                    logger.warning(f"[engine] stale-tracker pass skipped: {_st_err}")
            except Exception as _pm_err:
                logger.error(f"[engine] parity_mark failed: {_pm_err}")
                errors.append(f"parity_mark: {_pm_err}")

        # 6. Confluence
        confluence_count = detect_confluence(cur, strategy_results, regime_state, run_date)
        logger.info(f"Confluence signals: {confluence_count}")

        # 7. P&L updates
        pnl_updates, newly_closed_ids = update_pnl(cur, prices, run_date)
        logger.info(f"P&L rows updated: {pnl_updates}")

        # 7b. Broker-close reconcile (2026-06-08): close still-open signals for
        # tickers the cycle liquidated / circuit-breaker'd that are now flat at
        # the broker — closes update_pnl's price-based path can't see, so
        # #trade-reports + the dashboard count them. Scoped to this cycle's
        # close events; best-effort + fail-safe (empty broker ⇒ skip). Folds
        # into newly_closed_ids so they get OUE-classified below.
        try:
            from execution.open_reconcile import reconcile_broker_closes
            _bc = reconcile_broker_closes(cur, run_date)
            if _bc:
                _by = {r: list(_bc.values()).count(r) for r in set(_bc.values())}
                logger.info(f"Broker-close reconcile closed {len(_bc)} signal(s): {_by}")
                newly_closed_ids = list(set(newly_closed_ids) | set(_bc.keys()))
        except Exception as e:
            logger.warning(f"reconcile_broker_closes failed (non-fatal): {e}")

        # 8. Report triggers
        report_triggers = fire_report_triggers(cur, prices, run_date)
        logger.info(f"Report triggers fired: {report_triggers}")

        duration_s = round(time.time() - t0, 2)

        # 9. Log run
        log_run(cur, run_date, regime_state, {
            'strategies_run':    len(strategies),
            'signals_generated': total_signals,
            'confluence_count':  confluence_count,
            'pnl_updates':       pnl_updates,
            'report_triggers':   report_triggers,
            'duration_s':        duration_s,
            'errors':            errors,
        })

        conn.commit()

        # OUE classification — MUST be after the commit above so the
        # classifier's own connection can see the just-closed signal_pnl
        # rows. Best-effort: failures here never fail the cycle (closes are
        # already durable); a later run/backfill picks up any stragglers
        # since classify_batch skips already-classified signals.
        if newly_closed_ids:
            try:
                from execution.oue_classifier import classify_batch
                uri = os.environ.get('POSTGRES_URI', '')
                if uri:
                    stats = classify_batch(uri, newly_closed_ids)
                    logger.info(f"OUE classified {len(newly_closed_ids)} closes: {stats}")
            except Exception as e:
                logger.warning(f"OUE classify_batch failed (closes still persisted): {e}")

        # Peak RSS + who else was resident. The signals step was OOM-killed 5x in
        # the 10 days to 2026-08-05 and the post-mortems had to be reconstructed
        # from kernel OOM dumps, which only survive when a kill actually happens
        # — on 08-05 a 2310MB co-tenant python3 was sharing the box with this
        # process and could NOT be identified afterwards because it was gone.
        # Logging it on every run makes the next occurrence attributable, and
        # makes the headroom trend visible before it becomes a kill.
        logger.info(f"=== Execution Engine DONE in {duration_s}s ==={_memory_footprint()}")

        # Output JSON for caller
        print(json.dumps({
            'status':            'ok',
            'run_date':          str(run_date),
            'regime':            regime_state,
            'strategies_run':    len(strategies),
            'signals_generated': total_signals,
            'confluence_count':  confluence_count,
            'pnl_updates':       pnl_updates,
            'report_triggers':   report_triggers,
            'duration_s':        duration_s,
        }))

    except Exception as e:
        conn.rollback()
        logger.error(f"FATAL: {e}\n{traceback.format_exc()}")
        errors.append(str(e))
        if not dry_run:
            try:
                log_run(cur, run_date, 'UNKNOWN', {'errors': errors})
                conn.commit()
            except Exception:
                pass
        print(json.dumps({'status': 'error', 'error': str(e),
                          **({'dry_run': True} if dry_run else {})}))
        sys.exit(_fatal_exit_code(e))
    finally:
        cur.close()
        conn.close()


if __name__ == '__main__':
    main()
