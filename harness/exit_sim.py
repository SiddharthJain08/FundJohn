"""
Reference simulator for the Ensemble Exit Policy Specification v1.0.

Implements Sections 3 (combined quantities), 5 (generating procedure),
6 (barrier math and Monte Carlo), 7 (output), and 9 (validation harness).

Dependencies: numpy only. Deterministic given a seed.

This is a REFERENCE implementation. It is meant to be a known-good baseline
for an implementing agent to check against, not a production execution engine.
"""

from dataclasses import dataclass, field
from typing import List, Optional
import numpy as np


# --------------------------------------------------------------------------- #
# Data structures (Section 2)
# --------------------------------------------------------------------------- #
@dataclass
class Strategy:
    id: str
    sharpe: float                 # per-bar unless sharpe_horizon overridden upstream
    win_rate: float = 0.5
    avg_win: float = 1.0
    avg_loss: float = 1.0
    half_life: float = 1e12       # bars; default ~constant drift
    confidence: float = 1.0
    direction: int = 1            # +1 long; this spec assumes all +1


@dataclass
class Context:
    C: np.ndarray                 # NxN Jaccard similarity matrix
    sigma_underlying: float       # per-bar volatility, price or ATR units
    entry_price: float = 100.0
    txn_cost: float = 0.0         # round-trip, same units as PnL
    hurdle_g_star: float = 0.0    # redeployment growth rate per bar
    kelly_fraction: float = 0.5   # phi
    session_bars: Optional[float] = None   # None => non-intraday (no session bound)


@dataclass
class Config:
    k_min: float = 1.0
    mc_paths: int = 100_000
    mc_dt: float = 1.0
    a_grid: tuple = (0.5, 5.0, 0.1)        # (lo, hi, step) in sigma_eff units
    n_tranches: Optional[int] = None       # default N
    kappa_max: float = 30.0
    seed: int = 0
    growth_method: str = "mv"              # "mv" (mean-variance) or "log"
    take_hurdle_floor: float = 1e-6        # finite take/time exits when g_star = 0
    session_buffer_bars: float = 0.0
    use_confidence_weights: bool = False


# --------------------------------------------------------------------------- #
# Step A: combined quantities (Section 3)
# --------------------------------------------------------------------------- #
def combined_quantities(strategies: List[Strategy], ctx: Context, config: Config) -> dict:
    S = np.array([s.sharpe for s in strategies], float)
    C = np.asarray(ctx.C, float)
    kappa = float(np.linalg.cond(C))
    fallback = kappa > config.kappa_max

    if config.use_confidence_weights:
        w = np.array([s.confidence for s in strategies], float)
        S_comb = float(np.sqrt(S @ S))
    elif fallback:                          # Section 11.2 guard: drop the inversion
        w = S.copy()
        S_comb = float(np.sqrt(S @ S))
    else:
        w = np.linalg.solve(C, S)
        S_comb = float(np.sqrt(S @ np.linalg.solve(C, S)))

    w = w / np.sum(w)                       # normalize (long ensemble)
    sigma = float(ctx.sigma_underlying)
    m = S * sigma                           # per-bar drift proxy per strategy
    amp = w * m                             # per-component drift amplitude
    half = np.array([s.half_life for s in strategies], float)
    mu0 = float(np.sum(amp))

    def mu(t):                              # mixture of exponentials (Section 3.3)
        return float(np.sum(amp * np.power(2.0, -t / half)))

    return dict(w=w, S_comb=S_comb, sigma=sigma, mu0=mu0,
                amp=amp, half=half, mu=mu, kappa=kappa, fallback=fallback)


def cumulative_move(cq: dict, t: float) -> float:
    """E[X_t] = integral_0^t mu(s) ds for the exponential mixture."""
    amp, half = cq["amp"], cq["half"]
    return float(np.sum(amp * (half / np.log(2.0)) * (1.0 - np.power(2.0, -t / half))))


# --------------------------------------------------------------------------- #
# Step C scaffolding: time stop and take ladder (Section 5.1)
# --------------------------------------------------------------------------- #
def time_stop(cq: dict, ctx: Context, config: Config) -> float:
    """Edge-exhaustion time t* where total drift decays to the hurdle, bounded
    by the session for intraday."""
    sigma = cq["sigma"]
    hurdle = max(ctx.hurdle_g_star, config.take_hurdle_floor) * sigma
    if cq["mu"](0.0) <= hurdle:
        t_star = 0.0
    else:
        lo, hi = 0.0, 1.0
        while cq["mu"](hi) > hurdle and hi < 1e9:
            hi *= 2.0
        for _ in range(200):                # bisection
            mid = 0.5 * (lo + hi)
            if cq["mu"](mid) > hurdle:
                lo = mid
            else:
                hi = mid
        t_star = hi
    if ctx.session_bars is not None:
        return float(min(t_star, ctx.session_bars - config.session_buffer_bars))
    return float(t_star)


