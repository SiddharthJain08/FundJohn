"""Truth-table tests for src/research/bflow/flow_policy.py (PURE, no I/O).

Test B of the Phase-1b pre-registration
(``docs/superpowers/specs/2026-06-05-sp6-bflow-phase1b-flow-predictability-prereg.md`` §4).
SUPPORTIVE, NOT GATING. Every constant is frozen by the pre-registration; every
expected value below is hand-computed in the comments so the test specifies §4,
not the implementation.

State machine (LONG, variant='reversion', spec §4 verbatim):
  * scan minutes t >= 30 in order.
  * ARM at minute b where ofi_5(b) <= -0.6 AND Σ_{b-4..b} v >= 1.5 * 5 *
    (mean minute volume over the trailing 30 minutes [b-29..b]).
  * TRIGGER = first t in (b, b+10] with c_t > c_{t-1}; if none within 10
    minutes, DISARM and keep scanning (later bursts can re-arm).
  * On trigger at t: entry fill = vw_{t+1}. If the t+1 bar is missing/invalid,
    the trigger LAPSES and scanning continues from t+1.
  * FORCED FALLBACK: if no entry by minute 384, fill at p_eod_dump
    (used_fallback=True, delta_bps=0 by construction).
SHORT mirrors exactly (arm ofi_5 >= +0.6, trigger = first down-minute, sell at
vw_{t+1}). variant='momentum' flips ONLY the arm OFI sign (DIAGNOSTIC ONLY).

delta_bps (mirrors oracle.py): LONG gross = (p_eod_dump - entry)/p_eod_dump*1e4;
SHORT gross = (entry - p_eod_dump)/p_eod_dump*1e4; net = gross - (entry-minute
spread_bps - dump-window spread_bps). +2bps impact cancels in the differential.
"""
import math

import numpy as np
import pandas as pd
import pytest

from src.research.bflow import flow_policy as fp
from src.research.bflow import oracle


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def _row(minute, c, v=100.0, vw=None, h=None, l=None, o=None):
    """One bar row. Defaults to a valid bar with h=l=c (zero spread) and vw=c,
    o=c (so a minute-0 Δc defaults to 0 unless o is overridden)."""
    if vw is None:
        vw = c
    if h is None:
        h = c
    if l is None:
        l = c
    if o is None:
        o = c
    return {"minute": minute, "o": o, "h": h, "l": l, "c": c, "v": v, "vw": vw}


def make_df(rows):
    return pd.DataFrame(rows)


def _flat_bars(n=390, c=100.0, v=100.0):
    """n flat valid bars (no flow signal anywhere) plus a dump window."""
    return [_row(m, c=c, v=v) for m in range(n)]


def _with_dump(rows, dump_c=100.0, dump_v=100.0):
    """Append a 5-bar dump window (minutes 385..389) so dump_benchmark exists."""
    have = {r["minute"] for r in rows}
    out = list(rows)
    for m in range(385, 390):
        if m not in have:
            out.append(_row(m, c=dump_c, v=dump_v))
    return out


# A LONG reversion arm requires ofi_5 <= -0.6 (net sell flow) AND a volume burst.
# Build a 5-bar down-burst at minutes [b-4..b] with high volume; surrounded by
# flat low-volume bars so the trailing-30 mean is small and the 1.5x5 burst test
# clears. Then a single UP minute right after b triggers.
def _long_arm_then_up(b, low_v=10.0, burst_v=100.0, base_c=100.0):
    """Bars 0..b+2 with: flat at base_c for the trailing context (low volume),
    a 5-minute strict down-move with burst volume over [b-4..b] (ofi_5=-1, big
    volume), then an UP minute at b+1 (c rises) and another bar at b+2 for the
    fill vw. With base_c=100 the closes over [b-4..b] are 99,98,97,96,95 (c_b=95);
    the UP minute b+1 closes at 97 (>95) and the fill bar b+2 has vw=97. Returns
    (rows, expected_trigger_minute=b+1); the fill price is therefore 97.0."""
    rows = []
    # trailing context up to b-5: flat, low volume.
    for m in range(0, b - 4):
        rows.append(_row(m, c=base_c, v=low_v))
    # the down-burst [b-4 .. b]: strictly decreasing closes, big volume.
    for i, m in enumerate(range(b - 4, b + 1)):
        rows.append(_row(m, c=base_c - (i + 1), v=burst_v))
    # b+1: UP minute (close above b's close c_b=base-5) -> trigger; modest volume.
    rows.append(_row(b + 1, c=base_c - 4 + 1, v=low_v))   # c = base-3 (= 97)
    # b+2: the fill bar (vw read here) = base-3 (= 97).
    rows.append(_row(b + 2, c=base_c - 4 + 1, v=low_v))
    return rows, b + 1


