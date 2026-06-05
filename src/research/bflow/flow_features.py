"""SP-6 B-flow Phase 1b — order-flow features & forward targets (PURE).

No I/O, no DB, no network. Implements spec §3 of the Phase-1b pre-registration
(``docs/superpowers/specs/2026-06-05-sp6-bflow-phase1b-flow-predictability-prereg.md``)
EXACTLY — every window, sign convention, t-range and NaN rule is frozen there;
this module adds zero tuning knobs.

Input contract (one per (ticker, session) DataFrame): columns
``minute`` (int 0..389, offset from 09:30 ET; gaps possible, duplicates
tolerated — for a duplicated minute the last VALID row wins, exactly mirroring
``oracle.window_stats`` (``if valid_bar(b): by_min[m] = b``); a valid row is
never overwritten by a later invalid duplicate), ``o, h, l, c, v, vw`` (any may
be NaN). Output is always reindexed to the full minute axis 0..389 (index name
``minute``). The real session cache never produces duplicate minutes
(``get_session_bars`` never refetches a cached ticker and ``normalize_rth``
emits one bar per rounded minute); this rule only matters for synthetic input.

Validity (mirrors ``oracle.valid_bar``): a bar is usable iff vw>0 AND v>0 AND
h>=l, evaluated with the ``not (x > 0)`` idiom so the ``nan <= 0 is False`` trap
cannot bite. Here that idiom falls out for free from pandas comparisons:
``NaN > 0`` is False, so ``(vw > 0) & (v > 0) & (h >= l)`` is the valid mask and
an absent (reindexed-NaN) or invalid bar both evaluate to False. We NULL every
OHLCV column on invalid rows so an invalid-but-present bar poisons its trailing
window byte-identically to a true gap.

Features at minute t (trailing-only, inclusive of t, NOTHING after t):
  ofi_5  = Σ_{i=t-4..t} sign(c_i − c_{i-1})·v_i / Σ_{i=t-4..t} v_i     ∈ [−1,+1]
  ofi_15 = same, 15-min window
  vwap_disp_30 = (c_t − VWAP_{t-29..t}) / VWAP_{t-29..t}, VWAP = Σ v·vw / Σ v

  sign(0)=0 (a flat bar contributes 0 to the numerator but its volume STILL
  counts in the denominator). Minute 0 uses Δc = c−o of the SAME bar (no
  predecessor reference). For every other window bar i, Δc_i = c_i − c_{i-1};
  the first window bar's predecessor (minute t−k, OUTSIDE the window) must be a
  valid bar — if c_{t−k} is missing/invalid, Δc is NaN and the feature is NaN
  ("prior-close matters" — the spec-faithful reading: it special-cases ONLY
  minute 0 and invents no fallback for an interior first-bar predecessor).

  A feature is NaN whenever the trailing window is incomplete (t<k−1 for ofi_k,
  t<29 for vwap_disp_30) OR any bar in the window is invalid/missing OR (ofi
  only) the first window bar's predecessor is invalid/missing. ``np.sign``
  carries NaN through (``sign(NaN)=NaN``), so a NaN Δc poisons the window rather
  than silently collapsing to a 0 contribution.

Targets (forward simple returns from c_t):
  ret_to_dump = p_eod_dump/c_t − 1, for t ∈ [30, 330] inclusive (NaN outside, or
                when bar t invalid/missing, or when p_eod_dump is None/NaN).
  ret_fwd_k   = c_{t+k}/c_t − 1, k ∈ {5,15,30,60}, t ∈ [30, 389−k] inclusive
                (NaN outside the range, or when EITHER the base bar t or the
                forward endpoint bar t+k is invalid/missing).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_MINUTES = range(0, 390)
_OHLCV = ["o", "h", "l", "c", "v", "vw"]

OFI_WINDOWS = (("ofi_5", 5), ("ofi_15", 15))
VWAP_DISP_WINDOW = 30
FWD_HORIZONS = (5, 15, 30, 60)

# PRIMARY-target t-range (spec §3) and forward-target lower bound.
_RET_TO_DUMP_LO = 30
_RET_TO_DUMP_HI = 330
_RET_FWD_LO = 30
_LAST_MINUTE = 389


def _reindex_valid_frame(df):
    """Dedup on minute (last VALID row wins), reindex to the full 0..389 axis,
    and return a frame where EVERY OHLCV column is NaN on any row that is not a
    valid bar (absent rows are NaN already). Index name is ``minute``.

    Validity = (vw>0) & (v>0) & (h>=l), NaN-safe (NaN>0 is False), matching
    ``oracle.valid_bar``. Dedup mirrors ``oracle.window_stats`` exactly: a valid
    duplicate is never overwritten by a later INVALID one (we order valid rows
    after invalid within each minute, then keep='last')."""
    work = df.copy()
    # Guarantee numeric dtype so comparisons are NaN-safe (object cols with None
    # would raise on `> 0`). Missing columns -> all-NaN.
    for col in ["minute"] + _OHLCV:
        if col not in work.columns:
            work[col] = np.nan
        work[col] = pd.to_numeric(work[col], errors="coerce")

    # Drop rows with a non-finite / out-of-grid minute before reindexing.
    work = work[work["minute"].notna()].copy()
    work["minute"] = work["minute"].astype("int64")

    # Row-level validity (computed BEFORE dedup so a valid duplicate outranks a
    # later invalid one — exact oracle.window_stats parity). Stable sort by
    # validity puts the valid row(s) last within each minute -> keep='last'.
    row_valid = ((work["vw"] > 0) & (work["v"] > 0)
                 & (work["h"] >= work["l"])).fillna(False)
    work = work.assign(_valid=row_valid)
    work = work.sort_values("_valid", kind="stable")          # False then True
    work = work.drop_duplicates(subset="minute", keep="last")  # last valid wins
    work = work.drop(columns="_valid")

    work = work.set_index("minute").reindex(_MINUTES)
    work.index.name = "minute"

    valid = (work["vw"] > 0) & (work["v"] > 0) & (work["h"] >= work["l"])
    valid = valid.fillna(False)
    # Null OHLCV on invalid rows so they poison windows identically to gaps.
    for col in _OHLCV:
        work.loc[~valid, col] = np.nan
    return work, valid


def _delta_c(work):
    """Δc per minute: minute 0 uses c−o (same bar, no predecessor); every other
    minute uses c_t − c_{t-1}. A NaN'd (invalid/missing) bar -> NaN Δc, and a
    NaN predecessor -> NaN Δc, both via the diff. ``c.diff()`` already produces
    NaN at minute 0 (no predecessor); we overwrite it with c0 − o0."""
    c = work["c"]
    dc = c.diff()                      # NaN at minute 0
    dc.iloc[0] = work["c"].iloc[0] - work["o"].iloc[0]
    return dc


def _ofi(work, dc, k):
    """ofi_k = Σ_{i=t-k+1..t} sign(Δc_i)·v_i / Σ v_i, strictly trailing.

    ``np.sign`` preserves NaN (sign(NaN)=NaN) so any NaN Δc in the window
    propagates; ``min_periods=k`` forces t<k−1 -> NaN and makes ANY NaN in the
    window collapse the count below k -> NaN. The denominator uses the SAME
    nulled volume so a flat bar (sign 0) keeps its volume weight."""
    v = work["v"]
    signed = np.sign(dc) * v
    num = signed.rolling(window=k, min_periods=k).sum()
    den = v.rolling(window=k, min_periods=k).sum()
    out = num / den               # den>0 always when complete (v>0 on valid)
    return out


def _vwap_disp(work, k):
    """vwap_disp_k = (c_t − VWAP_{t-k+1..t}) / VWAP, VWAP = Σ v·vw / Σ v over the
    trailing k minutes. NaN if window incomplete or any bar invalid/missing
    (v is NaN on those rows -> rolling sum NaN)."""
    v = work["v"]
    vw = work["vw"]
    num_vwap = (v * vw).rolling(window=k, min_periods=k).sum()
    den_vwap = v.rolling(window=k, min_periods=k).sum()
    vwap = num_vwap / den_vwap
    return (work["c"] - vwap) / vwap


def compute_features(df):
    """Spec §3 features for one (ticker, session). Returns a DataFrame indexed
    by minute 0..389 with columns ``ofi_5, ofi_15, vwap_disp_30``.

    Pure; strictly trailing (no value at minute t depends on any bar after t)."""
    work, _valid = _reindex_valid_frame(df)
    dc = _delta_c(work)

    out = pd.DataFrame(index=pd.Index(_MINUTES, name="minute"))
    for name, k in OFI_WINDOWS:
        out[name] = _ofi(work, dc, k)
    out["vwap_disp_30"] = _vwap_disp(work, VWAP_DISP_WINDOW)
    return out


def compute_targets(df, p_eod_dump):
    """Spec §3 forward targets for one (ticker, session). Returns a DataFrame
    indexed by minute 0..389 with columns
    ``ret_to_dump, ret_fwd_5, ret_fwd_15, ret_fwd_30, ret_fwd_60``.

    ret_to_dump = p_eod_dump/c_t − 1 for t ∈ [30, 330] (NaN outside, or bar t
    invalid/missing, or p_eod_dump None/NaN). ret_fwd_k = c_{t+k}/c_t − 1 for
    t ∈ [30, 389−k] (NaN outside, or base/endpoint bar invalid/missing)."""
    work, valid = _reindex_valid_frame(df)
    c = work["c"]                    # NaN on invalid/missing rows

    out = pd.DataFrame(index=pd.Index(_MINUTES, name="minute"))

    # --- ret_to_dump (PRIMARY) ---
    p = _coerce_float(p_eod_dump)
    if p is None or not np.isfinite(p):
        out["ret_to_dump"] = np.nan
    else:
        rtd = p / c - 1.0           # NaN where c is NaN (invalid/missing bar)
        in_range = (out.index >= _RET_TO_DUMP_LO) & (out.index <= _RET_TO_DUMP_HI)
        out["ret_to_dump"] = rtd.where(in_range)

    # --- ret_fwd_k (SECONDARY) ---
    for k in FWD_HORIZONS:
        c_fwd = c.shift(-k)         # NaN if endpoint invalid/missing
        ret = c_fwd / c - 1.0       # NaN if base OR endpoint NaN
        hi = _LAST_MINUTE - k
        in_range = (out.index >= _RET_FWD_LO) & (out.index <= hi)
        out[f"ret_fwd_{k}"] = ret.where(in_range)

    return out


def _coerce_float(x):
    """None / un-floatable -> None; otherwise float(x). NaN passes through as a
    float and is rejected by the np.isfinite guard at the call site."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
