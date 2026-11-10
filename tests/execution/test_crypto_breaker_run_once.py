"""run_once orchestration (Task 4). Every dependency is faked: no DB, Redis,
broker or Discord is reachable from these tests."""
import json
from datetime import datetime, timedelta, timezone

from execution import crypto_breaker as cb

NOW = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)      # 13:00 ET Tue
IN_WINDOW = datetime(2026, 11, 10, 20, 58, tzinfo=timezone.utc)  # 15:58 ET


class FakeRedis:
    def __init__(s, log=None): s.d = {}; s.ttls = {}; s.log = log if log is not None else []
    def get(s, k): return s.d.get(k)
    def set(s, k, v, ex=None): s.d[k] = v; s.ttls[k] = ex; s.log.append(('set', k))
    def delete(s, k): s.d.pop(k, None); s.log.append(('delete', k))
    def ttl(s, k): return s.ttls.get(k)
    def scan_iter(s, match=None): return iter([k for k in list(s.d) if k.startswith(match.rstrip('*'))])


class FakeExec:
    def __init__(s, log=None, rejected=()):
        s.calls = []; s.log = log if log is not None else []; s.rejected = set(rejected)

    def execute_single(s, sess, equity, order, run_date):
        s.calls.append(order); s.log.append(('exec', order['ticker']))
        if order['ticker'] in s.rejected:
            return {'status': 'rejected', 'reason': 'insufficient'}
        return {'status': 'submitted', 'order_id': 'oid-' + order['ticker']}


class _Cur:
    def __init__(s, c): s.c = c; s.row = None
    def __enter__(s): return s
    def __exit__(s, *a): return False

    def execute(s, sql, params=None):
        c = s.c
        if c.aborted:  # psycopg2 InFailedSqlTransaction semantics
            raise RuntimeError('current transaction is aborted')
        if c.fail_next:
            c.fail_next -= 1; c.aborted = True
            raise RuntimeError('boom')
        c.sql.append((sql, params))
        if c.log is not None and sql.startswith('INSERT INTO crypto_breaker_evals'):
            c.log.append(('eval', params[_ACTION_IDX]))
        elif c.log is not None and sql.startswith('INSERT INTO pipeline_config'):
            c.log.append(('mirror', params[1]))
        s.row = c.respond(sql, params)

    def fetchone(s): return s.row


_ACTION_IDX = cb._EVAL_COLS.index('action')


class FakeConn:
    def __init__(s, respond=None, fail_next=0, log=None):
        s.sql = []; s.commits = 0; s.aborted = False; s.fail_next = fail_next; s.log = log
        s.respond = respond or (lambda sql, params: None)
    def cursor(s): return _Cur(s)
    def commit(s): s.commits += 1
    def rollback(s): s.aborted = False

    def evals(s):
        out = []
        for sql, p in s.sql:
            if sql.startswith('INSERT INTO crypto_breaker_evals'):
                cols = sql.split('(', 1)[1].split(')', 1)[0].replace(' ', '').split(',')
                out.append(dict(zip(cols, p)))
        return out

    def mirrors(s):
        return [p for sql, p in s.sql if sql.startswith('INSERT INTO pipeline_config')]


def _row(sym, cost, pl):
    return dict(symbol=sym, asset_class='crypto', qty='1', avg_entry_price=str(cost),
                current_price=str(cost + pl), market_value=str(cost + pl),
                cost_basis=str(cost), unrealized_pl=str(pl), unrealized_plpc=str(pl / cost))


def _deps(rows, *, log=None, rejected=(), regime_age_h=0.5, flow_age_h=0.5, redis=None):
    log = log if log is not None else []
    d = {'run_cli': (lambda args, timeout=15: (True, rows, None)) if rows is not None
         else (lambda args, timeout=15: (False, None, {'error': 'cli down'})),
         'redis': redis if redis is not None else FakeRedis(log),
         'conn': FakeConn(), 'executor': FakeExec(log, rejected),
         'poller': lambda oid, timeout_s=90: {'status': 'filled', 'filled_qty': '1',
                                              'filled_avg_price': '1'},
         'audit': [], 'posts': [],
         'account_equity': lambda: 100000.0,
         'regime_ts': lambda: NOW - timedelta(hours=regime_age_h),
         'flow_ts': lambda: NOW - timedelta(hours=flow_age_h)}
    audit, posts = d['audit'], d['posts']
    d['audit'] = audit.append
    d['poster'] = posts.append
    return d, audit, posts


