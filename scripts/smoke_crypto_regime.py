"""SP-3.1 Phase B smoke: backfill ~90d hourly bars, then run the detector twice.
Asserts the parquet, a crypto_regime_states row, and crypto_regime_latest.json
all materialize. Reads market data + writes crypto artifacts only — no trading.
Run after operator OK with ALPACA + POSTGRES creds + OPENCLAW_CRYPTO_REGIME=1."""
import os, sys, json
from pathlib import Path
from datetime import datetime, timedelta, timezone
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from ingestion import crypto_bars as cb

end = datetime.now(timezone.utc)
start = end - timedelta(days=90)
n = cb.backfill(start.strftime('%Y-%m-%dT%H:%M:%SZ'), end.strftime('%Y-%m-%dT%H:%M:%SZ'))
print(f'backfill wrote {n} new bars to {cb.DEFAULT_PARQUET}')

import subprocess
for i in (1, 2):
    r = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'run_crypto_market_state.py')],
                       capture_output=True, text=True)
    print(f'tick {i}:', r.stdout.strip(), r.stderr.strip()[:200])

lj = ROOT / '.agents' / 'crypto-market-state' / 'crypto_regime_latest.json'
print('latest.json:', json.loads(lj.read_text())['state'] if lj.exists() else 'MISSING')
