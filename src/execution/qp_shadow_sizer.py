"""Shadow tracking-QP for the regime-blended sizer's cap cascade (B6a).

The live cascade (rule C hurdle, beta budget, scale to lambda*NAV, dust,
per-ticker cap, cluster cap, bench-corr removal, SPY NAV clamp, net cap) is, in
effect, a sequential heuristic for one small QP: stay as close as possible to
the pre-cap target h subject to a gross target, per-ticker caps, a direction
lock and an optional net cap. This module solves that QP and EMITS ONE LINE
comparing it with the cascade. It never routes an order and never raises.

Rulings carried here:
  * R5 (controller): SHADOW ONLY. The QP never chooses direction (sgn = sign(h),
    constraint sgn*x >= 0) and its output is never fed to the executor.
  * D4: gross target G = min(lam_blended*NAV, sum of per-ticker caps), an
    equality; the per-ticker caps use lam_global exactly as the cascade does.
  * NOTES-cvxpy section 5: always ``solver=cp.CLARABEL`` explicitly; the problem
    is DPP (compiled once, re-solved with new Parameter values); tau is a Python
    constant because tau*Parameter-bearing terms break DPP; never
    ``ignore_dpp``.
cvxpy is imported lazily so importing the sizer stays cheap.
"""
from __future__ import annotations

import logging
import math
import os
import time

import numpy as np

logger = logging.getLogger(__name__)

FLAG = 'OPENCLAW_QP_SHADOW'
TAU_ENV = 'OPENCLAW_QP_SHADOW_TAU'
PREFIX = '[qp_shadow]'

_emitted = False
_cache: dict = {}   # single compiled problem: key (n, with_net, tau)


def qp_shadow_enabled() -> bool:
    return os.environ.get(FLAG) == '1'


def resolve_tau() -> float:
    try:
        v = float(os.environ.get(TAU_ENV, ''))
    except (TypeError, ValueError):
        return 0.0
    return v if (math.isfinite(v) and v >= 0.0) else 0.0


def build_caps(target_usd, *, nav, lam_global, gate_net_sharpe, bench_tkrs,
               bench_exempt, bench_frac, cap_active):
    """Per-ticker USD caps mirroring regime_blended_sizer.py:1918-1937 and
    :1972-1979 (benchmark NAV clamp). ``inf`` = uncapped."""
    from execution.regime_blended_sizer import PER_TICKER_CAP_SHARPE_FRAC
    caps = {}
    for t in target_usd:
        cap = math.inf
        gs = gate_net_sharpe.get(t)
        if cap_active and t not in bench_exempt and gs is not None:
            cap = PER_TICKER_CAP_SHARPE_FRAC * (abs(gs) + 1.0) * lam_global * nav
        if t in bench_tkrs:
            cap = min(cap, bench_frac * nav)
        caps[t] = cap
    return caps


def _build_problem(n, with_net, tau):
    import cvxpy as cp
    x = cp.Variable(n)
    P = {
        'h': cp.Parameter(n), 'cur': cp.Parameter(n),
        'cap': cp.Parameter(n, nonneg=True), 'sgn': cp.Parameter(n),
        'G': cp.Parameter(nonneg=True),
    }
    obj = cp.sum_squares(x - P['h'])
    if tau:
        obj = obj + tau * cp.norm1(x - P['cur'])
    cons = [P['sgn'] @ x == P['G'], cp.abs(x) <= P['cap'],
            cp.multiply(P['sgn'], x) >= 0]
    if with_net:
        P['netb'] = cp.Parameter(nonneg=True)
        cons += [cp.sum(x) <= P['netb'], cp.sum(x) >= -P['netb']]
    return cp.Problem(cp.Minimize(obj), cons), P, x