def _no_redis_deps(rows, respond=None, **kw):
    d, audit, posts = _deps(rows, **kw)
    d['redis'] = None
    d['conn'] = FakeConn(respond)
    return d, audit, posts


def _call(d, *, env=None, now=NOW, dry_run=False, force=False):
    deps = {k: v for k, v in d.items() if k != 'posts'}
    return cb.run_once(now=now, env=env or {}, dry_run=dry_run, force=force, deps=deps)


ARMED = {'OPENCLAW_CRYPTO_BREAKER': '1'}
SLEEVE_HIT = [_row('BTC/USD', 9000.0, -900.0), _row('ETH/USD', 1000.0, 0.0)]   # dd -0.09


def test_no_positions_skips_without_eval_row():
    d, audit, posts = _deps([])
    res = _call(d, env=ARMED)
    assert res['skipped'] == 'no_positions'
    assert d['conn'].evals() == [] and d['executor'].calls == [] and posts == []
    assert d['conn'].mirrors()  # hold mirror still rewritten


def test_shadow_never_calls_executor():
    d, audit, posts = _deps(SLEEVE_HIT)
    res = _call(d)
    assert res['mode'] == 'shadow' and d['executor'].calls == [] and audit == []
    ev = d['conn'].evals()
    assert len(ev) == 1 and ev[0]['action'] == 'shadow' and ev[0]['mode'] == 'shadow'
    assert ev[0]['n_positions'] == 2 and ev[0]['hold_flat'] is False
    assert len(posts) == 1 and posts[0].startswith('[crypto_breaker] shadow would_fire=sleeve_drawdown')
    assert cb.hold_flat_get(d['redis']) is None
    # shadow cooldown must not consume the armed cooldown
    assert not cb.cooldown_active(d['redis'], 'sleeve_drawdown', NOW)
    _call(d)
    assert len(posts) == 1  # once per cooldown


def test_armed_sleeve_fire_flattens_sets_hold_and_cooldown():
    log = []
    d, audit, posts = _deps(SLEEVE_HIT, log=log)
    res = _call(d, env=ARMED)
    assert res['action'] == 'flatten'
    assert sorted(o['ticker'] for o in d['executor'].calls) == ['BTC-USD', 'ETH-USD']
    first_exec = next(i for i, e in enumerate(log) if e[0] == 'exec')
    assert log.index(('set', cb.HOLD_KEY)) < first_exec
    assert log.index(('set', cb._cd_key('sleeve_drawdown', NOW))) < first_exec
    hold = cb.hold_flat_get(d['redis'])
    assert hold['reason'] == 'sleeve_drawdown' and hold['scope'] == 'sleeve'
    assert cb.cooldown_active(d['redis'], 'sleeve_drawdown', NOW)
    assert len(audit) == 2 and all(a['result_status'] == 'filled' for a in audit)
    started, ev = d['conn'].evals()  # pre-order evidence row, then the final row
    assert started['action'] == 'flatten:started' and started['skipped'] is None
    assert ev['action'] == 'flatten' and ev['hold_flat'] is True and ev['mode'] == 'armed'
    assert json.loads(ev['triggers'])[0]['id'] == 'T4a'
    assert posts[0] == '[crypto_breaker] FIRING reason=sleeve_drawdown n=2 notional=$9.1K'
    assert posts[1].startswith('[crypto_breaker] FIRED mode=armed reason=sleeve_drawdown')
    assert 'remain_open=0' in posts[1] and 'manual close' not in posts[1]
    m = json.loads(d['conn'].mirrors()[-1][1])
    assert m['sleeve']['reason'] == 'sleeve_drawdown' and m['tickers'] == {}


