import numpy as np
import pandas as pd
import pytest

from research.bflow import mr_policy as mp


def _mk_rows(n_sessions, delta=5.0, minute=100, leg="LONG", zeta=1.0,
             gross=None, cost=0.0):
    # gross_at_entry is the LONG-signed G (own session's value in the
    # accumulator); for LONG with cost=0 it equals delta.
    g = delta if gross is None else gross
    return [{"session": f"2024-01-{d:02d}", "ticker": "AAA", "leg": leg,
             "zeta": zeta, "triggered": True, "entry_minute": minute,
             "delta_net_bps": delta, "gross_at_entry": g,
             "cost_at_entry": cost, "fallback": False}
            for d in range(1, n_sessions + 1)]


def _mk_recs(n_sessions, G=5.0, C=0.0, minute=100):
    return [{"session": f"2024-01-{d:02d}", "ticker": "AAA",
             "minute": minute, "G": G, "C": C}
            for d in range(1, n_sessions + 1)]


def test_loso_identity_constant_world():
    """In a constant world (every session identical), LOSO null == the value,
    so excess == 0 exactly. Verifies (sum - own)/(n-1) arithmetic."""
    acc = mp.NullAccumulator()
    acc.add_records(_mk_recs(40))
    scored, excluded = mp.score_rows(_mk_rows(40), acc)
    assert excluded == 0
    assert all(np.isclose(r["excess_bps"], 0.0) for r in scored)


def test_null_floor_excludes_thin():
    acc = mp.NullAccumulator()
    acc.add_records(_mk_recs(10))        # 10 - 1 = 9 LOSO obs < 30
    scored, excluded = mp.score_rows(_mk_rows(10), acc)
    assert excluded == 10
    assert scored == []                  # every row was triggered-and-thin


def test_fallback_excess_is_zero():
    acc = mp.NullAccumulator()
    rows = [{"session": "2024-01-01", "ticker": "AAA", "leg": "LONG",
             "zeta": 1.0, "triggered": False, "entry_minute": None,
             "delta_net_bps": 0.0, "gross_at_entry": float("nan"),
             "cost_at_entry": float("nan"), "fallback": True}]
    scored, excluded = mp.score_rows(rows, acc)
    assert excluded == 0
    assert scored[0]["excess_bps"] == 0.0


def test_short_leg_null_sign():
    """Null for SHORT uses -G - C (not the negated long null)."""
    acc = mp.NullAccumulator()
    acc.add_records(_mk_recs(40, G=5.0, C=1.0))
    rows = _mk_rows(40, delta=-6.0, leg="SHORT", gross=5.0, cost=1.0)
    scored, _ = mp.score_rows(rows, acc)
    # short null = mean(-G - C) over OTHER sessions = -6.0 -> excess 0
    assert all(np.isclose(r["excess_bps"], 0.0) for r in scored)


def test_build_cell_weights():
    rows = (_mk_rows(3, minute=100) +                      # LONG z=1.0 @100 x3
            _mk_rows(2, minute=200, zeta=2.0))             # LONG z=2.0 @200 x2
    rows.append({"session": "2024-01-09", "ticker": "AAA", "leg": "LONG",
                 "zeta": 1.0, "triggered": False, "entry_minute": None,
                 "delta_net_bps": 0.0, "gross_at_entry": float("nan"),
                 "cost_at_entry": float("nan"), "fallback": True})
    w = mp.build_cell_weights(rows)
    assert w[("LONG", 1.0)] == {("AAA", 100): 3}
    assert w[("LONG", 2.0)] == {("AAA", 200): 2}
    assert w[("SHORT", 1.0)] == {}        # fallback rows contribute nothing


def test_pool_p95_adverse_with_own_subtraction():
    """Pool from histogram: values net_long = -1..-100 bps (one per session).
    p95 adverse of the full pool ~ 95; after removing the own value -100 the
    pool is -1..-99 and p95 adverse drops to ~94 (0.1bps quantization)."""
    cell = ("LONG", 1.0)
    weights = {cell: {("AAA", 100): 1}}
    acc = mp.NullAccumulator(cell_weights=weights)
    recs = [{"session": f"s{i}", "ticker": "AAA", "minute": 100,
             "G": -float(i), "C": 0.0} for i in range(1, 101)]
    acc.add_records(recs)
    full = acc.pool_p95_adverse(cell, own_values_bps=[])
    assert abs(full - 95.0) <= 0.5
    loso = acc.pool_p95_adverse(cell, own_values_bps=[-100.0])
    assert abs(loso - 94.0) <= 0.5
    assert loso < full


def test_guardrail_stats_integration():
    cell = ("LONG", 1.0)
    weights = {cell: {("AAA", 100): 1}}
    acc = mp.NullAccumulator(cell_weights=weights)
    acc.add_records([{"session": f"s{i}", "ticker": "AAA", "minute": 100,
                      "G": -float(i % 50), "C": 0.0} for i in range(1, 101)])
    rows = _mk_rows(40, delta=-20.0, gross=-20.0)
    for r in rows:
        r["excess_bps"] = 0.0
    g = mp.guardrail_stats(rows, acc)
    assert set(g) == {cell}
    assert np.isclose(g[cell]["policy_p95_adverse"], 20.0)
    assert np.isfinite(g[cell]["pool_p95_adverse"])


