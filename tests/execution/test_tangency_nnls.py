import importlib, importlib.util, math
from pathlib import Path
import numpy as np, pytest
og = importlib.import_module("execution.orthogonalization")

def _pd_sim(rng, n):
    A = rng.standard_normal((n, n)); C = A @ A.T
    d = np.sqrt(np.diag(C)); C = C / np.outer(d, d)
    ids = [f"s{i}" for i in range(n)]
    return ids, {ids[i]: {ids[j]: float(C[i, j]) for j in range(n) if j != i} for i in range(n)}

def test_nnls_matches_enum_oracle():
    rng = np.random.default_rng(3)
    for _ in range(300):
        n = int(rng.integers(2, 9)); ids, sim = _pd_sim(rng, n)
        s = [float(v) for v in rng.uniform(0.05, 2.0, n)]
        e, de = og._tangency_side_sharpe_enum(ids, s, sim, 0.10)
        q, dq = og._tangency_side_sharpe_nnls(ids, s, sim, 0.10)
        assert abs(e - q) < 1e-9 and not de and not dq

def test_nnls_removes_top10_truncation():
    ids = [f"s{i}" for i in range(25)]
    sim = {a: {b: 0.0 for b in ids if b != a} for a in ids}
    q, _ = og._tangency_side_sharpe_nnls(ids, [1.0] * 25, sim, 0.0)
    e, _ = og._tangency_side_sharpe_enum(ids, [1.0] * 25, sim, 0.0)
    assert q == pytest.approx(5.0, rel=1e-9) and e == pytest.approx(math.sqrt(10), rel=1e-9)

def test_nnls_non_psd_fallback_floor_and_degraded():
    sim = {"a": {"b": 0.9, "c": -0.9}, "b": {"a": 0.9, "c": 0.9}, "c": {"a": -0.9, "b": 0.9}}
    q, deg = og._tangency_side_sharpe_nnls(["a", "b", "c"], [1.2, 1.1, 1.0], sim, 0.10)
    assert math.isfinite(q) and q >= 1.2 - 1e-9 and deg is True

def test_all_nonpositive_side_returns_max():
    q, _ = og._tangency_side_sharpe_nnls(["a", "b"], [-0.5, -0.2], {}, 0.10)
    assert q == pytest.approx(-0.2)

def test_dispatch_default_is_enum_byte_identical(monkeypatch):
    monkeypatch.delenv("OPENCLAW_TANGENCY_NNLS", raising=False)
    ids, sim = _pd_sim(np.random.default_rng(1), 6); s = [0.3, 1.1, 0.7, 0.9, 1.4, 0.2]
    assert og._tangency_side_sharpe(ids, s, sim, 0.10) == og._tangency_side_sharpe_enum(ids, s, sim, 0.10)
    monkeypatch.setenv("OPENCLAW_TANGENCY_NNLS", "1")
    assert og._tangency_side_sharpe(ids, s, sim, 0.10) == og._tangency_side_sharpe_nnls(ids, s, sim, 0.10)

_spec = importlib.util.spec_from_file_location(
    "_tangency_invariants", Path(__file__).with_name("test_tangency_sadj.py"))
_INV = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_INV)

@pytest.mark.parametrize("name", sorted(n for n in dir(_INV) if n.startswith("test_")))
def test_invariants_hold_under_nnls(name, monkeypatch):
    monkeypatch.setenv("OPENCLAW_TANGENCY_NNLS", "1")
    fn = getattr(_INV, name)
    fn(monkeypatch) if fn.__code__.co_argcount else fn()


def test_diag_maxdiff_tiny_and_sadj_unchanged(monkeypatch):
    monkeypatch.delenv("OPENCLAW_TANGENCY_NNLS", raising=False)
    w = {"a": 1.43, "b": 1.27, "c": 0.78, "d": 0.9}
    sim = {"a": {"b": 0.29, "c": 0.33, "d": 0.1}, "b": {"c": 0.58, "d": 0.2}, "c": {"d": 0.4}}
    contribs = {"ZZTEST1": [("a", 1), ("b", 1), ("c", 1)], "ZZTEST2": [("a", 1), ("d", -1), ("c", -1)]}
    diag = {}
    out_d, nb_d = og.tangency_net_sharpe(contribs, sim, w, diag=diag)
    assert (out_d, nb_d) == og.tangency_net_sharpe(contribs, sim, w)
    assert diag["n_sides"] == 2 and diag["maxdiff"] < 1e-9      # ZZTEST1 long (3), ZZTEST2 short (2)
    assert og.tangency_diag_token(diag).startswith("tangency_nnls_maxdiff=")
    assert float(og.tangency_diag_token(diag).split("=")[1]) < 1e-9

def test_diag_token_flag_on_and_error(monkeypatch):
    monkeypatch.setenv("OPENCLAW_TANGENCY_NNLS", "1")
    diag = {}
    og.tangency_net_sharpe({"ZZTEST1": [("a", 1), ("b", 1)]}, {}, {"a": 1.0, "b": 0.8}, diag=diag)
    assert og.tangency_diag_token(diag) == "tangency_mode=nnls"
    monkeypatch.delenv("OPENCLAW_TANGENCY_NNLS")
    monkeypatch.setattr(og, "_tangency_side_sharpe_nnls", lambda *a, **k: 1 / 0)
    diag = {}
    out, _ = og.tangency_net_sharpe({"ZZTEST1": [("a", 1), ("b", 1)]}, {}, {"a": 1.0, "b": 0.8}, diag=diag)
    assert og.tangency_diag_token(diag) == "tangency_nnls_maxdiff=error:ZeroDivisionError" and out["ZZTEST1"] > 1.0
    assert og.tangency_diag_token({}) == "tangency_nnls_maxdiff=n/a"


def test_nnls_nan_sim_falls_back_to_enum_never_raises():
    sim = {"a": {"b": float("nan"), "c": 0.2}, "b": {"c": 0.1}}
    s = [1.2, 1.1, 0.9]
    q = og._tangency_side_sharpe_nnls(["a", "b", "c"], s, sim, 0.10)
    assert q == og._tangency_side_sharpe_enum(["a", "b", "c"], s, sim, 0.10)


def test_nnls_matches_enum_mixed_sign_gamma():
    rng = np.random.default_rng(11)
    for _ in range(200):
        n = int(rng.integers(2, 8)); ids, sim = _pd_sim(rng, n)
        s = [float(v) for v in rng.uniform(-1.0, 2.0, n)]
        e, _ = og._tangency_side_sharpe_enum(ids, s, sim, 0.10, gamma=0.3)
        q, _ = og._tangency_side_sharpe_nnls(ids, s, sim, 0.10, gamma=0.3)
        assert abs(e - q) < 1e-9


def test_diag_flag_off_truncation_gives_positive_maxdiff(monkeypatch):
    monkeypatch.delenv("OPENCLAW_TANGENCY_NNLS", raising=False)
    ids = [f"s{i}" for i in range(25)]
    w = {i: 1.0 for i in ids}
    diag = {}
    og.tangency_net_sharpe({"ZZTEST1": [(i, 1) for i in ids]}, {}, w, shrink=0.0, diag=diag)
    assert diag["maxdiff"] > 0.1
    assert float(og.tangency_diag_token(diag).split("=")[1]) > 0.1
