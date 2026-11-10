"""Crypto drawdown circuit breaker (Phase 2) -- pure core.

Position parsing, drawdown rules, mode, exec-window and Redis-backed
cooldown / hold-flat state. Actions + orchestration are added below by later
tasks. Fail-open everywhere: a missing Redis never raises.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _env_threshold(name: str, default: float, *, must_be_negative: bool = True) -> float:
    """Finite env float with the required sign, else the default (never a
    positive DD threshold: that would flatten every tick)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        v = float(raw)
        if math.isfinite(v) and (v < 0 if must_be_negative else v > 0):
            return v
    except ValueError:
        pass
    print(f'[crypto_breaker] ignoring invalid {name}={raw}; using {default}')
    return default


SLEEVE_DD = _env_threshold('OPENCLAW_CRYPTO_BREAKER_SLEEVE_DD', -0.08)
POSITION_DD = _env_threshold('OPENCLAW_CRYPTO_BREAKER_POSITION_DD', -0.15)
BLACKOUT_H = _env_threshold('OPENCLAW_CRYPTO_BREAKER_BLACKOUT_H', 3.0, must_be_negative=False)
COOLDOWN_TTL_S = 6 * 3600
HOLD_FLAT_TTL_S = 24 * 3600
HOLD_TICKER_TTL_S = 7 * 86400
ARM_ENV = 'OPENCLAW_CRYPTO_BREAKER'
TICK_ENV = 'OPENCLAW_CRYPTO_BREAKER_TICK'
REARM_ENV = 'OPENCLAW_CRYPTO_BREAKER_REARM'
HOLD_KEY = 'crypto_breaker:hold_flat'
_ET = ZoneInfo('America/New_York')


@dataclass
class CryptoPos:
    ticker: str
    symbol: str
    qty: float
    avg_entry: float | None
    price: float | None
    market_value: float
    cost_basis: float
    unrealized_pl: float
    unrealized_plpc: float | None


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def parse_positions(payload: list[dict]) -> tuple[list[CryptoPos], dict]:
    from ingestion.crypto_flow.universe import normalize_broker_symbol
    out: list[CryptoPos] = []
    c = {'equity_rows': 0, 'crypto_rows': 0, 'unparsed': 0, 'unmapped': 0,
         'zero_qty': 0, 'normaliser_errors': 0}
    for p in payload or []:
        if not isinstance(p, dict):
            continue
        sym = str(p.get('symbol') or '')
        try:
            tkr = normalize_broker_symbol(sym)
        except Exception as e:  # noqa: BLE001 - universe file missing/corrupt
            logger.warning('[crypto_breaker] normaliser failed for %s: %s', sym, e)
            tkr = None
            c['normaliser_errors'] += 1
        is_crypto = p.get('asset_class') == 'crypto'
        if not is_crypto and tkr is None:
            c['equity_rows'] += 1
            continue
        if tkr is None:
            tkr = sym.replace('/', '-')
            c['unmapped'] += 1
        qty, mv = _f(p.get('qty')), _f(p.get('market_value'))
        if qty is None or mv is None:
            c['unparsed'] += 1
            continue
        if qty <= 0:
            c['zero_qty'] += 1
            continue
        avg = _f(p.get('avg_entry_price'))
        cost = _f(p.get('cost_basis'))
        if cost is None:
            cost = qty * avg if avg is not None else 0.0
        pl = _f(p.get('unrealized_pl'))
        pl = mv - cost if pl is None else pl
        plpc = _f(p.get('unrealized_plpc'))
        if plpc is None:
            plpc = pl / cost if cost > 0 else None
        out.append(CryptoPos(tkr, sym, qty, avg, _f(p.get('current_price')),
                             mv, cost, pl, plpc))
        c['crypto_rows'] += 1
    return out, c


def load_crypto_positions(run_cli=None) -> tuple[list[CryptoPos] | None, dict]:
    if run_cli is None:
        from execution.regime_liquidator import _run_cli as run_cli
    try:
        ok, payload, err = run_cli(['position', 'list'], timeout=15)
    except Exception as e:  # noqa: BLE001 - risk control must not crash
        logger.warning('[crypto_breaker] position list raised: %s', e)
        return None, {}
    if not ok or not isinstance(payload, list):
        logger.warning('[crypto_breaker] position list failed: %s', (err or {}).get('error'))
        return None, {}
    try:
        return parse_positions(payload)
    except Exception as e:  # noqa: BLE001
        logger.warning('[crypto_breaker] position parse failed: %s', e)
        return None, {'error': str(e)}


def sleeve_drawdown(pos: list[CryptoPos]) -> tuple[float | None, float, float]:
    cost = sum(p.cost_basis for p in pos)
    pl = sum(p.unrealized_pl for p in pos)
    return (pl / cost if cost > 0 else None), cost, pl


def _age_h(ts, now):
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 3600.0


