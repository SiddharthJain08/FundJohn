"""SP-6 B-flow Phase 1 — oracle & benchmark math (PURE functions).

No I/O, no DB, no network. Consumes minute bars produced upstream by
``minbar_cache`` and produces the per-intent oracle record described in the
Phase-1 kill-test design (§5).

Bar contract (one dict per minute):
    {minute: int 0..389 (offset from 09:30 ET),
     o, h, l, c, v, vw: float (any may be None or NaN)}

Achievable-price convention: the minute-bar VWAP (``vw``), never bar low/high
(microstructure fantasy). A bar is usable only if ``valid_bar`` passes.

Cost model (b1_simulator.py parity): the per-bar spread haircut is
``min(0.5*(h-l)/vw*1e4, 50.0)`` bps. b1's model also adds a fixed 2.0bps
market-impact term; we deliberately do NOT add it here, because Phase 1 nets
the oracle policy against the EOD-dump policy and BOTH pay that fixed impact
exactly once — so it cancels in the *differential* cost. The NET alpha is

    net = gross - (oracle_window_spread_bps - eod_dump_window_spread_bps)

The differential is SIGNED with no floor: an oracle window tighter than the
close (dump) window is a genuine benefit (net > gross), a wider one is a real
cost (net < gross).

NaN/None safety: every numeric field is coerced through ``_f`` (None -> NaN)
before any comparison, and all comparisons use the ``not (x > 0)`` idiom so the
``nan <= 0 is False`` trap that has poisoned this codebase cannot bite.
"""
from __future__ import annotations

import math


def _f(x):
    """Coerce a possibly-None numeric to float. None -> NaN (so it fails every
    ``> 0`` / ``>=`` guard without raising TypeError on ``None > 0``)."""
    if x is None:
        return float("nan")
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


# --------------------------------------------------------------------------
# bar-level predicates / metrics
# --------------------------------------------------------------------------
def valid_bar(bar):
    """A bar is usable iff vw>0 AND v>0 AND h>=l (all NaN/None-safe)."""
    vw = _f(bar.get("vw"))
    v = _f(bar.get("v"))
    h = _f(bar.get("h"))
    l = _f(bar.get("l"))
    if not (vw > 0):     # NaN/None/<=0 -> False
        return False
    if not (v > 0):
        return False
    if not (h >= l):     # NaN h or l -> False
        return False
    return True


def spread_bps(bar):
    """Half-spread proxy in bps, capped at 50 (b1_simulator parity):
    ``min(0.5*(h-l)/vw*1e4, 50.0)``. Assumes a valid bar (vw>0)."""
    vw = _f(bar.get("vw"))
    h = _f(bar.get("h"))
    l = _f(bar.get("l"))
    if not (vw > 0):
        return 0.0
    return min(0.5 * (h - l) / vw * 1e4, 50.0)


# --------------------------------------------------------------------------
# benchmarks
# --------------------------------------------------------------------------
def dump_benchmark(bars):
    """Volume-weighted mean vw over valid bars with minute >= 385 (the
    15:55-15:59 dump window). None if there are no valid bars there."""
    num = 0.0
    den = 0.0
    for b in bars:
        if b.get("minute") is None or b["minute"] < 385:
            continue
        if not valid_bar(b):
            continue
        vw = _f(b.get("vw"))
        v = _f(b.get("v"))
        num += vw * v
        den += v
    if not (den > 0):
        return None
    return num / den


def close_benchmark(bars):
    """The ``c`` of the valid bar with the largest minute. None if none valid."""
    best = None
    best_min = None
    for b in bars:
        if not valid_bar(b):
            continue
        m = b.get("minute")
        if m is None:
            continue
        if best_min is None or m > best_min:
            best_min = m
            best = _f(b.get("c"))
    return best


def eod_dump_window_spread_bps(bars):
    """Volume-weighted mean per-bar spread_bps over valid bars minute >= 385.
    None if no valid bars there (mirrors dump_benchmark coverage)."""
    num = 0.0
    den = 0.0
    for b in bars:
        if b.get("minute") is None or b["minute"] < 385:
            continue
        if not valid_bar(b):
            continue
        v = _f(b.get("v"))
        num += spread_bps(b) * v
        den += v
    if not (den > 0):
        return None
    return num / den