# ==========================================================================
# output contract
# ==========================================================================
def test_output_keys_and_types():
    rows = _with_dump(_flat_bars())
    out = fp.simulate_intent(make_df(rows), "LONG", 100.0)
    assert set(out.keys()) == {
        "triggered", "entry_minute", "entry_price", "used_fallback",
        "delta_bps", "arm_count",
    }
    assert isinstance(out["triggered"], bool)
    assert isinstance(out["used_fallback"], bool)
    assert isinstance(out["arm_count"], int)


# ==========================================================================
# never-triggered -> forced fallback at the dump
# ==========================================================================
def test_never_triggered_fallback():
    # flat bars -> ofi_5 is 0 everywhere (all Δc = 0), never arms.
    rows = _with_dump(_flat_bars(c=100.0))
    out = fp.simulate_intent(make_df(rows), "LONG", 100.0)
    assert out["triggered"] is False
    assert out["used_fallback"] is True
    assert out["entry_minute"] is None
    assert out["entry_price"] == pytest.approx(100.0)   # p_eod_dump
    assert out["delta_bps"] == pytest.approx(0.0)        # fallback dilutes to 0
    assert out["arm_count"] == 0


# ==========================================================================
# volume-pace arithmetic (the arm gate) — hand-built
# ==========================================================================
def test_volume_pace_arm_threshold_clears():
    # Construct a down-burst at b=40 with ofi_5 = -1.0 and verify the volume
    # gate: Σ_{36..40} v >= 1.5 * 5 * mean30, mean30 = (Σ_{11..40} v)/30.
    # Trailing context minutes 0..35 flat at v=10 ; burst minutes 36..40 v=100.
    rows = []
    for m in range(0, 36):
        rows.append(_row(m, c=100.0, v=10.0))
    for i, m in enumerate(range(36, 41)):
        rows.append(_row(m, c=100.0 - (i + 1), v=100.0))   # strict down, +vol
    # up minute at 41 -> trigger; fill bar at 42.
    rows.append(_row(41, c=100.0 - 4, v=10.0))             # c=96 > c40=95 -> UP
    rows.append(_row(42, c=96.0, v=10.0, vw=96.0))
    rows = _with_dump(rows, dump_c=100.0)
    # mean30 over [11..40]: minutes 11..35 are v=10 (25 bars), 36..40 v=100 (5
    # bars) -> Σ = 250 + 500 = 750 ; mean30 = 750/30 = 25.0.
    # burst Σ_{36..40} v = 500. threshold = 1.5*5*25 = 187.5. 500 >= 187.5 -> arm.
    out = fp.simulate_intent(make_df(rows), "LONG", 100.0)
    assert out["triggered"] is True
    assert out["arm_count"] >= 1
    assert out["entry_minute"] == 42          # fill at t+1 (t=41 trigger)
    assert out["entry_price"] == pytest.approx(96.0)


def test_volume_pace_arm_threshold_fails_when_burst_too_small():
    # Same down-move + ofi_5=-1 at b=40 but burst volume too low to clear the
    # 1.5x5xmean30 gate -> no arm -> fallback.
    rows = []
    for m in range(0, 36):
        rows.append(_row(m, c=100.0, v=100.0))      # high trailing volume
    for i, m in enumerate(range(36, 41)):
        rows.append(_row(m, c=100.0 - (i + 1), v=100.0))   # burst == context
    rows.append(_row(41, c=96.0, v=100.0))
    rows = _with_dump(rows, dump_c=100.0)
    # mean30 over [11..40] = (30*100)/30 = 100. threshold = 1.5*5*100 = 750.
    # burst Σ = 500 < 750 -> NO arm.
    out = fp.simulate_intent(make_df(rows), "LONG", 100.0)
    assert out["arm_count"] == 0
    assert out["used_fallback"] is True


