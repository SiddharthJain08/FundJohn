import numpy as np
import pandas as pd

from research.bflow import mr_policy as mp


def _bar(m, p, v=1000.0):
    return {"minute": m, "o": p, "h": p + 0.2, "l": p - 0.2,
            "c": p, "v": v, "vw": p}


def _dippy(price=100.0):
    rows = [_bar(m, price) for m in range(390)]
    df = pd.DataFrame(rows)
    for m in range(60, 81):
        df.loc[df["minute"] == m, ["o", "h", "l", "c", "vw"]] = price * 0.95
    return df


def test_simulate_session_rows_full_grid():
    frames = {"AAA": _dippy(), "BBB": _dippy(50.0)}
    rows = mp.simulate_session_rows(frames, session="2024-01-05")
    # 2 tickers x 2 legs x 3 zetas, every eligible pair emits a row
    assert len(rows) == 12
    assert {r["session"] for r in rows} == {"2024-01-05"}
    assert {r["ticker"] for r in rows} == {"AAA", "BBB"}
    assert {(r["leg"], r["zeta"]) for r in rows} == {
        (l, z) for l in mp.LEGS for z in mp.ZETAS}


def test_simulate_session_rows_matches_simulate_pair():
    """The hoisted driver must be row-identical to the unhoisted wrapper."""
    frames = {"AAA": _dippy()}
    rows = mp.simulate_session_rows(frames, session="2024-01-05")
    from research.bflow import oracle
    dump = oracle.dump_benchmark(_dippy().to_dict("records"))
    for r in rows:
        expected = mp.simulate_pair(_dippy(), dump, leg=r["leg"], zeta=r["zeta"])
        for k, v in expected.items():
            if isinstance(v, float) and np.isnan(v):
                assert np.isnan(r[k])
            else:
                assert r[k] == v


def test_simulate_session_rows_skips_no_dump():
    df = _dippy()
    df = df[df["minute"] < 385]          # no dump window -> ineligible
    rows = mp.simulate_session_rows({"AAA": df}, session="2024-01-05")
    assert rows == []


def test_simulate_session_rows_skips_thin_ticker():
    df = _dippy().head(40)               # < 60 valid bars -> floor reject
    rows = mp.simulate_session_rows({"AAA": df}, session="2024-01-05")
    assert rows == []


def test_simulate_session_rows_fallback_parity():
    """Flat tape: z is NaN everywhere (zero trailing sd) -> all 6 rows are
    FALLBACK; the hoisted driver must match simulate_pair on that path too."""
    flat = pd.DataFrame([_bar(m, 100.0) for m in range(390)])
    rows = mp.simulate_session_rows({"AAA": flat}, session="2024-01-05")
    assert len(rows) == 6
    assert all(r["fallback"] for r in rows)
    from research.bflow import oracle
    dump = oracle.dump_benchmark(flat.to_dict("records"))
    for r in rows:
        expected = mp.simulate_pair(flat, dump, leg=r["leg"], zeta=r["zeta"])
        for k, v in expected.items():
            if isinstance(v, float) and np.isnan(v):
                assert np.isnan(r[k])
            else:
                assert r[k] == v


def test_session_delta_records_shape():
    frames = {"AAA": _dippy()}
    recs = mp.session_delta_records(frames, session="2024-01-05")
    df = pd.DataFrame(recs)
    assert set(df.columns) == {"session", "ticker", "minute", "G", "C"}
    # only minutes with finite G are emitted
    assert df["G"].notna().all()
    assert df["minute"].between(0, 388).all()
