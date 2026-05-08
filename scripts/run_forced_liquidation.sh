#!/bin/bash
# One-shot wrapper for scripts/run_forced_liquidation invocations.
# Loads only the env vars the liquidator needs (avoids the SEC_USER_AGENT
# parse error from sourcing the full .env), then runs the liquidator with
# --force. Designed to be invoked by a one-time systemd-run unit; not part
# of any recurring schedule.
set -euo pipefail

ROOT=/root/openclaw
LOG_DIR=$ROOT/logs
mkdir -p "$LOG_DIR"
LOG_FILE=$LOG_DIR/regime_liquidator_forced_$(date -u +%Y%m%d_%H%M%S).log

# Extract just the keys we need from .env. grep-pattern keeps quoting safe.
while IFS='=' read -r key val; do
    case "$key" in
        ALPACA_*|POSTGRES_*|REDIS_*|OPENCLAW_ALPACA_LIVE_LIQUIDATE)
            export "$key=$val" ;;
    esac
done < <(grep -E '^(ALPACA_|POSTGRES_|REDIS_|OPENCLAW_ALPACA_LIVE_LIQUIDATE=)' "$ROOT/.env")

cd "$ROOT"
exec /usr/bin/python3 src/execution/regime_liquidator.py --force >>"$LOG_FILE" 2>&1
