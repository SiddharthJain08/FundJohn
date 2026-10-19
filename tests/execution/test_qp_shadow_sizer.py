import math
import re
import time

import numpy as np
import pytest

from execution import qp_shadow_sizer as q

NAV = 100_000.0
T1, T2, T3, B = 'ZZTEST1', 'ZZTEST2', 'ZZTEST3', 'ZZBENCH'


def _run(h, caps, lam_nav=100_000.0, net=None, tau=0.0, cur=None, bench=()):
    return q.run_qp_shadow(h, cur or {}, nav=NAV, lam_nav=lam_nav, caps=caps,
                           bench_tkrs=set(bench), net_frac=net, tau=tau)


def _f(line, key):
    return re.search(r'\b%s=(\S+)' % key, line).group(1)


def _x(h, caps, lam_nav=100_000.0, net=None, tau=0.0, cur=None):
    names = list(h)
    hv = np.array([h[t] for t in names]); cv = np.array([(cur or {}).get(t, 0.0) for t in names])
    cap = np.array([caps[t] for t in names])
    G = min(lam_nav, cap.sum())
    x, st, _ = q.solve_qp(hv, cv, cap, np.sign(hv), nav=NAV, gross_usd=G, net_frac=net, tau=tau)
    return x, st, G


def test_problem_is_dpp():
    pytest.importorskip('cvxpy')
    assert q._build_problem(5, True, 0.0)[0].is_dpp()
    assert q._build_problem(5, True, 0.02)[0].is_dpp()
    assert q._build_problem(5, False, 0.02)[0].is_dpp()


def test_redistributes_shaved_mass():
    h = {T1: 30e3, T2: 20e3}
    x, st, _ = _x(h, {T1: 60e3, T2: 60e3})
    assert st == 'ok' and abs(x[0] - 55e3) < 1 and abs(x[1] - 45e3) < 1
    line = _run(h, {T1: 60e3, T2: 60e3})
    assert 'status=ok' in line and _f(line, 'gross_qp') == '100000'


def test_caps_bind():
    x, st, _ = _x({T1: 30e3, T2: 20e3}, {T1: 40e3, T2: 60e3})
    assert abs(x[0] - 40e3) < 1 and abs(x[1] - 60e3) < 1


def test_gross_target_clipped_to_sum_caps():
    line = _run({T1: 30e3, T2: 20e3}, {T1: 30e3, T2: 30e3})
    assert _f(line, 'gross_target') == '60000'
    assert 'status=ok' in line and _f(line, 'gross_qp') == '60000'


def test_sign_lock():
    x, st, _ = _x({T1: 50e3, T2: -10e3}, {T1: 90e3, T2: 90e3})
    assert x[0] >= -1e-6 and x[1] <= 1e-6


def test_net_cap_one_sided_book_reports_infeasible():
    line = _run({T1: 30e3, T2: 20e3, T3: 10e3}, {T1: 60e3, T2: 60e3, T3: 60e3}, net=0.6)
    assert 'status=infeasible' in line and _f(line, 'gross_qp') == 'n/a'


def test_net_cap_mixed_book_respected():
    h = {T1: 60e3, T2: -40e3}
    x, st, G = _x(h, {T1: 90e3, T2: 90e3}, net=0.6)
    assert st == 'ok' and abs(x.sum()) <= 0.6 * G + 1


def test_bench_frac_cap_and_spy_tokens():
    h = {T1: 30e3, B: 70e3}
    caps = q.build_caps(h, nav=NAV, lam_global=1.0, gate_net_sharpe={T1: 1.0},
                        bench_tkrs={B}, bench_exempt={B}, bench_frac=0.5, cap_active=True)
    assert caps[B] == 50e3
    line = _run(h, caps, bench={B})
    assert float(_f(line, 'spy_qp')) <= 50000 and _f(line, 'spy_cascade') == '70000'


