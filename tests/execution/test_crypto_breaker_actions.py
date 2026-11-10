from execution import crypto_breaker as cb


def _p(t, mv, qty=1.0):
    return cb.CryptoPos(t, t, qty, None, None, mv, mv, 0.0, 0.0)


class FakeExec:
    def __init__(s, rejected=()):
        s.calls = []; s.rejected = set(rejected)

    def execute_single(s, sess, equity, order, run_date):
        s.calls.append((sess, equity, order, run_date))
        if order['ticker'] in s.rejected:
            return {'status': 'rejected', 'reason': 'insufficient', 'http': 403}
        return {'status': 'submitted', 'order_id': 'oid-' + order['ticker'], 'qty': 0.5}


def _filled(oid, timeout_s=90):
    return {'status': 'filled', 'filled_qty': '0.5', 'filled_avg_price': '76000'}


def _run(pos, ex=None, poller=_filled, audit=None):
    rows = []
    sink = audit if callable(audit) else rows.append
    audit = rows if not callable(audit) else audit
    ex = ex or FakeExec()
    r = cb.close_position(pos, equity=100000, run_date='2026-11-10', reason='sleeve_drawdown',
                          executor=ex, poller=poller, audit=sink)
    return r, ex, audit


def test_close_position_full_close_order_shape():
    r, ex, audit = _run(_p('BTC-USD', 5000.0))
    sess, eq, o, rd = ex.calls[0]
    assert sess is None and eq == 100000 and rd == '2026-11-10'
    assert o == {'ticker': 'BTC-USD', 'direction': 'short', 'notional_usd': 5000.0,
                 'current_usd': 5000.0, 'strategy_id': '__crypto_breaker__', 'close_only': True}
    assert r['status'] == 'filled' and r['order_id'] == 'oid-BTC-USD'
    a = audit[0]
    assert a['result_status'] == 'filled' and a['direction'] == 'long'
    assert a['regime_to'] == 'CRYPTO_BREAKER' and a['source'] == 'crypto_breaker'
    assert a['asset_class'] == 'crypto' and a['avg_fill_price'] == 76000.0


def test_close_position_rejected_is_audited_with_error():
    r, _, audit = _run(_p('BTC-USD', 5000.0), ex=FakeExec(rejected={'BTC-USD'}))
    assert r['status'] == 'rejected' and r['error']
    assert audit[0]['result_status'] == 'rejected' and 'insufficient' in audit[0]['error_msg']


def test_close_position_pending_when_poll_none():
    r, _, audit = _run(_p('BTC-USD', 5000.0), poller=lambda *a, **k: None)
    assert r['status'] == 'pending' and audit[0]['result_status'] == 'pending'


def test_flatten_continues_after_one_failure():
    class Boom(FakeExec):
        def execute_single(s, sess, equity, order, run_date):
            if order['ticker'] == 'ETH-USD':
                raise RuntimeError('boom')
            return super().execute_single(sess, equity, order, run_date)
    audit = []
    res = cb.flatten_crypto([_p('SOL-USD', 100.0), _p('ETH-USD', 3000.0), _p('BTC-USD', 5000.0)],
                            equity=1e5, run_date='d', reason='sleeve_drawdown',
                            executor=Boom(), poller=_filled, audit=audit.append)
    assert [r['ticker'] for r in res] == ['BTC-USD', 'ETH-USD', 'SOL-USD']
    assert [r['status'] for r in res] == ['filled', 'error', 'filled']
    assert 'boom' in res[1]['error'] and len(audit) == 3