def test_armed_position_fire_closes_only_that_ticker_and_sets_hold_ticker():
    log = []
    rows = [_row('BTC/USD', 9000.0, 0.0), _row('ETH/USD', 1000.0, -200.0)]  # dd -0.02, ETH -20%
    d, audit, posts = _deps(rows, log=log)
    res = _call(d, env=ARMED)
    assert [o['ticker'] for o in d['executor'].calls] == ['ETH-USD']
    assert res['action'] == 'close:ETH-USD'
    key = 'crypto_breaker:hold_ticker:ETH-USD'
    assert log.index(('set', key)) < log.index(('exec', 'ETH-USD'))
    assert cb.hold_ticker_get(d['redis'], 'ETH-USD')['reason'] == 'position_drawdown:ETH-USD'
    assert cb.hold_flat_get(d['redis']) is None
    assert cb.cooldown_active(d['redis'], 'position_drawdown:ETH-USD', NOW)
    started, ev = d['conn'].evals()
    assert started['action'] == 'close_started:ETH-USD'
    assert ev['action'] == 'close:ETH-USD' and ev['hold_flat'] is False
    assert len(posts) == 2 and posts[0].startswith('[crypto_breaker] FIRING reason=position_drawdown:ETH-USD n=1')
    assert 'reason=position_drawdown:ETH-USD' in posts[1]
    m = json.loads(d['conn'].mirrors()[-1][1])
    assert m['sleeve'] is None and m['tickers']['ETH-USD']['reason'] == 'position_drawdown:ETH-USD'
    assert res['hold_tickers'] == {'ETH-USD': m['tickers']['ETH-USD']}


def test_fire_rejected_close_sets_hold_and_cooldown():
    d, audit, posts = _deps(SLEEVE_HIT, rejected={'BTC-USD', 'ETH-USD'})
    _call(d, env=ARMED)
    assert cb.hold_flat_get(d['redis'])['reason'] == 'sleeve_drawdown'
    assert cb.cooldown_active(d['redis'], 'sleeve_drawdown', NOW)
    assert [a['result_status'] for a in audit] == ['rejected', 'rejected']
    assert 'rejected=2' in posts[-1] and 'remain_open=2' in posts[-1]
    assert '2 remain open, manual close required (retry after cooldown); reason=insufficient' in posts[-1]


def test_cooldown_suppresses_second_fire():
    d, audit, posts = _deps(SLEEVE_HIT)
    _call(d, env=ARMED)
    n_exec, n_posts = len(d['executor'].calls), len(posts)
    res = _call(d, env=ARMED, now=NOW + timedelta(minutes=5))
    assert len(d['executor'].calls) == n_exec and len(posts) == n_posts
    ev = d['conn'].evals()[-1]
    assert ev['action'] == 'none' and 'cooldown:sleeve_drawdown' in ev['note']
    assert res['action'] == 'none'


def test_exec_window_skips_with_eval_row():
    d, audit, posts = _deps(SLEEVE_HIT)
    res = _call(d, env=ARMED, now=IN_WINDOW)
    assert res['skipped'] == 'exec_window' and d['executor'].calls == [] and posts == []
    ev = d['conn'].evals()
    assert len(ev) == 1 and ev[0]['skipped'] == 'exec_window' and ev[0]['action'] == 'skipped'
    # --force ignores the window
    d2, _, _ = _deps(SLEEVE_HIT)
    assert _call(d2, env=ARMED, now=IN_WINDOW, force=True)['action'] == 'flatten'


def test_blackout_alert_does_not_block_t4_but_never_orders_itself():
    d, audit, posts = _deps(SLEEVE_HIT, regime_age_h=4, flow_age_h=4)
    _call(d, env=ARMED)
    assert len(d['executor'].calls) == 2
    assert any(p.startswith('[crypto_breaker] BLACKOUT') for p in posts)
    assert any(p.startswith('[crypto_breaker] FIRED') for p in posts) and len(posts) == 3  # +FIRING
    rows = [_row('BTC/USD', 9000.0, -90.0), _row('ETH/USD', 1000.0, -10.0)]  # dd -0.01
    d, audit, posts = _deps(rows, regime_age_h=4, flow_age_h=4)
    res = _call(d, env=ARMED)
    assert d['executor'].calls == [] and len(posts) == 1 and posts[0].startswith('[crypto_breaker] BLACKOUT')
    assert res['action'] == 'none' and cb.hold_flat_get(d['redis']) is None
    _call(d, env=ARMED, now=NOW + timedelta(minutes=5))
    assert len(posts) == 1  # blackout cooldown