# ==========================================================================
# disarm-after-10 then re-arm later
# ==========================================================================
def test_disarm_after_10_then_rearm():
    # First down-burst arms at b=40 but the following 10 minutes are all flat or
    # down (no up-minute in (40,50]) -> DISARM. A second down-burst arms again
    # near b=70 and an up-minute at 71 triggers.
    rows = []
    for m in range(0, 36):
        rows.append(_row(m, c=100.0, v=10.0))
    # burst 1: 36..40 strict down, high vol -> arm at 40.
    for i, m in enumerate(range(36, 41)):
        rows.append(_row(m, c=100.0 - (i + 1), v=100.0))
    # minutes 41..50: keep going strictly DOWN (no up-minute) -> disarm at 50.
    for j, m in enumerate(range(41, 51)):
        rows.append(_row(m, c=95.0 - (j + 1), v=10.0))
    # flat low-vol filler 51..65 to reset the trailing-30 volume baseline.
    for m in range(51, 66):
        rows.append(_row(m, c=85.0, v=10.0))
    # burst 2: 66..70 strict down, high vol -> arm at 70.
    for i, m in enumerate(range(66, 71)):
        rows.append(_row(m, c=85.0 - (i + 1), v=100.0))
    # up minute at 71 -> trigger ; fill bar 72.
    rows.append(_row(71, c=85.0 - 4 + 1, v=10.0))      # c=82 > c70=80 -> UP
    rows.append(_row(72, c=82.0, v=10.0, vw=82.0))
    rows = _with_dump(rows, dump_c=100.0)
    out = fp.simulate_intent(make_df(rows), "LONG", 100.0)
    assert out["triggered"] is True
    assert out["arm_count"] >= 2               # armed, disarmed, armed again
    assert out["entry_minute"] == 72
    assert out["entry_price"] == pytest.approx(82.0)


# ==========================================================================
# invalid t+1 bar -> trigger LAPSES, scanning continues
# ==========================================================================
def test_invalid_t_plus_1_bar_lapses():
    # Arm at 40, up-minute trigger at 41, but the fill bar (42) is INVALID
    # (vw<=0) -> lapse. A clean second setup arms at 80 and fills at 82.
    # NOTE: the invalid minute 42 NaNs the volume of any trailing-30 window that
    # contains it, so the second arm must sit far enough that minute 42 is
    # OUTSIDE its [b-29..b] window (b-29 > 42 -> b >= 72); we use b=80.
    rows = []
    for m in range(0, 36):
        rows.append(_row(m, c=100.0, v=10.0))
    for i, m in enumerate(range(36, 41)):
        rows.append(_row(m, c=100.0 - (i + 1), v=100.0))
    rows.append(_row(41, c=96.0, v=10.0))               # up-minute trigger
    rows.append(_row(42, c=96.0, v=10.0, vw=0.0, h=1.0, l=2.0))  # INVALID fill
    # clean low-vol filler 43..75 (resets the trailing-30 baseline AND moves the
    # invalid minute 42 out of the second arm's trailing window).
    for m in range(43, 76):
        rows.append(_row(m, c=96.0, v=10.0))
    # second clean setup: down-burst 76..80, up-minute 81, fill bar 82.
    for i, m in enumerate(range(76, 81)):
        rows.append(_row(m, c=96.0 - (i + 1), v=100.0))
    rows.append(_row(81, c=96.0 - 4 + 1, v=10.0))       # c=93 > c80=91 -> UP
    rows.append(_row(82, c=93.0, v=10.0, vw=93.0))
    rows = _with_dump(rows, dump_c=100.0)
    out = fp.simulate_intent(make_df(rows), "LONG", 100.0)
    assert out["triggered"] is True
    assert out["entry_minute"] == 82            # NOT 42 (that one lapsed)
    assert out["entry_price"] == pytest.approx(93.0)


# ==========================================================================
# LOOKAHEAD TRAP — mutate every bar strictly after the fill minute; identical
# ==========================================================================
def test_lookahead_trap_long():
    rows, trig = _long_arm_then_up(b=40)
    rows = _with_dump(rows, dump_c=100.0)
    base = fp.simulate_intent(make_df(rows), "LONG", 100.0)
    assert base["triggered"] is True
    fill_min = base["entry_minute"]             # t+1 = the last bar the sim reads
    assert fill_min == trig + 1

    # Mutate EVERY bar strictly after the fill minute to absurd values. A causal
    # simulator cannot see them, so the decision dict must be byte-identical.
    mutated = []
    for r in rows:
        if r["minute"] > fill_min and r["minute"] < 385:
            rr = dict(r)
            rr["c"] = 9999.0
            rr["o"] = 9999.0
            rr["h"] = 9999.0
            rr["l"] = 9999.0
            rr["v"] = 1.0
            rr["vw"] = 9999.0
            mutated.append(rr)
        else:
            mutated.append(r)
    out2 = fp.simulate_intent(make_df(mutated), "LONG", 100.0)
    assert out2["triggered"] == base["triggered"]
    assert out2["entry_minute"] == base["entry_minute"]
    assert out2["entry_price"] == pytest.approx(base["entry_price"])
    assert out2["used_fallback"] == base["used_fallback"]
    assert out2["arm_count"] == base["arm_count"]
    assert out2["delta_bps"] == pytest.approx(base["delta_bps"])


