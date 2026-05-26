"""SP-3.1 Phase C: dedicated crypto-only redeploy. Gated on OPENCLAW_CRYPTO_REDEPLOY.
Sizes crypto positions (crypto_redeploy_sizer), submits via the executor's crypto path,
NEVER touches equity. No RTH gate (crypto 24/7). Own cooldown namespace redeploy:crypto:*."""
from __future__ import annotations
import argparse, os, sys
from datetime import date as _date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

COOLDOWN_TTL_S = 60 * 60
SENTINEL_TTL_S = 24 * 60 * 60


def _cooldown_key(run_date: str) -> str:
    return f'redeploy:crypto:cooldown:{run_date}'


def _sentinel_key(run_date: str, reason: str) -> str:
    return f'redeploy:crypto:fired:{run_date}:{reason}'


def _redis():
    """Return a Redis client or None on failure. Fail-open: missing redis is not fatal."""
    try:
        import redis as _r
        url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
        client = _r.from_url(url, socket_connect_timeout=3, decode_responses=True)
        client.ping()
        return client
    except Exception as e:
        print(f'[crypto-redeploy] redis unavailable ({e}) — proceeding without cooldown')
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--reason', required=True)
    ap.add_argument('--date', default=str(_date.today()))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)

    if os.environ.get('OPENCLAW_CRYPTO_REDEPLOY') != '1':
        print('[crypto-redeploy] gate OFF (OPENCLAW_CRYPTO_REDEPLOY != 1) — no-op')
        return 0

    r = _redis()
    if r:
        try:
            if r.get(_cooldown_key(args.date)):
                print('[crypto-redeploy] blocked: cooldown active'); return 0
            if r.get(_sentinel_key(args.date, args.reason[:80])):
                print('[crypto-redeploy] blocked: already fired this reason'); return 0
        except Exception as e:
            print(f'[crypto-redeploy] redis gate check failed ({e}) — proceeding')

    from execution.crypto_redeploy_sizer import size_crypto_positions
    from regime.crypto_regime import load_crypto_regime_state
    account_state = _load_account_state()
    crypto_regime = load_crypto_regime_state()
    signals_loader = _make_crypto_signals_loader()
    orders = size_crypto_positions(account_state, crypto_regime, signals_loader=signals_loader)
    if not orders:
        print('[crypto-redeploy] no crypto orders (empty signals or flat) — no-op'); return 0

    if args.dry_run:
        print(f'[crypto-redeploy] DRY-RUN — would submit {len(orders)} crypto orders: '
              f'{[o["ticker"] for o in orders]}'); return 0

    from execution import alpaca_executor as ae
    submitted = 0
    for o in orders:
        res = ae.execute_single(sess=None, equity=account_state.get('equity', 0.0),
                                order=o, run_date=args.date)
        if (res or {}).get('status') == 'submitted':
            submitted += 1
        print(f'[crypto-redeploy] {o["ticker"]} -> {(res or {}).get("status")}')

    if r:
        try:
            r.set(_cooldown_key(args.date), '1', ex=COOLDOWN_TTL_S)
            r.set(_sentinel_key(args.date, args.reason[:80]), '1', ex=SENTINEL_TTL_S)
        except Exception as e:
            print(f'[crypto-redeploy] failed to set cooldown/sentinel keys: {e}')
    print(f'[crypto-redeploy] submitted {submitted}/{len(orders)} crypto orders')
    return 0


def _load_account_state() -> dict:
    """NAV/equity from the Alpaca account (CLI)."""
    import subprocess, json
    try:
        proc = subprocess.run(['/root/go/bin/alpaca', 'account', 'get'],
                              capture_output=True, text=True, timeout=15, check=False)
        if proc.returncode == 0:
            acct = json.loads(proc.stdout)
            return {'equity': float(acct.get('equity') or 0.0)}
    except Exception:
        pass
    return {'equity': 0.0}


def _make_crypto_signals_loader():
    """Return signals_loader(regime, weight_by_strat) reading today's open crypto
    signals from execution_signals for the given crypto strategies."""
    def _load(regime, weight_by_strat):
        import psycopg2, psycopg2.extras
        uri = os.environ.get('POSTGRES_URI', '')
        if not uri or not weight_by_strat:
            return []
        sids = tuple(weight_by_strat.keys())
        conn = psycopg2.connect(uri)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT DISTINCT ON (strategy_id, ticker) strategy_id, ticker, direction "
                    "FROM execution_signals WHERE strategy_id IN %s AND status = 'open' "
                    "ORDER BY strategy_id, ticker, created_at DESC", (sids,))
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    return _load


if __name__ == '__main__':
    sys.exit(main())
