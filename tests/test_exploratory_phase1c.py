"""Light smoke test for the Phase-1c exploratory menu (NOT a gate — this is
hypothesis-generation tooling). Checks structural invariants, the residual is
orthogonal to flow by construction, and that the B raw-PRICE row matches the A
PRICE row (the built-in alignment check)."""
import numpy as np
import pandas as pd

from src.research.bflow import exploratory_phase1c as p1c


def _toy_ticker(seed):
    """A single valid (ticker, session) frame: 390 minutes, all bars valid."""
    rng = np.random.default_rng(seed)
    n = 390
    c = 100 + np.cumsum(rng.normal(0, 0.05, n))
    o = c + rng.normal(0, 0.01, n)
    spread = np.abs(rng.normal(0, 0.02, n))
    h = np.maximum(o, c) + spread
    l = np.minimum(o, c) - spread
    v = rng.integers(100, 1000, n).astype(float)
    vw = (h + l + c) / 3.0
    return pd.DataFrame({"minute": np.arange(n), "o": o, "h": h, "l": l,
                         "c": c, "v": v, "vw": vw})


def test_residual_orthogonal_and_alignment():
    frames = [p1c._ticker_joint_frame(_toy_ticker(s), 100.0) for s in (1, 2, 3)]
    flow = pd.concat([f[p1c.FLOW] for f in frames], ignore_index=True)
    price = pd.concat([f[p1c.PRICE] for f in frames], ignore_index=True)
    resid = p1c._pooled_ols_residual(price, flow)
    # residual orthogonal to flow on the both-finite set (OLS property)
    m = resid.notna() & flow.notna()
    assert m.sum() > 30
    corr = np.corrcoef(resid[m], flow[m])[0, 1]
    assert abs(corr) < 1e-8, f"residual not orthogonal to flow: corr={corr}"
    # B raw-PRICE IC == A PRICE IC (same object) for every target
    a = p1c._ic_anchor(frames)
    b = p1c._ic_residual(frames)
    for t in p1c.TARGETS:
        av = a[f"A:{p1c.PRICE}->{t}"]
        bv = b[f"B:raw_{p1c.PRICE}->{t}"]
        assert (np.isnan(av) and np.isnan(bv)) or av == bv


def test_quadrants_partition_signed_obs():
    frames = [p1c._ticker_joint_frame(_toy_ticker(s), 100.0) for s in (4, 5)]
    _ics, counts = p1c._ic_quadrant(frames)
    assert set(counts) == {q for q, _, _ in p1c._QUADRANTS}
    # quadrant counts sum to the count of obs where BOTH features are nonzero/finite
    flow = pd.concat([f[p1c.FLOW] for f in frames], ignore_index=True)
    price = pd.concat([f[p1c.PRICE] for f in frames], ignore_index=True)
    signed = ((np.sign(flow) != 0) & (np.sign(price) != 0)
              & flow.notna() & price.notna())
    assert sum(counts.values()) == int(signed.sum())


def test_menu_cell_count():
    c = p1c.count_cells(["LOW_VOL", "TRANSITIONING", "HIGH_VOL"])
    assert c["base"] == 36
    assert c["F"] == (6 + 12) * 3
    assert c["total"] == 36 + 54


def test_session_cells_keys_present():
    frames = {"AAA": _toy_ticker(7), "BBB": _toy_ticker(8)}
    dump = {"AAA": 100.0, "BBB": 101.0}
    ics, counts = p1c.session_cells(frames, dump)
    # every section emits its expected cells
    assert any(k.startswith("A:") for k in ics)
    assert any(k.startswith("B:disp_resid") for k in ics)
    assert any(k.startswith("C:") for k in ics)
    assert any(k.startswith("D:f_conf") for k in ics)
    assert any(k.startswith("E:burst") for k in ics)
    assert len(counts) == 4
