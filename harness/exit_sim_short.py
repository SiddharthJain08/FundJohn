"""
Short-position delta over exit_sim.py. Implements ONLY the changes from the
main spec needed for shorts, and verifies the two new invariants T-SYM and
T-CARRY. Everything not redefined here is inherited unchanged: the simulator
runs in P&L-excursion space and is sign-agnostic.

Deltas:
  D1  direction enters the price mapping only (excursion dynamics unchanged)
  D2  per-bar carry: effective drift = alpha(t) + carry, carry < 0 for a short
  D3  decay baseline: alpha decays toward 0, so effective drift decays toward
      carry (negative for a short) -> time-stop fires sooner
"""
import numpy as np
import exit_sim as e


def combined_quantities(strategies, ctx, config):
    cq = e.combined_quantities(strategies, ctx, config)
    carry = float(getattr(ctx, "carry_per_bar", 0.0))   # D2: per-bar holding carry
    cq["carry"] = carry
    cq["direction"] = int(getattr(strategies[0], "direction", 1))
    # augmented drift arrays = alpha components + one constant carry component (D3)
    cq["amp_d"] = np.append(cq["amp"], carry)
    cq["half_d"] = np.append(cq["half"], 1e9)           # ~constant component
    base_amp, base_half = cq["amp"], cq["half"]
    cq["mu"] = lambda t: float(np.sum(base_amp * np.power(2.0, -t / base_half)) + carry)
    return cq


def cumulative_move(cq, t):                              # D2: add carry drift
    return e.cumulative_move(cq, t) + cq.get("carry", 0.0) * t


def take_ladder(strategies, cq, ctx, config, T_max):
    # identical to base, except b_k uses the carry-adjusted cumulative move
    n = config.n_tranches or len(strategies)
    sigma, amp, half = cq["sigma"], cq["amp"], cq["half"]
    hurdle = max(ctx.hurdle_g_star, config.take_hurdle_floor) * sigma
    order = np.argsort(half)
    takes = []
    for k in range(min(n, len(strategies))):
        idx = int(order[k])
        Ak = amp[idx]
        t_k = 0.0 if Ak <= hurdle else half[idx] * np.log2(Ak / hurdle)
        t_k = float(np.clip(t_k, 0.0, T_max))
        takes.append(dict(tranche=k + 1, t=t_k, b=cumulative_move(cq, t_k), frac=float(amp[idx])))
    tot = sum(tk["frac"] for tk in takes)
    for tk in takes:
        tk["frac"] /= tot
    takes.sort(key=lambda d: d["b"])
    return takes


def monte_carlo(cq, a, takes, T_exit, ctx, config):
    # copy of base MC, drift built from the carry-augmented components (amp_d/half_d)
    rng = np.random.default_rng(config.seed)
    dt, sigma, cost = config.mc_dt, cq["sigma"], ctx.txn_cost
    M, nsteps = config.mc_paths, (int(np.ceil(T_exit / dt)) if T_exit > 0 else 0)
    ts = np.arange(nsteps) * dt
    drift_inc = (cq["amp_d"][:, None] * np.power(2.0, -ts[None, :] / cq["half_d"][:, None])).sum(0) * dt
    sig_inc = sigma * np.sqrt(dt)
    tb = np.array([t["b"] for t in takes]); tt = np.array([t["t"] for t in takes])
    tf = np.array([t["frac"] for t in takes]); K = len(takes)
    X = np.zeros(M); rem = np.ones(M); taken = np.zeros((M, K), bool)
    pnl = np.zeros(M); exit_t = np.full(M, float(T_exit)); stopped = np.zeros(M, bool)
    hit = np.zeros(K); idx = np.arange(M)
    for step in range(nsteps):
        if idx.size == 0:
            break
        t_now = (step + 1) * dt
        X[idx] += drift_inc[step] + sig_inc * rng.standard_normal(idx.size)
        sm = X[idx] <= -a; s_ids = idx[sm]
        pnl[s_ids] += -a * rem[s_ids]; exit_t[s_ids] = t_now; stopped[s_ids] = True; rem[s_ids] = 0.0
        cur = idx[~sm]
        for k in range(K):
            trig = (~taken[cur, k]) & ((X[cur] >= tb[k]) | (t_now >= tt[k]))
            t_ids = cur[trig]
            pnl[t_ids] += tb[k] * tf[k]; rem[t_ids] -= tf[k]; taken[t_ids, k] = True; hit[k] += t_ids.size
        fin = cur[rem[cur] <= 1e-9]; exit_t[fin] = t_now
        idx = cur[rem[cur] > 1e-9]
    rm = rem > 1e-9
    pnl[rm] += X[rm] * rem[rm]; pnl -= cost
    return dict(E_pnl=float(pnl.mean()), E_tau=float(exit_t.mean()), Var_pnl=float(pnl.var()),
                stopout_prob=float(stopped.mean()), take_hit_probs=(hit / M).tolist(), pnl=pnl, tau=exit_t)


