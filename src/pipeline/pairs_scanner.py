#!/usr/bin/env python3
"""Statistical pairs-trading scanner (Task D1/X1 foundation).

Weekly Engle-Granger cointegration scan over the active universe, bucketed
by GICS industry (fallback sector), producing
`data/derived/pair_ledger.parquet` consumed by a later sizing/execution task.
Pin the schema exactly: as_of, ticker_a, ticker_b, industry, beta, alpha,
half_life_days, sigma_spread, spread_mean, eg_pvalue, fdr_q, fdr_pass,
cost_ok, approved, n_obs.

REPLACE-ON-RESCAN, not append-only: pair_ledger is DERIVED data (fully
rebuildable from prices.parquet + the active universe), so a rescan of a
given `as_of` DROPS every existing row for that `as_of` and writes the
freshly-computed set in its place -- even when that set is empty. A
zero-pair rescan must erase a stale prior claim that pairs existed on that
date, not leave it standing. The write is still atomic (tmp file +
os.replace); rows for every OTHER `as_of` are left untouched.

Convention: spread = log(ticker_a) - beta*log(ticker_b) - alpha, where
ticker_a is whichever direction of the Engle-Granger test had the smaller
p-value (that direction's dependent variable becomes ticker_a).

Design notes / documented choices (deliberately spelled out here because
they weren't 100% pinned by the spec):

  * Universe table discovered via `information_schema.columns` on this box's
    Postgres (2026-08-24): table `universe` has columns
    ticker, name, sector, industry, market_cap, index_membership, active,
    added_at, last_updated. Ticker column = `ticker`; active flag = `active`
    (boolean); no ADV column exists, so `market_cap` is used as the
    liquidity proxy for the bucket-cap ordering (see build_buckets). NOTE:
    at scan-authoring time this `universe` table has 0 rows in this
    environment (all data lives in the separate, larger `universe_config`
    table used elsewhere in the codebase) — see the D1 report for this
    concern. The brief is explicit that the scanner reads `universe`, so
    that's what this module does; a real run here will legitimately report
    buckets=0/pairs_tested=0/approved=0 until `universe` is populated.

  * Bucket-cap ordering: sort by market_cap descending (larger cap = more
    liquid proxy), ties/missing broken alphabetically by ticker, then take
    the first `cap` (default 50).

  * sigma_spread/spread_mean use sample statistics (ddof=1) over the
    504-trading-day (or --window) close series.

  * Pair identity -- for BOTH the ledger's dedupe-on-rescan key and the
    cross-week persistence-rule lookup -- is the *canonical unordered* pair
    key `(min(ticker_a, ticker_b), max(ticker_a, ticker_b))`, computed by
    `_canon_pair()`, NOT the literal ordered (ticker_a, ticker_b) tuple. The
    EG-direction-driven ticker_a/ticker_b labeling is a property of a given
    scan (it flips to whichever leg had the smaller p-value on that
    particular window) rather than of the underlying tradeable relationship.
    Keying dedupe on the ordered tuple while keying persistence on an
    unordered set (as an earlier revision of this module did) let a
    direction flip between same-date reruns double-persist a pair under two
    "different" ordered keys and then resolve the persistence lookup by
    alphabetical accident; using the SAME canonical key everywhere closes
    that gap. Within a single `as_of`'s freshly-written rows, "keep last"
    (by write order) wins on a canon-key collision -- a safety net, since
    normal bucketing (each ticker lives in exactly one industry per scan)
    cannot itself produce two rows for the same unordered pair in one scan.

  * A scan that finds zero surviving pairs still performs the
    replace-on-rescan write (see above) -- it is a valid, non-error outcome
    that must still erase any stale prior rows for that `as_of`.

  * Coint()/FDR-pool errors: pairs whose `coint()` call raises, or whose
    resulting `eg_pvalue` is non-finite (NaN/inf), are dropped from the
    scan-wide BH pool BEFORE `bh_fdr()` runs (see `bh_fdr`'s docstring for
    why `n` = successful tests only) and counted in the `errors_dropped`
    summary field; one WARN log line per scan reports the count when
    nonzero. Previously these were silently swallowed by a bare
    `except Exception: return None`, which shrank `n` invisibly and could
    inflate every other pair's `fdr_q` in the same scan without any signal
    that it had happened.

  * Sector/industry + universe-table resilience (Task X1 controller item):
    `_fetch_active_universe()` (1) falls back from the pinned `universe`
    table to `universe_config` automatically -- logging the fallback -- when
    `universe` has 0 active rows, since that is the table this box's
    universe plumbing actually populates; and (2) backfills any row whose
    `industry` AND `sector` are both NULL/missing from
    `data/.cache/fmp_profile.json` (the flat `{SYMBOL: {..., "sector":,
    "industry": ...}}` cache produced by `scripts/refresh_fmp_profiles.py`;
    tombstone entries there carry neither key and are skipped naturally).
    Taxonomy-stability note: `build_buckets()` keys bucket identity on the
    raw industry string. The FMP profile cache and the `universe`/
    `universe_config` tables' own eventual industry/sector population are
    both sourced from the same FMP `/stable/profile` endpoint, so a bucket
    key is the same taxonomy label regardless of which of those two sources
    supplied it for a given row -- no cross-source label reconciliation is
    needed.

  * Prices are read via pyarrow predicate pushdown scoped to one bucket's
    tickers (<=50) and a bounded calendar-day window, mirroring
    src/execution/asset_correlation.py's `_load_returns` pattern — never the
    full prices.parquet panel.
"""
from __future__ import annotations