def test_messages_contain_reason_count_notional():
    res = [{'ticker': 'BTC-USD', 'status': 'filled', 'market_value': 12000.0},
           {'ticker': 'ETH-USD', 'status': 'rejected', 'market_value': 300.0, 'error': 'insufficient'}]
    trig = [{'id': 'T4a', 'reason': 'sleeve_drawdown'}]
    m = cb.fmt_fire_msg('armed', trig, res, {'since': 'X'})
    assert m.startswith('[crypto_breaker] FIRED mode=armed reason=sleeve_drawdown n=1/2 ')
    assert 'notional=$12.3K' in m and 'filled=1 rejected=1 remain_open=1 hold_flat=X' in m
    assert m.endswith(' — 1 remain open, manual close required (retry after cooldown); reason=insufficient')
    assert cb.fmt_fire_msg('armed', trig, [], None).endswith('remain_open=0 hold_flat=-')
    ok = cb.fmt_fire_msg('armed', trig, res[:1], {'since': 'X'})
    assert ok.endswith('remain_open=0 hold_flat=X') and 'manual close' not in ok
    pos = [cb.CryptoPos('BTC-USD', 'BTC/USD', 1.0, None, None, 9000.0, 9500.0, -500.0, -0.05),
           cb.CryptoPos('ETH-USD', 'ETH/USD', 1.0, None, None, 3000.0, None, None, None)]
    assert cb.fmt_firing_msg(trig[0], pos) == '[crypto_breaker] FIRING reason=sleeve_drawdown n=2 notional=$12.0K'
    ev = {'sleeve_dd': -0.0912, 'worst_ticker': 'BTC-USD', 'worst_plpc': -0.1634, 'n_positions': 2}
    s = cb.fmt_shadow_msg(ev, trig)
    assert s == '[crypto_breaker] shadow would_fire=sleeve_drawdown dd=-9.12% worst=BTC-USD@-16.34% n=2'
    ev = {'n_positions': 2, 'triggers': [{'id': 'T5', 'reason': 'data_blackout',
                                          'ages_h': {'regime': 4.0, 'flow': 5.5}}]}
    assert cb.fmt_blackout_msg(ev) == ('[crypto_breaker] BLACKOUT regime_age_h=4.0 flow_age_h=5.5 '
                                       'holding=2 \u2014 alert only, no orders')


def test_post_submit_poller_failure_is_pending_with_order_id():
    def boom(*a, **k):
        raise RuntimeError('net down')
    r, _, audit = _run(_p('BTC-USD', 5000.0), poller=boom)
    assert r['status'] == 'pending' and r['order_id'] == 'oid-BTC-USD' and 'net down' in r['error']
    assert audit[0]['result_status'] == 'pending' and 'net down' in audit[0]['error_msg']


def test_flatten_deadline_skips_remaining_positions():
    t = [0.0]
    seen = []

    def slow(oid, timeout_s=90):
        seen.append(timeout_s); t[0] += 100
        return _filled(oid)
    audit = []
    res = cb.flatten_crypto([_p('A-USD', 3.0), _p('B-USD', 2.0), _p('C-USD', 1.0)], equity=1, run_date='d',
                            reason='r', deadline_s=150, clock=lambda: t[0], executor=FakeExec(),
                            poller=slow, audit=audit.append)
    assert [r['status'] for r in res] == ['filled', 'filled', 'skipped']
    assert res[2]['error'] == 'deadline' and seen == [90, 50]
    assert audit[2]['result_status'] == 'skipped' and audit[2]['error_msg'] == 'flatten deadline'


class _Cur:
    def __init__(s): s.sql = []
    def execute(s, q, p=None): s.sql.append((q, p))
    def fetchone(s): return ('uuid-1',)
    def __enter__(s): return s
    def __exit__(s, *a): pass


class _Conn:
    def __init__(s): s.cur = _Cur()
    def cursor(s): return s.cur
    def commit(s): pass
    def __enter__(s): return s
    def __exit__(s, *a): pass


def test_through_real_writer_direction_and_error_msg(monkeypatch):
    import psycopg2
    c = _Conn()
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://x')
    monkeypatch.setattr(psycopg2, 'connect', lambda dsn: c)
    from execution import regime_liquidator as rl
    r, _, _ = _run(_p('BTC-USD', 5000.0), ex=FakeExec(rejected={'BTC-USD'}), audit=rl._write_liquidation_audit)
    assert r['status'] == 'rejected'
    sql, params = c.cur.sql[0]
    assert 'long_close' in params and 'rejected' in params
    assert 'error_msg' in sql and any(isinstance(x, str) and 'insufficient' in x for x in params)
