"""Truth-table tests for src/research/bflow/oracle.py (pure math, no I/O).

Every expected value here is hand-computed in the comments so the test is a
genuine specification, not a mirror of the implementation. Bars follow the
upstream minbar contract: dict {minute (int 0..389 offset from 09:30 ET),
o, h, l, c, v, vw}.
"""
import math

import pytest

from src.research.bflow import oracle


# --------------------------------------------------------------------------
# small bar-builder helpers
# --------------------------------------------------------------------------
def bar(minute, vw, v=100.0, h=None, l=None, c=None, o=None):
    """Build a bar. Defaults give a zero-spread (h==l==vw) bar so spread_bps=0
    unless overridden."""
    if h is None:
        h = vw
    if l is None:
        l = vw
    if c is None:
        c = vw
    if o is None:
        o = vw
    return {"minute": minute, "o": o, "h": h, "l": l, "c": c, "v": v, "vw": vw}


# ==========================================================================
# valid_bar
# ==========================================================================
def test_valid_bar_requires_positive_vw_v_and_h_ge_l():
    assert oracle.valid_bar(bar(0, vw=10.0, v=5.0, h=11.0, l=9.0)) is True
    # vw <= 0
    assert oracle.valid_bar(bar(0, vw=0.0, v=5.0)) is False
    assert oracle.valid_bar(bar(0, vw=-1.0, v=5.0)) is False
    # v <= 0
    assert oracle.valid_bar(bar(0, vw=10.0, v=0.0)) is False
    assert oracle.valid_bar(bar(0, vw=10.0, v=-3.0)) is False
    # h < l
    assert oracle.valid_bar(bar(0, vw=10.0, v=5.0, h=9.0, l=11.0)) is False
    # h == l is allowed
    assert oracle.valid_bar(bar(0, vw=10.0, v=5.0, h=10.0, l=10.0)) is True


def test_valid_bar_nan_and_none_safe():
    nan = float("nan")
    # NaN vw must NOT pass (nan > 0 is False)
    assert oracle.valid_bar(bar(0, vw=nan, v=5.0)) is False
    # NaN volume must NOT pass
    assert oracle.valid_bar(bar(0, vw=10.0, v=nan)) is False
    # NaN in h/l must NOT pass (h >= l with a nan is False)
    assert oracle.valid_bar(bar(0, vw=10.0, v=5.0, h=nan, l=9.0)) is False
    # None must NOT crash (None > 0 raises TypeError if unguarded) and must be invalid
    assert oracle.valid_bar({"minute": 0, "o": 1, "h": 1, "l": 1, "c": 1,
                             "v": 5.0, "vw": None}) is False
    assert oracle.valid_bar({"minute": 0, "o": 1, "h": 1, "l": 1, "c": 1,
                             "v": None, "vw": 10.0}) is False
    assert oracle.valid_bar({"minute": 0, "o": 1, "h": None, "l": 1, "c": 1,
                             "v": 5.0, "vw": 10.0}) is False


# ==========================================================================
# spread_bps  (= min(0.5*(h-l)/vw*1e4, 50.0))
# ==========================================================================
def test_spread_bps_basic():
    # h-l = 2, vw = 100 -> 0.5*2/100*1e4 = 100 -> capped at 50
    b = bar(0, vw=100.0, h=101.0, l=99.0)
    assert oracle.spread_bps(b) == pytest.approx(50.0)
    # h-l = 0.2, vw = 100 -> 0.5*0.2/100*1e4 = 10 (uncapped)
    b2 = bar(0, vw=100.0, h=100.1, l=99.9)
    assert oracle.spread_bps(b2) == pytest.approx(10.0)
    # zero spread
    b3 = bar(0, vw=100.0, h=100.0, l=100.0)
    assert oracle.spread_bps(b3) == pytest.approx(0.0)


# ==========================================================================
# dump_benchmark  (vol-weighted mean vw over valid bars with minute >= 385)
# ==========================================================================
def test_dump_benchmark_volume_weighted():
    bars = [
        bar(385, vw=10.0, v=100.0),
        bar(386, vw=20.0, v=300.0),
        bar(387, vw=12.0, v=0.0),     # invalid (v=0) -> skipped
        bar(388, vw=14.0, v=200.0),
        bar(389, vw=16.0, v=400.0),
        bar(100, vw=99.0, v=500.0),   # before window -> ignored
    ]
    # weighted: (10*100 + 20*300 + 14*200 + 16*400) / (100+300+200+400)
    #         = (1000 + 6000 + 2800 + 6400) / 1000 = 16200/1000 = 16.2
    assert oracle.dump_benchmark(bars) == pytest.approx(16.2)