# --------------------------------------------------------------------------
# windows
# --------------------------------------------------------------------------
def window_stats(bars, window=15):
    """Every contiguous run of minutes [m, m+window) where ALL minutes
    m..m+window-1 have valid bars. Returns a list of dicts:
        {start_minute, vwap, spread_bps}
    vwap = sum(vw*v)/sum(v) over the window;
    spread_bps = volume-weighted mean of per-bar spread_bps.
    Windows slide by 1 minute over starts 0..390-window (inclusive)."""
    by_min = {}
    for b in bars:
        m = b.get("minute")
        if m is None:
            continue
        if valid_bar(b):
            by_min[m] = b   # last valid bar for a duplicated minute wins

    out = []
    for start in range(0, 390 - window + 1):
        minutes = range(start, start + window)
        if any(m not in by_min for m in minutes):
            continue
        num_vw = 0.0
        den_v = 0.0
        num_sp = 0.0
        for m in minutes:
            b = by_min[m]
            v = _f(b.get("v"))
            vw = _f(b.get("vw"))
            num_vw += vw * v
            den_v += v
            num_sp += spread_bps(b) * v
        if not (den_v > 0):
            continue
        out.append({
            "start_minute": start,
            "vwap": num_vw / den_v,
            "spread_bps": num_sp / den_v,
        })
    return out


# --------------------------------------------------------------------------
# oracles
# --------------------------------------------------------------------------
def _dir_sign_is_long(direction):
    return str(direction).upper() == "LONG"


def strict_oracle(bars, direction):
    """LONG -> valid bar with min vw; SHORT -> max vw. Ties: earliest minute.
    Returns {price, minute, spread_bps} or None if no valid bar."""
    is_long = _dir_sign_is_long(direction)
    best = None
    for b in bars:
        if not valid_bar(b):
            continue
        m = b.get("minute")
        if m is None:
            continue
        vw = _f(b.get("vw"))
        if best is None:
            best = (vw, m, b)
            continue
        cur_vw, cur_m, _ = best
        if is_long:
            improve = vw < cur_vw            # strict -> earliest tie wins
        else:
            improve = vw > cur_vw
        # earliest-minute tie-break: same price, earlier minute
        tie_earlier = (vw == cur_vw and m < cur_m)
        if improve or tie_earlier:
            best = (vw, m, b)
    if best is None:
        return None
    vw, m, b = best
    return {"price": vw, "minute": m, "spread_bps": spread_bps(b)}


def window_oracle(bars, direction, window=15):
    """LONG -> window with min vwap; SHORT -> max. Ties: earliest start.
    Returns {price, start_minute, spread_bps} or None if no full window."""
    stats = window_stats(bars, window=window)
    if not stats:
        return None
    is_long = _dir_sign_is_long(direction)
    best = None
    for s in stats:
        if best is None:
            best = s
            continue
        if is_long:
            improve = s["vwap"] < best["vwap"]
        else:
            improve = s["vwap"] > best["vwap"]
        # stats already ascend by start_minute, so first-found wins ties;
        # we only replace on strict improvement.
        if improve:
            best = s
    return {
        "price": best["vwap"],
        "start_minute": best["start_minute"],
        "spread_bps": best["spread_bps"],
    }


# --------------------------------------------------------------------------
# alpha
# --------------------------------------------------------------------------
def gross_bps(p_eod, p_orc, direction):
    """LONG -> (p_eod-p_orc)/p_eod*1e4 (profit from buying below the dump);
    SHORT -> (p_orc-p_eod)/p_eod*1e4 (profit from selling above the dump)."""
    p_eod = _f(p_eod)
    p_orc = _f(p_orc)
    if _dir_sign_is_long(direction):
        return (p_eod - p_orc) / p_eod * 1e4
    return (p_orc - p_eod) / p_eod * 1e4


def _flip(direction):
    return "SHORT" if _dir_sign_is_long(direction) else "LONG"


# --------------------------------------------------------------------------
# per-intent record (§5)
# --------------------------------------------------------------------------
_METRIC_KEYS = (
    "p_eod_dump", "p_eod_close",
    "strict_price", "strict_minute", "strict_gross_bps", "strict_net_bps",
    "win_price", "win_start_minute", "win_gross_bps", "win_net_bps",
    "flip_win_price", "flip_win_start_minute", "flip_win_gross_bps",
    "flip_win_net_bps", "excess_net_bps",
    "directed_in_last15", "flipped_in_last15",
)


def _excluded_record(n_valid, reason):
    rec = {k: None for k in _METRIC_KEYS}
    rec["n_valid_bars"] = n_valid
    rec["exclude_reason"] = reason
    return rec


