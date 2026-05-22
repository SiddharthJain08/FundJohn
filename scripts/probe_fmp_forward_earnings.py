#!/usr/bin/env python3
"""SP-1 one-shot probe: does FMP Starter support per-ticker forward earnings?

Outcome determines whether `cboe_vol_indices.get_forward_earnings_calendar()`
is wired into the pipeline or not. If the probe SUCCEEDS, route forward
earnings to FMP per-ticker. If it FAILS (403 / not_authorized), expand the
lint allowlist usage so `ingest_earnings_calendar.py` uses
`cboe_vol_indices.get_forward_earnings_calendar()`.

# Outcome (run 2026-05-22 on FMP Starter):
#   earning_calendar              HTTP 403  FAIL (Legacy endpoint — not available post-2025-08-31)
#   earnings-calendar-confirmed   HTTP 403  FAIL (Legacy endpoint — not available post-2025-08-31)
#   earning-calendar-confirmed    HTTP 403  FAIL (Legacy endpoint — not available post-2025-08-31)
#   earnings/AAPL                 HTTP 403  FAIL (Legacy endpoint — not available post-2025-08-31)
#   historical/earning_calendar   HTTP 403  FAIL (Legacy endpoint — not available post-2025-08-31)
#   Decision: yfinance via cboe_vol_indices.get_forward_earnings_calendar()
#
# All five FMP v3/v4 earnings endpoints are restricted to legacy subscribers
# (pre-2025-08-31). Current Starter tier cannot access any of them.
# ingest_earnings_calendar.py already routes to cboe_vol_indices — no change needed.
# Re-run this probe if the FMP subscription tier changes.
"""
import os
import sys
import requests

API_KEY = os.environ.get('FMP_API_KEY')
if not API_KEY:
    sys.exit('FMP_API_KEY not set')

# Try the candidate endpoints in order; report which works.
CANDIDATES = [
    f'https://financialmodelingprep.com/api/v3/earning_calendar?symbol=AAPL&apikey={API_KEY}',
    f'https://financialmodelingprep.com/api/v3/earnings-calendar-confirmed?symbol=AAPL&apikey={API_KEY}',
    f'https://financialmodelingprep.com/api/v4/earning-calendar-confirmed?symbol=AAPL&apikey={API_KEY}',
    f'https://financialmodelingprep.com/api/v3/earnings/AAPL?apikey={API_KEY}',
    f'https://financialmodelingprep.com/api/v3/historical/earning_calendar/AAPL?apikey={API_KEY}',
]

print('FMP forward-earnings probe — Starter tier')
print('=' * 60)
for url in CANDIDATES:
    short = url.split('?')[0].rsplit('/', 1)[-1]
    try:
        r = requests.get(url, timeout=10)
        body_preview = (r.text or '')[:200].replace('\n', ' ')
        is_error_payload = 'Error Message' in r.text or '"error"' in r.text.lower()
        status = 'WORKS' if r.status_code == 200 and not is_error_payload else 'FAIL'
        print(f'  {short:30s} HTTP {r.status_code}  {status}')
        print(f'    body: {body_preview}')
    except Exception as e:
        print(f'  {short:30s} EXCEPTION: {e}')

print()
print('Decision matrix:')
print('  If any "WORKS" endpoint above → wire FMP per-ticker forward earnings.')
print('  If all "FAIL" → use cboe_vol_indices.get_forward_earnings_calendar() (yfinance).')
