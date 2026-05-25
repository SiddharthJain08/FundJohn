"""
Unified Market Data Store — R7 implementation.

FundJohn Pipeline Audit 2026-04-13

Single access layer for all market data, replacing the scattered per-source
read/write calls across pipeline.py and collector.js.

Storage layout (parquet, partitioned by date):
  {DATA_ROOT}/prices/{source}/{symbol}/YYYY-MM-DD.parquet
  {DATA_ROOT}/fundamentals/{source}/{symbol}/YYYY-MM-DD.parquet
  {DATA_ROOT}/filings/{source}/{symbol}/YYYY-MM-DD.parquet
  {DATA_ROOT}/research/{source}/{symbol}/YYYY-MM-DD.parquet
  {DATA_ROOT}/options/{source}/{symbol}/YYYY-MM-DD.parquet
  {DATA_ROOT}/compute/{symbol}/YYYY-MM-DD.parquet

Usage
-----
    store = MarketDataStore()

    # Write prices from Polygon
    await store.write_prices("polygon", "AAPL", df, date_str="2026-04-13")

    # Read latest prices (point-in-time safe — no data after as_of)
    df = await store.read_prices("AAPL", as_of="2026-04-13")

    # Read latest fundamentals across sources
    df = await store.read_fundamentals("AAPL", as_of="2026-04-13")
"""
import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

# Root data directory — can be overridden via env var
DATA_ROOT = Path(
    os.environ.get(
        "FUNDJOHN_DATA_ROOT",
        os.path.join(os.path.dirname(__file__), "../../../data"),
    )
)

# Parquet compression
PARQUET_COMPRESSION = "snappy"

