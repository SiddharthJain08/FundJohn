"""SP-6 B-flow Phase 1b — Test B: causal order-flow proxy policy (SUPPORTIVE).

No I/O, no DB, no network. Implements spec §4 of the Phase-1b pre-registration
(``docs/superpowers/specs/2026-06-05-sp6-bflow-phase1b-flow-predictability-prereg.md``)
EXACTLY. Test B is SUPPORTIVE, NOT GATING; ALL constants are frozen in spec §4
and copied verbatim into the module-level constants below — DO NOT TUNE.

The policy is a causal arm/trigger state machine that submits ONE intent per
(ticker, session), entering at a minute-bar VWAP, and scores it against the
Phase-1 EOD-dump benchmark NET of the differential spread cost — mirroring
``oracle.evaluate_intent`` byte-for-byte on the cost side (the fixed +2bps impact
of the b1 model cancels in the differential and is therefore never added).

State machine (LONG, variant='reversion', spec §4 verbatim):
  * scan minutes t >= SCAN_START_MINUTE (30) in order.
  * ARM at minute b where ofi_5(b) <= -OFI_ARM_THRESHOLD (-0.6) AND
    Σ_{b-4..b} v >= VOLUME_PACE_MULT (1.5) × BURST_WINDOW (5) ×
    (mean minute volume over the trailing TRAILING_VOL_WINDOW (30) minutes
    [b-29..b]).  i.e. the 5-minute burst volume is at least 1.5× the volume the
    trailing-30 pace would have produced over 5 minutes.
  * TRIGGER = the first t in (b, b+TRIGGER_WINDOW] (b, b+10] with c_t > c_{t-1};
    if none within 10 minutes, DISARM and keep scanning (later bursts re-arm).
  * On trigger at t: entry FILL = vw_{t+1} (decision at end of minute t, fill in
    the next minute — causal). If the t+1 bar is missing/invalid the trigger
    LAPSES and scanning continues from t+1 (conservative, causal).
  * FORCED FALLBACK: if no entry by minute FALLBACK_MINUTE (384), fill at
    p_eod_dump with used_fallback=True. A fallback intent has delta_bps = 0 by
    construction (it entered at the dump) — honestly diluting the mean toward 0.

SHORT mirrors exactly: arm at ofi_5 >= +OFI_ARM_THRESHOLD (a buy burst), trigger
= the first down-minute c_t < c_{t-1}, sell at vw_{t+1}.

variant='momentum' (DIAGNOSTIC ONLY, never gating): the identical machine with
ONLY the arm OFI sign flipped (LONG arms on ofi_5 >= +0.6, SHORT on <= -0.6);
the trigger/confirm conditions are UNCHANGED.

delta_bps (positive = beats the dump NET of differential cost), mirroring
``oracle.py`` exactly:
  LONG  gross = (p_eod_dump - entry_price)/p_eod_dump * 1e4
  SHORT gross = (entry_price - p_eod_dump)/p_eod_dump * 1e4
  net   = gross - (spread_bps(entry-minute bar) - dump-window spread_bps)
The +2bps impact term cancels in the differential, exactly as ``evaluate_intent``
documents, so we reuse ``oracle.gross_bps`` / ``oracle.spread_bps`` /
``oracle.eod_dump_window_spread_bps`` rather than re-deriving the math.
"""
from __future__ import annotations

import math

import numpy as np

from src.research.bflow import flow_features as ff
from src.research.bflow import oracle

# --------------------------------------------------------------------------
# Constants — frozen by pre-registration (spec §4) — do not tune.
# --------------------------------------------------------------------------
OFI_ARM_THRESHOLD = 0.6      # |ofi_5| arm gate (LONG <= -0.6, SHORT >= +0.6)
VOLUME_PACE_MULT = 1.5       # burst must be >= 1.5x the trailing pace
BURST_WINDOW = 5             # the 5-minute burst window [b-4..b]
TRAILING_VOL_WINDOW = 30     # trailing pace window [b-29..b]
TRIGGER_WINDOW = 10          # trigger search window (b, b+10]
SCAN_START_MINUTE = 30       # scan minutes t >= 30
FALLBACK_MINUTE = 384        # no entry by minute 384 -> forced dump fallback

_VALID_VARIANTS = ("reversion", "momentum")


def _is_long(direction):
    return str(direction).upper() == "LONG"


