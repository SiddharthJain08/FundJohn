"""Composite source builder for ticker_metadata_snapshots — SP-2 Phase B Task 8.

Shared row-builder used by:
  - scripts/backfill_universe_5y.py::_run_metadata (historical monthly snapshots)
  - src/pipeline/ticker_metadata_writer.py        (Phase A daily writer)

Schema mirrors `ticker_metadata_snapshots` (migration 111).

Sources & deviations from spec (2026-05-22-sp2-phase-b-5y-backfill.md):

  * `alpaca_tradable_universe` (Postgres) → asset_class/exchange/status/
    tradable/shortable/fractionable/easy_to_borrow/first_seen_at/last_seen_at.
    At a given snapshot_date we:
      - skip tickers where first_seen_at > snapshot_date (not yet listed)
      - mark status='inactive' when last_seen_at < snapshot_date.

  * Master parquet at data/master/prices.parquet → adv_usd_20d
    DEVIATION: spec template referenced `symbol`/`adj_close`; verified live
    parquet uses `ticker`/`close` (no adj_close column). We compute
    dollar_volume = close * volume and take the 20-bar mean ending at the
    snapshot. If <5 bars available before snapshot_date we return None for
    adv_usd_20d (row is still kept; ranking pool excludes it).

  * FMP `historical-market-capitalization` → market_cap
    DEVIATION: 2026-05-22 probe (docs/archive/superpowers/specs/sp2-fmp-mktcap-probe.md)
    showed this endpoint returns 403 on our Starter tier. Decision recorded:
    FALLBACK:prices_x_shares — but a historical shares-outstanding feed is not
    yet wired (FMP `profile` only returns *current* sharesOutstanding). For
    Task 8 we therefore set `market_cap = None` for all historical (non-today)
    snapshots, and the daily writer continues to fill `market_cap` from its
    FMP `profile` cache for today's row. Documented degradation; flagged in
    DONE_WITH_CONCERNS report. Consequence: `in_r1000`/`in_r3000` are False
    for all historical snapshots until a shares-out backfill ships.

  * FMP `profile?symbol=<T>` → sector/industry. For non-current snapshots
    we stub as None (documented proxy — sectors do drift but the daily
    writer's cache fills them for today's row).

  * data/sp500_historical_membership_v1.csv → in_sp500 (point-in-time,
    matches Wikipedia membership history).

  * `in_r1000`/`in_r3000` — top-1000 / top-3000 by market_cap rank within the
    snapshot, intersected with `tradable=True AND status='active'`. Tickers
    with `market_cap is None` are excluded from the ranking pool.

  * `options_eligible` — False for historical snapshots (chain probe is
    expensive and time-varying); daily writer overrides for today.

  * `listed_date` / `delisted_date` — None for now (live writer fills the
    today row from FMP profile.ipoDate + alpaca last_seen_at).
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
MASTER_PRICES = ROOT / 'data' / 'master' / 'prices.parquet'
SP500_CSV = ROOT / 'data' / 'sp500_historical_membership_v1.csv'

# Soft throttle between FMP calls when (and if) we ever call them from inside
# this module. Matches the `MIN_BACKFILL_CALLS` pattern in src/pipeline/
# backfillers/fmp.py — keeps us under FMP Starter's 300 req/min cap.
_FMP_SLEEP_S = 0.05


# ── SP500 historical membership ───────────────────────────────────────────────

def _record_fmp(endpoint, status, body):
    """data_provider_health, best-effort (2026-08-23)."""
    try:
        from src.maintenance.provider_health import record_http
        return record_http('fmp', endpoint, status, body)
    except Exception:  # noqa: BLE001
        return None

def _sp500_membership_on(d: date) -> set[str]:
    """Return the set of tickers that were SP500 members on date `d`.

    The CSV schema is (ticker, added_on, removed_on). `removed_on` empty means
    currently a member. A member is "in" on date d iff added_on <= d AND
    (removed_on is empty OR removed_on > d).
    """
    if not SP500_CSV.exists():
        return set()
    rows = pd.read_csv(SP500_CSV, comment='#')
    if rows.empty:
        return set()
    iso = d.isoformat()
    # Some rows have empty removed_on (still in SP500). pandas reads empty as
    # NaN; treat NaN as "still in".
    in_on = (rows['added_on'].fillna('9999-12-31') <= iso) & (
        rows['removed_on'].isna() | (rows['removed_on'].fillna('') > iso)
    )
    return set(rows.loc[in_on, 'ticker'].astype(str))


# ── Market cap (historical) ───────────────────────────────────────────────────
def _market_cap_for(symbol: str, on: date) -> Optional[float]:
    """Historical market cap via FMP /stable/historical-market-capitalization.

    Ported 2026-08-23 from /api/v3/historical-market-capitalization/{sym},
    which returns 403 "Legacy Endpoint" for this key (the 2026-05-22 probe's
    "403 on the Starter tier" was this, not a plan limit). Stable payload:
    [{"symbol", "date", "marketCap"}]; from/to honoured; [] on a non-trading
    date — so we ask for the trailing week and take the latest row on/before
    `on` (month-end snapshots often fall on a weekend). Returns None on any
    failure so build_month_snapshot still produces structurally valid rows.
    """
    # Short-circuit: if FMP_API_KEY is missing we definitely can't call it.
    api_key = os.environ.get('FMP_API_KEY')
    if not api_key:
        return None

    try:
        import requests  # local import; this module shouldn't require requests if unused
    except ImportError:
        return None

    url = 'https://financialmodelingprep.com/stable/historical-market-capitalization'
    try:
        r = requests.get(
            url,
            params={
                'symbol': symbol,
                'from': (on - timedelta(days=7)).isoformat(),
                'to': on.isoformat(),
                'apikey': api_key,
            },
            timeout=10,
        )
        time.sleep(_FMP_SLEEP_S)  # soft-throttle under Starter cap
        _record_fmp('historical-market-capitalization', r.status_code, getattr(r, 'text', ''))
        if r.status_code != 200:
            return None
        payload = r.json()
        if not isinstance(payload, list) or not payload:
            return None
        iso = on.isoformat()
        rows = [p for p in payload if isinstance(p, dict) and str(p.get('date', ''))[:10] <= iso]
        if not rows:
            return None
        best = max(rows, key=lambda p: str(p.get('date', ''))[:10])
        mc = best.get('marketCap')
        try:
            mc_f = float(mc) if mc is not None else None
        except (TypeError, ValueError):
            return None
        return mc_f or None
    except Exception:
        return None


# ── adv_usd_20d from master parquet ───────────────────────────────────────────
def _adv_usd_20d_batch(
    symbols: Iterable[str],
    on: date,
    master_path: Path = MASTER_PRICES,
) -> dict[str, Optional[float]]:
    """Batch compute 20-day rolling mean of close*volume ending on/before `on`.

    Returns a dict {symbol: float | None}. Symbols missing from the parquet
    (or with <5 bars on/before `on`) get None. Reading the parquet once and
    filtering in-memory is much cheaper than per-ticker pq filters when we
    have ~3000 symbols.
    """
    out: dict[str, Optional[float]] = {s: None for s in symbols}
    if not master_path.exists():
        return out
    try:
        df = pd.read_parquet(
            master_path, columns=['ticker', 'date', 'close', 'volume'],
        )
    except Exception:
        return out
    if df.empty:
        return out

    on_iso = on.isoformat()
    syms = set(out.keys())
    df = df[df['ticker'].isin(syms) & (df['date'].astype(str) <= on_iso)]
    if df.empty:
        return out

    df = df.sort_values(['ticker', 'date'])
    # Take the last 20 bars per ticker (on or before `on`).
    tail20 = df.groupby('ticker').tail(20)
    # Count per group; require >=5.
    counts = tail20.groupby('ticker').size()
    eligible = counts[counts >= 5].index
    tail20 = tail20[tail20['ticker'].isin(eligible)]
    if tail20.empty:
        return out

    tail20 = tail20.copy()
    tail20['dollar_volume'] = (
        tail20['close'].astype(float) * tail20['volume'].astype(float)
    )
    means = tail20.groupby('ticker')['dollar_volume'].mean()
    for sym, v in means.items():
        out[sym] = float(v) if pd.notna(v) else None
    return out


# ── Alpaca status (historical) ────────────────────────────────────────────────
def _alpaca_status_batch(
    symbols: list[str], on: date, pg,
) -> dict[str, dict]:
    """Read alpaca_tradable_universe in one query for the given symbols.

    Filters per-row by point-in-time semantics:
      - listed_date > on      → ticker not yet listed; row absent from result
        (falls back to first_seen_at when listed_date IS NULL — legacy behaviour)
      - last_seen_at < on     → mark status='inactive' (still returned)
    """
    if not symbols:
        return {}
    out: dict[str, dict] = {}
    with pg.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, asset_class, exchange, status, tradable, shortable,
                   fractionable, easy_to_borrow, first_seen_at, last_seen_at,
                   listed_date
              FROM alpaca_tradable_universe
             WHERE symbol = ANY(%s)
            """,
            (list(symbols),),
        )
        cols = [d.name for d in cur.description]
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            first_seen = d.get('first_seen_at')
            last_seen = d.get('last_seen_at')
            listed = d.get('listed_date')
            # Coerce TIMESTAMPTZ / datetime → date for comparison.
            if isinstance(first_seen, datetime):
                first_seen = first_seen.date()
            if isinstance(last_seen, datetime):
                last_seen = last_seen.date()
            if isinstance(listed, datetime):
                listed = listed.date()
            effective_listing = listed or first_seen   # PIT: listed_date wins
            if effective_listing and effective_listing > on:
                continue  # not yet listed on the snapshot date
            if last_seen and last_seen < on:
                d['status'] = 'inactive'
            out[d['symbol']] = d
    return out


