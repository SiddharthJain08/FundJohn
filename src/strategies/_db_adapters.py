from __future__ import annotations
import os
import psycopg2
import pandas as pd
from datetime import date

from src.strategies.universe_meta import TickerMetadata


class PostgresMetadataDB:
    def __init__(self, dsn):
        self._dsn = dsn

    def fetch_metadata_as_of(self, as_of):
        with psycopg2.connect(self._dsn) as c, c.cursor() as cur:
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
        # date column is stored as ISO string (YYYY-MM-DD); string comparison is correct
        df = df[df["date"] <= as_of.isoformat()]
        counts = df.groupby("ticker").size().to_dict()
        self._counts_by_month[month] = counts
        return counts

    def has_floor(self, symbol, as_of):
        counts = self._load_month(as_of)
        return counts.get(symbol, 0) >= self._min_bars
