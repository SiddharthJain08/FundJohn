"""SP-3.1 Phase A live paper smoke: one ~$20 BTC buy via execute_single, then
a full close. Prints the result dicts. Paper account only. Not wired into the
daily cycle. Run manually after operator OK, with Alpaca paper creds in env:
    set -a; . <(grep -E '^ALPACA_(API_KEY|SECRET_KEY|BASE_URL)=' /root/openclaw/.env); set +a
    export APCA_API_BASE_URL="$ALPACA_BASE_URL"
    python3 scripts/smoke_crypto_execution.py

NOTE: Alpaca's crypto minimum order is $10 cost basis (Task 0 snapshot), so the
buy must be >= $10; EQUITY=20 with pct_nav=1.0 sizes a ~$20 order."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from execution import alpaca_executor as ae

EQUITY = 20.0  # pct_nav=1.0 -> ~$20 notional (above the $10 crypto minimum)

buy = {'ticker': 'BTC-USD', 'direction': 'long', 'pct_nav': 1.0,
       'strategy_id': 'SMOKE_crypto'}
print('BUY  ->', ae.execute_single(sess=None, equity=EQUITY, order=buy, run_date='2026-05-26'))

# Full close: notional_usd == current_usd takes the no-percentage full-close
# branch, flattening whatever the buy filled.
close = {'ticker': 'BTC-USD', 'close_only': True, 'notional_usd': 20.0,
         'current_usd': 20.0, 'strategy_id': 'SMOKE_crypto'}
print('CLOSE ->', ae.execute_single(sess=None, equity=EQUITY, order=close, run_date='2026-05-26'))