def take_ladder(strategies, cq, ctx, config, T_max) -> list:
    """Takes derived from decay, ordered by half-life ascending. NOT from
    constituent targets (principle P3)."""
    n = config.n_tranches or len(strategies)
    sigma, amp, half = cq["sigma"], cq["amp"], cq["half"]
    hurdle = max(ctx.hurdle_g_star, config.take_hurdle_floor) * sigma
    order = np.argsort(half)                # take fastest-decaying first
    takes = []
    for k in range(min(n, len(strategies))):
        idx = int(order[k])
        Ak = amp[idx]
        t_k = 0.0 if Ak <= hurdle else half[idx] * np.log2(Ak / hurdle)
        t_k = float(np.clip(t_k, 0.0, T_max))
        takes.append(dict(tranche=k + 1, t=t_k, b=cumulative_move(cq, t_k),
                          frac=float(amp[idx])))
    tot = sum(tk["frac"] for tk in takes)
    for tk in takes:                        # fractions sum to 1
        tk["frac"] /= tot
    takes.sort(key=lambda d: d["b"])        # ascending price for the MC
    return takes


# --------------------------------------------------------------------------- #
# Section 6: Monte Carlo simulator (authoritative for decaying drift)
# --------------------------------------------------------------------------- #
def monte_carlo(cq, a, takes, T_exit, ctx, config) -> dict:
    rng = np.random.default_rng(config.seed)
    dt, sigma, cost = config.mc_dt, cq["sigma"], ctx.txn_cost
    M = config.mc_paths
    nsteps = int(np.ceil(T_exit / dt)) if T_exit > 0 else 0

    ts_start = np.arange(nsteps) * dt
    drift_inc = (cq["amp"][:, None] *
                 np.power(2.0, -ts_start[None, :] / cq["half"][:, None])).sum(0) * dt
    sig_inc = sigma * np.sqrt(dt)

    take_b = np.array([tk["b"] for tk in takes], float)
    take_t = np.array([tk["t"] for tk in takes], float)
    take_f = np.array([tk["frac"] for tk in takes], float)
    K = len(takes)

    X = np.zeros(M)
    rem = np.ones(M)
    taken = np.zeros((M, K), bool)
    pnl = np.zeros(M)
    exit_t = np.full(M, float(T_exit))
    stopped = np.zeros(M, bool)
    hit = np.zeros(K)
    idx = np.arange(M)                       # active path ids; compacted each step

    for step in range(nsteps):
        if idx.size == 0:
            break
        t_now = (step + 1) * dt
        X[idx] += drift_inc[step] + sig_inc * rng.standard_normal(idx.size)

        stop_mask = X[idx] <= -a             # stop closes full remainder at fill -a
        s_ids = idx[stop_mask]
        pnl[s_ids] += -a * rem[s_ids]
        exit_t[s_ids] = t_now
        stopped[s_ids] = True
        rem[s_ids] = 0.0

        cur = idx[~stop_mask]
        for k in range(K):                   # take by price level b_k or decay time t_k
            trig_mask = (~taken[cur, k]) & ((X[cur] >= take_b[k]) | (t_now >= take_t[k]))
            t_ids = cur[trig_mask]
            pnl[t_ids] += take_b[k] * take_f[k]
            rem[t_ids] -= take_f[k]
            taken[t_ids, k] = True
            hit[k] += t_ids.size

        fin = cur[rem[cur] <= 1e-9]          # fully released
        exit_t[fin] = t_now
        idx = cur[rem[cur] > 1e-9]           # survivors

    rem_mask = rem > 1e-9                     # time-stop: close remainder at mark
    pnl[rem_mask] += X[rem_mask] * rem[rem_mask]
    pnl -= cost                               # one round trip per trade

    return dict(E_pnl=float(pnl.mean()), E_tau=float(exit_t.mean()),
                Var_pnl=float(pnl.var()), stopout_prob=float(stopped.mean()),
                take_hit_probs=(hit / M).tolist(), pnl=pnl, tau=exit_t)


# --------------------------------------------------------------------------- #
# Section 4: growth objective
# --------------------------------------------------------------------------- #
def growth(sim, cq, ctx, config) -> float:
    sigma, phi = cq["sigma"], ctx.kelly_fraction
    R = sim["pnl"] / sigma                   # PnL in vol units; sigma is fixed across candidates
    E_tau = max(sim["E_tau"], 1e-9)
    if config.growth_method == "log":
        arg = np.clip(1.0 + phi * R, 1e-6, None)
        return float(np.mean(np.log(arg)) / E_tau)
    return float((phi * R.mean() - 0.5 * phi * phi * R.var()) / E_tau)   # mean-variance