def evaluate(pos, *, now, regime_ts, flow_ts, thr=None) -> dict:
    t = {'sleeve_dd': SLEEVE_DD, 'position_dd': POSITION_DD, 'blackout_h': BLACKOUT_H}
    t.update(thr or {})
    dd, cost, pl = sleeve_drawdown(pos)
    triggers = []
    if dd is not None and dd <= t['sleeve_dd']:
        triggers.append({'id': 'T4a', 'reason': 'sleeve_drawdown'})
    else:
        for p in pos:
            if p.unrealized_plpc is not None and p.unrealized_plpc <= t['position_dd']:
                triggers.append({'id': 'T4b', 'reason': f'position_drawdown:{p.ticker}',
                                 'ticker': p.ticker})
    ages = {'regime': _age_h(regime_ts, now), 'flow': _age_h(flow_ts, now)}
    blackout = all(a is None or a > t['blackout_h'] for a in ages.values())
    if blackout:
        triggers.append({'id': 'T5', 'reason': 'data_blackout', 'ages_h': ages})
    ranked = [p for p in pos if p.unrealized_plpc is not None]
    worst = min(ranked, key=lambda p: p.unrealized_plpc) if ranked else None
    return {'sleeve_dd': dd, 'sleeve_cost': cost, 'sleeve_pl': pl,
            'worst_ticker': worst.ticker if worst else None,
            'worst_plpc': worst.unrealized_plpc if worst else None,
            'triggers': triggers, 'blackout': blackout}


def mode(env=os.environ, dry_run=False) -> str:
    if dry_run:
        return 'dry_run'
    return 'armed' if (env.get(ARM_ENV) or '').strip() == '1' else 'shadow'


def in_exec_window(now_utc) -> bool:
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    et = now_utc.astimezone(_ET)
    if et.weekday() >= 5:
        return False
    return (15, 55) <= (et.hour, et.minute) < (16, 5)


def _redis():
    try:
        import redis
        r = redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379'),
                           socket_connect_timeout=3, decode_responses=True)
        r.ping()
        return r
    except Exception as e:  # noqa: BLE001
        logger.warning('[crypto_breaker] redis unavailable: %s', e)
        return None


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - fail open
        logger.warning('[crypto_breaker] redis op failed: %s', e)
        return default


def _cd_key(reason, now):
    return f'crypto_breaker:fired:{now.astimezone(timezone.utc):%Y-%m-%d}:{reason}'


def cooldown_active(r, reason, now) -> bool:
    return False if r is None else bool(_safe(lambda: r.get(_cd_key(reason, now))))


def set_cooldown(r, reason, now) -> None:
    if r is not None:
        _safe(lambda: r.set(_cd_key(reason, now), now.isoformat(), ex=COOLDOWN_TTL_S))


def _get_json(r, key):
    raw = _safe(lambda: r.get(key)) if r is not None else None
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def hold_flat_get(r) -> dict | None:
    return _get_json(r, HOLD_KEY)


def hold_flat_set(r, reason, now) -> dict:
    hold = {'since': now.astimezone(timezone.utc).isoformat(), 'reason': reason,
            'scope': 'sleeve'}
    if r is not None:
        _safe(lambda: r.set(HOLD_KEY, json.dumps(hold), ex=HOLD_FLAT_TTL_S))
    return hold


def hold_ticker_set(r, ticker, reason, now):
    hold = {'since': now.astimezone(timezone.utc).isoformat(), 'reason': reason,
            'ticker': ticker}
    if r is not None:
        _safe(lambda: r.set(f'crypto_breaker:hold_ticker:{ticker}', json.dumps(hold),
                            ex=HOLD_TICKER_TTL_S))
    return hold


def hold_ticker_get(r, ticker) -> dict | None:
    return _get_json(r, f'crypto_breaker:hold_ticker:{ticker}')


def _iso_variants(dt) -> set:
    """Copy of account_breaker._iso_variants (that module drags in
    alpaca_trader/psycopg2 at import)."""
    if dt is None:
        return set()
    if getattr(dt, 'tzinfo', None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return {dt.isoformat(), dt.replace(microsecond=0).isoformat(),
            dt.isoformat().replace('+00:00', 'Z'),
            dt.replace(microsecond=0).isoformat().replace('+00:00', 'Z')}


def rearm_requested(env, hold: dict | None) -> bool:
    token = (env.get(REARM_ENV) or '').strip()
    if not token or not hold or not hold.get('since'):
        return False
    try:
        since = datetime.fromisoformat(str(hold['since']).replace('Z', '+00:00'))
    except ValueError:
        return False
    return token in _iso_variants(since)


def clear_hold(r) -> None:
    if r is not None:
        _safe(lambda: r.delete(HOLD_KEY))


def mirror_hold_to_pipeline_config(conn, hold: dict | None) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pipeline_config (key, value, description, updated_at) "
                "VALUES (%s,%s,%s,NOW()) ON CONFLICT (key) DO UPDATE "
                "SET value=EXCLUDED.value, updated_at=NOW()",
                ('crypto_breaker_hold_flat', json.dumps(hold) if hold else '',
                 'crypto breaker hold-flat mirror (Redis is the source)'))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f'[crypto_breaker] hold mirror failed: {e}')
