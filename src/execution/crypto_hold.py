"""Hold-flat reader shared by the crypto redeploy sizer and the crypto executor.

Reads the state written by the crypto breaker (src/execution/crypto_breaker.py)
without importing it. Key names are DUPLICATED from the breaker (HOLD_KEY /
the `crypto_breaker:hold_ticker:{ticker}` f-string / mirror key and the
mirror's {sleeve, tickers} shape -- see crypto_breaker.parse_mirror); keep in sync.
Fail-open: any error yields the empty state; never raises.
Env OPENCLAW_CRYPTO_HOLD_DISABLE=1 forces the empty state, honoured ONLY under pytest."""
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
                                    decode_responses=True, socket_connect_timeout=2,
                                    socket_timeout=2)
    except Exception:  # noqa: BLE001
        return None


def _default_conn():
    try:
        import psycopg2
        return psycopg2.connect(os.environ['POSTGRES_URI'], connect_timeout=3)
    except Exception:  # noqa: BLE001
        return None


def _loads(raw):
    try:
        v = json.loads(raw) if raw else None
        return v if isinstance(v, dict) and v else None
    except (TypeError, ValueError):
        return None


def _parse_mirror(raw) -> dict:
    """{sleeve, tickers} mirror shape, or the legacy bare sleeve hold."""
    v = raw if isinstance(raw, dict) else _loads(raw)
    if not v:
        return _empty()
    if 'sleeve' in v or 'tickers' in v:
        sl, tk = v.get('sleeve'), v.get('tickers')
        return {'sleeve': sl if isinstance(sl, dict) and sl else None,
                'tickers': {k: x for k, x in tk.items() if isinstance(x, dict)}
                if isinstance(tk, dict) else {}}
    return {'sleeve': v if v.get('since') else None, 'tickers': {}}


def _from_mirror(conn) -> dict:
    if conn is None:
        return _empty()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM pipeline_config WHERE key = %s", (MIRROR_KEY,))
            row = cur.fetchone()
        return _parse_mirror(row[0] if row else None)
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return _empty()


def hold_state(conn=None, redis=None) -> dict:
    """{'sleeve': dict|None, 'tickers': {ticker: dict}}; Redis first, then the
    pipeline_config mirror (sleeve + tickers) when Redis cannot be asked."""
    try:
        if os.environ.get('OPENCLAW_CRYPTO_HOLD_DISABLE') == '1':
            if 'PYTEST_CURRENT_TEST' in os.environ:
                return _empty()
            print('[crypto_hold] OPENCLAW_CRYPTO_HOLD_DISABLE ignored outside pytest')
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
            return _from_mirror(c)
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