def test_rearm_clears_hold_and_posts():
    d, audit, posts = _deps([_row('BTC/USD', 9000.0, 0.0)])
    hold = cb.hold_flat_set(d['redis'], 'sleeve_drawdown', NOW - timedelta(hours=2))
    env = dict(ARMED, OPENCLAW_CRYPTO_BREAKER_REARM=hold['since'])
    res = _call(d, env=env)
    assert cb.hold_flat_get(d['redis']) is None and res['hold'] is None
    assert any(p.startswith('[crypto_breaker] re-armed') for p in posts)
    assert d['conn'].mirrors()[0][1] == ''
    assert d['conn'].evals()[-1]['hold_flat'] is False


DEGRADED = '[crypto_breaker] DEGRADED: positions unavailable (cli down) — not evaluating'


def test_positions_unavailable_writes_skip_row_posts_degraded_no_action():
    d, audit, posts = _deps(None)
    res = _call(d, env=ARMED)
    assert res['skipped'] == 'positions_unavailable' and d['executor'].calls == []
    assert posts == [DEGRADED]
    ev = d['conn'].evals()
    assert len(ev) == 1 and ev[0]['skipped'] == 'positions_unavailable'
    assert ev[0]['n_positions'] == 0 and ev[0]['action'] == 'skipped'
    assert 'error=cli down' in ev[0]['note']
    assert d['redis'].ttls[cb._cd_key('positions_unavailable', NOW)] == 6 * 3600
    _call(d, env=ARMED, now=NOW + timedelta(minutes=5))
    assert posts == [DEGRADED]  # once per cooldown
    # Redis down: post anyway, every tick
    d, _, posts = _no_redis_deps(None)
    _call(d, env=ARMED)
    _call(d, env=ARMED, now=NOW + timedelta(minutes=5))
    assert posts == [DEGRADED, DEGRADED]


def test_fake_positions_file_honoured_only_with_dry_run(tmp_path, capsys):
    f = tmp_path / 'pos.json'
    f.write_text(json.dumps(SLEEVE_HIT))
    env = dict(ARMED, OPENCLAW_CRYPTO_BREAKER_FAKE_POSITIONS=str(f))
    d, audit, posts = _deps(None)  # real CLI would fail; the file must be used
    res = _call(d, env=env, dry_run=True)
    assert res['mode'] == 'dry_run' and d['executor'].calls == [] and audit == []
    assert d['conn'].evals()[0]['action'] == 'shadow' and res['n'] == 2
    assert 'FAKE POSITIONS' in capsys.readouterr().out


def test_fake_positions_file_ignored_without_dry_run(tmp_path, capsys):
    f = tmp_path / 'pos.json'
    f.write_text(json.dumps(SLEEVE_HIT))
    env = dict(ARMED, OPENCLAW_CRYPTO_BREAKER_FAKE_POSITIONS=str(f))
    d, audit, posts = _deps([])  # the real (empty) book is used, not the file
    res = _call(d, env=env)
    assert res['mode'] == 'armed' and res['skipped'] == 'no_positions'
    assert d['executor'].calls == [] and audit == []
    assert 'ignored without --dry-run' in capsys.readouterr().out


def test_main_rc(monkeypatch):
    monkeypatch.setattr(cb.os, 'nice', lambda n: 0)
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {'mode': 'armed', 'skipped': 'positions_unavailable'}
    monkeypatch.setattr(cb, 'run_once', fake)
    assert cb.main(['--force']) == 1 and seen['force'] is True and seen['dry_run'] is False
    monkeypatch.setattr(cb, 'run_once', lambda **kw: {'mode': 'shadow', 'skipped': 'positions_unavailable'})
    assert cb.main(['--dry-run']) == 0


MIRROR_HOLD = {'since': '2026-11-10T16:00:00+00:00', 'reason': 'sleeve_drawdown', 'scope': 'sleeve'}