def _arm_sign_is_negative(direction, variant):
    """Does the arm condition look for a SELL burst (ofi_5 <= -0.6)?

    reversion LONG  -> sell burst (True)      reversion SHORT -> buy burst (False)
    momentum  LONG  -> buy  burst (False)     momentum  SHORT -> sell burst (True)

    Only the ARM OFI sign flips between variants (spec §4); trigger/confirm are
    unchanged and handled separately by ``_is_long``."""
    long_side = _is_long(direction)
    if variant == "momentum":
        long_side = not long_side
    # reversion: LONG arms on negative flow, SHORT on positive.
    return long_side


def _fnum(x):
    """Coerce to float; None / un-floatable -> NaN (so every `> 0` guard fails
    NaN-safely via the `not (x > 0)` idiom)."""
    if x is None:
        return float("nan")
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def simulate_intent(bars_df, direction, p_eod_dump, variant="reversion"):
    """Run the spec-§4 causal proxy policy for one (ticker, session).

    Parameters
    ----------
    bars_df : DataFrame with columns minute,o,h,l,c,v,vw (the per-(ticker,
        session) contract; gaps/duplicates tolerated — reindexed internally).
    direction : 'LONG' or 'SHORT'.
    p_eod_dump : the Phase-1 dump benchmark price (float) or None/NaN.
    variant : 'reversion' (default, the pre-registered rule) or 'momentum'
        (DIAGNOSTIC ONLY — only the arm OFI sign flips).

    Returns a dict with keys: triggered (bool), entry_minute (int|None),
    entry_price (float|None), used_fallback (bool), delta_bps (float),
    arm_count (int).

    Strictly causal: the decision at minute t reads only bars with minute <= t,
    and the fill at vw_{t+1} reads only bar t+1; nothing after the fill minute is
    ever consulted (the LOOKAHEAD-TRAP invariant)."""
    if variant not in _VALID_VARIANTS:
        raise ValueError(f"variant must be one of {_VALID_VARIANTS}, got {variant!r}")

    # Reindexed valid frame (minute 0..389; OHLCV NaN on invalid/missing rows) +
    # the boolean validity mask, EXACTLY mirroring flow_features / oracle.
    work, valid = ff._reindex_valid_frame(bars_df)

    # ofi_5 over the spec window (trailing, NaN where incomplete/invalid). Reuse
    # flow_features so the feature definition is single-sourced (frozen in §3).
    feats = ff.compute_features(bars_df)
    ofi5 = feats["ofi_5"]

    # Per-minute arrays (NaN on invalid/missing rows).
    c = work["c"]
    v = work["v"]
    vw = work["vw"]

    long_side = _is_long(direction)
    arm_negative = _arm_sign_is_negative(direction, variant)

    arm_count = 0
    scan_min = SCAN_START_MINUTE

    # Forward scan for an arm; on each arm look for a trigger in its 10-min
    # window. scan_min advances on disarm (b+1) and on lapse (trigger t -> t+1).
    while scan_min <= FALLBACK_MINUTE - 1:   # need room for at least a t+1 fill
        armed_at = None
        for b in range(scan_min, FALLBACK_MINUTE):   # b can be up to 383
            if _arm_condition(ofi5, v, b, arm_negative):
                armed_at = b
                break
        if armed_at is None:
            break                                    # no further arm possible

        arm_count += 1
        b = armed_at

        # TRIGGER search in (b, b+10].
        trigger_t = None
        for t in range(b + 1, b + TRIGGER_WINDOW + 1):
            if t > FALLBACK_MINUTE:                  # fill (t+1) would be > 385
                break
            if _trigger_condition(c, t, long_side):
                trigger_t = t
                break

        if trigger_t is None:
            scan_min = b + 1                         # DISARM; re-scan from b+1
            continue

        t = trigger_t
        fill_min = t + 1
        # Entry fill = vw_{t+1}. Lapse if the t+1 bar is missing/invalid OR the
        # fill would land past the fallback minute (no entry by 384 -> fallback).
        if fill_min > FALLBACK_MINUTE or not bool(valid.get(fill_min, False)):
            scan_min = t + 1                         # LAPSE; continue from t+1
            continue

        entry_price = float(vw.loc[fill_min])
        entry_spread = oracle.spread_bps({
            "vw": entry_price,
            "h": float(work["h"].loc[fill_min]),
            "l": float(work["l"].loc[fill_min]),
        })
        delta = _delta_bps(p_eod_dump, entry_price, entry_spread, bars_df, direction)
        return {
            "triggered": True,
            "entry_minute": int(fill_min),
            "entry_price": entry_price,
            "used_fallback": False,
            "delta_bps": delta,
            "arm_count": int(arm_count),
        }

    # FORCED FALLBACK — no causal entry by minute 384: fill at the dump.
    p = _fnum(p_eod_dump)
    if not np.isfinite(p):
        fb_price = None
        fb_delta = float("nan")
    else:
        fb_price = float(p)
        fb_delta = 0.0                               # entered AT the dump -> 0 bps
    return {
        "triggered": False,
        "entry_minute": None,
        "entry_price": fb_price,
        "used_fallback": True,
        "delta_bps": fb_delta,
        "arm_count": int(arm_count),
    }


