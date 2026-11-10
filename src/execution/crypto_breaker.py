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


# --------------------------------------------------------------------------
# Actions: close / flatten through the crypto executor path, audit, Discord.
# Every dependency is injectable; defaults import lazily (heavy deps).
# --------------------------------------------------------------------------
CLOSE_STRATEGY_ID = '__crypto_breaker__'


def close_position(pos, *, equity, run_date, reason, executor=None, poller=None, audit=None,
                   poll_timeout_s: float = 90) -> dict:
    """Full-close one crypto position. Never raises; always audits."""
    out = {'ticker': pos.ticker, 'status': 'error', 'order_id': None, 'filled_qty': None,
           'avg_fill_price': None, 'error': None, 'market_value': abs(pos.market_value)}
    status, err = 'submit_error', None
    try:
        if executor is None:
            from execution import alpaca_executor as executor
        if poller is None:
            from execution.regime_liquidator import _poll_to_terminal as poller
        mv = abs(pos.market_value)
        order = {'ticker': pos.ticker, 'direction': 'short' if pos.qty > 0 else 'long',
                 'notional_usd': mv, 'current_usd': mv,
                 'strategy_id': CLOSE_STRATEGY_ID, 'close_only': True}
        res = executor.execute_single(None, equity, order, run_date)
        if isinstance(res, dict) and res.get('status') == 'submitted':
            out['order_id'] = res.get('order_id')
            status = 'pending'  # order is live; a poll/classify failure stays pending
            try:
                final = poller(out['order_id'], timeout_s=poll_timeout_s)
                from execution.regime_liquidator import _classify_outcome
                status = _classify_outcome(True, final, abs(pos.qty))
                if isinstance(final, dict):
                    out['filled_qty'] = _f(final.get('filled_qty'))
                    out['avg_fill_price'] = _f(final.get('filled_avg_price'))
            except Exception as e:  # noqa: BLE001
                err = f'post-submit {type(e).__name__}: {e}'
        else:
            status = 'rejected'
            err = str((res or {}).get('reason') or res) if isinstance(res, dict) or res is None \
                else f'not submitted: {res}'
        out['status'] = status
    except Exception as e:  # noqa: BLE001 - a risk action must never raise
        err = f'{type(e).__name__}: {e}'
        out['status'] = 'error'
    out['error'] = err
    row = {'run_date': run_date, 'regime_from': 'CRYPTO', 'regime_to': 'CRYPTO_BREAKER',
           'ticker': pos.ticker, 'direction': 'long' if pos.qty > 0 else 'short',  # writer maps to <dir>_close
           'qty': abs(pos.qty), 'result_status': status, 'market_value_usd': pos.market_value,
           'filled_qty': out['filled_qty'], 'avg_fill_price': out['avg_fill_price'],
           'asset_class': 'crypto', 'source': 'crypto_breaker',
           'error_msg': f'{reason}: {err}' if err else None}
    try:
        if audit is None:
            from execution.regime_liquidator import _write_liquidation_audit as audit
        audit(row)
    except Exception as e:  # noqa: BLE001
        print(f'[crypto_breaker] audit write failed for {pos.ticker}: {e}')
    return out


def flatten_crypto(pos, *, equity, run_date, reason, deadline_s=None, clock=None, **deps) -> list[dict]:
    """Close every position, largest exposure first, continuing past failures.
    A shared deadline bounds the whole flatten; positions past it are skipped
    (audited) so the 240 s process timeout never cuts a close mid-flight."""
    import time
    clock = clock or time.monotonic
    if deadline_s is None:
        deadline_s = _env_threshold('OPENCLAW_CRYPTO_BREAKER_FLATTEN_DEADLINE_S', 150.0,
                                    must_be_negative=False)
    start, out = clock(), []
    for p in sorted(pos, key=lambda p: abs(p.market_value), reverse=True):
        remaining = deadline_s - (clock() - start)
        if remaining <= 0:
            out.append(_skipped(p, run_date, deps.get('audit')))
            continue
        out.append(close_position(p, equity=equity, run_date=run_date, reason=reason,
                                  poll_timeout_s=min(90, max(10, remaining)), **deps))
    return out


