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
    # The episode-bound gate sync-refetches whenever the sentinel isn't
    # running/done; a `failed` sentinel now triggers ONE retry. Stub it to a
    # no-op so this unit test never invokes the real collector (node + PG) —
    # the legacy `s is None`-only refetch path no longer applies.
    monkeypatch.setattr(rd, '_sync_refetch', lambda date, episode=None: 0)
    assert rd._data_ready_gate('2026-06-09') == 'abort'


def test_gate_no_sentinel_runs_sync_refetch(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(rd, '_redis', lambda: r)
    called = {'n': 0}
    def fake_sync(date, episode=None):
        called['n'] += 1
        import src.execution.intraday_prefetch as p
        p.set_prefetch_done(r, date, n_tickers=503); return 0
    monkeypatch.setattr(rd, '_sync_refetch', fake_sync)
    monkeypatch.setattr(rd, '_freshness_ok', lambda date: True)
    assert rd._data_ready_gate('2026-06-09') == 'proceed'
    assert called['n'] == 1


# ── Episode-bound gate (closes the stale-`done` freshness hole) ──────────────

def _seed_done(r, date, episode, n_tickers=503):
    """Seed a `done` sentinel carrying a specific episode (running→done
    preserves the episode via read-update, mirroring the live flow)."""
    import src.execution.intraday_prefetch as p
    p.set_prefetch_running(r, date, target_state='(t)', episode=episode)
    p.set_prefetch_done(r, date, n_tickers=n_tickers)


def _seed_failed(r, date, episode):
    import src.execution.intraday_prefetch as p
    p.set_prefetch_running(r, date, target_state='(t)', episode=episode)
    p.set_prefetch_failed(r, date, error='conn loss')


def test_gate_proceeds_on_matching_episode(monkeypatch):
    r = FakeRedis()
    _seed_done(r, '2026-06-09', 'E1')
    monkeypatch.setattr(rd, '_redis', lambda: r)
    monkeypatch.setattr(rd, '_freshness_ok', lambda date: True)
    # If the matching done were ignored, _sync_refetch would be needed; assert
    # the gate proceeds WITHOUT a refetch on the matching episode.
    called = {'n': 0}
    monkeypatch.setattr(rd, '_sync_refetch',
                        lambda date, episode=None: called.__setitem__('n', called['n'] + 1) or 0)
    assert rd._data_ready_gate('2026-06-09', 'E1') == 'proceed'
    assert called['n'] == 0, 'matching done episode must not trigger a sync-refetch'


def test_gate_syncrefetch_on_mismatched_episode(monkeypatch):
    """Stale `done` from a PRIOR episode (OLD) must NOT satisfy the gate for E1;
    the gate sync-refetches fresh (stamped E1), then proceeds."""
    r = FakeRedis()
    _seed_done(r, '2026-06-09', 'OLD')   # lingering prior-episode done
    monkeypatch.setattr(rd, '_redis', lambda: r)
    monkeypatch.setattr(rd, '_freshness_ok', lambda date: True)
    calls = []
    def fake_sync(date, episode=None):
        calls.append(episode)
        _seed_done(r, date, episode)   # fresh fetch stamps the expected episode
        return 0
    monkeypatch.setattr(rd, '_sync_refetch', fake_sync)
    assert rd._data_ready_gate('2026-06-09', 'E1') == 'proceed'
    assert calls == ['E1'], f'expected one sync-refetch stamped E1; got {calls}'


def test_gate_aborts_on_matching_failed(monkeypatch):
    r = FakeRedis()
    _seed_failed(r, '2026-06-09', 'E1')
    monkeypatch.setattr(rd, '_redis', lambda: r)
    monkeypatch.setattr(rd, '_freshness_ok', lambda date: False)
    # Matching-episode failed sentinel exists → no refetch, immediate abort.
    monkeypatch.setattr(rd, '_sync_refetch', lambda date, episode=None: 0)
    assert rd._data_ready_gate('2026-06-09', 'E1') == 'abort'