def test_build_caps_mirrors_cascade():
    h = {T1: 1.0, T2: 1.0, T3: 1.0}
    kw = dict(nav=NAV, lam_global=1.2, gate_net_sharpe={T1: -2.0, T3: 0.5},
              bench_tkrs=set(), bench_exempt=set(), bench_frac=0.5)
    off = q.build_caps(h, cap_active=False, **kw)
    assert all(math.isinf(v) for v in off.values())
    on = q.build_caps(h, cap_active=True, **kw)
    assert on[T1] == pytest.approx(0.10 * 3.0 * 1.2 * NAV)
    assert math.isinf(on[T2])
    kw.update(bench_tkrs={T3})
    b = q.build_caps(h, cap_active=True, **kw)
    assert b[T3] == min(0.10 * 1.5 * 1.2 * NAV, 0.5 * NAV)


def test_tau_cuts_turnover_keeps_gross():
    h = {T1: 50e3, T2: 30e3, T3: 20e3}
    cur = {T1: 20e3, T2: 40e3, T3: 40e3}
    caps = {t: 90e3 for t in h}
    x0, _, G = _x(h, caps, cur=cur, tau=0.0)
    x1, st, _ = _x(h, caps, cur=cur, tau=0.02)
    c = np.array([cur[t] for t in h])
    assert st == 'ok' and np.abs(x1 - c).sum() < np.abs(x0 - c).sum()
    assert np.abs(x1).sum() == pytest.approx(G, abs=1)


def test_solver_exception_reported(monkeypatch):
    import cvxpy
    def boom(self, *a, **k):
        raise cvxpy.error.SolverError('x')
    monkeypatch.setattr(cvxpy.Problem, 'solve', boom)
    line = _run({T1: 30e3, T2: 20e3}, {T1: 60e3, T2: 60e3})
    assert 'status=error:SolverError' in line


def test_line_format_exact():
    line = _run({T1: 30e3, T2: 20e3}, {T1: 60e3, T2: 60e3})
    assert re.match(r'^\[qp_shadow\] n=\d+ l1_dist=\S+ gross_qp=\S+ gross_cascade=\S+ '
                    r'spy_qp=\S+ spy_cascade=\S+ solve_ms=\S+ status=\S+ gross_target=\S+ '
                    r'net_cap=\S+ tau=\S+$', line)


def test_tau_env_parsing(monkeypatch):
    for raw, want in [('', 0.0), ('abc', 0.0), ('-1', 0.0), ('0.02', 0.02)]:
        monkeypatch.setenv(q.TAU_ENV, raw)
        assert q.resolve_tau() == want
    monkeypatch.delenv(q.TAU_ENV)
    assert q.resolve_tau() == 0.0


def _count(tmp_path):
    p = tmp_path / 'qp_shadow.log'
    return p.read_text().splitlines() if p.exists() else []


def test_end_cycle_emits_skipped_once(monkeypatch, tmp_path):
    monkeypatch.setenv(q.FLAG, '1')
    q.begin_cycle(); q.end_cycle()
    lines = _count(tmp_path)
    assert len(lines) == 1 and 'status=skipped:no_targets' in lines[0]
    q.begin_cycle(); _run({T1: 30e3}, {T1: 60e3}); q.end_cycle()
    assert len(_count(tmp_path)) == 2
    q.begin_cycle(); q.emit_status('error:X'); q.end_cycle()
    assert len(_count(tmp_path)) == 3
    monkeypatch.setenv(q.FLAG, '0')
    q.begin_cycle(); q.end_cycle()
    assert len(_count(tmp_path)) == 3


def test_150_names_solve_time():
    rng = np.random.default_rng(0)
    n = 150
    h = {f'ZZ{i}': float(v) for i, v in enumerate(rng.standard_normal(n) * 2000)}
    caps = {t: 4000.0 for t in h}
    _run(h, caps)  # compile
    t = time.time(); line = _run(h, caps, net=0.6)
    assert 'status=ok' in line and time.time() - t < 5
    print('150-name solve_ms', _f(line, 'solve_ms'))
