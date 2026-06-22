# harness/tests/test_smoke.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, "/root/openclaw/src")
import math
import run_tdom
import inputs


def test_smoke_strided_slice_completes():
    # A representative tiny slice: strided draw across the full window so it
    # spans days/regimes/sides (a same-day head of 30 is leg-thin after the
    # eligible-prune). Design doc Sec.8.7: "~50-cluster dry run completes".
    out = run_tdom.run(window_start="2026-05-04", half_life_mode="cadence",
                       carry_mode="zero", sample=60, seed=0, n_boot=300)
    assert "combined" in out and "min_stop_cumulative" in out["combined"]
    d = out["combined"]["min_stop_cumulative"]
    assert set(["delta", "lo", "hi"]).issubset(d)
    assert out["n_trades"] >= 20


def test_floor_pin_probe_evaluates():
    # floor_pin_probe must prime its OWN price cache (it can run before run()).
    # Strided draw so the probed slice has ATR-rich, eligible clusters.
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv("/root/openclaw/.env")
    conn = psycopg2.connect(os.environ["POSTGRES_URI"])
    try:
        allc = inputs.extract_clusters(conn)
        stride = max(1, len(allc) // 40)
        sample = allc[::stride][:40]
        probe = run_tdom.floor_pin_probe(sample, None, conn, n=40, seed=0)
    finally:
        conn.close()
    assert probe["evaluated"] > 0
    assert math.isfinite(probe["frac_at_floor"])
    assert 0.0 <= probe["frac_at_floor"] <= 1.0