# ==========================================================================
# SHORT mirror symmetry on a mirrored price path
# ==========================================================================
def _mirror(rows, pivot=100.0):
    """Reflect every price field about ``pivot`` (c -> 2*pivot - c, etc.). A LONG
    setup on ``rows`` becomes the exact SHORT setup on the mirror: down-bursts
    become up-bursts, up-minutes become down-minutes, ofi_5 sign flips."""
    out = []
    for r in rows:
        rr = dict(r)
        for k in ("o", "h", "l", "c", "vw"):
            rr[k] = 2 * pivot - r[k]
        # h/l can invert under reflection; re-order so h>=l stays valid.
        if rr["h"] < rr["l"]:
            rr["h"], rr["l"] = rr["l"], rr["h"]
        out.append(rr)
    return out


def test_short_long_mirror_symmetry():
    rows, trig = _long_arm_then_up(b=40)
    rows = _with_dump(rows, dump_c=100.0)
    long_out = fp.simulate_intent(make_df(rows), "LONG", 100.0)

    mrows = _mirror(rows, pivot=100.0)
    # mirrored dump price = 2*100 - 100 = 100 (symmetric pivot).
    short_out = fp.simulate_intent(make_df(mrows), "SHORT", 100.0)

    assert short_out["triggered"] == long_out["triggered"]
    assert short_out["arm_count"] == long_out["arm_count"]
    assert short_out["entry_minute"] == long_out["entry_minute"]
    # entry price mirrors about the pivot.
    assert short_out["entry_price"] == pytest.approx(
        2 * 100.0 - long_out["entry_price"])
    # delta_bps is sign-convention symmetric -> equal magnitude & sign.
    assert short_out["delta_bps"] == pytest.approx(long_out["delta_bps"])


# ==========================================================================
# delta_bps sign correctness — both directions
# ==========================================================================
def test_delta_bps_long_positive_when_entry_below_dump():
    # LONG fills BELOW the dump (entry 97, dump 100) -> beats the dump -> +Δbps.
    rows, trig = _long_arm_then_up(b=40)          # fill vw = 97.0 at b+2
    rows = _with_dump(rows, dump_c=100.0)
    out = fp.simulate_intent(make_df(rows), "LONG", 100.0)
    assert out["triggered"] is True
    assert out["entry_price"] == pytest.approx(97.0)
    # gross = (100 - 97)/100 * 1e4 = 300 bps ; zero spreads everywhere -> net=300.
    assert out["delta_bps"] == pytest.approx(300.0)


def test_delta_bps_short_positive_when_entry_above_dump():
    rows, trig = _long_arm_then_up(b=40)
    rows = _with_dump(rows, dump_c=100.0)
    mrows = _mirror(rows, pivot=100.0)            # SHORT fills at 103, dump 100
    out = fp.simulate_intent(make_df(mrows), "SHORT", 100.0)
    assert out["triggered"] is True
    assert out["entry_price"] == pytest.approx(103.0)
    # gross = (103 - 100)/100 * 1e4 = 300 bps -> +Δbps (sells above the dump).
    assert out["delta_bps"] == pytest.approx(300.0)


def test_delta_bps_subtracts_entry_spread_differential():
    # Make the entry (fill) bar carry a spread while the dump window is tight, so
    # the differential spread cost reduces net below gross by exactly the oracle
    # differential. h-l on the fill bar = 0.96 over vw 96 -> spread_bps =
    # min(0.5*0.96/96*1e4, 50) = min(50.0, 50) = 50.0 ; dump spread = 0.
    rows = []
    for m in range(0, 36):
        rows.append(_row(m, c=100.0, v=10.0))
    for i, m in enumerate(range(36, 41)):
        rows.append(_row(m, c=100.0 - (i + 1), v=100.0))
    rows.append(_row(41, c=96.0, v=10.0))                       # up trigger
    # fill bar 42: vw=96, spread -> 50 bps capped.
    rows.append(_row(42, c=96.0, v=10.0, vw=96.0, h=96.48, l=95.52))
    rows = _with_dump(rows, dump_c=100.0)                       # dump h=l -> 0 sp
    out = fp.simulate_intent(make_df(rows), "LONG", 100.0)
    fill_bar = {"minute": 42, "o": 96.0, "h": 96.48, "l": 95.52,
                "c": 96.0, "v": 10.0, "vw": 96.0}
    entry_sp = oracle.spread_bps(fill_bar)
    assert entry_sp == pytest.approx(50.0)
    # gross 400 ; net = 400 - (50 - 0) = 350.
    assert out["delta_bps"] == pytest.approx(400.0 - 50.0)