import argparse
import datetime
import itertools
import json
import logging
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

REPO_ROOT = "/root/openclaw"
PRICES_PARQUET = f"{REPO_ROOT}/data/master/prices.parquet"
COST_BPS_PATH = f"{REPO_ROOT}/data/derived/ticker_cost_bps.json"
DEFAULT_LEDGER_PATH = f"{REPO_ROOT}/data/derived/pair_ledger.parquet"
ENV_PATH = f"{REPO_ROOT}/.env"
FMP_PROFILE_CACHE_PATH = f"{REPO_ROOT}/data/.cache/fmp_profile.json"

# Discovered empirically via information_schema.columns (see module docstring).
UNIVERSE_TICKER_COL = "ticker"
UNIVERSE_ACTIVE_COL = "active"
DEFAULT_UNIVERSE_TABLE = "universe"
# item 7: `universe` is 0 rows on this box; `universe_config` is the table
# the rest of the codebase's universe plumbing actually populates. Same
# ticker/active column names as `universe` (see module docstring).
FALLBACK_UNIVERSE_TABLE = "universe_config"

DEFAULT_WINDOW = 504
DEFAULT_MIN_CORR = 0.6
DEFAULT_FDR_Q = 0.10
DEFAULT_COST_K = 2.0
DEFAULT_CORR_LOOKBACK_DAYS = 90
DEFAULT_MIN_OBS_FRAC = 0.9
DEFAULT_HALF_LIFE_BAND = (5.0, 30.0)
DEFAULT_BUCKET_CAP = 50
DEFAULT_TICKER_COST_BPS = 10.0

Z_ENTRY = 2.0
Z_EXIT = 0.5
LEG_CROSSINGS_PER_ROUND_TRIP = 4

LEDGER_COLUMNS = [
    "as_of", "ticker_a", "ticker_b", "industry", "beta", "alpha",
    "half_life_days", "sigma_spread", "spread_mean", "eg_pvalue",
    "fdr_q", "fdr_pass", "cost_ok", "approved", "n_obs",
]


class PairsScannerDataError(Exception):
    """Raised for DB/parquet access failures (CLI exit code 1). A merely
    empty universe / zero surviving pairs is NOT an error (exit code 0)."""


# Sentinel distinguishing "coint() raised" (an error -- counted in
# errors_dropped, see item 4) from evaluate_pair's plain `None` return
# ("not enough overlapping history for this pair" -- not an error).
_COINT_ERROR = object()


def _canon_pair(ticker_a, ticker_b):
    """Canonical UNORDERED pair identity, used for both ledger dedupe-on-
    rescan and the cross-week persistence lookup (see module docstring)."""
    return (ticker_a, ticker_b) if ticker_a <= ticker_b else (ticker_b, ticker_a)