def test_dump_benchmark_none_when_empty():
    # no valid bars in [385, 389]
    bars = [bar(100, vw=10.0, v=100.0), bar(384, vw=11.0, v=100.0)]
    assert oracle.dump_benchmark(bars) is None
    # bars exist in window but all invalid
    bars2 = [bar(385, vw=10.0, v=0.0), bar(386, vw=0.0, v=100.0)]
    assert oracle.dump_benchmark(bars2) is None


# ==========================================================================
# close_benchmark  (c of the valid bar with the largest minute)
# ==========================================================================
def test_close_benchmark_largest_valid_minute():
    bars = [
        bar(380, vw=10.0, v=100.0, c=10.5),
        bar(389, vw=12.0, v=0.0, c=99.0),   # invalid (v=0) -> ignored despite largest minute
        bar(388, vw=11.0, v=100.0, c=11.5),
    ]
    # largest VALID minute is 388 -> c = 11.5
    assert oracle.close_benchmark(bars) == pytest.approx(11.5)


def test_close_benchmark_none_when_no_valid():
    assert oracle.close_benchmark([bar(0, vw=0.0, v=100.0)]) is None
    assert oracle.close_benchmark([]) is None


# ==========================================================================
# window_stats  (contiguous full runs)
# ==========================================================================
def test_window_stats_full_contiguity_required():
    # window=3. minutes 0,1,2,3,4 valid except minute 2 invalid.
    bars = [
        bar(0, vw=10.0, v=100.0),
        bar(1, vw=10.0, v=100.0),
        bar(2, vw=10.0, v=0.0),    # invalid breaks any window covering it
        bar(3, vw=10.0, v=100.0),
        bar(4, vw=10.0, v=100.0),
    ]
    stats = oracle.window_stats(bars, window=3)
    # only contiguous full triples: none, because every window of 3 in [0..4]
    # touches minute 2 (windows starting at 0,1,2). minute 2 invalid -> 0 windows.
    starts = sorted(s["start_minute"] for s in stats)
    assert starts == []


def test_window_stats_values():
    # window=2, two clean adjacent windows
    bars = [
        bar(10, vw=10.0, v=100.0, h=10.0, l=10.0),
        bar(11, vw=20.0, v=300.0, h=20.0, l=20.0),
        bar(12, vw=30.0, v=200.0, h=30.0, l=30.0),
    ]
    stats = oracle.window_stats(bars, window=2)
    by_start = {s["start_minute"]: s for s in stats}
    assert set(by_start) == {10, 11}
    # window [10,11]: vwap = (10*100 + 20*300)/(100+300) = (1000+6000)/400 = 17.5
    assert by_start[10]["vwap"] == pytest.approx(17.5)
    # window [11,12]: vwap = (20*300 + 30*200)/(300+200) = (6000+6000)/500 = 24.0
    assert by_start[11]["vwap"] == pytest.approx(24.0)
    # spread_bps all zero (h==l)
    assert by_start[10]["spread_bps"] == pytest.approx(0.0)


def test_window_stats_spread_is_volume_weighted():
    # window=2; per-bar spread_bps differ; weighted by volume
    # bar A: h-l=0.2, vw=100 -> spread 10 bps, v=100
    # bar B: h-l=0.4, vw=100 -> spread 20 bps, v=300
    bars = [
        bar(0, vw=100.0, v=100.0, h=100.1, l=99.9),
        bar(1, vw=100.0, v=300.0, h=100.2, l=99.8),
    ]
    stats = oracle.window_stats(bars, window=2)
    assert len(stats) == 1
    # weighted spread = (10*100 + 20*300)/400 = (1000+6000)/400 = 17.5
    assert stats[0]["spread_bps"] == pytest.approx(17.5)


# ==========================================================================
# strict_oracle
# ==========================================================================
def test_strict_oracle_long_min_vw():
    bars = [
        bar(0, vw=30.0, v=100.0, h=30.0, l=30.0),
        bar(1, vw=10.0, v=100.0, h=10.0, l=10.0),   # cheapest
        bar(2, vw=20.0, v=100.0, h=20.0, l=20.0),
    ]
    res = oracle.strict_oracle(bars, "LONG")
    assert res["price"] == pytest.approx(10.0)
    assert res["minute"] == 1
    assert res["spread_bps"] == pytest.approx(0.0)


