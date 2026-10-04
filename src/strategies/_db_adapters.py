from __future__ import annotations
import os
import psycopg2
import pandas as pd
from datetime import date

from src.strategies.universe_meta import TickerMetadata, overlay_security_types

# History rule (spec 2026-10-04 §1): security type is static, so the LATEST
# non-NULL value per symbol applies to every snapshot date.
LATEST_SECURITY_TYPE_SQL = """
    SELECT DISTINCT ON (symbol) symbol, security_type
    FROM ticker_metadata_snapshots
    WHERE security_type IS NOT NULL
    ORDER BY symbol, snapshot_date DESC
"""
_HAS_SECURITY_TYPE_COL_SQL = """
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'ticker_metadata_snapshots' AND column_name = 'security_type'
"""


class PostgresMetadataDB:
    def __init__(self, dsn, conn=None):
        self._dsn = dsn
        self._conn = conn      # optional long-lived connection (SP-7 C1: one per cycle)
        # Single-slot memo (most-recent as_of) — ticker_metadata_snapshots is
        # append-only daily, so a memo is correct and collapses the live
        # resolver's 67 identical queries per cycle into one.  A NEW as_of
        # evicts the old entry so batch callers iterating many as_of values
        # (e.g. build_tier_membership ~60 monthly snapshots ×5k symbols) do
        # not accumulate snapshots on the 8GB no-swap box.
        self._memo: dict = {}
        self._security_types: dict | None = None

    def fetch_latest_security_types(self, conn=None) -> dict:
        """{symbol: latest non-NULL security_type}. Memoized per instance.
        Returns {} (with a warning) when migration 163 has not been applied,
        so the resolver keeps working on a pre-163 database — every existing
        predicate ignores security_type; only stocks_* tiers read it."""
        if self._security_types is None:
            conn = conn if conn is not None else self._conn
            try:
                if conn is not None:
                    self._security_types = self._fetch_security_types(conn)
                else:
                    with psycopg2.connect(self._dsn) as c:
                        self._security_types = self._fetch_security_types(c)
            except Exception as e:  # noqa: BLE001 — FAIL-OPEN: no Phase-1 strategy reads it
                import logging
                logging.getLogger(__name__).warning(
                    'security_type overlay unavailable (%s: %s) — continuing with '
                    'unknown types; existing universes are unaffected', type(e).__name__, e)
                if conn is not None:
                    try:
                        conn.rollback()   # leave the caller's transaction usable
                    except Exception:  # noqa: BLE001
                        pass
                self._security_types = {}   # memoize: do not re-issue the failing query
        return self._security_types

    @staticmethod
    def _fetch_security_types(c) -> dict:
        with c.cursor() as cur:
            cur.execute(_HAS_SECURITY_TYPE_COL_SQL)
            if cur.fetchone() is None:
                import logging
                logging.getLogger(__name__).warning(
                    'ticker_metadata_snapshots.security_type missing (migration 163 '
                    'not applied) — security types unknown; stocks_* tiers fall back '
                    'to in_sp500 only')
                return {}
            cur.execute(LATEST_SECURITY_TYPE_SQL)
            return {sym: st for sym, st in cur.fetchall()}

    def fetch_metadata_as_of(self, as_of):
        if as_of in self._memo:
            return self._memo[as_of]
        if self._conn is not None:
            rows = self._fetch(self._conn, as_of)
        else:
            with psycopg2.connect(self._dsn) as c:
                rows = self._fetch(c, as_of)
                # same connection: still exactly one connect per new as_of
                self.fetch_latest_security_types(c)   # memoizes on first use
        # Static-attribute overlay (latest known type on every as_of).
        rows = overlay_security_types(rows, self.fetch_latest_security_types())
        # Single-slot memo: the live path uses one as_of per process (full
        # 67→1 collapse); batch callers iterate distinct as_of values once
        # each, so retaining history is pure memory cost on the 8GB box.
        self._memo = {as_of: rows}
        return rows

    def _fetch(self, c, as_of):
        with c.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (symbol)
                    snapshot_date, symbol, asset_class, exchange, status, tradable,
                    shortable, fractionable, easy_to_borrow, market_cap, adv_usd_20d,
                    sector, industry, options_eligible, in_sp500, in_r1000, in_r3000,
                    listed_date, delisted_date
                FROM ticker_metadata_snapshots
                WHERE snapshot_date <= %s
                ORDER BY symbol, snapshot_date DESC
            """, (as_of,))
            cols = [d.name for d in cur.description]
            rows = []
            class _Row: pass
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                row = _Row()
                row.symbol = d["symbol"]
                row.snapshot_date = d.pop("snapshot_date")
                # market_cap and adv_usd_20d come back as Decimal from psycopg2; cast to float
                if d["market_cap"] is not None:
                    d["market_cap"] = float(d["market_cap"])
                if d["adv_usd_20d"] is not None:
                    d["adv_usd_20d"] = float(d["adv_usd_20d"])
                row.metadata = TickerMetadata(**d)
                rows.append(row)
            return rows


class ParquetCoverage:
    def __init__(self, prices_path="/root/openclaw/data/master/prices.parquet", min_bars=60):
        self._path = prices_path
        self._min_bars = min_bars
        # cache: {month_iso → {ticker → bar_count}}
        self._counts_by_month: dict[str, dict[str, int]] = {}

    def _load_month(self, as_of):
        month = as_of.isoformat()[:7]
        if month in self._counts_by_month:
            return self._counts_by_month[month]
        if not os.path.exists(self._path):
            self._counts_by_month[month] = {}
            return self._counts_by_month[month]
        df = pd.read_parquet(self._path, columns=["ticker", "date"])
        # SP-2 Phase B Task 5: drop quarantined (ticker, date) pairs so a
        # ticker whose unsuperseded bars push it below MIN_BARS_FOR_INCLUSION
        # is correctly excluded from the universe. The filter is a no-op
        # when data_quarantine has zero unsuperseded rows for prices.parquet.
        from src.pipeline.quarantine_filter import filter_quarantined
        df = filter_quarantined(df, "prices.parquet")
        # date column is stored as ISO string (YYYY-MM-DD); string comparison is correct
        df = df[df["date"] <= as_of.isoformat()]
        counts = df.groupby("ticker").size().to_dict()
        self._counts_by_month[month] = counts
        return counts

    def has_floor(self, symbol, as_of):
        counts = self._load_month(as_of)
        return counts.get(symbol, 0) >= self._min_bars