# ── Pure math: BH-FDR ────────────────────────────────────────────────────────
def bh_fdr(pvalues):
    """Benjamini-Hochberg q-values (pure function, no scipy dependency).

    Sort p ascending; q_i = p_i * n / rank_i (rank is 1-based over the sorted
    order); then enforce monotonicity by taking the cumulative min from the
    largest rank down to the smallest, clipped to 1.0. Returns q-values in
    the SAME order as the input `pvalues`.

    `n = len(pvalues)` is a DELIBERATE choice: by the time this is called,
    `run_scan` has already filtered `pvalues` down to only the pairs that
    were SUCCESSFULLY tested -- i.e. `coint()` did not raise AND produced a
    finite `eg_pvalue`. Pairs dropped for either reason are excluded from
    both the numerator pool and `n` (they are counted separately in
    `errors_dropped`), so a silent uptick in coint() failures cannot shrink
    `n` and inflate every other pair's `fdr_q` without a visible signal.
    """
    n = len(pvalues)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: pvalues[i])
    q_sorted = [0.0] * n
    for rank, i in enumerate(order, start=1):
        q_sorted[rank - 1] = pvalues[i] * n / rank
    for k in range(n - 2, -1, -1):
        q_sorted[k] = min(q_sorted[k], q_sorted[k + 1])
    q_sorted = [min(q, 1.0) for q in q_sorted]
    q = [0.0] * n
    for rank, i in enumerate(order, start=1):
        q[i] = q_sorted[rank - 1]
    return q


