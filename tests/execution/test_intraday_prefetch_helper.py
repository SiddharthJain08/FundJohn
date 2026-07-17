import json
import src.execution.intraday_prefetch as p

class FakeRedis:
    def __init__(self): self.kv = {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True
    def delete(self, k): self.kv.pop(k, None)

def test_sentinel_roundtrip():
    r = FakeRedis()
    p.set_prefetch_running(r, '2026-06-09', target_state='HIGH_VOL', episode='2026-06-09:HIGH_VOL:t0')
    s = p.read_prefetch(r, '2026-06-09')
    assert s['status'] == 'running' and s['target_state'] == 'HIGH_VOL'
    p.set_prefetch_done(r, '2026-06-09', n_tickers=503)
    s = p.read_prefetch(r, '2026-06-09')
    assert s['status'] == 'done' and s['n_tickers'] == 503

def test_failed_status():
    r = FakeRedis()
    p.set_prefetch_running(r, '2026-06-09', target_state='HIGH_VOL', episode='e')
    p.set_prefetch_failed(r, '2026-06-09', error='conn loss')
    assert p.read_prefetch(r, '2026-06-09')['status'] == 'failed'

def test_should_prefetch_debounce():
    r = FakeRedis()
    assert p.should_prefetch(r, '2026-06-09', episode='e1') is True
    p.set_prefetch_running(r, '2026-06-09', target_state='HIGH_VOL', episode='e1')
    assert p.should_prefetch(r, '2026-06-09', episode='e1') is False
    assert p.should_prefetch(r, '2026-06-09', episode='e2') is True

def test_inflight_lock():
    r = FakeRedis()
    assert p.acquire_inflight(r) is True
    assert p.acquire_inflight(r) is False
    p.release_inflight(r)
    assert p.acquire_inflight(r) is True