def test_strict_oracle_short_max_vw():
    bars = [
        bar(0, vw=30.0, v=100.0),
        bar(1, vw=10.0, v=100.0),
        bar(2, vw=20.0, v=100.0),
    ]
    res = oracle.strict_oracle(bars, "SHORT")
    assert res["price"] == pytest.approx(30.0)
    assert res["minute"] == 0


def test_strict_oracle_skips_invalid_and_nan():
    nan = float("nan")
    bars = [
        bar(0, vw=5.0, v=0.0),       # invalid (v=0) even though cheapest
        bar(1, vw=nan, v=100.0),     # NaN vw must not become oracle
        bar(2, vw=10.0, v=100.0),    # cheapest VALID
        bar(3, vw=20.0, v=100.0),
    ]
    res = oracle.strict_oracle(bars, "LONG")
    assert res["price"] == pytest.approx(10.0)
    assert res["minute"] == 2


def test_strict_oracle_ties_earliest_minute():
    bars = [
        bar(5, vw=10.0, v=100.0),
        bar(2, vw=10.0, v=100.0),   # same price, earlier minute -> wins
        bar(8, vw=10.0, v=100.0),
    ]
    res = oracle.strict_oracle(bars, "LONG")
    assert res["minute"] == 2


def test_strict_oracle_none_when_no_valid():
    assert oracle.strict_oracle([bar(0, vw=0.0, v=100.0)], "LONG") is None


# ==========================================================================
# window_oracle
# ==========================================================================
def test_window_oracle_long_picks_min_vwap_window():
    # window=2, three windows; cheapest vwap window is the trough
    bars = [
        bar(0, vw=30.0, v=100.0, h=30.0, l=30.0),
        bar(1, vw=10.0, v=100.0, h=10.0, l=10.0),
        bar(2, vw=10.0, v=100.0, h=10.0, l=10.0),
        bar(3, vw=40.0, v=100.0, h=40.0, l=40.0),
    ]
    # window vwaps: [0,1]=(30+10)/2=20 ; [1,2]=(10+10)/2=10 ; [2,3]=(10+40)/2=25
    res = oracle.window_oracle(bars, "LONG", window=2)
    assert res["price"] == pytest.approx(10.0)
    assert res["start_minute"] == 1


def test_window_oracle_short_picks_max_vwap_window():
    bars = [
        bar(0, vw=30.0, v=100.0),
        bar(1, vw=10.0, v=100.0),
        bar(2, vw=10.0, v=100.0),
        bar(3, vw=40.0, v=100.0),
    ]
    # window vwaps as above: 20, 10, 25 -> max is [2,3]=25
    res = oracle.window_oracle(bars, "SHORT", window=2)
    assert res["price"] == pytest.approx(25.0)
    assert res["start_minute"] == 2


def test_window_oracle_ties_earliest_start():
    bars = [
        bar(0, vw=10.0, v=100.0),
        bar(1, vw=10.0, v=100.0),
        bar(2, vw=10.0, v=100.0),
    ]
    # both windows [0,1] and [1,2] have vwap 10 -> earliest start 0
    res = oracle.window_oracle(bars, "LONG", window=2)
    assert res["start_minute"] == 0


def test_window_oracle_none_when_no_full_window():
    bars = [bar(0, vw=10.0, v=100.0), bar(1, vw=10.0, v=0.0)]  # bar 1 invalid
    assert oracle.window_oracle(bars, "LONG", window=2) is None


# ==========================================================================
# gross_bps
# ==========================================================================
def test_gross_bps_long_and_short():
    # LONG: p_eod=100, p_or=95 -> (100-95)/100*1e4 = 500 bps
    assert oracle.gross_bps(100.0, 95.0, "LONG") == pytest.approx(500.0)
    # SHORT: p_eod=100, p_or=105 -> (105-100)/100*1e4 = 500 bps
    assert oracle.gross_bps(100.0, 105.0, "SHORT") == pytest.approx(500.0)
    # LONG paying more than dump -> negative
    assert oracle.gross_bps(100.0, 105.0, "LONG") == pytest.approx(-500.0)