def test_cell_stats_clustered_t():
    """t = mean/(sd/sqrt(n)) over per-session means — registered shape."""
    rows = []
    rng = np.random.default_rng(7)
    for d in range(1, 41):
        rows.append({"session": f"2024-02-{d:02d}", "ticker": "AAA",
                     "leg": "LONG", "zeta": 1.0, "triggered": True,
                     "entry_minute": 100, "delta_net_bps": 5.0,
                     "gross_at_entry": 5.0, "cost_at_entry": 0.0,
                     "fallback": False,
                     "excess_bps": 2.0 + rng.normal(0, 0.5)})
    stats = mp.cell_stats(rows)
    cell = stats[("LONG", 1.0)]
    assert cell["n_sessions"] == 40
    sess_means = pd.DataFrame(rows).groupby("session")["excess_bps"].mean()
    t_expected = sess_means.mean() / (sess_means.std(ddof=1) / np.sqrt(40))
    assert np.isclose(cell["t"], t_expected)


def test_verdict_rules():
    # leg passes: >=2/3 cells with t >= +3 AND guardrail ok
    stats = {("LONG", 1.0): {"t": 3.5, "n_sessions": 800},
             ("LONG", 1.5): {"t": 3.2, "n_sessions": 800},
             ("LONG", 2.0): {"t": 1.0, "n_sessions": 800},
             ("SHORT", 1.0): {"t": -0.5, "n_sessions": 800},
             ("SHORT", 1.5): {"t": 0.2, "n_sessions": 800},
             ("SHORT", 2.0): {"t": 2.9, "n_sessions": 800}}
    guard = {("LONG", 1.0): {"policy_p95_adverse": 40.0, "pool_p95_adverse": 35.0},
             ("LONG", 1.5): {"policy_p95_adverse": 60.0, "pool_p95_adverse": 35.0},
             ("LONG", 2.0): {"policy_p95_adverse": 40.0, "pool_p95_adverse": 35.0},
             ("SHORT", 1.0): {"policy_p95_adverse": 30.0, "pool_p95_adverse": 35.0},
             ("SHORT", 1.5): {"policy_p95_adverse": 30.0, "pool_p95_adverse": 35.0},
             ("SHORT", 2.0): {"policy_p95_adverse": 30.0, "pool_p95_adverse": 35.0}}
    v = mp.leg_verdicts(stats, guard)
    # LONG: 2 cells pass t-bar, but cell (LONG,1.5) breaches 35+10 < 60
    assert v["LONG"] == "PASS-WITH-TAIL-BREACH"
    assert v["SHORT"] == "FAIL"
    guard[("LONG", 1.5)]["policy_p95_adverse"] = 44.0   # within 35+10
    assert mp.leg_verdicts(stats, guard)["LONG"] == "PASS"


def test_guardrail_uses_all_triggered_including_thin_null():
    """Spec §3: the >=30-obs floor excludes entries from SCORING only — the
    guardrail must run on ALL triggered entries. A thin-null ticker's entries
    must appear in both guardrail sides (this was the final-review blocker)."""
    cell = ("LONG", 1.0)
    # AAA: thick null (40 sessions @ minute 100, delta -10)
    rows = _mk_rows(40, delta=-10.0, gross=-10.0, minute=100)
    # BBB: thin null (10 sessions @ minute 200, delta -50) -> excluded from scoring
    for d in range(1, 11):
        rows.append({"session": f"2024-03-{d:02d}", "ticker": "BBB",
                     "leg": "LONG", "zeta": 1.0, "triggered": True,
                     "entry_minute": 200, "delta_net_bps": -50.0,
                     "gross_at_entry": -50.0, "cost_at_entry": 0.0,
                     "fallback": False})
    weights = mp.build_cell_weights(rows)
    assert weights[cell][("BBB", 200)] == 10
    acc = mp.NullAccumulator(cell_weights=weights)
    acc.add_records(_mk_recs(40, G=-10.0, minute=100))                 # AAA
    acc.add_records([{"session": f"2024-03-{d:02d}", "ticker": "BBB",
                      "minute": 200, "G": -50.0, "C": 0.0}
                     for d in range(1, 11)])                           # BBB
    scored, excluded = mp.score_rows(rows, acc)
    assert excluded == 10                      # BBB thin -> out of scoring
    g_all = mp.guardrail_stats(rows, acc)      # the CORRECT call (all rows)
    g_scored = mp.guardrail_stats(scored, acc) # the buggy pre-fix call
    # policy tail must reflect BBB's -50s: 10/50 triggered entries at -50
    # -> 5th percentile of all-triggered deltas = -50 -> p95_adverse = 50
    assert np.isclose(g_all[cell]["policy_p95_adverse"], 50.0)
    # the scored-only set has no BBB -> tail collapses to the -10s
    assert np.isclose(g_scored[cell]["policy_p95_adverse"], 10.0)


def test_diagnostics_shapes():
    rows = _mk_rows(40, delta=-20.0, gross=-20.0)
    for r in rows:
        r["excess_bps"] = 0.0
    rows.append({"session": "2024-01-01", "ticker": "BBB", "leg": "LONG",
                 "zeta": 1.0, "triggered": False, "entry_minute": None,
                 "delta_net_bps": 0.0, "gross_at_entry": float("nan"),
                 "cost_at_entry": float("nan"), "fallback": True,
                 "excess_bps": 0.0})
    d = mp.diagnostics(rows)
    cell = d[("LONG", 1.0)]
    assert 0.0 < cell["fallback_rate"] < 1.0
    assert cell["p_adverse_5"] == 1.0     # all triggered deltas are -20 < -5
    assert cell["p_adverse_25"] == 0.0
    assert len(cell["entry_minute_deciles"]) == 3
    assert "buckets" in cell
