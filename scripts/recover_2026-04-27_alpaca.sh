#!/bin/bash
# One-shot recovery script for 2026-04-27's sized handoff.
#
# 2026-04-27's daily cycle produced a clean sized handoff at
# output/handoffs/2026-04-27_sized.json (94 orders, deterministic sizer)
# but couldn't submit to Alpaca that afternoon — the OPG window was
# closed (16:30 ET) and the regular bracket window had already passed.
#
# This script is invoked by a one-shot systemd transient timer at
# 2026-04-28 13:30 UTC (9:30 AM ET, market open) so the orders submit
# under tif='day' + bracket order class — full automatic stop/target
# coverage.
#
# IMPORTANT: this script is one-shot. The systemd-run --on-calendar
# transient timer self-cleans after firing. Do NOT add this to cron.
# The regular daily cycle at 14:00 UTC handles 2026-04-28's own signals
# 30 minutes after this script runs.
set -euo pipefail
exec >> /root/openclaw/logs/alpaca_recover_2026-04-27.log 2>&1

cd /root/openclaw
echo
echo "==== $(date -u) — recovery run start ===="

# Load .env via python-dotenv (handles unquoted parens that break bash sourcing)
exec python3 - <<'PY'
import os
from pathlib import Path
ROOT = Path('/root/openclaw')
for line in (ROOT / '.env').read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    k, v = line.split('=', 1)
    k, v = k.strip(), v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        v = v[1:-1]
    os.environ.setdefault(k, v)

# Sanity-check the env so a silent miss doesn't masquerade as an Alpaca 401.
for k in ('ALPACA_API_KEY', 'ALPACA_SECRET_KEY', 'ALPACA_BASE_URL', 'POSTGRES_URI'):
    assert os.environ.get(k), f'env missing: {k}'

import sys
sys.argv = ['alpaca_executor.py', '--date', '2026-04-27']
script = str(ROOT / 'src' / 'execution' / 'alpaca_executor.py')
exec(compile(open(script).read(), script, 'exec'),
     {'__name__': '__main__', '__file__': script})
PY