def test_redis_down_keeps_mirror_and_reads_hold_from_it():
    def respond(sql, params):
        if 'FROM pipeline_config' in sql:
            return (json.dumps(MIRROR_HOLD),)
        if 'FROM crypto_breaker_evals' in sql:
            return (1,)  # the flatten that set the hold, 2 h ago
        return None
    d, audit, posts = _no_redis_deps(SLEEVE_HIT, respond)
    res = _call(d, env=ARMED)
    assert res['hold'] == MIRROR_HOLD and d['executor'].calls == [] and posts == []
    writes = [p for sql, p in d['conn'].sql if sql.startswith('INSERT INTO pipeline_config')]
    assert writes == []  # never '' over a real hold
    ev = d['conn'].evals()[0]
    assert ev['hold_flat'] is True and 'cooldown:sleeve_drawdown' in ev['note']
    # empty book while Redis is down: still no '' wipe
    d, _, _ = _no_redis_deps([], respond)
    _call(d, env=ARMED)
    assert not any(sql.startswith('INSERT INTO pipeline_config') for sql, _ in d['conn'].sql)


def test_redis_down_armed_cooldown_from_evals():
    seen = []

    def respond(sql, params):
        if 'FROM crypto_breaker_evals' in sql:
            seen.append((sql, params))
            return (1,) if len(seen) > 1 else None  # first tick: nothing recent
        return None
    d, audit, posts = _no_redis_deps(SLEEVE_HIT, respond)
    _call(d, env=ARMED)
    assert len(d['executor'].calls) == 2 and len(posts) == 2  # FIRING + FIRED
    sql, params = seen[0]
    assert "mode = %s" in sql and "NOT LIKE 'skipped%%'" in sql and params[0] == 'armed'
    assert params[1] == NOW - timedelta(hours=6)
    # the new hold IS recorded in the mirror (not a wipe)
    w = [p for s_, p in d['conn'].sql if s_.startswith('INSERT INTO pipeline_config')]
    assert json.loads(w[-1][1])['sleeve']['reason'] == 'sleeve_drawdown'
    _call(d, env=ARMED, now=NOW + timedelta(minutes=5))
    assert len(d['executor'].calls) == 2 and len(posts) == 2
    # T4b fallback scopes the query to that ticker
    rows = [_row('BTC/USD', 9000.0, 0.0), _row('ETH/USD', 1000.0, -200.0)]
    seen.clear()
    d, _, _ = _no_redis_deps(rows, respond)
    _call(d, env=ARMED)
    q = next((s_, p) for s_, p in seen if p[-1] == '%close_started:ETH-USD%')
    assert q[1][-2] == '%close:ETH-USD%'
    assert '(action LIKE %s OR action LIKE %s)' in q[0] and 'mode = %s AND ts_utc > %s' in q[0]


def test_no_state_store_armed_does_not_act():
    d, audit, posts = _deps(SLEEVE_HIT)
    d['redis'] = None; d['conn'] = None
    res = _call(d, env=ARMED)
    assert res['skipped'] == 'no_state_store' and d['executor'].calls == [] and audit == []
    assert posts == ['[crypto_breaker] DEGRADED: no state store — not acting']


def test_failed_statement_does_not_lose_eval_row():
    d, audit, posts = _deps(SLEEVE_HIT)
    d['conn'] = FakeConn(fail_next=1)
    del d['flow_ts']  # the flow-ts query is the first statement and raises
    _call(d)
    ev = d['conn'].evals()
    assert len(ev) == 1 and ev[0]['action'] == 'shadow'


def test_refire_keeps_existing_since():
    d, audit, posts = _deps(SLEEVE_HIT)
    old = cb.hold_flat_set(d['redis'], 'sleeve_drawdown', NOW - timedelta(hours=7))
    _call(d, env=ARMED)  # cooldown (6 h) expired, hold (24 h) still live
    assert len(d['executor'].calls) == 2
    assert cb.hold_flat_get(d['redis'])['since'] == old['since']


def test_close_does_not_fetch_equity(monkeypatch):
    import sys
    import types
    fetched = []
    fake = types.ModuleType('execution.alpaca_trader')
    fake._alpaca_session = lambda: fetched.append('session')
    fake._fetch_account_state = lambda sess: fetched.append('account') or {'equity': 1.0}
    monkeypatch.setitem(sys.modules, 'execution.alpaca_trader', fake)
    d, audit, posts = _deps(SLEEVE_HIT)
    del d['account_equity']
    _call(d, env=ARMED)
    assert len(d['executor'].calls) == 2 and audit and fetched == []
    assert all(c is not None for c in d['executor'].calls)


