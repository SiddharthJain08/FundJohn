#!/bin/bash
# One-shot wrapper for scripts/close_remaining_positions.py.
# Loads only the env keys the script needs (mirrors run_forced_liquidation.sh
# so the SEC_USER_AGENT parse error from `.` /sourcing the full .env can't
# bite us). Designed to be invoked by a transient systemd-run unit; not
# part of any recurring schedule.
set -euo pipefail

ROOT=/root/openclaw
LOG_DIR=$ROOT/logs
mkdir -p "$LOG_DIR"
LOG_FILE=$LOG_DIR/close_remaining_$(date -u +%Y%m%d_%H%M%S).log

# Extract only the keys we need from .env. Pattern-grep keeps quoting safe.
while IFS='=' read -r key val; do
    case "$key" in
        ALPACA_*|POSTGRES_*|REDIS_*|OPENCLAW_ALPACA_LIVE_LIQUIDATE)
            export "$key=$val" ;;
    esac
done < <(grep -E '^(ALPACA_|POSTGRES_|REDIS_|OPENCLAW_ALPACA_LIVE_LIQUIDATE=)' "$ROOT/.env")

# The script's CLI invocation needs ALPACA_API_SECRET (Alpaca CLI uses
# either *_API_SECRET or *_SECRET_KEY; align them both).
export ALPACA_API_SECRET="${ALPACA_API_SECRET:-${ALPACA_SECRET_KEY:-}}"

cd "$ROOT"
exec /usr/bin/python3 scripts/close_remaining_positions.py >>"$LOG_FILE" 2>&1
