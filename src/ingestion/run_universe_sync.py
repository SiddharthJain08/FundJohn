"""Thin wrapper called by cron-schedule.js to sync the full ticker universe."""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.ingestion.pipeline import sync_universe_to_db

result = asyncio.run(sync_universe_to_db())
print(f"added={result['added']} deactivated={result['deactivated']} total={result['total']}")
if 'error' in result:
    print(f"ERROR: {result['error']}", file=sys.stderr)
    sys.exit(1)
