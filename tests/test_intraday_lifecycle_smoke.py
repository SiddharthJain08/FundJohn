"""tests/test_intraday_lifecycle_smoke.py

Mocked tick1→tick3 lifecycle: prefetch done → tick-3 gate proceeds.
No live I/O.
"""
import scripts.redeploy_pipeline as rd
import src.execution.intraday_prefetch as p


class FakeRedis:
    def __init__(self): self.kv = {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True
    def delete(self, k): self.kv.pop(k, None)
    def ttl(self, k): return 60


def test_lifecycle_prefetch_then_gate_proceeds(monkeypatch):
    r = FakeRedis()
    p.set_prefetch_running(r, '2026-06-09', target_state='HIGH_VOL', episode='2026-06-09:HIGH_VOL:t0', started_at='t0')
    p.set_prefetch_done(r, '2026-06-09', n_tickers=503)
    monkeypatch.setattr(rd, '_redis', lambda: r)
    monkeypatch.setattr(rd, '_freshness_ok', lambda date: True)
    assert rd._data_ready_gate('2026-06-09') == 'proceed'
