"""Phase-1d mr_policy: delta vectors + simulate_pair.

Synthetic-session conventions copied from tests/test_bflow_flow_policy.py:
a bar dict is {"minute": m, "o":p,"h":p+0.2,"l":p-0.2,"c":p,"v":1000,"vw":p}.
"""
import numpy as np
import pandas as pd
import pytest

from research.bflow import mr_policy as mp
from research.bflow import oracle


def _bar(m, p, v=1000.0):
    return {"minute": m, "o": p, "h": p + 0.2, "l": p - 0.2,
            "c": p, "v": v, "vw": p}


def _flat_session(price=100.0, n=390):
    return pd.DataFrame([_bar(m, price) for m in range(n)])


def test_delta_vectors_flat_session_zero_gross():
    df = _flat_session()
    dump = oracle.dump_benchmark(df.to_dict("records"))
    G, C = mp.delta_vectors(df, dump)
    # flat tape: gross identically 0 at every minute with a valid next bar
    assert np.allclose(G[:-1][np.isfinite(G[:-1])], 0.0)
    # cost differential: identical bars -> entry spread == dump spread -> 0
    assert np.allclose(C[:-1][np.isfinite(C[:-1])], 0.0)
    # minute 389 has no bar 390 -> NaN
    assert np.isnan(G[389]) and np.isnan(C[389])


def test_delta_vectors_long_short_identity():
    df = _flat_session()
    df.loc[df["minute"] == 100, ["o", "h", "l", "c", "vw"]] = 99.0  # dip at 100
    dump = oracle.dump_benchmark(df.to_dict("records"))
    G, C = mp.delta_vectors(df, dump)
    # buying the dip fill at minute 100 means decision minute 99
    assert G[99] > 0
    nl, ns = mp.net_legs(G, C)
    assert np.isclose(nl[99], G[99] - C[99])
    assert np.isclose(ns[99], -G[99] - C[99])


def test_simulate_pair_triggers_on_dip():
    df = _flat_session()
    # carve a deep V: minutes 60..80 fall to 95 then recover
    for m in range(60, 81):
        df.loc[df["minute"] == m, ["o", "h", "l", "c", "vw"]] = 95.0
    dump = oracle.dump_benchmark(df.to_dict("records"))
    row = mp.simulate_pair(df, dump, leg="LONG", zeta=1.0)
    assert row["triggered"] is True
    assert 30 <= row["entry_minute"] <= 383
    assert np.isfinite(row["delta_net_bps"])
    assert np.isfinite(row["gross_at_entry"]) and np.isfinite(row["cost_at_entry"])


def test_simulate_pair_fallback_on_flat():
    df = _flat_session()  # constant tape -> vwap_disp_30 == 0, trailing sd == 0 -> z NaN
    dump = oracle.dump_benchmark(df.to_dict("records"))
    row = mp.simulate_pair(df, dump, leg="LONG", zeta=1.0)
    assert row["triggered"] is False
    assert row["entry_minute"] is None
    assert row["delta_net_bps"] == 0.0   # fallback fills AT the benchmark


def test_simulate_pair_void_trigger_continues_scanning():
    df = _flat_session()
    # First dip ONSET at 60 (95.0); a SECOND deeper drop at minute 120 (90.0).
    # vwap_disp_30 is onset-sensitive (a sustained plateau converges to 0) and
    # an invalid bar NaNs the feature for the next 30 minutes — so the
    # re-trigger needs a FRESH negative-displacement onset after the blackout.
    for m in range(60, 120):
        df.loc[df["minute"] == m, ["o", "h", "l", "c", "vw"]] = 95.0
    for m in range(120, 151):
        df.loc[df["minute"] == m, ["o", "h", "l", "c", "vw"]] = 90.0
    # invalidate the bar right after the first would-be trigger minute
    first = mp.simulate_pair(
        df, oracle.dump_benchmark(df.to_dict("records")), leg="LONG", zeta=1.0
    )["entry_minute"]
    df.loc[df["minute"] == first + 1, "v"] = 0.0   # invalid fill bar -> VOID
    dump = oracle.dump_benchmark(df.to_dict("records"))
    row = mp.simulate_pair(df, dump, leg="LONG", zeta=1.0)
    assert row["triggered"] is True
    assert row["entry_minute"] > first   # scanned past the void


def test_simulate_pair_short_mirror():
    df = _flat_session()
    for m in range(60, 81):   # rip UP -> short trigger
        df.loc[df["minute"] == m, ["o", "h", "l", "c", "vw"]] = 105.0
    dump = oracle.dump_benchmark(df.to_dict("records"))
    row = mp.simulate_pair(df, dump, leg="SHORT", zeta=1.0)
    assert row["triggered"] is True


def test_z_convention_matches_running_z():
    """The z used for triggering MUST be energy_counterfactual.running_z of
    compute_features(df)['vwap_disp_30'] — verbatim, t-inclusive."""
    from research.bflow.energy_counterfactual import running_z
    from research.bflow.flow_features import compute_features
    df = _flat_session()
    for m in range(60, 81):
        df.loc[df["minute"] == m, ["o", "h", "l", "c", "vw"]] = 95.0
    z_expected = running_z(compute_features(df)["vwap_disp_30"])
    z_actual = mp.trigger_z(df)
    pd.testing.assert_series_equal(z_actual, z_expected)