def solve_qp(h, cur, cap, sgn, *, nav, gross_usd, net_frac, tau):
    """Returns (x_usd | None, status, solve_ms). Never raises."""
    t0 = time.perf_counter()
    try:
        import cvxpy as cp
        n = len(h)
        with_net = net_frac is not None
        key = (n, with_net, float(tau))
        if key not in _cache:
            _cache.clear()
            _cache[key] = _build_problem(n, with_net, float(tau))
        prob, P, x = _cache[key]
        g = gross_usd / nav
        capn = np.where(np.isfinite(cap), cap / nav, g)
        P['h'].value = np.asarray(h, float) / nav
        P['cur'].value = np.asarray(cur, float) / nav
        P['cap'].value = np.maximum(capn, 0.0)
        P['sgn'].value = np.asarray(sgn, float)
        P['G'].value = max(g, 0.0)
        if with_net:
            P['netb'].value = max(net_frac * g, 0.0)
        # bounded: a runaway solve must not stall the live trade step; a hit
        # limit surfaces as a non-optimal status / SolverError (status path).
        prob.solve(solver=cp.CLARABEL, max_iter=200, time_limit=2.0)
        st = prob.status
        ms = (time.perf_counter() - t0) * 1000.0
        if st == 'optimal':
            status = 'ok'
        elif st == 'optimal_inaccurate':
            status = 'ok_inaccurate'
        elif st in ('infeasible', 'infeasible_inaccurate'):
            return None, 'infeasible', ms
        else:
            return None, str(st), ms
        if x.value is None:
            return None, 'no_solution', ms
        return np.asarray(x.value, float) * nav, status, ms
    except Exception as exc:  # noqa: BLE001 — shadow must never raise
        return None, f'error:{type(exc).__name__}', (time.perf_counter() - t0) * 1000.0


def format_line(*, n, l1_dist, gross_qp, gross_cascade, spy_qp, spy_cascade,
                solve_ms, status, gross_target, net_frac, tau):
    f = lambda v: 'n/a' if v is None else '%.0f' % v  # noqa: E731
    return ('%s n=%d l1_dist=%s gross_qp=%s gross_cascade=%.0f spy_qp=%s '
            'spy_cascade=%.0f solve_ms=%.1f status=%s gross_target=%.0f '
            'net_cap=%s tau=%g') % (
        PREFIX, n, f(l1_dist), f(gross_qp), gross_cascade, f(spy_qp),
        spy_cascade, solve_ms, status, gross_target,
        'off' if net_frac is None else '%.2f' % net_frac, tau)


def _emit(line):
    global _emitted
    _emitted = True
    try:
        logger.info(line)
        from lib import shadow_log
        shadow_log.record('qp_shadow', line)
    except Exception:  # noqa: BLE001
        pass
    return line


def emit_status(status):
    return _emit(format_line(n=0, l1_dist=None, gross_qp=None, gross_cascade=0.0,
                             spy_qp=None, spy_cascade=0.0, solve_ms=0.0,
                             status=status, gross_target=0.0, net_frac=None,
                             tau=resolve_tau()))


def run_qp_shadow(target_usd, broker, *, nav, lam_nav, caps, bench_tkrs,
                  net_frac, tau, h_usd=None):
    """``h_usd`` = the QP's tracking target (the cascade's post-scale PRE-cap
    dict); ``target_usd`` = the cascade's FINAL targets, used only for the
    gross_cascade / spy_cascade / l1_dist tokens. Default: h_usd = target_usd."""
    try:
        if h_usd is None:
            h_usd = target_usd
        names = [t for t, v in h_usd.items()
                 if v is not None and math.isfinite(v) and v != 0.0]
        if not names or not nav or nav <= 0:
            return emit_status('skipped:no_targets')
        h = np.array([h_usd[t] for t in names], float)
        c = np.array([float(target_usd.get(t) or 0.0) for t in names], float)
        cur = np.array([broker.get(t, 0.0) for t in names], float)
        cap = np.array([caps.get(t, math.inf) for t in names], float)
        cap = np.where(np.isfinite(cap), np.maximum(cap, 0.0), np.inf)
        sgn = np.sign(h)
        # (1 - 1e-9): keep the gross equality strictly interior to sum(cap)
        G = min(float(lam_nav), float(cap.sum()) * (1.0 - 1e-9))
        x, status, ms = solve_qp(h, cur, cap, sgn, nav=nav, gross_usd=G,
                                 net_frac=net_frac, tau=tau)
        isb = np.array([t in bench_tkrs for t in names])
        if x is None:
            l1 = gq = sq = None
        else:
            l1, gq, sq = (float(np.abs(x - c).sum()), float(np.abs(x).sum()),
                          float(np.abs(x[isb]).sum()))
        return _emit(format_line(
            n=len(names), l1_dist=l1, gross_qp=gq,
            gross_cascade=float(np.abs(c).sum()), spy_qp=sq,
            spy_cascade=float(np.abs(c[isb]).sum()), solve_ms=ms, status=status,
            gross_target=G, net_frac=net_frac, tau=tau))
    except Exception as exc:  # noqa: BLE001
        try:
            return emit_status(f'error:{type(exc).__name__}')
        except Exception:  # noqa: BLE001
            return ''


def begin_cycle():
    global _emitted
    _emitted = False


def end_cycle():
    if qp_shadow_enabled() and not _emitted:
        emit_status('skipped:no_targets')
