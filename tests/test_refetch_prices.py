import types
import scripts.refetch_prices as rp

class FakeRedis:
    def __init__(self): self.kv = {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True
    def delete(self, k): self.kv.pop(k, None)

def test_refetch_success_writes_done(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(rp, '_redis', lambda: r)
    monkeypatch.setattr(rp, '_run_price_fill', lambda date: 0)
    monkeypatch.setattr(rp, '_freshness_ok', lambda date: (True, 503))
    rc = rp.run('2026-06-09')
    assert rc == 0
    import src.execution.intraday_prefetch as p
    s = p.read_prefetch(r, '2026-06-09')
    assert s['status'] == 'done' and s['n_tickers'] == 503

def test_refetch_collector_failure_writes_failed(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(rp, '_redis', lambda: r)
    monkeypatch.setattr(rp, '_run_price_fill', lambda date: 1)
    monkeypatch.setattr(rp, '_freshness_ok', lambda date: (False, 0))
    rc = rp.run('2026-06-09')
    assert rc == 1
    import src.execution.intraday_prefetch as p
    assert p.read_prefetch(r, '2026-06-09')['status'] == 'failed'

def test_refetch_stale_after_success_writes_failed(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(rp, '_redis', lambda: r)
    monkeypatch.setattr(rp, '_run_price_fill', lambda date: 0)
    monkeypatch.setattr(rp, '_freshness_ok', lambda date: (False, 0))
    rc = rp.run('2026-06-09')
    assert rc == 1
    import src.execution.intraday_prefetch as p
    assert p.read_prefetch(r, '2026-06-09')['status'] == 'failed'

def test_refetch_timeout_writes_failed(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(rp, '_redis', lambda: r)
    monkeypatch.setattr(rp, '_run_price_fill', lambda date: 124)
    monkeypatch.setattr(rp, '_freshness_ok', lambda date: (False, 0))
    rc = rp.run('2026-06-09')
    assert rc == 1
    import src.execution.intraday_prefetch as p
    assert p.read_prefetch(r, '2026-06-09')['status'] == 'failed'

def test_freshness_cutoff_is_today():
    import scripts.refetch_prices as rp
    assert rp._freshness_cutoff('2026-06-09') == '2026-06-09'