def evaluate_intent(bars, direction, window=15):
    """Full per-intent record (§5). Computes the directed window/strict oracle,
    the sign-flipped null on the SAME bars, the signed-differential NET alpha,
    and the headline excess = directed_net - flipped_net.

    NET = gross - (oracle_window_spread_bps - eod_dump_window_spread_bps). The
    fixed 2bps impact of b1's model is NOT added: it is paid once under both the
    oracle and the EOD-dump policy, so it cancels in this differential.

    Exclusions (all metric fields None, exclude_reason set):
      n_valid_bars < 60 -> 'too_few_bars'
      dump_benchmark None -> 'no_dump_window'
      no valid contiguous window -> 'no_window'
    Otherwise exclude_reason is None.
    """
    n_valid = sum(1 for b in bars if valid_bar(b))
    if n_valid < 60:
        return _excluded_record(n_valid, "too_few_bars")

    p_eod_dump = dump_benchmark(bars)
    if p_eod_dump is None:
        return _excluded_record(n_valid, "no_dump_window")

    win = window_oracle(bars, direction, window=window)
    if win is None:
        return _excluded_record(n_valid, "no_window")

    p_eod_close = close_benchmark(bars)
    dump_spread = eod_dump_window_spread_bps(bars)
    if dump_spread is None:
        dump_spread = 0.0

    # --- strict oracle (directed) ---
    strict = strict_oracle(bars, direction)
    strict_gross = gross_bps(p_eod_dump, strict["price"], direction)
    strict_net = strict_gross - (strict["spread_bps"] - dump_spread)

    # --- directed window oracle ---
    win_gross = gross_bps(p_eod_dump, win["price"], direction)
    win_net = win_gross - (win["spread_bps"] - dump_spread)

    # --- sign-flipped window oracle on the SAME bars ---
    # The flipped statistic is what the directed prize WOULD have been had the
    # signal pointed the other way: opposite direction picks the opposite
    # window AND uses the opposite gross sign convention.
    flip_dir = _flip(direction)
    flip_win = window_oracle(bars, flip_dir, window=window)  # non-None (same window set)
    flip_gross = gross_bps(p_eod_dump, flip_win["price"], flip_dir)
    flip_net = flip_gross - (flip_win["spread_bps"] - dump_spread)

    excess_net = win_net - flip_net

    return {
        "p_eod_dump": p_eod_dump,
        "p_eod_close": p_eod_close,
        "strict_price": strict["price"],
        "strict_minute": strict["minute"],
        "strict_gross_bps": strict_gross,
        "strict_net_bps": strict_net,
        "win_price": win["price"],
        "win_start_minute": win["start_minute"],
        "win_gross_bps": win_gross,
        "win_net_bps": win_net,
        "flip_win_price": flip_win["price"],
        "flip_win_start_minute": flip_win["start_minute"],
        "flip_win_gross_bps": flip_gross,
        "flip_win_net_bps": flip_net,
        "excess_net_bps": excess_net,
        "directed_in_last15": win["start_minute"] >= 375,
        "flipped_in_last15": flip_win["start_minute"] >= 375,
        "n_valid_bars": n_valid,
        "exclude_reason": None,
    }


# --------------------------------------------------------------------------
# kill-criterion helpers (pure; used by the driver)
# --------------------------------------------------------------------------
def per_session_median(values):
    """Median of a list of floats. None if empty."""
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])


def kill_verdict(per_session_medians, min_med_bps=2.0, min_frac_pos=0.60):
    """Pre-committed kill criterion (§6.3) on the NULL-RELATIVE excess.

    per_session_medians: dict session -> median_excess (bps).
    GO requires BOTH:
      (a) across-session median of the per-session medians > min_med_bps, AND
      (b) fraction of sessions with a strictly-positive per-session median
          >= min_frac_pos.

    Returns {go, median_of_medians, frac_pos_sessions, n_sessions}.
    """
    meds = [m for m in per_session_medians.values() if m is not None]
    n = len(meds)
    mom = per_session_median(meds)
    if n == 0:
        return {"go": False, "median_of_medians": None,
                "frac_pos_sessions": 0.0, "n_sessions": 0}
    n_pos = sum(1 for m in meds if m > 0)
    frac_pos = n_pos / n
    go = (mom is not None) and (mom > min_med_bps) and (frac_pos >= min_frac_pos)
    return {
        "go": go,
        "median_of_medians": mom,
        "frac_pos_sessions": frac_pos,
        "n_sessions": n,
    }