def generate_exit_policy(strategies, ctx, config=None):
    config = config or e.Config()
    if config.n_tranches is None:
        config.n_tranches = len(strategies)
    cq = combined_quantities(strategies, ctx, config)
    d = cq["direction"]                                  # D1: +1 long, -1 short
    T_exit = e.time_stop(cq, ctx, config)
    if not np.isfinite(T_exit) or T_exit > 1e6:
        T_exit = 50.0 * float(max(cq["half"].min(), 1.0))
    takes = take_ladder(strategies, cq, ctx, config, T_max=T_exit)
    lo, hi, step = config.a_grid
    b_ref = takes[-1]["b"] if takes else cq["sigma"]
    best = None
    for am in np.arange(lo, hi + 1e-9, step):
        a = am * cq["sigma"]
        tau_guess = max(abs(a * b_ref) / cq["sigma"] ** 2, 1.0)
        a = max(a, config.k_min * cq["sigma"] * np.sqrt(tau_guess))
        sim = monte_carlo(cq, a, takes, T_exit, ctx, config)
        G = e.growth(sim, cq, ctx, config)
        if best is None or G > best["G"]:
            best = dict(a=float(a), a_mult=float(am), sim=sim, G=float(G))
    ep = ctx.entry_price
    return dict(
        stop=dict(distance=best["a"], price=ep - d * best["a"]),                 # D1
        takes=[dict(tranche=t["tranche"], fraction=t["frac"], distance=t["b"],
                    price=ep + d * t["b"], time_bars=t["t"]) for t in takes],     # D1
        time_stop=dict(bars=T_exit),
        diagnostics=dict(direction=d, carry=cq["carry"], mu0=cq["mu0"], sigma_eff=cq["sigma"],
                         S_comb=cq["S_comb"], E_tau=best["sim"]["E_tau"], growth_G=best["G"],
                         E_pnl_net=best["sim"]["E_pnl"],
                         a_mult=best["a_mult"], stopout_prob=best["sim"]["stopout_prob"]),
    )


# --------------------------------------------------------------------------- #
# New validation invariants
# --------------------------------------------------------------------------- #
def _ens(half_lives, direction, sharpe=1.0, sigma=1.0, **ctx_kw):
    strat = [e.Strategy(f"s{i}", sharpe=sharpe, half_life=h, direction=direction)
             for i, h in enumerate(half_lives)]

    class C(e.Context):
        pass
    ctx = e.Context(C=np.eye(len(half_lives)), sigma_underlying=sigma, **ctx_kw)
    return strat, ctx


def test_sym():
    """T-SYM: with zero carry, a short is the mirror of a long. Excursion-space
    quantities (stop/take distances, time-stop) are identical; only prices flip
    about entry."""
    cfg = e.Config(mc_paths=40_000, mc_dt=0.5, seed=11, a_grid=(0.5, 5.0, 0.5))
    sL, cL = _ens([6, 12, 24], +1, hurdle_g_star=0.03, entry_price=100.0)
    sS, cS = _ens([6, 12, 24], -1, hurdle_g_star=0.03, entry_price=100.0)
    setattr(cL, "carry_per_bar", 0.0); setattr(cS, "carry_per_bar", 0.0)
    L = generate_exit_policy(sL, cL, cfg); S = generate_exit_policy(sS, cS, cfg)
    dL, dS = L["stop"]["distance"], S["stop"]["distance"]
    print(f"[T-SYM  ] stop dist  long={dL:.4f}  short={dS:.4f}  (equal)")
    print(f"[T-SYM  ] stop price long={L['stop']['price']:.3f}  short={S['stop']['price']:.3f}  (mirror of 100)")
    assert abs(dL - dS) < 1e-9
    assert abs((L["stop"]["price"] - 100.0) + (S["stop"]["price"] - 100.0)) < 1e-9
    for tL, tS in zip(L["takes"], S["takes"]):
        assert abs(tL["distance"] - tS["distance"]) < 1e-9
        assert abs((tL["price"] - 100.0) + (tS["price"] - 100.0)) < 1e-9
    assert abs(L["time_stop"]["bars"] - S["time_stop"]["bars"]) < 1e-9
    return True


def test_carry():
    """T-CARRY: for a short, raising borrow cost (more negative carry) shortens
    the time-stop and lowers total expected trade PnL, because the effective
    drift decays toward a more negative baseline and crosses the hurdle sooner.
    Per-unit-time growth need NOT fall: exiting sooner frees capital for
    redeployment, which is the hurdle logic working as intended."""
    cfg = e.Config(mc_paths=40_000, mc_dt=0.5, seed=12, a_grid=(0.5, 5.0, 0.5))
    out = {}
    for borrow in (0.000, 0.010, 0.030):
        s, c = _ens([6, 12, 24], -1, sharpe=1.0, sigma=1.0, hurdle_g_star=0.0)
        setattr(c, "carry_per_bar", -borrow)             # short pays borrow+div
        o = generate_exit_policy(s, c, cfg)
        d = o["diagnostics"]
        out[borrow] = (o["time_stop"]["bars"], d["E_pnl_net"], d["growth_G"])
        print(f"[T-CARRY] borrow={borrow:.3f}  T_exit={out[borrow][0]:6.1f}  "
              f"E[PnL]={out[borrow][1]:+.3f}  G*(per-bar)={out[borrow][2]:+.4f}")
    Ts = [out[b][0] for b in (0.0, 0.01, 0.03)]
    Ps = [out[b][1] for b in (0.0, 0.01, 0.03)]
    assert Ts[0] >= Ts[1] >= Ts[2], ("time-stop did not tighten", Ts)
    assert Ps[0] >= Ps[1] >= Ps[2], ("trade PnL did not fall", Ps)
    return True


if __name__ == "__main__":
    r = {}
    for name, fn in [("T-SYM", test_sym), ("T-CARRY", test_carry)]:
        try:
            r[name] = fn()
        except AssertionError as ex:
            r[name] = False; print("  FAILED", name, ex)
    print("\nSummary:", {k: ("PASS" if v else "FAIL") for k, v in r.items()})