def test_positions_unavailable_refreshes_mirror():
    d, audit, posts = _deps(None)
    cb.hold_flat_set(d['redis'], 'sleeve_drawdown', NOW)
    _call(d, env=ARMED)
    assert json.loads(d['conn'].mirrors()[-1][1])['sleeve']['reason'] == 'sleeve_drawdown'


# ---- final-review fix wave -------------------------------------------------

def test_armed_fire_evidence_trail_precedes_first_order():
    log = []
    d, audit, posts = _deps(SLEEVE_HIT, log=log)
    d['conn'] = FakeConn(log=log)
    d['poster'] = lambda m: log.append(('post', m.split(' ', 2)[1]))
    _call(d, env=ARMED)
    first_exec = next(i for i, e in enumerate(log) if e[0] == 'exec')
    i_eval = log.index(('eval', 'flatten:started'))
    i_hold = log.index(('set', cb.HOLD_KEY))
    i_mirror = next(i for i, e in enumerate(log) if e[0] == 'mirror')
    i_firing = log.index(('post', 'FIRING'))
    assert i_eval < i_hold < i_mirror < i_firing < first_exec
    assert json.loads(log[i_mirror][1])['sleeve']['reason'] == 'sleeve_drawdown'
    assert log.index(('post', 'FIRED')) > first_exec
    assert log[-1][0] == 'mirror' and log[-2] == ('eval', 'flatten')


def test_redis_down_fire_mirrors_hold_before_first_order():
    log = []
    d, audit, posts = _no_redis_deps(SLEEVE_HIT, log=log)
    d['conn'] = FakeConn(log=log)
    _call(d, env=ARMED)
    first_exec = next(i for i, e in enumerate(log) if e[0] == 'exec')
    i_mirror = next(i for i, e in enumerate(log) if e[0] == 'mirror')
    assert log.index(('eval', 'flatten:started')) < i_mirror < first_exec
    assert json.loads(log[i_mirror][1])['sleeve']['reason'] == 'sleeve_drawdown'


def test_started_rows_count_as_cooling_in_evals_fallback():
    seen = []

    def respond(sql, params):
        seen.append((sql, params))
        return None
    conn = FakeConn(respond)
    cb._cooling_from_evals(conn, 'armed', 'sleeve_drawdown', NOW)
    sql, params = seen[-1]
    # 'flatten:started' / 'close_started:<T>' pass every exclusion
    for act in ('flatten:started', 'close_started:ETH-USD'):
        assert act not in ('none', 'shadow') and not act.startswith('skipped')
    assert "action <> 'none' AND action <> 'shadow' AND action NOT LIKE 'skipped%%'" in sql
    cb._cooling_from_evals(conn, 'armed', 'position_drawdown:ETH-USD', NOW)
    sql, params = seen[-1]
    assert params[2:] == ('%close:ETH-USD%', '%close_started:ETH-USD%')
    # a started row found => cooling
    assert cb._cooling_from_evals(FakeConn(lambda s_, p: (1,)), 'armed', 'sleeve_drawdown', NOW)


def test_redis_down_reads_new_shape_mirror_with_tickers():
    tk = {'since': '2026-11-10T16:00:00+00:00', 'reason': 'position_drawdown:ETH-USD',
          'ticker': 'ETH-USD'}

    def respond(sql, params):
        if 'FROM pipeline_config' in sql:
            return (json.dumps({'sleeve': None, 'tickers': {'ETH-USD': tk}}),)
        if 'FROM crypto_breaker_evals' in sql:
            return (1,)
        return None
    rows = [_row('BTC/USD', 9000.0, 0.0), _row('ETH/USD', 1000.0, -200.0)]
    d, audit, posts = _no_redis_deps(rows, respond)
    res = _call(d, env=ARMED)
    assert res['hold'] is None and res['hold_tickers'] == {'ETH-USD': tk}
    assert d['executor'].calls == [] and d['conn'].mirrors() == []