# --------------------------------------------------------------------------- #
# Section 5: full generating procedure + Section 7 output
# --------------------------------------------------------------------------- #
def optimize_stop(strategies, ctx, config) -> dict:
    cq = combined_quantities(strategies, ctx, config)
    T_exit = time_stop(cq, ctx, config)
    if not np.isfinite(T_exit) or T_exit > 1e6:
        T_exit = 50.0 * float(max(cq["half"].min(), 1.0))
    takes = take_ladder(strategies, cq, ctx, config, T_max=T_exit)

    lo, hi, step = config.a_grid
    b_ref = takes[-1]["b"] if takes else cq["sigma"]
    best = None
    for am in np.arange(lo, hi + 1e-9, step):
        a = am * cq["sigma"]
        tau_guess = max(a * b_ref / cq["sigma"] ** 2, 1.0)        # driftless E[tau] proxy
        a = max(a, config.k_min * cq["sigma"] * np.sqrt(tau_guess))   # A-5 noise-band floor
        sim = monte_carlo(cq, a, takes, T_exit, ctx, config)
        G = growth(sim, cq, ctx, config)
        if best is None or G > best["G"]:
            best = dict(a=float(a), a_mult=float(am), takes=takes,
                        T_exit=float(T_exit), sim=sim, G=float(G))
    best["cq"] = cq
    return best


def generate_exit_policy(strategies, ctx, config=None) -> dict:
    config = config or Config()
    if config.n_tranches is None:
        config.n_tranches = len(strategies)
    best = optimize_stop(strategies, ctx, config)
    cq, sim, ep = best["cq"], best["sim"], ctx.entry_price
    clock = None
    if ctx.session_bars is not None and config.mc_dt > 0:
        clock = f"{best['T_exit']:.1f} bars from entry"
    return dict(
        stop=dict(distance=best["a"], price=ep - best["a"]),
        takes=[dict(tranche=tk["tranche"], fraction=tk["frac"], distance=tk["b"],
                    price=ep + tk["b"], time_bars=tk["t"]) for tk in best["takes"]],
        time_stop=dict(bars=best["T_exit"], clock_time=clock),
        diagnostics=dict(weights=cq["w"].tolist(), mu0=cq["mu0"], sigma_eff=cq["sigma"],
                         S_comb=cq["S_comb"], E_tau=sim["E_tau"], E_pnl_net=sim["E_pnl"],
                         Var_pnl=sim["Var_pnl"], growth_G=best["G"], a_mult=best["a_mult"],
                         stopout_prob=sim["stopout_prob"], take_hit_probs=sim["take_hit_probs"],
                         kappa_C=cq["kappa"], fallback_used=cq["fallback"]),
    )


# --------------------------------------------------------------------------- #
# Section 9: validation harness
# --------------------------------------------------------------------------- #
def test_inv():
    """T-INV: zero cost, constant drift, clean double barrier.
    Optional stopping requires E[PnL] = mu0 * E[tau]."""
    sigma, mu0 = 1.0, 0.05
    strat = [Strategy("s", sharpe=mu0 / sigma, half_life=1e12)]
    ctx = Context(C=np.array([[1.0]]), sigma_underlying=sigma, txn_cost=0.0)
    cfg = Config(mc_paths=300_000, mc_dt=0.03, seed=1)
    cq = combined_quantities(strat, ctx, cfg)
    a = b = 3.0
    takes = [dict(tranche=1, t=1e12, b=b, frac=1.0)]
    sim = monte_carlo(cq, a, takes, T_exit=60.0, ctx=ctx, config=cfg)
    lhs, rhs = sim["E_pnl"], cq["mu0"] * sim["E_tau"]
    rel = abs(lhs - rhs) / (abs(rhs) + 1e-9)
    print(f"[T-INV  ] E[PnL]={lhs:+.4f}  mu0*E[tau]={rhs:+.4f}  rel_err={rel:.3%}")
    assert rel <= 0.03, (lhs, rhs, rel)
    return True


def _ensemble(half_lives, sharpe=1.0, sigma=1.0, **ctx_kw):
    n = len(half_lives)
    strat = [Strategy(f"s{i}", sharpe=sharpe, half_life=h) for i, h in enumerate(half_lives)]
    ctx = Context(C=np.eye(n), sigma_underlying=sigma, **ctx_kw)
    return strat, ctx


