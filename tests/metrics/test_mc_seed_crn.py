"""B3b: request-derived seed, per-trade CRN streams, seed persistence (synthetic only)."""
import metrics.intraday_path_montecarlo as ipm
import metrics.regime_param_montecarlo as rpm

POOL = [{'ticker': 'ZZTEST1', 'direction': 'LONG'} for _ in range(20)]


class _Gen:
    sigma = 0.005

    def __init__(self, rec):
        self.rec = rec

    def sample_path(self, n_bars, rng):
        path = [rng.gauss(0.0, self.sigma) for _ in range(n_bars)]
        self.rec.append(tuple(path))
        return path


def _stub(monkeypatch, rec=None):
    rec = [] if rec is None else rec
    monkeypatch.setattr(ipm, '_gen_for_ticker',
                        lambda t, window_days=90: (_Gen(rec), 'gbm'))
    return rec


def _run(**kw):
    args = dict(strategy_id='S_zz', regime_state='LOW_VOL', current_size=1.0,
                proposed_size=1.0, proposed_stop_pct=0.02, proposed_target_pct=0.05,
                proposed_max_hold_days=5, n_iter=50, trade_pool=list(POOL))
    args.update(kw)
    return ipm.run_path_mc(**args)


def test_default_seed_reproducible(monkeypatch):
    _stub(monkeypatch)
    a = _run(proposal_id=9)
    b = _run(proposal_id=9)
    assert a == b
    assert a['seed'] == ipm.derive_seed('S_zz', 'LOW_VOL', 9, 0.02, 0.05, 5)


def test_seed_depends_on_proposal():
    assert (ipm.derive_seed('S', 'R', 1, 0.02, 0.05, 5)
            != ipm.derive_seed('S', 'R', 2, 0.02, 0.05, 5))


def test_crn_prefix_stable_across_n_iter(monkeypatch):
    rec = _stub(monkeypatch)
    _run(n_iter=50, seed=123)
    first = list(rec)
    rec.clear()
    _run(n_iter=100, seed=123)
    assert len(first) == 50
    assert rec[:50] == first


class _Cur:
    def __init__(self, store):
        self.store = store

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.store.append((sql, params))
    def fetchone(self): return (1,)


class _Conn:
    def __init__(self, store):
        self.store = store

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return _Cur(self.store)
    def commit(self): pass


def test_persist_run_writes_seed(monkeypatch):
    _stub(monkeypatch)
    cap = []
    monkeypatch.setattr(ipm, '_connect', lambda: _Conn(cap))
    r = _run(proposal_id=3)
    ipm.persist_run(r, proposal_id=3)
    sql, params = cap[0]
    assert 'seed, proposal_id' in sql
    assert params[-2] == r['seed'] and params[-1] == 3


def test_linear_mc_persists_derived_seed(monkeypatch):
    cap = []
    monkeypatch.setattr(rpm, '_load_realized_pnls',
                        lambda *a, **k: [0.01 * ((i % 7) - 3) for i in range(30)])
    monkeypatch.setattr(rpm, '_connect', lambda: _Conn(cap))
    r1 = rpm.bootstrap_size_scalar('S_zz', 'LOW_VOL', 1.0, 1.5, n_iter=50,
                                   seed=None, proposal_id=4)
    assert isinstance(r1['seed'], int)
    assert r1['seed'] in cap[0][1]
    r2 = rpm.bootstrap_size_scalar('S_zz', 'LOW_VOL', 1.0, 1.5, n_iter=50,
                                   seed=None, proposal_id=4)
    assert r1['sharpe_p50'] == r2['sharpe_p50']


def test_crn_paths_invariant_to_policy(monkeypatch):
    rec = _stub(monkeypatch)
    _run(n_iter=50, seed=123)
    base = list(rec)
    for kw in ({'proposed_stop_pct': 0.05},
               {'proposed_target_pct': 0.10, 'proposed_size': 2.0}):
        rec.clear()
        _run(n_iter=50, seed=123, **kw)
        assert rec == base
