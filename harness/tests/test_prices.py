# harness/tests/test_prices.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import prices, math


def test_slice_is_small_and_correct():
    out = prices.load_daily({"AAPL"}, "2026-05-01", "2026-06-01")
    assert "AAPL" in out and len(out["AAPL"]) > 5
    df = out["AAPL"]
    assert set(["high", "low", "close"]).issubset({c.lower() for c in df.columns})
    assert df["close"].notna().all()


def test_atr_positive_and_bounded():
    # Real-data adaptation: the panel's AAPL slice has a data gap before 2026-04-10,
    # so a 2026-03-01 start yields <20 bars up to as_of 2026-05-04. Widen the load
    # window to 2026-01-01 so >=20 bars precede the as_of date (51 bars verified).
    out = prices.load_daily({"AAPL"}, "2026-01-01", "2026-06-01")
    a = prices.atr(out["AAPL"], n=20, as_of="2026-05-04")
    assert a > 0 and math.isfinite(a)


def test_atr_nan_when_insufficient():
    out = prices.load_daily({"AAPL"}, "2026-05-28", "2026-06-01")
    assert math.isnan(prices.atr(out["AAPL"], n=20))
