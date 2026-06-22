"""Direction-aware dispatch over the verified reference generators.
Long  -> exit_sim.generate_exit_policy
Short -> exit_sim_short.generate_exit_policy (deltas D1-D3)
Normalizes both into a single Policy dict. Reference modules are NOT edited."""
import exit_sim as e
import exit_sim_short as es

Policy_keys = ("stop_dist", "takes", "time_stop_bars", "direction", "diagnostics")

def generate(strategies, ctx, config=None):
    config = config or e.Config()
    dirs = {int(getattr(s, "direction", 1)) for s in strategies}
    if len(dirs) != 1:
        raise ValueError("mixed-sign ensemble: refuse to net long and short (spec A-7)")
    d = dirs.pop()
    raw = (es.generate_exit_policy if d == -1 else e.generate_exit_policy)(strategies, ctx, config)
    diag = raw.get("diagnostics", {})
    return dict(
        stop_dist=float(raw["stop"]["distance"]),
        takes=[dict(distance=float(t["distance"]), fraction=float(t["fraction"]),
                    time_bars=float(t["time_bars"])) for t in raw["takes"]],
        time_stop_bars=float(raw["time_stop"]["bars"]),
        direction=d,
        diagnostics=dict(a_mult=diag.get("a_mult"), stopout_prob=diag.get("stopout_prob"),
                         S_comb=diag.get("S_comb"), mu0=diag.get("mu0"),
                         E_tau=diag.get("E_tau"), kappa_C=diag.get("kappa_C"),
                         fallback_used=diag.get("fallback_used"), carry=diag.get("carry", 0.0)),
    )