# --------------------------------------------------------------------------
# arm / trigger predicates (pure; strictly trailing)
# --------------------------------------------------------------------------
def _arm_condition(ofi5, v, b, arm_negative):
    """True iff minute ``b`` satisfies the spec-§4 arm gate.

    OFI gate (sign per ``arm_negative``):
        sell burst  -> ofi_5(b) <= -OFI_ARM_THRESHOLD
        buy  burst  -> ofi_5(b) >= +OFI_ARM_THRESHOLD
    NaN ofi_5 (incomplete/invalid window) fails BOTH via the `not (x op y)`
    idiom. Volume gate: Σ_{b-4..b} v >= 1.5 × 5 × mean(v over [b-29..b]); a NaN
    in EITHER window (missing/invalid bar) makes the sum NaN -> the gate fails."""
    o = float(ofi5.loc[b])
    if arm_negative:
        # NaN ofi_5 -> (NaN <= -0.6) is False -> not False -> return False.
        if not (o <= -OFI_ARM_THRESHOLD):
            return False
    else:
        if not (o >= OFI_ARM_THRESHOLD):
            return False

    # Volume burst + trailing pace. Windows are over MINUTE INDICES; any missing
    # minute (NaN volume on the reindexed frame) poisons the sum -> NaN -> fail.
    if b - (BURST_WINDOW - 1) < 0 or b - (TRAILING_VOL_WINDOW - 1) < 0:
        return False                                 # window runs off the open
    burst_sum = float(v.loc[b - (BURST_WINDOW - 1):b].sum(min_count=BURST_WINDOW))
    trail_sum = float(
        v.loc[b - (TRAILING_VOL_WINDOW - 1):b].sum(min_count=TRAILING_VOL_WINDOW))
    if not (burst_sum >= 0) or not (trail_sum >= 0):  # NaN (incomplete) -> fail
        return False
    mean30 = trail_sum / TRAILING_VOL_WINDOW
    threshold = VOLUME_PACE_MULT * BURST_WINDOW * mean30
    return bool(burst_sum >= threshold)


def _trigger_condition(c, t, long_side):
    """LONG confirm = first UP minute (c_t > c_{t-1}); SHORT confirm = first DOWN
    minute (c_t < c_{t-1}). A NaN close at t or t-1 (missing/invalid bar) fails
    both comparisons via the `not (x op y)` idiom (NaN comparisons are False)."""
    ct = float(c.loc[t])
    cp = float(c.loc[t - 1])
    if long_side:
        return bool(ct > cp)
    return bool(ct < cp)


# --------------------------------------------------------------------------
# delta_bps (oracle.py parity — differential spread, +2bps impact cancels)
# --------------------------------------------------------------------------
def _delta_bps(p_eod_dump, entry_price, entry_spread, bars_df, direction):
    """Per-intent Δbps vs the dump, NET of the differential spread cost. Mirrors
    ``oracle.evaluate_intent``: net = gross - (entry_spread - dump_spread). The
    fixed +2bps impact cancels in the differential and is never added.

    NaN-safe: a None/NaN dump price -> NaN delta (the fill could not be priced)."""
    p = _fnum(p_eod_dump)
    if not np.isfinite(p):
        return float("nan")
    gross = oracle.gross_bps(p, entry_price, direction)
    dump_spread = oracle.eod_dump_window_spread_bps(_bars_records(bars_df))
    if dump_spread is None:
        dump_spread = 0.0
    return gross - (entry_spread - dump_spread)


def _bars_records(bars_df):
    """The bars as a list of dicts for ``oracle.eod_dump_window_spread_bps``
    (which iterates dicts). The raw input is used (NOT the reindexed frame) so
    the dump window is computed identically to ``oracle.dump_benchmark``."""
    return bars_df.to_dict("records")
