"""Batched daily/intraday bars via `alpaca data multi-bars` — Python twin of
collector.js fillPricesAlpacaBatch (2026-08-22).

One contract for every Python caller that pulls bars for many symbols
(backfillers/universe_prices, ingestion/ingest_prices_30m_alpaca). Live
behaviour of the endpoint, probed on the CLI and encoded here (see
docs/reference/alpaca-cli-integration.md §5/§8):

  * `--limit 10000` counts data points across ALL symbols in the request, so
    the chunk size shrinks with the range: 200 symbols for a 1-day daily gap,
    ~31 for a full daily year, ~2 for a year of 30-minute bars.
  * well-formed symbols Alpaca doesn't know are silently ABSENT from `bars`
    (rc=0) — they come back as [] so callers can treat them as "no data".
  * a MALFORMED symbol (`BRK-B`, `^GSPC`, `ES=F`, `BTC-USD`) fails the WHOLE
    request with rc=1 `invalid symbol: X`. Callers must pre-filter with
    partition_symbols(); a reject that still slips through drops X and
    retries the chunk (X → []).
  * any other failure (5xx, timeout, auth) raises MultiBarsError — the
    caller decides whether to fall back per symbol.
  * every call carries --quiet (stderr stays a pure JSON error document,
    parsed whole — v0.0.10+ pretty-prints it) and --timeout.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

ALPACA_BIN = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')

MAX_SYMBOLS_PER_REQUEST = 200   # URL-length headroom; 200 × 1 day = 42 KB, 0.3 s
TARGET_POINTS_PER_REQUEST = 8000  # keep each request under the 10 000-point page
PAGE_LIMIT = 10000
MAX_PAGES = 200
MAX_INVALID_DROPS = 3

_STOCK_SYMBOL_RE = re.compile(r'^[A-Z][A-Z0-9]*(\.[A-Z]+)?$')
_INVALID_SYMBOL_RE = re.compile(r'invalid symbol:\s*([^\s,"]+)', re.I)


class MultiBarsError(RuntimeError):
    """A multi-bars chunk failed for a non-symbol reason (5xx, timeout, auth)."""

    def __init__(self, message: str, *, status=None, code=None, rc=None, auth_error=False):
        super().__init__(message)
        self.status, self.code, self.rc, self.auth_error = status, code, rc, auth_error


def is_alpaca_stock_symbol(api_symbol) -> bool:
    return bool(api_symbol) and bool(_STOCK_SYMBOL_RE.match(str(api_symbol)))


def to_api_symbol(ticker: str) -> str:
    """Universe BRK-B / AGM-PRI → Alpaca BRK.B / AGM.PRI (first separator only,
    same bridge as collector.fillPricesAlpaca)."""
    return str(ticker).replace('-', '.', 1)


def partition_symbols(tickers: Iterable[str]) -> Tuple[Dict[str, str], List[str]]:
    """Split universe tickers into {api_symbol: ticker} that can go in a batch
    and the list of malformed ones (index / futures / forex / dash-crypto)
    that must take a per-symbol path."""
    good: Dict[str, str] = {}
    bad: List[str] = []
    for t in tickers:
        api = to_api_symbol(t)
        if is_alpaca_stock_symbol(api):
            good[api] = t
        else:
            bad.append(t)
    return good, bad


def _trading_days(start: str, end: str) -> int:
    try:
        days = (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days
    except ValueError:
        return 1
    if days < 0:
        return 0
    return max(1, round(days * 5 / 7) + 1)


def chunk_size(start: str, end: str, bars_per_day: int = 1,
               target_points: int = TARGET_POINTS_PER_REQUEST,
               max_symbols: int = MAX_SYMBOLS_PER_REQUEST) -> int:
    """Symbols per request so a request is ≈ one page. Never 0."""
    td = _trading_days(start, end)
    if td == 0:
        return 1
    points_per_symbol = max(1, td * max(1, int(bars_per_day)))
    return max(1, min(max_symbols, target_points // points_per_symbol))


def _parse_stderr(text: str) -> dict:
    text = (text or '').strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {'error': text[:300]}
    except ValueError:
        return {'error': text[:300]}


def _run(args: List[str], timeout: int, alpaca_bin: str):
    cmd = [alpaca_bin, *args, '--quiet', '--timeout', str(max(1, int(timeout) - 1))]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=int(timeout) + 5, check=False)
    except FileNotFoundError:
        raise MultiBarsError(f'alpaca CLI not found at {alpaca_bin}', rc=127)
    except subprocess.TimeoutExpired:
        raise MultiBarsError(f'alpaca multi-bars timed out after {int(timeout) + 5}s', rc=124)
    return proc


def _fetch_chunk(symbols: List[str], start: str, end: str, *, timeframe: str, adjustment: str,
                 feed: str, timeout: int, alpaca_bin: str, max_pages: int) -> Dict[str, list]:
    out: Dict[str, list] = {s: [] for s in symbols}
    live = list(symbols)
    page_token: Optional[str] = None
    pages = 0
    drops = 0
    while live:
        args = ['data', 'multi-bars', '--symbols', ','.join(live), '--start', start, '--end', end,
                '--timeframe', timeframe, '--adjustment', adjustment, '--feed', feed,
                '--limit', str(PAGE_LIMIT), '--sort', 'asc']
        if page_token:
            args += ['--page-token', page_token]
        proc = _run(args, timeout, alpaca_bin)
        if proc.returncode != 0:
            ej = _parse_stderr(proc.stderr)
            msg = ej.get('error') or proc.stderr.strip()[:200] or f'exit {proc.returncode}'
            m = _INVALID_SYMBOL_RE.search(msg)
            # Validation rejects happen before any page is served; drop the
            # named symbol (it stays [] in the result) and retry the chunk.
            if m and pages == 0 and drops < MAX_INVALID_DROPS and m.group(1) in live:
                bad = m.group(1)
                logger.warning('multi-bars: skip non-stock symbol %s (%s)', bad, msg)
                live = [s for s in live if s != bad]
                drops += 1
                continue
            raise MultiBarsError(
                f'alpaca data multi-bars ({start}→{end}, {timeframe}, {len(live)} symbols): {msg}',
                status=ej.get('status'), code=ej.get('code'), rc=proc.returncode,
                auth_error=(proc.returncode == 2))
        try:
            payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except ValueError as e:
            raise MultiBarsError(f'alpaca data multi-bars: non-JSON stdout ({e})', rc=proc.returncode)
        for api, rows in (payload.get('bars') or {}).items():
            if api in out and isinstance(rows, list):
                out[api].extend(rows)
        page_token = payload.get('next_page_token') or None
        pages += 1
        if not page_token:
            break
        if pages >= max_pages:
            logger.warning('multi-bars: %s→%s chunk hit %d-page cap, stopping early', start, end, max_pages)
            break
    return out


def fetch_multi_bars(symbols: Iterable[str], start: str, end: str, *,
                     timeframe: str = '1Day', adjustment: str = 'split', feed: str = 'sip',
                     bars_per_day: int = 1, timeout: int = 60, alpaca_bin: Optional[str] = None,
                     max_pages: int = MAX_PAGES) -> Dict[str, list]:
    """Return {api_symbol: [raw bar dicts]} for EVERY requested symbol ([] when
    Alpaca has nothing). Symbols must already be API form (use
    partition_symbols); malformed input raises ValueError before any call.
    Raises MultiBarsError on a non-symbol failure."""
    syms = [str(s) for s in symbols]
    if not syms:
        return {}
    bad = [s for s in syms if not is_alpaca_stock_symbol(s)]
    if bad:
        raise ValueError(f'malformed Alpaca stock symbols (pre-filter with partition_symbols): {bad}')
    alpaca_bin = alpaca_bin or ALPACA_BIN
    size = chunk_size(start, end, bars_per_day)
    out: Dict[str, list] = {}
    for i in range(0, len(syms), size):
        out.update(_fetch_chunk(syms[i:i + size], start, end, timeframe=timeframe, adjustment=adjustment,
                                feed=feed, timeout=timeout, alpaca_bin=alpaca_bin, max_pages=max_pages))
    return out


__all__ = ['MultiBarsError', 'is_alpaca_stock_symbol', 'to_api_symbol', 'partition_symbols',
           'chunk_size', 'fetch_multi_bars', 'ALPACA_BIN']