# ── In-batch r1000/r3000 ranking ──────────────────────────────────────────────
def rank_in_r1000_r3000(rows_or_df) -> tuple[set[str], set[str]]:
    """Rank tickers in r1000 / r3000 by descending market_cap.

    Pool = rows where tradable=True AND status='active' AND market_cap is not
    None. Tickers outside the pool are NOT in r1000/r3000.

    Accepts either a list[dict] (Phase A live writer) or a pandas DataFrame
    (Phase B builder) — extracted as a shared helper so both call sites stay
    DRY.
    """
    if hasattr(rows_or_df, 'empty'):  # DataFrame
        df = rows_or_df
        if df.empty:
            return set(), set()
        elig = df[
            df.get('tradable', False).fillna(False)
            & (df.get('status', '') == 'active')
            & df['market_cap'].notna()
        ].copy()
        if elig.empty:
            return set(), set()
        elig = elig.sort_values('market_cap', ascending=False)
        syms = elig['symbol'].astype(str).tolist()
    else:
        # list[dict] — Phase A live writer's pre-existing path
        ranked = sorted(
            (
                (r['symbol'], r.get('market_cap'))
                for r in rows_or_df
                if (
                    r.get('tradable', False)
                    and r.get('status') == 'active'
                    and r.get('market_cap') is not None
                )
            ),
            key=lambda x: -(x[1] or 0.0),
        )
        syms = [s for s, _ in ranked]
    r1000 = set(syms[:1000])
    r3000 = set(syms[:3000])
    return r1000, r3000