# ==========================================================================
# per_session_median / kill_verdict
# ==========================================================================
def test_per_session_median():
    assert oracle.per_session_median([3.0, 1.0, 2.0]) == pytest.approx(2.0)
    assert oracle.per_session_median([1.0, 2.0, 3.0, 4.0]) == pytest.approx(2.5)
    assert oracle.per_session_median([]) is None


def test_kill_verdict_go_case():
    # 5 sessions, medians of per-session excess; 4 of 5 positive (0.8 >= 0.60)
    # median-of-medians = median([5,4,3,2,-1]) = 3 > 2.0 -> GO
    sess = {"a": 5.0, "b": 4.0, "c": 3.0, "d": 2.0, "e": -1.0}
    v = oracle.kill_verdict(sess)
    assert v["go"] is True
    assert v["median_of_medians"] == pytest.approx(3.0)
    assert v["frac_pos_sessions"] == pytest.approx(0.8)
    assert v["n_sessions"] == 5


def test_kill_verdict_kill_on_low_median():
    # median-of-medians below 2.0 -> KILL even if frac positive ok
    sess = {"a": 1.5, "b": 1.0, "c": 0.5, "d": 0.5, "e": 0.5}
    v = oracle.kill_verdict(sess)
    # median = 0.5 (sorted: 0.5,0.5,0.5,1.0,1.5) not > 2.0
    assert v["go"] is False


def test_kill_verdict_kill_on_low_frac():
    # median-of-medians high but too few positive sessions
    # medians: [10, 9, -1, -1, -1] -> median = -1 -> also fails median.
    # use a set where median > 2 but frac < 0.60:
    # [3, 3, 3, -1, -1] -> median=3 (>2), positives=3/5=0.6 -> that's >=0.60 (GO).
    # need frac < 0.60: [3,3,-1,-1,-1] median = -1 -> fails median too.
    # construct: medians [5,5,5,5,-1,-1,-1] (7) median=5>2, pos=4/7=0.571<0.60 -> KILL
    sess = {f"s{i}": 5.0 for i in range(4)}
    sess.update({f"n{i}": -1.0 for i in range(3)})
    v = oracle.kill_verdict(sess)
    assert v["median_of_medians"] == pytest.approx(5.0)
    assert v["frac_pos_sessions"] == pytest.approx(4.0 / 7.0)
    assert v["frac_pos_sessions"] < 0.60
    assert v["go"] is False


def test_kill_verdict_exactly_at_thresholds():
    # median exactly 2.0 is NOT > 2.0 -> KILL even with good frac
    sess = {"a": 2.0, "b": 2.0, "c": 2.0, "d": 2.0, "e": 2.0}
    v = oracle.kill_verdict(sess)
    assert v["median_of_medians"] == pytest.approx(2.0)
    assert v["frac_pos_sessions"] == pytest.approx(1.0)
    assert v["go"] is False  # 2.0 not strictly greater than 2.0

    # frac exactly 0.60 IS >= 0.60; pair with median strictly above 2.0 -> GO
    # 5 sessions: 3 positive at 3.0, 2 non-positive at 0.0 -> median sorted
    # [0,0,3,3,3] = 3.0 > 2.0 ; positives (strictly > 0) = 3/5 = 0.60 -> GO
    sess2 = {"a": 3.0, "b": 3.0, "c": 3.0, "d": 0.0, "e": 0.0}
    v2 = oracle.kill_verdict(sess2)
    assert v2["median_of_medians"] == pytest.approx(3.0)
    assert v2["frac_pos_sessions"] == pytest.approx(0.60)
    assert v2["go"] is True


# ==========================================================================
# evaluate_intent — the full per-intent record
# ==========================================================================
def _full_session_bars(vw_fn, v=100.0, spread=0.0):
    """Build a full 0..389 session. vw_fn(minute)->vw. Constant volume & spread
    so spreads cancel and dump benchmark is a plain mean of the tail."""
    bars = []
    for m in range(390):
        vwv = vw_fn(m)
        half = spread / 2.0
        bars.append(bar(m, vw=vwv, v=v, h=vwv + half, l=vwv - half))
    return bars


