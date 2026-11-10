"""Hold-flat reader shared by the crypto redeploy sizer and the crypto executor.

Reads the state written by the crypto breaker (src/execution/crypto_breaker.py)
without importing it. Key names are DUPLICATED from the breaker (HOLD_KEY /
the `crypto_breaker:hold_ticker:{ticker}` f-string / mirror key); keep in sync.
Fail-open: any error yields the empty state; never raises.
Env OPENCLAW_CRYPTO_HOLD_DISABLE=1 forces the empty state (tests)."""
from __future__ import annotations

import json
import os

HOLD_KEY = 'crypto_breaker:hold_flat'
HOLD_TICKER_PREFIX = 'crypto_breaker:hold_ticker:'
MIRROR_KEY = 'crypto_breaker_hold_flat'


def _empty() -> dict:
    return {'sleeve': None, 'tickers': {}}


def _default_redis():
    try:
        import redis
        return redis.Redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379'),
                                    decode_responses=True, socket_connect_timeout=2)
    except Exception:  # noqa: BLE001
        return None


def _default_conn():
    try:
        import psycopg2
        return psycopg2.connect(os.environ['POSTGRES_URI'])
    except Exception:  # noqa: BLE001
        return None


def _loads(raw):
    try:
        v = json.loads(raw) if raw else None
        return v if isinstance(v, dict) and v else None
    except (TypeError, ValueError):
        return None


def _from_mirror(conn) -> dict | None:
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM pipeline_config WHERE key = %s", (MIRROR_KEY,))
            row = cur.fetchone()
        raw = row[0] if row else None
        return (raw or None) if isinstance(raw, dict) else _loads(raw)
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def hold_state(conn=None, redis=None) -> dict:
    """{'sleeve': dict|None, 'tickers': {ticker: dict}}; Redis first, then the
    pipeline_config mirror (sleeve only) when Redis cannot be asked."""
    try:
        if os.environ.get('OPENCLAW_CRYPTO_HOLD_DISABLE') == '1':
            return _empty()
        r = redis if redis is not None else _default_redis()
        if r is not None:
            try:
                sleeve = _loads(r.get(HOLD_KEY))
                tickers = {}
                for k in r.scan_iter(match=HOLD_TICKER_PREFIX + '*'):
                    k = k.decode() if isinstance(k, bytes) else k
                    tk = k[len(HOLD_TICKER_PREFIX):]
                    v = _loads(r.get(k))
                    if tk and v is not None:
                        tickers[tk] = v
                return {'sleeve': sleeve, 'tickers': tickers}
            except Exception:  # noqa: BLE001 - Redis unavailable -> mirror
                pass
        own = conn is None
        c = conn if conn is not None else _default_conn()
        try:
            return {'sleeve': _from_mirror(c), 'tickers': {}}
        finally:
            if own and c is not None:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        return _empty()


def is_held(ticker: str, state: dict | None) -> bool:
    state = state or {}
    return bool(state.get('sleeve')) or (ticker or '') in (state.get('tickers') or {})