# Source priority order for reads (most preferred first). AlphaVantage was
# removed 2026-04-28 — Polygon now covers prices end-to-end, with yahoo as
# tier-2 fallback.
SOURCE_PRIORITY: Dict[str, List[str]] = {
    "prices":       ["polygon", "yahoo"],
    "fundamentals": ["fmp", "polygon"],
    "filings":      ["edgar", "fmp"],
    "research":     ["tavily"],
    "options":      ["polygon", "yahoo"],
    "compute":      ["local"],
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _date_str(d: Union[str, date, datetime, None]) -> str:
    """Normalise any date-like to YYYY-MM-DD string."""
    if d is None:
        return date.today().isoformat()
    if isinstance(d, (date, datetime)):
        return d.strftime("%Y-%m-%d")
    return str(d)


def _partition_path(
    datatype: str,
    source:   str,
    symbol:   str,
    date_str: str,
) -> Path:
    """Return the parquet file path for a given partition."""
    if datatype == "compute":
        return DATA_ROOT / datatype / symbol.upper() / f"{date_str}.parquet"
    return DATA_ROOT / datatype / source / symbol.upper() / f"{date_str}.parquet"


def _df_from_records(records: Any, date_str: str) -> pd.DataFrame:
    """
    Convert heterogeneous API response to a flat DataFrame.
    Accepts: list[dict], dict with 'results' key, or existing DataFrame.
    """
    if isinstance(records, pd.DataFrame):
        df = records.copy()
    elif isinstance(records, list):
        df = pd.DataFrame(records)
    elif isinstance(records, dict):
        # Try common wrapper keys
        for key in ("results", "data", "items", "rows", "candles", "values"):
            if key in records and isinstance(records[key], list):
                df = pd.DataFrame(records[key])
                break
        else:
            df = pd.DataFrame([records])
    else:
        raise ValueError(f"Cannot convert {type(records)} to DataFrame")

    if df.empty:
        return df

    # Ensure a date column
    if "date" not in df.columns:
        df["date"] = date_str

    return df


# ── Core Store ───────────────────────────────────────────────────────────────

class MarketDataStore:
    """
    Unified parquet-based market data store with PIT-safe reads.

    All write operations are synchronous parquet writes wrapped in
    asyncio.to_thread for non-blocking use in async pipelines.

    PIT safety: read methods accept `as_of` parameter and never return
    data partitions later than that date, preventing look-ahead bias
    in backtesting scenarios.
    """

    def __init__(self, data_root: Path = DATA_ROOT):
        self._root = Path(data_root)
        self._write_lock = asyncio.Lock()

    # ── Write ─────────────────────────────────────────────────────────────────

    async def _write(
        self,
        datatype: str,
        source:   str,
        symbol:   str,
        data:     Any,
        date_str: str,
        schema:   Optional[pa.Schema] = None,
    ) -> Path:
        """Write records to the appropriate parquet partition."""
        df = _df_from_records(data, date_str)
        if df.empty:
            logger.debug("DataStore: empty DataFrame for %s/%s/%s — skipping write", datatype, source, symbol)
            return Path("")

        path = _partition_path(datatype, source, symbol, date_str)

        def _sync_write():
            path.parent.mkdir(parents=True, exist_ok=True)
            table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
            pq.write_table(table, path, compression=PARQUET_COMPRESSION)
            logger.debug("DataStore: wrote %d rows → %s", len(df), path)

        async with self._write_lock:
            await asyncio.to_thread(_sync_write)

        return path

    # ── Read (PIT-safe) ───────────────────────────────────────────────────────

    def _read_partitions(
        self,
        datatype:  str,
        source:    str,
        symbol:    str,
        as_of:     str,
        lookback:  int = 365,
    ) -> pd.DataFrame:
        """
        Read all partitions for a given source/symbol up to (and including) as_of.
        Never reads partitions later than as_of (PIT safety).
        """
        end   = date.fromisoformat(as_of)
        start = end - timedelta(days=lookback)

        frames = []
        for delta in range(lookback + 1):
            d = start + timedelta(days=delta)
            if d > end:
                break
            p = _partition_path(datatype, source, symbol, d.isoformat())
            if p.exists():
                try:
                    df = pq.read_table(p).to_pandas()
                    frames.append(df)
                except Exception as exc:
                    logger.warning("DataStore: failed to read %s — %s", p, exc)

        if not frames:
            return pd.DataFrame()

        combined = pd.concat(frames, ignore_index=True)

        # PIT filter: drop any row with a date column value after as_of
        if "date" in combined.columns:
            try:
                combined["date"] = pd.to_datetime(combined["date"])
                combined = combined[combined["date"] <= pd.Timestamp(as_of)]
            except Exception:
                pass

        return combined.drop_duplicates()

    def _read_best_source(
        self,
        datatype:  str,
        symbol:    str,
        as_of:     str,
        lookback:  int = 365,
    ) -> pd.DataFrame:
        """Try sources in priority order, return first non-empty result."""
        for src in SOURCE_PRIORITY.get(datatype, ["local"]):
            df = self._read_partitions(datatype, src, symbol, as_of, lookback)
            if not df.empty:
                logger.debug("DataStore: read %s/%s from source '%s'", datatype, symbol, src)
                return df
        logger.warning("DataStore: no data found for %s/%s as_of=%s", datatype, symbol, as_of)
        return pd.DataFrame()

    # ── Public API — Prices ───────────────────────────────────────────────────

    async def write_prices(
        self,
        source:   str,
        symbol:   str,
        data:     Any,
        date_str: Optional[str] = None,
    ) -> Path:
        return await self._write("prices", source, symbol, data, _date_str(date_str))

    async def read_prices(
        self,
        symbol:   str,
        as_of:    Optional[str] = None,
        source:   Optional[str] = None,
        lookback: int = 365,
    ) -> pd.DataFrame:
        as_of = _date_str(as_of)
        if source:
            return await asyncio.to_thread(
                self._read_partitions, "prices", source, symbol, as_of, lookback
            )
        return await asyncio.to_thread(
            self._read_best_source, "prices", symbol, as_of, lookback
        )

    # ── Public API — Fundamentals ─────────────────────────────────────────────

    async def write_fundamentals(
        self,
        source:   str,
        symbol:   str,
        data:     Any,
        date_str: Optional[str] = None,
    ) -> Path:
        return await self._write("fundamentals", source, symbol, data, _date_str(date_str))

    async def read_fundamentals(
        self,
        symbol:   str,
        as_of:    Optional[str] = None,
        source:   Optional[str] = None,
        lookback: int = 400,
    ) -> pd.DataFrame:
        as_of = _date_str(as_of)
        if source:
            return await asyncio.to_thread(
                self._read_partitions, "fundamentals", source, symbol, as_of, lookback
            )
        return await asyncio.to_thread(
            self._read_best_source, "fundamentals", symbol, as_of, lookback
        )

    # ── Public API — Filings ──────────────────────────────────────────────────

    async def write_filings(
        self,
        source:   str,
        symbol:   str,
        data:     Any,
        date_str: Optional[str] = None,
    ) -> Path:
        return await self._write("filings", source, symbol, data, _date_str(date_str))

    async def read_filings(
        self,
        symbol:   str,
        as_of:    Optional[str] = None,
        source:   Optional[str] = None,
        lookback: int = 90,
    ) -> pd.DataFrame:
        as_of = _date_str(as_of)
        if source:
            return await asyncio.to_thread(
                self._read_partitions, "filings", source, symbol, as_of, lookback
            )
        return await asyncio.to_thread(
            self._read_best_source, "filings", symbol, as_of, lookback
        )

    # ── Public API — Research / News ──────────────────────────────────────────

    async def write_research(
        self,
        source:   str,
        symbol:   str,
        data:     Any,
        date_str: Optional[str] = None,
    ) -> Path:
        return await self._write("research", source, symbol, data, _date_str(date_str))

    async def read_research(
        self,
        symbol:   str,
        as_of:    Optional[str] = None,
        lookback: int = 7,
    ) -> pd.DataFrame:
        as_of = _date_str(as_of)
        return await asyncio.to_thread(
            self._read_best_source, "research", symbol, as_of, lookback
        )

    # ── Public API — Options ──────────────────────────────────────────────────

    async def write_options(
        self,
        source:   str,
        symbol:   str,
        data:     Any,
        date_str: Optional[str] = None,
    ) -> Path:
        return await self._write("options", source, symbol, data, _date_str(date_str))

    async def read_options(
        self,
        symbol:   str,
        as_of:    Optional[str] = None,
        source:   Optional[str] = None,
        lookback: int = 30,
    ) -> pd.DataFrame:
        as_of = _date_str(as_of)
        if source:
            return await asyncio.to_thread(
                self._read_partitions, "options", source, symbol, as_of, lookback
            )
        return await asyncio.to_thread(
            self._read_best_source, "options", symbol, as_of, lookback
        )

    # ── Public API — Computed Signals ─────────────────────────────────────────

    async def write_compute(
        self,
        symbol:   str,
        data:     Any,
        date_str: Optional[str] = None,
    ) -> Path:
        return await self._write("compute", "local", symbol, data, _date_str(date_str))

    async def read_compute(
        self,
        symbol:   str,
        as_of:    Optional[str] = None,
        lookback: int = 365,
    ) -> pd.DataFrame:
        as_of = _date_str(as_of)
        return await asyncio.to_thread(
            self._read_partitions, "compute", "local", symbol, as_of, lookback
        )

    # ── Observability ─────────────────────────────────────────────────────────

    async def storage_stats(self) -> Dict[str, Any]:
        """Return basic storage stats (file counts and sizes) per datatype."""
        stats: Dict[str, Any] = {}
        for datatype in ("prices", "fundamentals", "filings", "research", "options", "compute"):
            dt_root = self._root / datatype
            if not dt_root.exists():
                stats[datatype] = {"files": 0, "size_mb": 0.0}
                continue
            files     = list(dt_root.rglob("*.parquet"))
            total_sz  = sum(f.stat().st_size for f in files if f.is_file())
            stats[datatype] = {
                "files":   len(files),
                "size_mb": round(total_sz / 1_048_576, 2),
            }
        return stats

    async def list_symbols(self, datatype: str = "prices") -> List[str]:
        """List all symbols with data for a given datatype."""
        dt_root = self._root / datatype
        if not dt_root.exists():
            return []
        symbols: set = set()
        for p in dt_root.rglob("*.parquet"):
            # Extract symbol (2nd-to-last path component for partitioned, 1st for compute)
            parts = p.relative_to(dt_root).parts
            sym_part = parts[1] if len(parts) > 2 else parts[0] if parts else None
            if sym_part:
                symbols.add(sym_part)
        return sorted(symbols)


# ── Module-level singleton ────────────────────────────────────────────────────

_store: Optional[MarketDataStore] = None


def get_store(data_root: Path = DATA_ROOT) -> MarketDataStore:
    """Return (or create) the module-level singleton MarketDataStore."""
    global _store
    if _store is None:
        _store = MarketDataStore(data_root)
    return _store
