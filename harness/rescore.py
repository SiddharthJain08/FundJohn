"""Re-score the T-DOM gate arm under corrected growth metrics.

The committed run scored G with R = pnl/sigma (ATR units) + log clip at 1e-6.
With ~49% stop-outs at wide floor-pinned stops, R <= -2 there -> 1+phi*R <= 0 ->
clipped to ln(1e-6) = -13.8, which the WIDEST-stop policy (ensemble) eats hardest.
Spec §4 actually defines R as return on RISK CAPITAL (= stop distance), so a full
stop-out is R = -1 (ln 0.5 = -0.69, never clipped). This re-derives the verdict
under both normalizations x {log, mean-variance}, plus per-policy clip rates, on
the SAME 800-cluster strided sample / seed as the committed gate arm.

Point estimates (G matrix + clip rates) are written FIRST (decisive + instant);
the slower day-block bootstrap is appended after. flush=True throughout.

Run: cd <worktree> && nice -n 19 python3 harness/rescore.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(__file__))  # harness/ importable
import numpy as np
import run_tdom as R
import growth as G

OUT = os.path.join(os.path.dirname(__file__), "out")
DUMP = os.path.join(OUT, "trades_autocorr_tiered.json")
RESULT = os.path.join(OUT, "rescore_autocorr_tiered.json")
POLICY = ["ensemble", "min_stop_cumulative", "conf_weighted_atr", "current_live_v2"]
GATES = ["min_stop_cumulative", "conf_weighted_atr"]


def p(*a):
    print(*a, flush=True)


def main():
    if not os.path.exists(DUMP):
        p("re-running gate arm (autocorr/tiered, sample=800, mc_paths=2000) -> dump ...")
        R.run(half_life_mode="autocorr", carry_mode="tiered", sample=800,
              mc_paths=2000, seed=0, n_boot=1, dump_trades=DUMP)
    d = json.load(open(DUMP))
    recs, side = d["recs"], np.array(d["side"], int)
    p(f"dump loaded: n={len(side)} trades, arm={d['arm']}")

    report = {"arm": d["arm"], "splits": {}}

    # ---- Pass 1: point estimates (instant, decisive) ----
    for split, mask in (("combined", np.ones(len(side), bool)),
                        ("long", side == 1), ("short", side == -1)):
        idx = np.where(mask)[0]
        sub = {k: [recs[k][i] for i in idx] for k in POLICY}
        n = len(idx)
        p(f"\n===== {split}  n={n} =====")
        gmat = {}
        for norm in ("sigma", "riskcap"):
            for method in ("log", "mv"):
                gs = {k: G.growth_G(sub[k], norm=norm, method=method) for k in POLICY}
                gmat[f"{norm}/{method}"] = gs
                p(f" G[{norm:7}/{method:3}] " +
                  "  ".join(f"{k[:6]}={gs[k]:+.4f}" for k in POLICY))
        crs = {k: G.clip_rate(sub[k], norm="sigma") for k in POLICY}
        crr = {k: G.clip_rate(sub[k], norm="riskcap") for k in POLICY}
        p(" clip_rate(sigma):   " + "  ".join(f"{k[:6]}={crs[k]:.3f}" for k in POLICY))
        p(" clip_rate(riskcap): " + "  ".join(f"{k[:6]}={crr[k]:.3f}" for k in POLICY))
        report["splits"][split] = dict(n=n, G=gmat, clip_sigma=crs, clip_riskcap=crr,
                                       deltas={})
    with open(RESULT, "w") as f:
        json.dump(report, f, indent=1)
    p(f"\n[partial] point estimates written to {RESULT}")

    # ---- Pass 2: day-block bootstrap on the gate-deciding metric + sigma cross-check ----
    for split in ("combined", "long", "short"):
        idx = np.where({"combined": np.ones(len(side), bool),
                        "long": side == 1, "short": side == -1}[split])[0]
        sub = {k: [recs[k][i] for i in idx] for k in POLICY}
        if len(idx) < 2:
            continue
        for tag, (norm, method) in (("riskcap/log", ("riskcap", "log")),
                                    ("sigma/log", ("sigma", "log"))):
            p(f"\n-- T-DOM ({tag}) {split} ensemble vs baselines --")
            report["splits"][split]["deltas"][tag] = {}
            for b in POLICY[1:]:
                bd = G.bootstrap_delta(sub["ensemble"], sub[b], norm=norm, method=method,
                                       n_boot=1000, seed=0)
                report["splits"][split]["deltas"][tag][b] = bd
                p(f"   vs {b:20} dG={bd['delta']:+.4f} "
                  f"CI[{bd['lo']:+.4f},{bd['hi']:+.4f}] p>0={bd['p_gt0']:.3f}")
            with open(RESULT, "w") as f:
                json.dump(report, f, indent=1)
        gp = all(report["splits"][split]["deltas"]["riskcap/log"][b]["lo"] > 0 for b in GATES)
        report["splits"][split]["tdom_pass_riskcap_log"] = bool(gp)
        p(f"   ==> tdom_pass(riskcap/log, {split}) = {gp}")

    with open(RESULT, "w") as f:
        json.dump(report, f, indent=1)
    p(f"\nwrote {RESULT}")


if __name__ == "__main__":
    main()