def _skipped(p, run_date, audit):
    row = {'run_date': run_date, 'regime_from': 'CRYPTO', 'regime_to': 'CRYPTO_BREAKER',
           'ticker': p.ticker, 'direction': 'long' if p.qty > 0 else 'short', 'qty': abs(p.qty),
           'result_status': 'skipped', 'market_value_usd': p.market_value,
           'asset_class': 'crypto', 'source': 'crypto_breaker', 'error_msg': 'flatten deadline'}
    try:
        if audit is None:
            from execution.regime_liquidator import _write_liquidation_audit as audit
        audit(row)
    except Exception as e:  # noqa: BLE001
        print(f'[crypto_breaker] audit write failed for {p.ticker}: {e}')
    return {'ticker': p.ticker, 'status': 'skipped', 'order_id': None, 'filled_qty': None,
            'avg_fill_price': None, 'error': 'deadline', 'market_value': abs(p.market_value)}


def post(msg: str, poster=None) -> None:
    try:
        if poster is None:
            from execution import regime_liquidator
            poster = lambda m: regime_liquidator._post_to_discord('trade-reports', m)  # noqa: E731
        poster(msg[:1900])
    except Exception as e:  # noqa: BLE001
        print(f'[crypto_breaker] discord post failed: {e}')


def _usd(v: float) -> str:
    v = abs(v)
    return f'${v / 1e6:.1f}M' if v >= 1e6 else f'${v / 1e3:.1f}K' if v >= 1e3 else f'${v:.0f}'


def fmt_fire_msg(mode, triggers, results, hold) -> str:
    n = lambda *s: sum(r.get('status') in s for r in results)  # noqa: E731
    since = (hold or {}).get('since') or '-'
    return (f"[crypto_breaker] FIRED mode={mode} reason={','.join(t['reason'] for t in triggers)} "
            f"n={n('filled', 'partial')}/{len(results)} "
            f"notional={_usd(sum(r.get('market_value') or 0 for r in results))} "
            f"filled={n('filled')} rejected={n('rejected')} hold_flat={since}")


def fmt_shadow_msg(ev, triggers) -> str:
    dd, w = ev.get('sleeve_dd'), ev.get('worst_plpc')
    return (f"[crypto_breaker] shadow would_fire={','.join(t['reason'] for t in triggers)} "
            f"dd={'n/a' if dd is None else f'{dd:.2%}'} worst={ev.get('worst_ticker')}@"
            f"{'n/a' if w is None else f'{w:.2%}'} n={ev.get('n_positions', '?')}")


def fmt_blackout_msg(ev) -> str:
    ages = ev.get('ages_h') or next((t.get('ages_h') for t in ev.get('triggers', [])
                                     if t.get('id') == 'T5'), None) or {}
    return (f"[crypto_breaker] BLACKOUT regime_age_h={ages.get('regime')} flow_age_h={ages.get('flow')} "
            f"holding={ev.get('n_positions', '?')} — alert only, no orders")


# --------------------------------------------------------------------------
# Orchestration: one tick. Every DB/Redis step is fail-open; a failure there
# never blocks an armed T4 action and never causes an action when not armed.
# --------------------------------------------------------------------------
FAKE_POS_ENV = 'OPENCLAW_CRYPTO_BREAKER_FAKE_POSITIONS'
_EVAL_COLS = ('ts_utc', 'mode', 'n_positions', 'sleeve_cost_basis', 'sleeve_unrealized_pl',
              'sleeve_dd', 'worst_ticker', 'worst_plpc', 'triggers', 'action', 'skipped',
              'hold_flat', 'note')


def write_eval(conn, row: dict) -> None:
    if conn is None:
        print('[crypto_breaker] no DB connection; eval row not written')
        return
    try:
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO crypto_breaker_evals ({', '.join(_EVAL_COLS)}) "
                        f"VALUES ({', '.join(['%s'] * len(_EVAL_COLS))})",
                        tuple(row.get(c) for c in _EVAL_COLS))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f'[crypto_breaker] eval row write failed: {e}')
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass


def _default_conn():
    try:
        import psycopg2
        return psycopg2.connect(os.environ['POSTGRES_URI'])
    except Exception as e:  # noqa: BLE001
        print(f'[crypto_breaker] DB unavailable: {e}')
        return None


def _default_equity() -> float:
    from execution.alpaca_trader import _alpaca_session, _fetch_account_state
    return float(_fetch_account_state(_alpaca_session())['equity'])


def _parse_ts(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        ts = v
    else:
        ts = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _default_regime_ts():
    from regime.crypto_regime import load_crypto_regime_state
    return _parse_ts(load_crypto_regime_state()['regime_data'].get('ts_utc'))


def _flow_ts_from(conn):
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute('SELECT MAX(ts_utc) FROM crypto_flow_snapshot')
        row = cur.fetchone()
    return _parse_ts(row[0]) if row else None


def _try(fn, what, default=None):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - fail open
        print(f'[crypto_breaker] {what} failed: {e}')
        return default


def _load_fake_positions(path):
    try:
        with open(path) as fh:
            payload = json.load(fh)
        if not isinstance(payload, list):
            raise ValueError('fake positions file must hold a JSON list')
        return parse_positions(payload)
    except Exception as e:  # noqa: BLE001
        print(f'[crypto_breaker] fake positions unreadable: {e}')
        return None, {}


def run_once(*, now=None, env=None, dry_run=False, force=False, deps: dict | None = None) -> dict:
    deps = dict(deps or {})
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    fake_path = (env.get(FAKE_POS_ENV) or '').strip()
    if fake_path:
        print(f'[crypto_breaker] FAKE POSITIONS from {fake_path} — dry_run forced')
        dry_run = True
    m = mode(env, dry_run)
    r = deps['redis'] if 'redis' in deps else _redis()
    conn = deps['conn'] if 'conn' in deps else _default_conn()
    poster = deps.get('poster')
    act_deps = {k: deps[k] for k in ('executor', 'poller', 'audit') if k in deps}
    res = {'mode': m, 'n': 0, 'skipped': None, 'action': 'none', 'hold': None, 'ev': None}

    def _eval_row(**kw):
        row = {'ts_utc': now, 'mode': m, 'n_positions': 0, 'triggers': '[]',
               'hold_flat': bool(res['hold'])}
        row.update(kw)
        write_eval(conn, row)

    def _summary():
        ev = res['ev'] or {}
        dd, w = ev.get('sleeve_dd'), ev.get('worst_plpc')
        trig = ','.join(t['id'] for t in ev.get('triggers', [])) or '-'
        print(f"[crypto_breaker] mode={m} n={res['n']} "
              f"dd={'n/a' if dd is None else f'{dd:.2%}'} "
              f"worst={ev.get('worst_ticker') or '-'}@{'n/a' if w is None else f'{w:.2%}'} "
              f"triggers={trig} action={res['skipped'] and 'skipped:' + res['skipped'] or res['action']} "
              f"hold={(res['hold'] or {}).get('since') or '-'}")
        return res

    # (2) hold + re-arm
    hold = hold_flat_get(r)
    if rearm_requested(env, hold):
        clear_hold(r)
        post(f"[crypto_breaker] re-armed (cleared hold_flat since={hold.get('since')} "
             f"reason={hold.get('reason')})", poster)
        hold = None
        if conn is not None:
            mirror_hold_to_pipeline_config(conn, None)
    res['hold'] = hold

    # (3) positions
    if fake_path:
        pos, _counters = _load_fake_positions(fake_path)
    else:
        pos, _counters = load_crypto_positions(deps.get('run_cli'))
    if pos is None:
        res['skipped'] = 'positions_unavailable'
        _eval_row(action='skipped', skipped='positions_unavailable')
        return _summary()
    if conn is not None and not pos:
        mirror_hold_to_pipeline_config(conn, hold)
    if not pos:
        print('[crypto_breaker] no crypto positions')
        res['skipped'] = 'no_positions'
        return _summary()
    res['n'] = len(pos)

    # (4) daily lane's crypto execution window
    if in_exec_window(now) and not force:
        res['skipped'] = 'exec_window'
        _eval_row(n_positions=len(pos), action='skipped', skipped='exec_window')
        if conn is not None:
            mirror_hold_to_pipeline_config(conn, hold)
        return _summary()

    # (5) evaluate
    regime_ts = _try(deps.get('regime_ts') or _default_regime_ts, 'regime ts')
    flow_ts = _try(deps.get('flow_ts') or (lambda: _flow_ts_from(conn)), 'flow ts')
    ev = evaluate(pos, now=now, regime_ts=regime_ts, flow_ts=flow_ts)
    ev['n_positions'] = len(pos)
    res['ev'] = ev
    notes, actions = [], []
    # non-armed modes keep their own cooldown keys so a shadow/dry-run post can
    # never consume (and so suppress) the first real armed fire.
    cd = (lambda reason: reason) if m == 'armed' else (lambda reason: f'{m}:{reason}')

    # (6) T5 blackout: alert only, never orders
    if ev['blackout']:
        if cooldown_active(r, cd('data_blackout'), now):
            notes.append('cooldown:data_blackout')
        else:
            post(fmt_blackout_msg(ev), poster)
            set_cooldown(r, cd('data_blackout'), now)
            notes.append('blackout_alert')

    # (7) T4 drawdown triggers
    live = []
    for t in (t for t in ev['triggers'] if t['id'] in ('T4a', 'T4b')):
        if cooldown_active(r, cd(t['reason']), now):
            notes.append(f"cooldown:{t['reason']}")
        else:
            live.append(t)
    if live and m != 'armed':
        post(fmt_shadow_msg(ev, live), poster)
        for t in live:
            set_cooldown(r, cd(t['reason']), now)
        actions.append('shadow')
    elif live:
        run_date = now.astimezone(_ET).date().isoformat()
        equity = _try(deps.get('account_equity') or _default_equity, 'account equity', 0.0)
        for t in live:
            if t['id'] == 'T4a':
                # hold BEFORE orders: a SIGTERM mid-flatten still leaves the lane held
                hold = hold_flat_set(r, t['reason'], now)
                res['hold'] = hold
                set_cooldown(r, t['reason'], now)
                results = flatten_crypto(pos, equity=equity, run_date=run_date,
                                         reason=t['reason'], **act_deps)
                post(fmt_fire_msg(m, [t], results, hold), poster)
                actions.append('flatten')
            else:
                p = next(p for p in pos if p.ticker == t['ticker'])
                th = hold_ticker_set(r, p.ticker, t['reason'], now)
                set_cooldown(r, t['reason'], now)
                result = close_position(p, equity=equity, run_date=run_date,
                                        reason=t['reason'], **act_deps)
                post(fmt_fire_msg(m, [t], [result], th), poster)
                actions.append(f'close:{p.ticker}')
    res['action'] = ','.join(actions) or 'none'

    # (8) eval row, (9) mirror, (10) summary
    _eval_row(n_positions=len(pos), sleeve_cost_basis=ev['sleeve_cost'],
              sleeve_unrealized_pl=ev['sleeve_pl'], sleeve_dd=ev['sleeve_dd'],
              worst_ticker=ev['worst_ticker'], worst_plpc=ev['worst_plpc'],
              triggers=json.dumps(ev['triggers'], default=str), action=res['action'],
              note=','.join(notes) or None)
    if conn is not None:
        mirror_hold_to_pipeline_config(conn, hold)
    return _summary()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='Crypto drawdown circuit breaker (one tick)')
    ap.add_argument('--dry-run', action='store_true', help='evaluate + post, never order')
    ap.add_argument('--force', action='store_true', help='ignore the 15:55-16:05 ET exec window')
    a = ap.parse_args(argv)
    try:
        os.nice(10)
    except Exception:  # noqa: BLE001
        pass
    res = run_once(dry_run=a.dry_run, force=a.force)
    return 1 if res.get('skipped') == 'positions_unavailable' and res.get('mode') == 'armed' else 0


if __name__ == '__main__':
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_root / 'src'))
    sys.path.insert(0, str(_root))
    sys.exit(main())
