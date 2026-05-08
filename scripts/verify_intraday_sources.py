#!/usr/bin/env python3
"""verify_intraday_sources.py — read-only Stage-0 probe for Path C.

Confirms the data sources the intraday HMM detector will rely on are
reachable + ship the fields we need. Prints a JSON summary to stdout
and exits 0 (always — this is informational; the operator decides what
to do with the result).

Probes:
  1. Alpaca CLI `data option chain --underlying-symbol SPY` ships
     bid/ask + delta + impliedVolatility for near-ATM strikes.
  2. Polygon historical OPRA aggregates for one known 2024 contract.
  3. yfinance intraday VIX bars (fallback historical training source).

Usage:
    POLYGON_API_KEY=... ALPACA_API_KEY=... ALPACA_SECRET_KEY=... \\
        python3 scripts/verify_intraday_sources.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date

ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')


def probe_alpaca_chain() -> dict:
    """Pull a 5-strike near-ATM SPY chain bracket and check which fields
    Alpaca ships. Returns a structured summary."""
    if not os.environ.get('ALPACA_API_KEY'):
        return {'ok': False, 'reason': 'ALPACA_API_KEY not set'}
    expiry = '2026-06-12'   # arbitrary live-listed expiry
    proc = subprocess.run(
        [ALPACA_CLI, 'data', 'option', 'chain',
         '--underlying-symbol', 'SPY',
         '--expiration-date-gte', expiry,
         '--expiration-date-lte', expiry,
         '--strike-price-gte', '575',
         '--strike-price-lte', '585',
         '--limit', '10'],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return {'ok': False, 'reason': 'CLI error',
                'stderr': proc.stderr[:300]}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return {'ok': False, 'reason': f'JSON decode failed: {e}'}
    snaps = data.get('snapshots', {})
    fields_present = {'bid': 0, 'ask': 0, 'delta': 0, 'iv': 0,
                      'volume': 0, 'open_interest': 0}
    for sym, snap in snaps.items():
        q = snap.get('latestQuote') or {}
        g = snap.get('greeks') or {}
        if q.get('bp', 0) > 0:                     fields_present['bid']           += 1
        if q.get('ap', 0) > 0:                     fields_present['ask']           += 1
        if g.get('delta', 0):                      fields_present['delta']         += 1
        if snap.get('impliedVolatility') is not None: fields_present['iv']         += 1
    return {'ok': True, 'contracts_returned': len(snaps),
            'fields_populated': fields_present, 'expiry_probed': expiry}


def probe_polygon_opra_historical() -> dict:
    """Single Polygon aggs call on a known 2024 contract. Confirms
    whether the current tier serves OPRA 1-min bars going back. If
    denied, the plan is to use yfinance intraday VIX for historical
    training + accumulate live data forward."""
    api_key = os.environ.get('POLYGON_API_KEY')
    if not api_key:
        return {'ok': False, 'reason': 'POLYGON_API_KEY not set'}
    import urllib.request as _ur
    import urllib.error as _ue
    contract = 'O:SPY240621C00540000'
    url = (f'https://api.polygon.io/v3/aggs/ticker/{contract}'
           f'/range/1/minute/2024-06-03/2024-06-03'
           f'?adjusted=true&sort=asc&limit=5&apiKey={api_key}')
    try:
        with _ur.urlopen(url, timeout=10) as r:
            payload = r.read().decode()
    except _ue.HTTPError as e:
        return {'ok': False, 'http_status': e.code,
                'reason': f'HTTP {e.code}', 'contract': contract}
    except Exception as e:
        return {'ok': False, 'reason': f'request error: {e}'}
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return {'ok': False, 'reason': 'non-JSON response',
                'raw_head': payload[:200]}
    return {'ok': True, 'status': data.get('status'),
            'results_count': data.get('resultsCount'),
            'contract': contract}


def probe_yfinance_intraday_vix() -> dict:
    """yfinance intraday VIX as historical training fallback."""
    try:
        import yfinance as yf
    except ImportError:
        return {'ok': False, 'reason': 'yfinance not installed'}
    try:
        df = yf.Ticker('^VIX').history(period='5d', interval='5m', auto_adjust=False)
    except Exception as e:
        return {'ok': False, 'reason': f'fetch error: {e}'}
    if df.empty:
        return {'ok': False, 'reason': 'empty result'}
    return {'ok': True, 'rows': int(len(df)),
            'first_ts': str(df.index[0]),
            'last_ts': str(df.index[-1]),
            'last_vix': float(df['Close'].iloc[-1])}


def main():
    summary = {
        'date': date.today().isoformat(),
        'alpaca_option_chain': probe_alpaca_chain(),
        'polygon_opra_historical': probe_polygon_opra_historical(),
        'yfinance_intraday_vix': probe_yfinance_intraday_vix(),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