def test_evaluate_intent_monotone_down_long():
    # Strictly decreasing price: vw = 200 - 0.1*m. Cheapest is the LAST minute.
    bars = _full_session_bars(lambda m: 200.0 - 0.1 * m)
    rec = oracle.evaluate_intent(bars, "LONG", window=15)
    assert rec["exclude_reason"] is None
    assert rec["n_valid_bars"] == 390
    # dump = mean of vw over minutes 385..389 = mean(200-0.1*m for m in 385..389)
    # vw at 385..389 = 161.5,161.4,161.3,161.2,161.1 -> mean = 161.3
    assert rec["p_eod_dump"] == pytest.approx(161.3)
    # p_eod_close = c of last valid bar = vw at 389 = 200 - 38.9 = 161.1
    assert rec["p_eod_close"] == pytest.approx(161.1)
    # strict LONG oracle = cheapest = last minute 389, price 161.1
    assert rec["strict_minute"] == 389
    assert rec["strict_price"] == pytest.approx(161.1)
    # strict gross = (161.3 - 161.1)/161.3 * 1e4
    exp_strict_gross = (161.3 - 161.1) / 161.3 * 1e4
    assert rec["strict_gross_bps"] == pytest.approx(exp_strict_gross)
    # spreads all zero -> differential zero -> net == gross
    assert rec["strict_net_bps"] == pytest.approx(exp_strict_gross)
    # window oracle LONG = cheapest 15-window = the last full window starting at 375
    assert rec["win_start_minute"] == 375
    assert rec["directed_in_last15"] is True
    # win vwap = mean of vw over 375..389 (constant volume)
    win_vw = sum(200.0 - 0.1 * m for m in range(375, 390)) / 15.0
    exp_win_gross = (161.3 - win_vw) / 161.3 * 1e4
    assert rec["win_gross_bps"] == pytest.approx(exp_win_gross)
    assert rec["win_net_bps"] == pytest.approx(exp_win_gross)  # zero spread differential


def test_evaluate_intent_monotone_down_short_mirror():
    bars = _full_session_bars(lambda m: 200.0 - 0.1 * m)
    rec = oracle.evaluate_intent(bars, "SHORT", window=15)
    # SHORT strict oracle = max vw = first minute 0, price 200.0
    assert rec["strict_minute"] == 0
    assert rec["strict_price"] == pytest.approx(200.0)
    # SHORT gross = (p_or - p_eod)/p_eod = (200 - 161.3)/161.3*1e4
    exp = (200.0 - 161.3) / 161.3 * 1e4
    assert rec["strict_gross_bps"] == pytest.approx(exp)
    # window SHORT = max vwap window = first window starting at 0
    assert rec["win_start_minute"] == 0
    assert rec["directed_in_last15"] is False


def test_evaluate_intent_excess_directed_beats_flipped():
    # Construct a day with a deep early TROUGH and only a shallow late peak.
    # LONG wants the trough (big prize); SHORT (flipped) wants the peak (small).
    # baseline 100; a sharp dip near minute 50, mild bump near minute 300.
    def vw_fn(m):
        base = 100.0
        if 45 <= m < 60:           # 15-min deep trough -> LONG window oracle lives here
            return 80.0
        if 295 <= m < 310:         # 15-min mild peak -> SHORT window oracle lives here
            return 104.0
        return base
    bars = _full_session_bars(vw_fn)  # zero spread everywhere
    rec = oracle.evaluate_intent(bars, "LONG", window=15)
    assert rec["exclude_reason"] is None
    # dump = mean vw over 385..389 = baseline 100
    assert rec["p_eod_dump"] == pytest.approx(100.0)
    # directed LONG window oracle: cheapest window = the trough [45,60) vwap=80
    assert rec["win_price"] == pytest.approx(80.0)
    assert rec["win_start_minute"] == 45
    # win gross = (100 - 80)/100 * 1e4 = 2000 bps ; zero spread -> net == gross
    assert rec["win_gross_bps"] == pytest.approx(2000.0)
    assert rec["win_net_bps"] == pytest.approx(2000.0)
    # flipped = window_oracle SHORT on same bars: max window = peak [295,310) vwap=104
    # flip gross uses SHORT sign: (104 - 100)/100 * 1e4 = 400 bps
    assert rec["flip_win_price"] == pytest.approx(104.0)
    assert rec["flip_win_start_minute"] == 295
    assert rec["flip_win_gross_bps"] == pytest.approx(400.0)
    assert rec["flip_win_net_bps"] == pytest.approx(400.0)
    # excess = directed net - flipped net = 2000 - 400 = 1600
    assert rec["excess_net_bps"] == pytest.approx(1600.0)


