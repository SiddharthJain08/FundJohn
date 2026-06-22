"""Recovery driver: finish the T-DOM matrix after the orchestrated run was
LRU-evicted mid arm-3.

Arms 1 & 2 (autocorr_tiered, autocorr_zero) + floor_pin.json are already on disk
and durable. This script runs ONLY the two remaining cadence arms with IDENTICAL
parameters (sample=800, seed=0, n_boot=2000, window_start=2026-05-04), then loads
all four arm JSONs + floor_pin.json and writes REPORT.md via the same
run_tdom._write_report used by main(). Idempotent: re-running recomputes the
cadence arms and rewrites the report. Writes harness/out/DONE last as a sentinel.

Mirrors run_tdom.main()'s cluster flow EXACTLY so the four arms are comparable.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, "/root/openclaw/src")

import psycopg2  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import run_tdom as R  # noqa: E402
import inputs as I  # noqa: E402

OUT = R.OUT_DIR
WINDOW_START = "2026-05-04"
SAMPLE = 800
SEED = 0
N_BOOT = 2000

ARM_NAMES = ("autocorr_tiered", "autocorr_zero", "cadence_tiered", "cadence_zero")


def main():
    load_dotenv("/root/openclaw/.env")
    conn = psycopg2.connect(os.environ["POSTGRES_URI"])
    try:
        # --- mirror main()'s cluster flow exactly ---
        full = I.extract_clusters(conn, window_start=WINDOW_START)
        n_full = len(full)
        if SAMPLE is not None and SAMPLE < len(full):
            stride = max(1, len(full) // SAMPLE)
            clusters = full[::stride][:SAMPLE]
        else:
            clusters = full
        regime_tables = R._load_regime_tables({c.regime for c in clusters})

        cap_info = dict(n_full_clusters=n_full, n_after_cap=len(clusters),
                        sample_cap=SAMPLE,
                        distinct_days_full=len({c.day for c in full}),
                        distinct_days_capped=len({c.day for c in clusters}))

        # --- run ONLY the two missing cadence arms ---
        for hl_mode, carry_mode in (("cadence", "tiered"), ("cadence", "zero")):
            key = f"{hl_mode}_{carry_mode}"
            print(f"[finish] running arm {key} ...", flush=True)
            res = R.run(window_start=WINDOW_START, half_life_mode=hl_mode,
                        carry_mode=carry_mode, seed=SEED, conn=conn,
                        clusters=clusters, regime_tables=regime_tables,
                        n_boot=N_BOOT)
            res["cap_info"] = cap_info
            with open(os.path.join(OUT, f"tdom_{key}.json"), "w") as f:
                json.dump(res, f, indent=2)
            print(f"[finish] arm {key}: n_trades={res['n_trades']} "
                  f"combined_pass={res['combined']['tdom_pass']}", flush=True)

        # --- assemble report from all four arm JSONs ---
        results = {}
        for key in ARM_NAMES:
            with open(os.path.join(OUT, f"tdom_{key}.json")) as f:
                results[key] = json.load(f)
        with open(os.path.join(OUT, "floor_pin.json")) as f:
            probe = json.load(f)
        ci = probe.get("cap_info") or cap_info

        R._write_report(results, probe, ci)
        print("[finish] REPORT.md written", flush=True)

        # consistency cross-check: all four arms should keep the same trades
        ntr = {k: results[k]["n_trades"] for k in ARM_NAMES}
        print(f"[finish] n_trades per arm: {ntr}", flush=True)

        with open(os.path.join(OUT, "DONE"), "w") as f:
            f.write("ok\n")
        print("[finish] DONE", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