# ── Public: build a single month's snapshot ───────────────────────────────────
SCHEMA_COLUMNS = [
    'snapshot_date', 'symbol', 'asset_class', 'exchange', 'status',
    'tradable', 'shortable', 'fractionable', 'easy_to_borrow',
    'market_cap', 'adv_usd_20d', 'sector', 'industry', 'options_eligible',
    'in_sp500', 'in_r1000', 'in_r3000', 'listed_date', 'delisted_date',
]


def build_month_snapshot(
    snapshot_date: date,
    universe: list[str],
    pg,
    *,
    market_cap_lookup: Optional[dict[str, Optional[float]]] = None,
    fetch_market_cap: bool = False,
) -> pd.DataFrame:
    """Return one row per (snapshot_date, ticker) matching the snapshots schema.

    Parameters
    ----------
    snapshot_date : date
        Point-in-time anchor. Tickers not yet listed on this date are dropped.
    universe : list[str]
        Candidate tickers. Output is filtered to those present in
        alpaca_tradable_universe with first_seen_at <= snapshot_date.
    pg : psycopg2 connection
        Used to read alpaca_tradable_universe.
    market_cap_lookup : dict[str, float|None] | None
        Optional pre-fetched market caps (e.g. from FMP profile cache for the
        live daily path). When provided, this dict overrides any per-symbol
        lookup. Symbols absent from the dict get None.
    fetch_market_cap : bool
        If True, fall back to calling FMP per-symbol when the lookup misses a
        value. Default False because the historical endpoint is 403 on our
        Starter tier (probe 2026-05-22) — caller can opt-in once that lights
        up. Daily-writer path passes the cache directly via market_cap_lookup,
        so it never needs this flag.

    Returns an EMPTY DataFrame with SCHEMA_COLUMNS if no tickers match.
    """
    alpaca = _alpaca_status_batch(list(universe), snapshot_date, pg)
    sp500 = _sp500_membership_on(snapshot_date)

    # Symbols that survive the point-in-time first_seen check.
    present = [s for s in universe if s in alpaca]

    # adv_usd_20d in one parquet read.
    adv_map = _adv_usd_20d_batch(present, snapshot_date)

    # market_cap: priority = explicit lookup → optional FMP fallback → None.
    def _mc(sym: str) -> Optional[float]:
        if market_cap_lookup is not None and sym in market_cap_lookup:
            return market_cap_lookup[sym]
        if fetch_market_cap:
            return _market_cap_for(sym, snapshot_date)
        return None

    rows: list[dict] = []
    for sym in present:
        a = alpaca[sym]
        rows.append({
            'snapshot_date': snapshot_date,
            'symbol': sym,
            'asset_class': a.get('asset_class') or 'us_equity',
            'exchange': a.get('exchange'),
            'status': a.get('status') or 'active',
            'tradable': bool(a.get('tradable')),
            'shortable': bool(a.get('shortable')),
            'fractionable': bool(a.get('fractionable')),
            'easy_to_borrow': bool(a.get('easy_to_borrow')),
            'market_cap': _mc(sym),
            'adv_usd_20d': adv_map.get(sym),
            'sector': None,           # historical proxy; daily writer fills today's row
            'industry': None,
            'options_eligible': False,  # historical proxy; daily writer overrides
            'in_sp500': sym in sp500,
            'in_r1000': False,        # filled below after ranking
            'in_r3000': False,        # filled below after ranking
            'listed_date': None,
            'delisted_date': None,
        })

    df = pd.DataFrame(rows, columns=SCHEMA_COLUMNS)
    if df.empty:
        return df

    r1000, r3000 = rank_in_r1000_r3000(df)
    df['in_r1000'] = df['symbol'].isin(r1000)
    df['in_r3000'] = df['symbol'].isin(r3000)
    return df
