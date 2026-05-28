# src/pipeline/premarket_helpers.py
"""Small shared helpers for the pre-market scanner.

  * resolve_premarket_webhook  -- agent_registry lookup with fallback
  * is_trading_day_in_et       -- alpaca-clock-based "is today a trading day"
  * load_open_equity_positions -- alpaca position list, filtered to us_equity
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.execution.regime_liquidator import _run_cli
from src.execution.pipeline_orchestrator import _load_channel_webhooks


_ET = ZoneInfo('America/New_York')


def resolve_premarket_webhook() -> str | None:
    name = os.environ.get('OPENCLAW_PREMARKET_DISCORD_WEBHOOK_NAME', 'premarket-watch')
    hooks = _load_channel_webhooks()
    return hooks.get(name) or hooks.get('trade-reports')


def is_trading_day_in_et() -> bool:
    ok, payload, _err = _run_cli(['clock'], timeout=10)
    if not ok or not isinstance(payload, dict):
        return False

    next_open_str = payload.get('next_open')
    if not next_open_str:
        return False

    try:
        next_open = datetime.fromisoformat(next_open_str.replace('Z', '+00:00'))
    except ValueError:
        return False

    # Use broker's clock timestamp as "now" so comparisons are deterministic
    # against the broker's view of time; fall back to wall-clock if absent.
    now_str = payload.get('timestamp')
    if now_str:
        try:
            today_et = datetime.fromisoformat(now_str.replace('Z', '+00:00')).astimezone(_ET).date()
        except ValueError:
            today_et = datetime.now(_ET).date()
    else:
        today_et = datetime.now(_ET).date()

    return next_open.astimezone(_ET).date() == today_et


def load_open_equity_positions() -> list[dict]:
    ok, payload, _err = _run_cli(['position', 'list'], timeout=15)
    if not ok or not isinstance(payload, list):
        return []
    out: list[dict] = []
    for raw in payload:
        if raw.get('asset_class') != 'us_equity':
            continue
        try:
            out.append({
                'symbol': raw['symbol'],
                'qty': float(raw['qty']),
                'avg_entry_price': float(raw.get('avg_entry_price', 0) or 0),
                'market_value': float(raw.get('market_value', 0) or 0),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out