def test_evaluate_intent_excess_symmetric_is_zero():
    # Symmetric day: a trough and an equal-magnitude peak around the baseline.
    # Zero spread everywhere -> win_spread == flip_spread == dump_spread == 0,
    # so excess reduces to (win_gross - flip_gross). With equal-magnitude
    # trough/peak, both grosses are 1000 bps -> excess == 0 exactly.
    # (NOTE: a uniform h-l does NOT give equal spread_bps because spread_bps
    # divides by vw and trough/peak vw differ; zero spread is the clean way.)
    def vw_fn(m):
        if 45 <= m < 60:
            return 90.0           # trough: 10 below baseline
        if 295 <= m < 310:
            return 110.0          # peak: 10 above baseline
        return 100.0
    bars = _full_session_bars(vw_fn, spread=0.0)
    rec = oracle.evaluate_intent(bars, "LONG", window=15)
    assert rec["exclude_reason"] is None
    # dump = baseline 100 (tail bars at 100)
    assert rec["p_eod_dump"] == pytest.approx(100.0)
    # LONG window prize: trough vwap 90 -> gross (100-90)/100*1e4 = 1000
    # SHORT window prize: peak vwap 110 -> gross (110-100)/100*1e4 = 1000
    # zero spread -> net == gross for both -> excess == 0.
    assert rec["win_gross_bps"] == pytest.approx(1000.0)
    assert rec["flip_win_gross_bps"] == pytest.approx(1000.0)
    assert rec["excess_net_bps"] == pytest.approx(0.0, abs=1e-9)


def test_evaluate_intent_net_uses_signed_spread_differential():
    # Oracle window WIDER spread than dump window -> net < gross (cost).
    # Make the trough window have a large spread, the dump tail tight.
    def make(trough_spread, dump_spread):
        bars = []
        for m in range(390):
            if 45 <= m < 60:
                vwv, sp = 80.0, trough_spread
            elif m >= 385:
                vwv, sp = 100.0, dump_spread
            else:
                vwv, sp = 100.0, 0.0
            half = sp / 2.0
            bars.append(bar(m, vw=vwv, v=100.0, h=vwv + half, l=vwv - half))
        return bars

    # trough spread 0.8 on vw=80 -> per-bar spread = 0.5*0.8/80*1e4 = 50 bps
    # dump spread 0.0 -> dump window spread 0 bps
    # gross (LONG) = (100-80)/100*1e4 = 2000
    # differential = oracle_spread(50) - dump_spread(0) = 50 -> net = 2000-50 = 1950
    bars_wide = make(trough_spread=0.8, dump_spread=0.0)
    rec_wide = oracle.evaluate_intent(bars_wide, "LONG", window=15)
    assert rec_wide["win_gross_bps"] == pytest.approx(2000.0)
    assert rec_wide["win_net_bps"] == pytest.approx(1950.0)

    # Now tighter oracle than dump -> net > gross (benefit; signed, no floor).
    # trough spread 0 -> oracle spread 0 ; dump spread 0.4 on vw=100 -> 20 bps
    # differential = 0 - 20 = -20 -> net = 2000 - (-20) = 2020
    bars_tight = make(trough_spread=0.0, dump_spread=0.4)
    rec_tight = oracle.evaluate_intent(bars_tight, "LONG", window=15)
    assert rec_tight["win_gross_bps"] == pytest.approx(2000.0)
    assert rec_tight["win_net_bps"] == pytest.approx(2020.0)