# ── Pure math: half-life ─────────────────────────────────────────────────────
def ar1_half_life(spread):
    """AR(1) half-life of a spread series: regress delta_spread_t on
    spread_{t-1} (with intercept); theta = -coef; half_life = ln(2)/theta.
    Hard-fails (returns None) when coef >= 0 (non-mean-reverting: theta<=0)."""
    s = np.asarray(spread, dtype=float)
    if len(s) < 3:
        return None
    y = s[1:] - s[:-1]
    x = s[:-1]
    X = np.column_stack([np.ones_like(x), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    phi_coef = float(coef[1])
    theta = -phi_coef
    if theta <= 0:
        return None
    return math.log(2.0) / theta


# ── Pure math: OLS hedge ─────────────────────────────────────────────────────
def ols_hedge(log_a, log_b):
    """OLS log(A) = alpha + beta*log(B) via numpy lstsq. Returns
    (alpha, beta, spread) where spread = log_a - beta*log_b - alpha."""
    log_a = np.asarray(log_a, dtype=float)
    log_b = np.asarray(log_b, dtype=float)
    X = np.column_stack([np.ones_like(log_b), log_b])
    coef, *_ = np.linalg.lstsq(X, log_a, rcond=None)
    alpha, beta = float(coef[0]), float(coef[1])
    spread = log_a - beta * log_b - alpha
    return alpha, beta, spread


# ── Pure math: cost gate ─────────────────────────────────────────────────────
def cost_ok(sigma_spread, cost_a_bps, cost_b_bps, cost_k=DEFAULT_COST_K,
            z_entry=Z_ENTRY, z_exit=Z_EXIT, crossings=LEG_CROSSINGS_PER_ROUND_TRIP):
    """(z_entry - z_exit)*sigma_spread >= cost_k*crossings*mean(cost_bps)/1e4."""
    lhs = (z_entry - z_exit) * sigma_spread
    rhs = cost_k * crossings * ((cost_a_bps + cost_b_bps) / 2.0) / 1e4
    return lhs >= rhs


# ── Pure: pairwise Pearson on aligned dicts ─────────────────────────────────
def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _log_returns_series(dated_closes):
    """dated_closes: list[(date_str, close)] sorted ascending -> {date: logret}."""
    out = {}
    for i in range(1, len(dated_closes)):
        _, p0 = dated_closes[i - 1]
        d1, p1 = dated_closes[i]
        if p0 and p0 == p0 and p0 > 0 and p1 and p1 == p1 and p1 > 0:
            out[d1] = math.log(p1) - math.log(p0)
    return out


def pearson_prefilter(dated_closes_a, dated_closes_b, as_of, lookback_days, min_corr):
    """Trailing `lookback_days` CALENDAR-day slice; daily log-return Pearson
    corr on overlapping dates. Returns (passed: bool, corr: float|None)."""
    lo = (as_of - datetime.timedelta(days=lookback_days)).isoformat()
    hi = as_of.isoformat()
    a = [(d, p) for d, p in dated_closes_a if lo <= d <= hi]
    b = [(d, p) for d, p in dated_closes_b if lo <= d <= hi]
    ra = _log_returns_series(a)
    rb = _log_returns_series(b)
    common = sorted(set(ra) & set(rb))
    if len(common) < 5:
        return False, None
    rho = _pearson([ra[d] for d in common], [rb[d] for d in common])
    if rho is None:
        return False, None
    return (rho >= min_corr), rho


def _is_usable_close(p):
    """Finite, positive, non-NaN. `p != p` is the NaN check (NaN != NaN)."""
    return p is not None and p == p and p > 0


def build_aligned_window(dated_closes_a, dated_closes_b, as_of, window, min_obs_frac):
    """Trailing `window`-trading-day close window ending at as_of for each
    leg (tail of that leg's own usable closes <= as_of), then intersect
    dates. NaN/non-positive closes are dropped BEFORE the tail-slice and the
    intersection, so a masked/missing close never counts toward n_obs or the
    min_obs_frac*window coverage floor (a plain `math.log(nan)` would return
    nan silently rather than raising, so this filter -- not the log() call --
    is what actually enforces "non-NaN closes" here). Returns None if the
    overlap < min_obs_frac*window. Else returns (dates, log_a, log_b, n_obs).
    """
    hi = as_of.isoformat()
    a_tail = [(d, p) for d, p in dated_closes_a if d <= hi and _is_usable_close(p)][-window:]
    b_tail = [(d, p) for d, p in dated_closes_b if d <= hi and _is_usable_close(p)][-window:]
    da = {d: p for d, p in a_tail}
    db = {d: p for d, p in b_tail}
    common = sorted(set(da) & set(db))
    n_obs = len(common)
    if n_obs < min_obs_frac * window:
        return None
    log_a = np.array([math.log(da[d]) for d in common])
    log_b = np.array([math.log(db[d]) for d in common])
    return common, log_a, log_b, n_obs


# ── Bucketing ────────────────────────────────────────────────────────────────
def build_buckets(universe_rows, cap=DEFAULT_BUCKET_CAP):
    """Bucket by industry (fallback sector when industry is NULL); drop rows
    where both are NULL; drop buckets of size < 2. Cap each bucket at `cap`
    entries, preferring descending market_cap (liquidity proxy — the
    `universe` table has no ADV column), ties/missing market_cap broken
    alphabetically by ticker."""
    raw = {}
    for row in universe_rows:
        key = row.get("industry") or row.get("sector")
        if key is None:
            continue
        raw.setdefault(key, []).append(row)

    def sort_key(r):
        mc = r.get("market_cap")
        has_mc = mc is not None
        return (0 if has_mc else 1, -(mc if has_mc else 0.0), r["ticker"])

    out = {}
    for key, rows in raw.items():
        if len(rows) < 2:
            continue
        capped = sorted(rows, key=sort_key)[:cap]
        if len(capped) < 2:
            continue
        out[key] = capped
    return out


# ── DB / parquet / JSON I/O (monkey-patched in tests) ───────────────────────
import re as _re

_IDENT_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Candidate liquidity columns to try, in preference order, when discovering
# what a given universe table actually has (brief step 2: "prefer ordering
# by a liquidity column if the universe table has one (adv/market_cap --
# discover), else alphabetical"). `universe` has `market_cap` (numeric);
# `universe_config` (the table the rest of the codebase actually reads --
# see module docstring) has NO numeric liquidity column at all, only a
# `market_cap_tier` text bucket, so it falls through to None -> alphabetical.
LIQUIDITY_COLUMN_CANDIDATES = ["market_cap", "adv", "adv_usd", "average_dollar_volume", "dollar_volume"]


def _discover_liquidity_column(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    )
    cols = {r[0] for r in cur.fetchall()}
    for cand in LIQUIDITY_COLUMN_CANDIDATES:
        if cand in cols:
            return cand
    return None


def _load_fmp_profile_cache():
    """item 7: flat `{SYMBOL: {..., "sector":, "industry": ...}}` cache
    produced by scripts/refresh_fmp_profiles.py (no wrapper/metadata at the
    top level -- verified against the live file and that script's
    `normalize_profile`/`atomic_write_json`). Tombstone entries
    (`{"_fetched_at": ..., "_empty": true}`) carry neither key, so `.get()`
    below skips them naturally. Returns {} if the cache doesn't exist or is
    unreadable -- this is a best-effort backfill, never a hard requirement."""
    path = Path(FMP_PROFILE_CACHE_PATH)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _fill_missing_sector_industry_from_fmp_cache(rows):
    """item 7: mutate `rows` in place -- any row with BOTH industry AND
    sector NULL/missing gets them filled from the FMP profile cache when an
    entry is present there. Rows with at least one of the two already set
    (or with no cache entry / a tombstone entry) are left untouched. See the
    module docstring's taxonomy-stability note."""
    needs_fill = [r for r in rows if not r.get("industry") and not r.get("sector")]
    if not needs_fill:
        return
    cache = _load_fmp_profile_cache()
    if not cache:
        return
    for row in needs_fill:
        ticker = row.get("ticker")
        entry = cache.get(ticker) or cache.get(str(ticker).upper())
        if not isinstance(entry, dict):
            continue
        industry = entry.get("industry")
        sector = entry.get("sector")
        if industry:
            row["industry"] = industry
        if sector:
            row["sector"] = sector


def _fetch_active_universe(uri=None, table=DEFAULT_UNIVERSE_TABLE, _allow_fallback=True):
    """Step 1: active rows from Postgres table `universe` (per the brief's
    pinned default; `table` is overridable via --universe-table for when a
    differently-named/differently-shaped universe table needs to be pointed
    at instead -- see module docstring for the `universe` vs `universe_config`
    situation on this box). Returns
    [{ticker, industry, sector, market_cap}], market_cap None when the table
    has no discoverable liquidity column (build_buckets then falls back to
    alphabetical ordering for the bucket cap, per the brief).

    item 7: (a) when `table` comes back with 0 active rows and
    `_allow_fallback` is set, automatically retries once against
    `FALLBACK_UNIVERSE_TABLE` ("universe_config") -- logging the fallback --
    so the default CLI invocation still produces a usable universe on a box
    where `universe` itself is unpopulated; (b) backfills industry/sector
    from the FMP profile cache for any row missing both (see
    `_fill_missing_sector_industry_from_fmp_cache`)."""
    if not _IDENT_RE.match(table):
        raise PairsScannerDataError(f"invalid --universe-table identifier: {table!r}")
    import dotenv
    dotenv.load_dotenv(ENV_PATH)
    if uri is None:
        uri = os.environ.get("POSTGRES_URI") or os.environ.get("DATABASE_URL")
    if not uri:
        raise PairsScannerDataError("POSTGRES_URI/DATABASE_URL not set")
    try:
        import psycopg2
    except Exception as exc:  # pragma: no cover - env-dependent
        raise PairsScannerDataError(f"psycopg2 unavailable: {exc}") from exc
    try:
        conn = psycopg2.connect(uri)
    except Exception as exc:
        raise PairsScannerDataError(f"universe DB connect failed: {exc}") from exc
    try:
        cur = conn.cursor()
        liquidity_col = _discover_liquidity_column(cur, table)
        select_liquidity = liquidity_col if liquidity_col else "NULL"
        cur.execute(
            f"SELECT {UNIVERSE_TICKER_COL}, industry, sector, {select_liquidity} "
            f"FROM {table} WHERE {UNIVERSE_ACTIVE_COL} = true"
        )
        rows = cur.fetchall()
    except Exception as exc:
        raise PairsScannerDataError(f"universe query failed: {exc}") from exc
    finally:
        conn.close()

    if not rows and _allow_fallback and table != FALLBACK_UNIVERSE_TABLE:
        logger.warning(
            "pairs-scanner: universe table %r has 0 active rows; falling back to %r",
            table, FALLBACK_UNIVERSE_TABLE,
        )
        return _fetch_active_universe(uri=uri, table=FALLBACK_UNIVERSE_TABLE, _allow_fallback=False)

    out = [
        {
            "ticker": r[0],
            "industry": r[1],
            "sector": r[2],
            "market_cap": (float(r[3]) if r[3] is not None else None),
        }
        for r in rows
    ]
    _fill_missing_sector_industry_from_fmp_cache(out)
    return out


def _load_bucket_closes(tickers, as_of, window):
    """Step 3: sliced pyarrow read (predicate pushdown on ticker+date) of
    close prices for one bucket's tickers, bounded to enough calendar days to
    cover the trailing `window` trading days (the 90-day corr-prefilter
    window is a subset of this same slice). Mirrors
    src/execution/asset_correlation.py's `_load_returns`: never touches rows
    outside this bucket's tickers/date-range, so RSS stays bounded to one
    bucket (<=50 tickers) at a time, not the full prices.parquet panel."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    tickers = list(tickers)
    if not tickers:
        return {}
    calendar_days_back = int(window * 2) + 30
    lo = (as_of - datetime.timedelta(days=calendar_days_back)).isoformat()
    hi = as_of.isoformat()
    flt = (
        pc.field("ticker").isin(tickers)
        & (pc.field("date") >= lo)
        & (pc.field("date") <= hi)
    )
    tbl = pq.read_table(PRICES_PARQUET, columns=["ticker", "date", "close"], filters=flt)
    df = tbl.to_pandas()
    df["date"] = df["date"].astype(str)
    out = {}
    for tk, g in df.groupby("ticker"):
        g = g.sort_values("date")
        out[str(tk)] = list(zip(g["date"].tolist(), g["close"].astype(float).tolist()))
    return out


def _load_cost_bps():
    """Step 9: {ticker: one-way bps} from data/derived/ticker_cost_bps.json.
    A missing file falls back to DEFAULT_TICKER_COST_BPS for every ticker
    (see evaluate_pair) -- logged once here as its own WARN line, distinct
    from the (unlogged, expected-to-happen-routinely) per-ticker fallback
    that fires inside evaluate_pair for individual tickers absent from an
    otherwise-present file."""
    path = Path(COST_BPS_PATH)
    if not path.exists():
        logger.warning(
            "pairs-scanner: cost-bps file %s not found; using the %.1fbps "
            "default for EVERY ticker this scan", path, DEFAULT_TICKER_COST_BPS,
        )
        return {}
    with open(path) as f:
        data = json.load(f)
    return data.get("cost_bps", {})


# ── Ledger I/O (replace-on-rescan per as_of; see module docstring) ─────────
def _load_ledger(out_path):
    out_path = Path(out_path)
    if not out_path.exists():
        return None
    df = pd.read_parquet(out_path)
    if not df.empty:
        df["as_of"] = pd.to_datetime(df["as_of"]).dt.date
    return df


def _previous_scan_pass_map(existing_df, as_of):
    """{canon_pair: fdr_pass} for the ledger's most recent as_of strictly
    before `as_of`, keyed by the SAME canonical unordered pair identity used
    by the ledger's own dedupe (`_canon_pair` -- see module docstring). By
    construction there is at most one row per canon pair per as_of once
    `_write_ledger_for_as_of`'s dedupe has run, so "newest row per canon key
    wins" reduces to "the single row for the newest prior as_of"."""
    if existing_df is None or existing_df.empty:
        return {}
    prior = existing_df[existing_df["as_of"] < as_of]
    if prior.empty:
        return {}
    prev_as_of = prior["as_of"].max()
    prev_rows = prior[prior["as_of"] == prev_as_of]
    return {
        _canon_pair(row.ticker_a, row.ticker_b): bool(row.fdr_pass)
        for row in prev_rows.itertuples()
    }


def _records_to_df(pair_records, as_of):
    rows = [
        {
            "as_of": as_of,
            "ticker_a": r["ticker_a"],
            "ticker_b": r["ticker_b"],
            "industry": r["industry"],
            "beta": float(r["beta"]),
            "alpha": float(r["alpha"]),
            "half_life_days": float(r["half_life_days"]),
            "sigma_spread": float(r["sigma_spread"]),
            "spread_mean": float(r["spread_mean"]),
            "eg_pvalue": float(r["eg_pvalue"]),
            "fdr_q": float(r["fdr_q"]),
            "fdr_pass": bool(r["fdr_pass"]),
            "cost_ok": bool(r["cost_ok"]),
            "approved": bool(r["approved"]),
            "n_obs": int(r["n_obs"]),
        }
        for r in pair_records
    ]
    return pd.DataFrame(rows, columns=LEDGER_COLUMNS)


def _write_ledger_for_as_of(new_df, existing_df, as_of, out_path):
    """Replace-on-rescan (item 3): DROP every existing row for `as_of`, then
    append the freshly-scanned `new_df` rows -- even when `new_df` is empty,
    so a zero-pair rescan erases a stale prior claim rather than leaving it
    standing. Rows for every OTHER as_of are untouched. Atomic write (tmp
    file + os.replace).

    Also applies the canonical-unordered-pair dedupe (item 2) as a safety
    net within the surviving rows, keyed on (as_of, canon_pair), "keep last"
    by write order -- a no-op under normal bucketing (each ticker lives in
    exactly one industry per scan, so one scan cannot itself emit two rows
    for the same unordered pair), but it means the ledger's dedupe identity
    and the persistence lookup's identity (`_previous_scan_pass_map`) always
    agree, closing the direction-flip double-persist gap ordered-tuple
    dedupe used to allow."""
    if existing_df is not None and not existing_df.empty:
        kept = existing_df[existing_df["as_of"] != as_of]
    else:
        kept = pd.DataFrame(columns=LEDGER_COLUMNS)
    combined = pd.concat([kept, new_df], ignore_index=True) if not kept.empty else new_df

    if not combined.empty:
        canon = [_canon_pair(a, b) for a, b in zip(combined["ticker_a"], combined["ticker_b"])]
        combined = combined.assign(
            _canon_a=[c[0] for c in canon],
            _canon_b=[c[1] for c in canon],
        )
        combined = combined.drop_duplicates(subset=["as_of", "_canon_a", "_canon_b"], keep="last")
        combined = combined.drop(columns=["_canon_a", "_canon_b"])

    combined = combined.sort_values(["as_of", "ticker_a", "ticker_b"]).reset_index(drop=True)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.parent / f".{out_path.name}.tmp{os.getpid()}"
    combined.to_parquet(tmp_path, index=False)
    os.replace(tmp_path, out_path)


# ── Per-pair evaluation ──────────────────────────────────────────────────────
def evaluate_pair(ticker1, dated_closes_1, ticker2, dated_closes_2, as_of, window,
                   min_obs_frac, cost_bps_map, cost_k, default_cost_bps=DEFAULT_TICKER_COST_BPS):
    """Steps 5-9 for one candidate pair. Returns a dict of ledger fields
    (minus fdr_q/fdr_pass/approved, filled in after the scan-wide BH pass);
    `None` if the pair doesn't have enough overlapping history (not an
    error); or the `_COINT_ERROR` sentinel if `coint()` itself raised (an
    error -- the caller counts these in `errors_dropped`, item 4)."""
    built = build_aligned_window(dated_closes_1, dated_closes_2, as_of, window, min_obs_frac)
    if built is None:
        return None
    _, log1, log2, n_obs = built

    from statsmodels.tsa.stattools import coint

    try:
        _, p_1dep, _ = coint(log1, log2, trend="c")
        _, p_2dep, _ = coint(log2, log1, trend="c")
    except Exception:
        return _COINT_ERROR

    if p_1dep <= p_2dep:
        ticker_a, ticker_b, log_a, log_b, eg_pvalue = ticker1, ticker2, log1, log2, float(p_1dep)
    else:
        ticker_a, ticker_b, log_a, log_b, eg_pvalue = ticker2, ticker1, log2, log1, float(p_2dep)

    alpha, beta, spread = ols_hedge(log_a, log_b)
    spread_mean = float(np.mean(spread))
    sigma_spread = float(np.std(spread, ddof=1)) if len(spread) > 1 else 0.0
    half_life = ar1_half_life(spread)

    cost_a = cost_bps_map.get(ticker_a, default_cost_bps)
    cost_b = cost_bps_map.get(ticker_b, default_cost_bps)
    is_cost_ok = cost_ok(sigma_spread, cost_a, cost_b, cost_k=cost_k)

    return {
        "ticker_a": ticker_a,
        "ticker_b": ticker_b,
        "beta": beta,
        "alpha": alpha,
        "half_life_days": half_life if half_life is not None else float("nan"),
        "sigma_spread": sigma_spread,
        "spread_mean": spread_mean,
        "eg_pvalue": eg_pvalue,
        "cost_ok": bool(is_cost_ok),
        "n_obs": n_obs,
    }


# ── Orchestration ────────────────────────────────────────────────────────────
def run_scan(as_of, window=DEFAULT_WINDOW, min_corr=DEFAULT_MIN_CORR,
             fdr_q_threshold=DEFAULT_FDR_Q, cost_k=DEFAULT_COST_K,
             out_path=DEFAULT_LEDGER_PATH, corr_lookback_days=DEFAULT_CORR_LOOKBACK_DAYS,
             min_obs_frac=DEFAULT_MIN_OBS_FRAC, half_life_band=DEFAULT_HALF_LIFE_BAND,
             bucket_cap=DEFAULT_BUCKET_CAP, uri=None, universe_table=DEFAULT_UNIVERSE_TABLE):
    """Run one full weekly scan for `as_of`. Returns a summary dict:
    {buckets, pairs_tested, fdr_pass, approved, errors_dropped}. Raises
    PairsScannerDataError on DB/parquet access failures; an empty universe /
    zero pairs is a valid (non-error) outcome -- it still performs the
    replace-on-rescan ledger write for this as_of (item 3; see module
    docstring). `universe_table` defaults to the brief's pinned `universe`
    table; `_fetch_active_universe` auto-falls-back to `universe_config` when
    it is empty (item 7)."""
    if isinstance(as_of, str):
        as_of = datetime.date.fromisoformat(as_of)

    universe_rows = _fetch_active_universe(uri=uri, table=universe_table)
    buckets = build_buckets(universe_rows, cap=bucket_cap)
    cost_bps_map = _load_cost_bps()

    pair_records = []
    errors_dropped = 0
    for industry, rows in buckets.items():
        tickers = [r["ticker"] for r in rows]
        try:
            closes = _load_bucket_closes(tickers, as_of, window)
        except PairsScannerDataError:
            raise
        except Exception as exc:
            raise PairsScannerDataError(f"price load failed for bucket {industry!r}: {exc}") from exc

        for t1, t2 in itertools.combinations(tickers, 2):
            c1, c2 = closes.get(t1), closes.get(t2)
            if not c1 or not c2:
                continue
            passed, _rho = pearson_prefilter(c1, c2, as_of, corr_lookback_days, min_corr)
            if not passed:
                continue
            result = evaluate_pair(t1, c1, t2, c2, as_of, window, min_obs_frac,
                                    cost_bps_map, cost_k)
            if result is None:
                continue  # not enough overlapping history -- not an error
            if result is _COINT_ERROR:
                errors_dropped += 1  # item 4: coint() raised
                continue
            if not math.isfinite(result["eg_pvalue"]):
                errors_dropped += 1  # item 1: non-finite p -- never enters the FDR pool
                continue
            result["industry"] = industry
            pair_records.append(result)

    if errors_dropped:
        logger.warning(
            "pairs-scanner: as_of=%s dropped %d pair(s) from the FDR pool "
            "(coint() failure or non-finite eg_pvalue) -- see errors_dropped",
            as_of, errors_dropped,
        )

    pairs_tested = len(pair_records)
    if pairs_tested:
        qvals = bh_fdr([r["eg_pvalue"] for r in pair_records])
        for r, q in zip(pair_records, qvals):
            r["fdr_q"] = float(q)
            r["fdr_pass"] = bool(q < fdr_q_threshold)
    fdr_pass_count = sum(1 for r in pair_records if r.get("fdr_pass"))

    existing_df = _load_ledger(out_path)
    prev_pass_map = _previous_scan_pass_map(existing_df, as_of)

    lo, hi = half_life_band
    for r in pair_records:
        hl = r["half_life_days"]
        hl_ok = (hl == hl) and (lo <= hl <= hi)  # hl==hl is False for NaN
        pair_key = _canon_pair(r["ticker_a"], r["ticker_b"])
        prev_pass = prev_pass_map.get(pair_key, False)
        r["approved"] = bool(r["fdr_pass"] and prev_pass and hl_ok and r["cost_ok"])

    approved_count = sum(1 for r in pair_records if r["approved"])

    # item 3: replace-on-rescan unconditionally -- even pairs_tested == 0
    # must erase a stale prior claim for this as_of.
    new_df = _records_to_df(pair_records, as_of)
    _write_ledger_for_as_of(new_df, existing_df, as_of, out_path)

    return {
        "buckets": len(buckets),
        "pairs_tested": pairs_tested,
        "fdr_pass": fdr_pass_count,
        "approved": approved_count,
        "errors_dropped": errors_dropped,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description="Pairs-trading cointegration scanner (X1 foundation).")
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--min-corr", type=float, default=DEFAULT_MIN_CORR)
    ap.add_argument("--fdr-q", type=float, default=DEFAULT_FDR_Q)
    ap.add_argument("--cost-k", type=float, default=DEFAULT_COST_K)
    ap.add_argument("--out", default=DEFAULT_LEDGER_PATH)
    ap.add_argument("--universe-table", default=DEFAULT_UNIVERSE_TABLE,
                     help="Postgres table to read the active universe from "
                          "(default: universe, per spec). Not part of the "
                          "brief's pinned CLI surface -- added so an empty "
                          "`universe` table can be pointed at an alternative "
                          "without a code change; see module docstring.")
    args = ap.parse_args(argv)

    try:
        as_of = datetime.date.fromisoformat(args.as_of)
        summary = run_scan(
            as_of=as_of, window=args.window, min_corr=args.min_corr,
            fdr_q_threshold=args.fdr_q, cost_k=args.cost_k, out_path=args.out,
            universe_table=args.universe_table,
        )
    except PairsScannerDataError as exc:
        print(f"[pairs-scanner] ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"[pairs-scanner] as_of={args.as_of} buckets={summary['buckets']} "
        f"pairs_tested={summary['pairs_tested']} fdr_pass={summary['fdr_pass']} "
        f"approved={summary['approved']} errors_dropped={summary['errors_dropped']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
