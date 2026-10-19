import json, math, statistics
import pytest
from execution import account_breaker as ab

def test_reflection_values(monkeypatch):
    monkeypatch.setattr(ab, "nav_daily_sigma", lambda path=None, lookback=60: 0.01)
    assert ab.p_trip_within(-0.06) == pytest.approx(0.383, abs=0.005)    # 4 % headroom, 1 %/d
    assert ab.p_trip_within(-0.04) == pytest.approx(0.19, abs=0.005)     # 6 % headroom
    assert ab.p_trip_within(-0.10) == 1.0 and ab.p_trip_within(-0.12) == 1.0

def test_p_trip_degrades_to_none(monkeypatch, tmp_path):
    assert ab.p_trip_within(None) is None
    days = {f"2026-09-{d:02d}": {"close": 100.0} for d in range(1, 26)}       # zero variance
    (tmp_path / "flat.json").write_text(json.dumps({"days": days}))
    assert ab.nav_daily_sigma(tmp_path / "flat.json") is None
    short = {f"2026-09-{d:02d}": {"close": 100.0 + d} for d in range(1, 20)}  # 19 points
    (tmp_path / "short.json").write_text(json.dumps({"days": short}))
    assert ab.nav_daily_sigma(tmp_path / "short.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert ab.nav_daily_sigma(tmp_path / "bad.json") is None
    assert ab.p_trip_within(-0.05, path=tmp_path / "bad.json") is None

def test_sigma_uses_last_60_returns(tmp_path):
    closes = [100.0 * (1.0 + 0.003 * ((i * 7) % 5 - 2)) ** 1 + i for i in range(80)]
    days = {f"2026-{7 + i // 28:02d}-{i % 28 + 1:02d}": {"close": c} for i, c in enumerate(closes)}
    (tmp_path / "n.json").write_text(json.dumps({"days": days}))
    tail = closes[-61:]
    expect = statistics.stdev([b / a - 1 for a, b in zip(tail, tail[1:])])
    assert ab.nav_daily_sigma(tmp_path / "n.json") == pytest.approx(expect, rel=1e-12)

def test_format_line_token_only_when_passed():
    st = {"dd": -0.05, "daily": None, "rule": "none", "breach": False, "peak": 1.0}
    kw = dict(equity=1.0, bench_mv=0.0, alpha=1.0, st=st, open_equity=1.0, open_src="stored", halted=False)
    assert "p_trip" not in ab.format_line("shadow", **kw)
    assert ab.format_line("shadow", p_trip=None, **kw).endswith(" p_trip_21d~=n/a")
    assert ab.format_line("shadow", p_trip=0.3812, **kw).endswith(" p_trip_21d~=0.38")

def test_both_run_once_lines_pass_p_trip():
    import inspect
    assert inspect.getsource(ab.run_once).count("p_trip=p_trip_within(") == 2


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf')])
def test_p_trip_non_finite_dd_is_none(bad):
    assert ab.p_trip_within(bad) is None
