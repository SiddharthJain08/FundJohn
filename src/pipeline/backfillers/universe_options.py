"""Fetch + validate option-EOD chunks for the SP-2 Phase B 5y backfill driver.

Used by `scripts/backfill_universe_5y.py::_run_options` (Task 9). The parallel
cutover-gap one-shot at `scripts/backfill_options_eod_cutover_gap.py` retains
its own self-contained copy of the helpers (operator-preserved CLI surface +
already-validated in production); this module is the reusable home so any
future options-EOD backfill window can share the driver's audit/quarantine
machinery without re-deriving the contract.

Pure module — only subprocess + pandas IO. The actual master parquet write is
delegated to `src/pipeline/backfillers/alpaca_options.py::_append_parquet`
(dedupes on (date, contract_symbol) under a thread lock).

Scope note: Phase B does NOT do full 5y options backfill (deferred to SP-3
alongside crypto). The driver-mode integration in `_run_options` is what lets
future narrow-window backfills reuse the same audit/quarantine machinery the
prices/metadata targets already have.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import date as _date
from typing import Iterable, Optional, Tuple

import pandas as pd


log = logging.getLogger(__name__)

ALPACA_BIN = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')

# Minimum schema for a staged options-EOD chunk. The master parquet contains
# many additional columns (greeks, market_price, implied_volatility, etc.)
# but for the cutover-gap path we only ever fill date/contract_symbol/OHLCV
# — the rest stay NULL (which dedupe-on-(date, contract_symbol) handles fine).
REQUIRED_COLUMNS = ('date', 'contract_symbol')


def _chunk(xs: list, n: int) -> Iterable[list]:
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _alpaca(args: list[str]) -> dict:
    res = subprocess.run([ALPACA_BIN] + args, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f'alpaca rc={res.returncode}: {res.stderr.strip()}')
    return json.loads(res.stdout)


def enumerate_contracts_for_date(d: _date, universe: list[str]) -> list[str]:
    """For each ticker, pull current chain, filter to contracts with expiry >= d.

    Rationale: any contract still listed today with expiry >= d existed on d,
    so its bars on d are fetchable. This is intentionally a lossy approximation
    — contracts that expired between d and today are NOT recovered. For the
    cutover-gap window (0-14d) the loss is minimal. A future full-history path
    would need a per-date chain snapshot from a different source.
    """
    contracts: list[str] = []
    for ticker in universe:
        try:
            page = _alpaca([
                'data', 'option', 'chain',
                '--underlying-symbol', ticker, '--limit', '100',
            ])
            for sym in (page.get('snapshots') or {}).keys():
                # Parse YYMMDD from OCC symbol (root + YYMMDD + C/P + 8-digit strike)
                expiry: Optional[_date] = None
                for i, ch in enumerate(sym):
                    if ch.isdigit():
                        try:
                            yy, mm, dd = sym[i:i + 2], sym[i + 2:i + 4], sym[i + 4:i + 6]
                            expiry = _date(2000 + int(yy), int(mm), int(dd))
                        except (ValueError, IndexError):
                            expiry = None
                        break
                if expiry is not None and expiry >= d:
                    contracts.append(sym)
        except Exception as e:
            log.warning('chain enum failed for %s: %s', ticker, e)
    return contracts


def fetch_eod_for_contracts(contracts: list[str], d: _date) -> pd.DataFrame:
    """Batch alpaca data option bars 100 at a time for date `d`.

    Returns a DataFrame with at minimum REQUIRED_COLUMNS populated. Empty
    DataFrame on no data. Per-batch errors are logged but do not raise — a
    partial chunk is still a valid chunk for audit purposes (validate() catches
    "no rows at all" via the nonempty check).
    """
    rows: list[dict] = []
    for batch in _chunk(contracts, 100):
        try:
            payload = _alpaca([
                'data', 'option', 'bars',
                '--symbols', ','.join(batch),
                '--start', d.isoformat(),
                '--end', d.isoformat(),
                '--timeframe', '1Day',
            ])
            for sym, bars in (payload.get('bars') or {}).items():
                for b in bars:
                    rows.append({
                        'date': d.isoformat(),
                        'contract_symbol': sym,
                        'open': b.get('o'),
                        'high': b.get('h'),
                        'low': b.get('l'),
                        'close': b.get('c'),
                        'volume': b.get('v'),
                        'vwap': b.get('vw'),
                        'transactions': b.get('n'),
                        'data_source': 'alpaca_aat_plus_backfill',
                    })
        except Exception as e:
            log.warning('bars batch fail %s: %s', batch[:3], e)
    if not rows:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))
    return pd.DataFrame(rows)


def validate(df: Optional[pd.DataFrame], d: _date) -> Tuple[bool, Optional[str]]:
    """Returns (ok, error_str_or_None).

    Minimum gates:
      - DataFrame nonempty.
      - REQUIRED_COLUMNS present.
      - All rows have date == d.isoformat() (catches batch-fetch shape errors).

    Intentionally no row-count threshold like prices'. Per-date option-EOD
    rowcount is universe-bounded but highly variable (a small-cap with 30
    near-expiry contracts looks very different from SPY with 5000+).
    """
    if df is None or len(df) == 0:
        return False, 'empty DataFrame'
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        return False, f'missing columns: {missing}'
    expected = d.isoformat()
    bad = df[df['date'] != expected]
    if len(bad) > 0:
        sample = bad['date'].iloc[0]
        return False, f'date mismatch: expected {expected}, found {sample} ({len(bad)} rows)'
    return True, None
