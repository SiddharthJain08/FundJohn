"""Thin wrapper once called by cron-schedule.js (Sunday 08:00 ET) to sync the
full ticker universe from FMP into universe_config.

RETIRED 2026-08-23: FMP /api/v3/available-traded/list is 403 "Legacy
Endpoint" for this key, so every weekly run printed
``added=0 deactivated=0 total=0`` (journal 08-16). The universe is owned by
the src/universe/ resolver over alpaca_tradable_universe, which
src/maintenance/refresh_tradable_universe.py refreshes from `alpaca asset
list` (openclaw-tradable-universe-refresh.timer). The cron entry is gone;
this wrapper stays so any stray invocation exits 0 with a clear message and
never touches universe_config.
"""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.ingestion.pipeline import sync_universe_to_db

result = asyncio.run(sync_universe_to_db())
print(f"added={result['added']} deactivated={result['deactivated']} total={result['total']} "
      f"retired={result.get('retired', False)} — FMP universe sync is RETIRED; "
      f"universe authority = resolver + alpaca_tradable_universe")
if 'error' in result:
    print(f"ERROR: {result['error']}", file=sys.stderr)
    sys.exit(1)