# ==========================================================================
# momentum variant differs ONLY in arm sign
# ==========================================================================
def test_momentum_variant_arms_on_buy_burst_for_long():
    # A strict UP-burst with volume over minutes 36..40 (ofi_5 climbs through
    # +0.6). Under REVERSION a LONG needs ofi_5<=-0.6, so it never arms here
    # (buy-flow) -> fallback. Under MOMENTUM a LONG arms on the buy-burst
    # (ofi_5>=+0.6) and its LONG confirm (first c_t>c_{t-1}) fires immediately.
    #
    # Hand-trace (momentum LONG): minutes 0..35 flat c=100 v=10. At minute 36
    # c=101, ofi_5 window [32..36] = (0+0+0+0 + 1*100)/(10+10+10+10+100) = 100/140
    # = 0.714 >= 0.6 -> ARM at 36 (volume gate: burst Σ[32..36]=140 ;
    # trailing-30 Σ[7..36] = 29*10 + 100 = 390, mean30=13, threshold
    # =1.5*5*13=97.5 ; 140>=97.5). Trigger search (36,46]: minute 37 c=102>101
    # -> trigger at 37 -> fill vw_38 = c_38 = 103.
    rows = []
    for m in range(0, 36):
        rows.append(_row(m, c=100.0, v=10.0))
    # UP-burst 36..40 (strict up, high vol) -> ofi_5 climbs through +0.6.
    for i, m in enumerate(range(36, 41)):
        rows.append(_row(m, c=100.0 + (i + 1), v=100.0))   # 101,102,103,104,105
    rows = _with_dump(rows, dump_c=100.0)

    rev = fp.simulate_intent(make_df(rows), "LONG", 100.0, variant="reversion")
    mom = fp.simulate_intent(make_df(rows), "LONG", 100.0, variant="momentum")
    # reversion LONG: ofi_5 positive (buy flow) -> never arms -> fallback.
    assert rev["arm_count"] == 0
    assert rev["used_fallback"] is True
    # momentum LONG: arms on the buy-burst at 36 -> triggers at 37 -> fill at 38.
    assert mom["arm_count"] >= 1
    assert mom["triggered"] is True
    assert mom["entry_minute"] == 38
    assert mom["entry_price"] == pytest.approx(103.0)


def test_momentum_only_flips_arm_sign_not_confirm():
    # On the SAME down-burst path, reversion LONG arms & triggers; momentum LONG
    # does NOT arm (down-flow, momentum needs +0.6). Confirm rule (first up-min)
    # is identical for both — only the arm sign differs.
    rows, trig = _long_arm_then_up(b=40)
    rows = _with_dump(rows, dump_c=100.0)
    rev = fp.simulate_intent(make_df(rows), "LONG", 100.0, variant="reversion")
    mom = fp.simulate_intent(make_df(rows), "LONG", 100.0, variant="momentum")
    assert rev["triggered"] is True
    assert mom["arm_count"] == 0           # momentum needs +0.6, sees -1.0
    assert mom["used_fallback"] is True


# ==========================================================================
# constants are frozen by the pre-registration
# ==========================================================================
def test_frozen_constants_match_spec():
    assert fp.OFI_ARM_THRESHOLD == 0.6
    assert fp.VOLUME_PACE_MULT == 1.5
    assert fp.BURST_WINDOW == 5
    assert fp.TRAILING_VOL_WINDOW == 30
    assert fp.TRIGGER_WINDOW == 10
    assert fp.SCAN_START_MINUTE == 30
    assert fp.FALLBACK_MINUTE == 384


# ==========================================================================
# no dump benchmark -> fallback cannot price; entry None, delta_bps NaN-safe
# ==========================================================================
def test_no_dump_window_fallback_handles_missing_p_eod():
    # p_eod_dump passed as None (e.g. the caller could not compute it).
    rows = _flat_bars()        # no flow, never arms
    out = fp.simulate_intent(make_df(rows), "LONG", None)
    assert out["triggered"] is False
    assert out["used_fallback"] is True
    assert out["entry_price"] is None
    assert out["entry_minute"] is None
    assert math.isnan(out["delta_bps"])
