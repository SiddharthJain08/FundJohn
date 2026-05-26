"""SP-3.1 Phase B: hourly BTC/ETH bar feed for the crypto regime detector.

Fetches 1H bars via the Alpaca CLI (independent of union_universe — the regime
detector must have data even when no crypto strategy is live) and appends to the
append-only master parquet data/master/crypto_bars_1h.parquet, keyed by
(ticker, ts_utc). NEVER drops rows (CLAUDE.md invariant)."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
ALPACA_CLI = '/root/go/bin/alpaca'
DEFAULT_PARQUET = ROOT / 'data' / 'master' / 'crypto_bars_1h.parquet'
CRYPTO_REGIME_SYMBOLS = ('BTC-USD', 'ETH-USD')
COLUMNS = ['ticker', 'ts_utc', 'open', 'high', 'low', 'close', 'volume', 'vwap']


def _alpaca_crypto_symbol(ticker: str) -> str:
    """'BTC-USD' -> 'BTC/USD' (matches Phase A executor + collector.js)."""
    return ticker.strip().upper().replace('-', '/')


def _parse_bars(payload: dict, api_symbol: str, ticker: str) -> list[dict]:
    """Map an `alpaca data crypto bars` payload to row dicts for `ticker`."""
    bars = (payload.get('bars') or {}).get(api_symbol) or []
    out = []
    for b in bars:
        out.append({
            'ticker': ticker, 'ts_utc': b['t'],
            'open': float(b.get('o') or 0.0), 'high': float(b.get('h') or 0.0),
            'low': float(b.get('l') or 0.0), 'close': float(b.get('c') or 0.0),
            'volume': float(b.get('v') or 0.0), 'vwap': float(b.get('vw') or 0.0),
        })
    return out


def _run_cli(args: list[str], timeout: int = 60) -> dict | None:
    proc = subprocess.run([ALPACA_CLI, *args], capture_output=True, text=True,
                          timeout=timeout, check=False)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def fetch_bars(ticker: str, start: str, end: str, *, limit: int = 1000,
               max_pages: int = 200) -> list[dict]:
    """Fetch 1H bars for `ticker` in [start, end] (RFC3339 or YYYY-MM-DD),
    following next_page_token. Returns row dicts (possibly empty)."""
    api_symbol = _alpaca_crypto_symbol(ticker)
    base = ['data', 'crypto', 'bars', '--symbols', api_symbol,
            '--start', start, '--end', end, '--timeframe', '1Hour', '--limit', str(limit)]
    rows: list[dict] = []
    page_token, pages = None, 0
    while True:
        args = [*base, '--page-token', page_token] if page_token else base
        payload = _run_cli(args)
        if not payload:
            break
        rows.extend(_parse_bars(payload, api_symbol, ticker))
        page_token = payload.get('next_page_token')
        pages += 1
        if not page_token or pages >= max_pages:
            break
    return rows


def append_bars(rows: list[dict], *, parquet_path: Path = DEFAULT_PARQUET) -> int:
    """Append rows to the master parquet, deduping on (ticker, ts_utc).
    APPEND-ONLY: existing rows are always preserved; only genuinely new
    (ticker, ts_utc) keys are added. Returns the number of NEW rows written."""
    if not rows:
        return 0
    new = pd.DataFrame(rows, columns=COLUMNS)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    if parquet_path.exists():
        existing = pd.read_parquet(parquet_path)
        before = len(existing)
        combined = pd.concat([existing, new], ignore_index=True)
    else:
        before = 0
        combined = new
    combined = combined.drop_duplicates(subset=['ticker', 'ts_utc'], keep='first')
    combined = combined.sort_values(['ticker', 'ts_utc']).reset_index(drop=True)
    combined.to_parquet(parquet_path, index=False)
    return len(combined) - before


def update_recent(*, hours_back: int = 48, parquet_path: Path = DEFAULT_PARQUET) -> int:
    """Fetch the last `hours_back` hours for all regime symbols and append."""
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours_back)
    total = 0
    for tk in CRYPTO_REGIME_SYMBOLS:
        rows = fetch_bars(tk, start.strftime('%Y-%m-%dT%H:%M:%SZ'),
                          end.strftime('%Y-%m-%dT%H:%M:%SZ'))
        total += append_bars(rows, parquet_path=parquet_path)
    return total


def backfill(start: str, end: str, *, parquet_path: Path = DEFAULT_PARQUET) -> int:
    """Operator-invoked one-time history seed for all regime symbols."""
    total = 0
    for tk in CRYPTO_REGIME_SYMBOLS:
        rows = fetch_bars(tk, start, end)
        total += append_bars(rows, parquet_path=parquet_path)
    return total
