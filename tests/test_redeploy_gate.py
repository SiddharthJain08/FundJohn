import scripts.redeploy_pipeline as rd


class FakeRedis:
    def __init__(self, kv=None): self.kv = kv or {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True
    def delete(self, k): self.kv.pop(k, None)
    def ttl(self, k): return 60


def test_gate_proceeds_when_done_and_fresh(monkeypatch):
    import src.execution.intraday_prefetch as p
    r = FakeRedis()
    p.set_prefetch_done(r, '2026-06-09', n_tickers=503)
    monkeypatch.setattr(rd, '_redis', lambda: r)
    monkeypatch.setattr(rd, '_freshness_ok', lambda date: True)
    assert rd._data_ready_gate('2026-06-09') == 'proceed'


def test_gate_aborts_on_failed(monkeypatch):
    import src.execution.intraday_prefetch as p
    r = FakeRedis()
    p.set_prefetch_failed(r, '2026-06-09', error='conn loss')
    monkeypatch.setattr(rd, '_redis', lambda: r)
    monkeypatch.setattr(rd, '_freshness_ok', lambda date: False)
    assert rd._data_ready_gate('2026-06-09') == 'abort'


def test_gate_no_sentinel_runs_sync_refetch(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(rd, '_redis', lambda: r)
    called = {'n': 0}
    def fake_sync(date):
        called['n'] += 1
        import src.execution.intraday_prefetch as p
        p.set_prefetch_done(r, date, n_tickers=503); return 0
    monkeypatch.setattr(rd, '_sync_refetch', fake_sync)
    monkeypatch.setattr(rd, '_freshness_ok', lambda date: True)
    assert rd._data_ready_gate('2026-06-09') == 'proceed'
    assert called['n'] == 1