def test_cost():
    """T-COST: raising transaction cost strictly lowers optimal growth G*, and
    never tightens the optimal stop (a* is non-decreasing in cost). This is the
    precise refutation of min-stop: cost penalizes short-lived, frequently
    re-entered (tight) stops, so the optimizer can only hold or widen, never
    tighten, when cost rises."""
    cfg = Config(mc_paths=60_000, mc_dt=0.5, seed=2, a_grid=(0.5, 5.0, 0.5))
    s_lo, c_lo = _ensemble([6, 12, 24], hurdle_g_star=0.03)
    s_hi, c_hi = _ensemble([6, 12, 24], hurdle_g_star=0.03, txn_cost=2.5)
    out_lo = generate_exit_policy(s_lo, c_lo, cfg)
    out_hi = generate_exit_policy(s_hi, c_hi, cfg)
    a_lo, G_lo = out_lo["diagnostics"]["a_mult"], out_lo["diagnostics"]["growth_G"]
    a_hi, G_hi = out_hi["diagnostics"]["a_mult"], out_hi["diagnostics"]["growth_G"]
    print(f"[T-COST ] cost=0:   a*_mult={a_lo:.2f}  G*={G_lo:+.4f}")
    print(f"[T-COST ] cost=2.5: a*_mult={a_hi:.2f}  G*={G_hi:+.4f}")
    assert a_hi >= a_lo - 1e-9, ("stop tightened under cost", a_lo, a_hi)
    assert G_hi < G_lo, ("growth did not fall under cost", G_lo, G_hi)
    return True


def test_vol():
    """T-VOL: optimal absolute stop a* scales ~linearly with sigma_eff
    (problem is scale-invariant in sigma units, cost held at 0)."""
    cfg = Config(mc_paths=60_000, mc_dt=0.5, seed=3, a_grid=(0.5, 6.0, 0.5))
    s1, c1 = _ensemble([8, 16, 32], sigma=1.0, hurdle_g_star=0.02)
    s2, c2 = _ensemble([8, 16, 32], sigma=2.0, hurdle_g_star=0.02)
    a1 = generate_exit_policy(s1, c1, cfg)["stop"]["distance"]
    a2 = generate_exit_policy(s2, c2, cfg)["stop"]["distance"]
    ratio = a2 / a1
    print(f"[T-VOL  ] a*(sigma=1)={a1:.3f}  a*(sigma=2)={a2:.3f}  ratio={ratio:.3f} (target ~2)")
    assert 1.7 <= ratio <= 2.3, ratio
    return True


def test_decay():
    """T-DECAY: take time t_k increases with the corresponding strategy's
    half-life (faster decay takes profit sooner)."""
    cfg = Config(mc_paths=20_000, mc_dt=0.5, seed=4)
    strat, ctx = _ensemble([3, 6, 12], sharpe=1.0, sigma=1.0, hurdle_g_star=0.05)
    cq = combined_quantities(strat, ctx, cfg)
    takes = take_ladder(strat, cq, ctx, cfg, T_max=time_stop(cq, ctx, cfg))
    times = [tk["t"] for tk in sorted(takes, key=lambda d: d["b"])]
    print(f"[T-DECAY] take times (half_life 3,6,12) = {[f'{t:.1f}' for t in times]}")
    assert all(times[i] < times[i + 1] for i in range(len(times) - 1)), times
    return True


def test_cond():
    """T-COND: an ill-conditioned C triggers the fallback and the run stays finite."""
    cfg = Config(mc_paths=15_000, mc_dt=0.5, seed=5, a_grid=(0.5, 5.0, 0.5))
    n = 3
    C = np.full((n, n), 0.999)
    np.fill_diagonal(C, 1.0)                 # near-singular
    strat = [Strategy(f"s{i}", sharpe=1.0, half_life=10) for i in range(n)]
    ctx = Context(C=C, sigma_underlying=1.0, hurdle_g_star=0.05)
    out = generate_exit_policy(strat, ctx, cfg)
    print(f"[T-COND ] kappa={out['diagnostics']['kappa_C']:.1f}  "
          f"fallback={out['diagnostics']['fallback_used']}  "
          f"finite={np.isfinite(out['stop']['distance'])}")
    assert out["diagnostics"]["fallback_used"] is True
    assert np.isfinite(out["stop"]["distance"])
    return True


def run_all():
    results = {}
    for name, fn in [("T-INV", test_inv), ("T-COST", test_cost), ("T-VOL", test_vol),
                     ("T-DECAY", test_decay), ("T-COND", test_cond)]:
        try:
            results[name] = fn()
        except AssertionError as e:
            results[name] = False
            print(f"  FAILED {name}: {e}")
    print("\nSummary:", {k: ("PASS" if v else "FAIL") for k, v in results.items()})
    return results


if __name__ == "__main__":
    run_all()