def test_rearm_ticker_clears_that_ticker_hold_and_mirror():
    d, audit, posts = _deps([_row('BTC/USD', 9000.0, 0.0)])
    cb.hold_ticker_set(d['redis'], 'ETH-USD', 'position_drawdown:ETH-USD', NOW - timedelta(hours=1))
    cb.hold_ticker_set(d['redis'], 'SOL-USD', 'position_drawdown:SOL-USD', NOW - timedelta(hours=1))
    res = _call(d, env=dict(ARMED, OPENCLAW_CRYPTO_BREAKER_REARM_TICKER='eth-usd'))
    assert cb.hold_ticker_get(d['redis'], 'ETH-USD') is None
    assert cb.hold_ticker_get(d['redis'], 'SOL-USD') is not None
    assert list(res['hold_tickers']) == ['SOL-USD']
    assert any(p.startswith('[crypto_breaker] re-armed ETH-USD') for p in posts)
    assert list(json.loads(d['conn'].mirrors()[-1][1])['tickers']) == ['SOL-USD']
    # REARM (sleeve) never touches ticker holds
    hold = cb.hold_flat_set(d['redis'], 'sleeve_drawdown', NOW - timedelta(hours=2))
    res = _call(d, env=dict(ARMED, OPENCLAW_CRYPTO_BREAKER_REARM=hold['since']))
    assert res['hold'] is None and list(res['hold_tickers']) == ['SOL-USD']
    # last ticker cleared -> mirror ''
    _call(d, env=dict(ARMED, OPENCLAW_CRYPTO_BREAKER_REARM_TICKER='SOL-USD'))
    assert d['conn'].mirrors()[-1][1] == ''


def test_rearm_ticker_ignored_when_redis_down():
    def respond(sql, params):
        if 'FROM pipeline_config' in sql:
            return (json.dumps({'sleeve': None, 'tickers': {'ETH-USD': {'since': 'x'}}}),)
        return None
    d, audit, posts = _no_redis_deps([_row('BTC/USD', 9000.0, 0.0)], respond)
    res = _call(d, env=dict(ARMED, OPENCLAW_CRYPTO_BREAKER_REARM_TICKER='ETH-USD'))
    assert 'ETH-USD' in res['hold_tickers'] and posts == [] and d['conn'].mirrors() == []


def test_counters_in_summary_and_eval_note(capsys):
    rows = SLEEVE_HIT + [dict(_row('BTC/USD', 100.0, 0.0)),
                         dict(symbol='DOGE/USD', asset_class='crypto', qty='5', market_value='50')]
    d, audit, posts = _deps(rows)
    _call(d)
    ev = d['conn'].evals()[-1]
    assert 'counters=' in ev['note'] and 'no_cost=1' in ev['note'] and 'duplicate_rows=1' in ev['note']
    out = capsys.readouterr().out
    assert 'counters=' in out and 'duplicate_rows=1' in out


def test_default_regime_ts_reads_state_file_directly(tmp_path):
    import sys
    f = tmp_path / 'crypto_regime_latest.json'
    f.write_text(json.dumps({'state': 'RISK_ON', 'ts_utc': '2026-11-10T17:00:00Z'}))
    sys.modules.pop('regime.crypto_regime', None)
    assert cb._default_regime_ts(str(f)) == datetime(2026, 11, 10, 17, 0, tzinfo=timezone.utc)
    assert 'regime.crypto_regime' not in sys.modules
    assert cb.REGIME_LATEST.endswith('.agents/crypto-market-state/crypto_regime_latest.json')


def test_redis_client_and_db_connect_carry_timeouts(monkeypatch):
    import psycopg2
    import redis
    seen = {}

    class R:
        def ping(s): return True

    def from_url(url, **kw):
        seen.setdefault('redis', []).append(kw)
        return R()
    monkeypatch.setattr(redis.Redis, 'from_url', staticmethod(from_url))
    monkeypatch.setattr(psycopg2, 'connect',
                        lambda *a, **kw: seen.setdefault('pg', []).append(kw) or
                        type('C', (), {'autocommit': False})())
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake/db')
    assert cb._redis() is not None and cb._default_conn() is not None
    from execution import crypto_hold as ch
    ch._default_redis(); ch._default_conn()
    assert len(seen['redis']) == 2 and len(seen['pg']) == 2
    for kw in seen['redis']:
        assert kw['socket_connect_timeout'] == 2 and kw['socket_timeout'] == 2
    for kw in seen['pg']:
        assert kw['connect_timeout'] == 3