def test_evaluate_intent_excess_uses_flip_spread_in_flip_leg():
    # GUARD: the flipped net must use the FLIPPED window's spread, not the
    # directed window's. With equal grosses but DIFFERENT non-zero spreads at
    # the directed (trough) vs flipped (peak) windows, excess is non-trivial and
    # the dump_spread cancels out of it. A bug using win_spread in the flip leg
    # would make excess == 0 here, so this catches it.
    #   trough vw=80, spread 0.64 -> 0.5*0.64/80*1e4 = 40 bps  (LONG window)
    #   peak   vw=120, spread 0.72 -> 0.5*0.72/120*1e4 = 30 bps (SHORT/flip window)
    #   dump   vw=100, spread 0.20 -> 0.5*0.20/100*1e4 = 10 bps
    bars = []
    for m in range(390):
        if 45 <= m < 60:
            vwv, sp = 80.0, 0.64
        elif 295 <= m < 310:
            vwv, sp = 120.0, 0.72
        elif m >= 385:
            vwv, sp = 100.0, 0.20
        else:
            vwv, sp = 100.0, 0.0
        half = sp / 2.0
        bars.append(bar(m, vw=vwv, v=100.0, h=vwv + half, l=vwv - half))
    rec = oracle.evaluate_intent(bars, "LONG", window=15)
    assert rec["exclude_reason"] is None
    assert rec["p_eod_dump"] == pytest.approx(100.0)
    # directed LONG: trough window, gross (100-80)/100*1e4 = 2000, spread 40
    assert rec["win_gross_bps"] == pytest.approx(2000.0)
    assert rec["win_net_bps"] == pytest.approx(2000.0 - (40.0 - 10.0))  # 1970
    # flipped SHORT: peak window, gross (120-100)/100*1e4 = 2000, spread 30
    assert rec["flip_win_gross_bps"] == pytest.approx(2000.0)
    assert rec["flip_win_net_bps"] == pytest.approx(2000.0 - (30.0 - 10.0))  # 1980
    # excess = 1970 - 1980 = -10 (dump_spread cancels; = -(win_spread - flip_spread))
    assert rec["excess_net_bps"] == pytest.approx(-10.0)


def test_evaluate_intent_exclusion_too_few_bars():
    # only 59 valid bars -> too_few_bars
    bars = [bar(m, vw=100.0, v=100.0) for m in range(59)]
    rec = oracle.evaluate_intent(bars, "LONG", window=15)
    assert rec["exclude_reason"] == "too_few_bars"
    assert rec["n_valid_bars"] == 59
    assert rec["win_net_bps"] is None
    assert rec["strict_price"] is None
    assert rec["excess_net_bps"] is None


def test_evaluate_intent_exclusion_no_dump_window():
    # >=60 valid bars but none at minute >= 385 -> no_dump_window
    bars = [bar(m, vw=100.0, v=100.0) for m in range(0, 100)]
    rec = oracle.evaluate_intent(bars, "LONG", window=15)
    assert rec["n_valid_bars"] == 100
    assert rec["exclude_reason"] == "no_dump_window"
    assert rec["p_eod_dump"] is None
    assert rec["win_net_bps"] is None


def test_evaluate_intent_exclusion_no_window():
    # >=60 valid bars, dump window present, but no contiguous 15-window exists
    # (insert an invalid bar every <15 minutes so no full window forms).
    bars = []
    for m in range(390):
        if m % 10 == 5:
            bars.append(bar(m, vw=100.0, v=0.0))   # invalid every 10 minutes
        else:
            bars.append(bar(m, vw=100.0, v=100.0))
    # valid count = 390 - 39 = 351 (>=60). dump window 385..389 mod 10 = 5,6,7,8,9
    # -> 385 invalid, 386..389 valid -> a dump benchmark DOES exist (passes the
    # no_dump_window guard) so the exclusion must fall through to no_window.
    assert oracle.dump_benchmark(bars) is not None  # dump window genuinely present
    assert oracle.window_oracle(bars, "LONG", window=15) is None  # no full window
    rec = oracle.evaluate_intent(bars, "LONG", window=15)
    assert rec["exclude_reason"] == "no_window"
    # excluded record nulls ALL metric fields (spec §5 EXCLUSIONS)
    assert rec["p_eod_dump"] is None
    assert rec["win_net_bps"] is None


def test_evaluate_intent_flag_fields_present_when_excluded():
    bars = [bar(m, vw=100.0, v=100.0) for m in range(10)]
    rec = oracle.evaluate_intent(bars, "LONG", window=15)
    # all metric fields None on exclusion, but the keys exist
    for k in ("p_eod_dump", "p_eod_close", "strict_price", "strict_minute",
              "strict_gross_bps", "strict_net_bps", "win_price",
              "win_start_minute", "win_gross_bps", "win_net_bps",
              "flip_win_price", "flip_win_start_minute", "flip_win_gross_bps",
              "flip_win_net_bps", "excess_net_bps", "directed_in_last15",
              "flipped_in_last15"):
        assert k in rec
        assert rec[k] is None
    assert rec["n_valid_bars"] == 10
    assert rec["exclude_reason"] == "too_few_bars"
