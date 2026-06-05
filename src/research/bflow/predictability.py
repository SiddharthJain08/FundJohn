"""SP-6 B-flow Phase 1b — Test A: order-flow predictive structure (GATING).

Implements spec §3 of the Phase-1b pre-registration
(``docs/superpowers/specs/2026-06-05-sp6-bflow-phase1b-flow-predictability-prereg.md``)
EXACTLY — every window, sign convention, t-range and NaN rule lives in
``flow_features`` (frozen there); this module adds the cross-sectional statistic
and the across-session aggregation, and zero tuning knobs.

The statistic (§3):
  * For ONE session, pool ALL valid (ticker, minute) observations across the
    session's tickers and compute the Spearman rank IC (pandas ``.rank()`` then
    Pearson on the ranks — NO scipy) between a feature and a target. One IC per
    session per cell.
  * Across sessions: mean IC, sd IC, and t = mean(IC) / (sd(IC)/√n_sessions).
    **The SESSION is the cluster** — the t is computed across sessions only,
    NEVER from a pooled standard error (that would mistake within-session
    autocorrelated minutes for independent draws).

Cells = every (feature ∈ {ofi_5, ofi_15, vwap_disp_30}) × (target ∈
{ret_to_dump, ret_fwd_5, ret_fwd_15, ret_fwd_30, ret_fwd_60}) = 15 cells, in a
fixed deterministic order so the IC grid / summary are byte-stable run-to-run.

Three independent inclusion gates, kept separate:
  1. ticker-level — a (ticker, session) pair participates only if it passes the
     Phase-1 60-valid-bar floor (``oracle.valid_bar`` count ≥ 60). Fail ⇒ the
     ticker contributes to NO cell.
  2. pair-level — an observation enters a cell's pool only if BOTH that cell's
     feature AND target are finite at that (ticker, minute). (Per cell, because
     the targets have different valid minute ranges: ret_to_dump∈[30,330],
     ret_fwd_60∈[30,329], …)
  3. cell-level — a session-cell with fewer than ``MIN_OBS_PER_SESSION_CELL``
     pooled observations is NaN.

A NaN/None dump price for a ticker only NaNs that ticker's ``ret_to_dump``
column (handled inside ``flow_features.compute_targets``); its ``ret_fwd_*``
columns still pool.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.research.bflow import flow_features as ff
from src.research.bflow import oracle

# Implementer robustness floor — a session-cell pooled over fewer than this many
# valid (feature, target) pairs is reported NaN. This is a DATA-QUALITY floor
# (it keeps a 3-observation IC out of the across-session mean), NOT a spec
# threshold; the spec fixes none. Reported in the data-quality table, not the
# verdict.
MIN_OBS_PER_SESSION_CELL = 30

# Minimum valid bars for a (ticker, session) to participate at all (the Phase-1
# floor reused verbatim — see oracle.evaluate_intent's 'too_few_bars' guard).
MIN_VALID_BARS = 60

_FEATURES = ("ofi_5", "ofi_15", "vwap_disp_30")
_TARGETS = ("ret_to_dump", "ret_fwd_5", "ret_fwd_15", "ret_fwd_30", "ret_fwd_60")

# Delimiter chosen so it cannot collide with the underscores already present in
# every feature/target name (ofi_5, ret_fwd_5, …); a plain '_' join would be
# ambiguous.
_CELL_SEP = "|"


def cell_name(feature, target):
    """Stable name for the (feature, target) cell."""
    return f"{feature}{_CELL_SEP}{target}"


# Fixed-order list of all 15 cells (feature-major). Every public structure keys
# off this so the grid columns / summary index are deterministic.
CELLS = tuple(cell_name(f, t) for f in _FEATURES for t in _TARGETS)


# --------------------------------------------------------------------------
# Spearman rank IC (pure; no scipy)
# --------------------------------------------------------------------------
def _spearman_ic(x, y):
    """Spearman rank IC between two aligned Series: drop pairs where either side
    is NaN, then Pearson correlation of the ranks (``method='average'`` ties,
    pandas default). Returns NaN if fewer than ``MIN_OBS_PER_SESSION_CELL`` pairs
    survive OR either ranked vector is constant (zero variance ⇒ correlation
    undefined; we guard explicitly so numpy never emits a RuntimeWarning)."""
    x = pd.Series(x).reset_index(drop=True)
    y = pd.Series(y).reset_index(drop=True)
    both = pd.concat([x, y], axis=1)
    both.columns = ["x", "y"]
    both = both.dropna()
    n = len(both)
    if n < MIN_OBS_PER_SESSION_CELL:
        return float("nan")
    rx = both["x"].rank()              # average-rank ties
    ry = both["y"].rank()
    # zero-variance guard: a constant input (or all-tied ranks) -> undefined.
    if not (rx.std(ddof=0) > 0) or not (ry.std(ddof=0) > 0):
        return float("nan")
    # Pearson on the ranks == Spearman.
    rxv = rx.to_numpy()
    ryv = ry.to_numpy()
    rxc = rxv - rxv.mean()
    ryc = ryv - ryv.mean()
    denom = np.sqrt((rxc * rxc).sum() * (ryc * ryc).sum())
    if not (denom > 0):
        return float("nan")
    return float((rxc * ryc).sum() / denom)


# --------------------------------------------------------------------------
# ticker validity (Phase-1 60-bar floor)
# --------------------------------------------------------------------------
def _valid_bar_count(bars):
    """Count valid bars in a (ticker, session) input, matching ``oracle.valid_bar``
    (vw>0 AND v>0 AND h>=l, NaN-safe) exactly.

    Accepts either a DataFrame (the documented ``bars df`` input) or a list of
    bar dicts (the ``minbar_cache`` shape) so the function is robust to either
    upstream producer."""
    if isinstance(bars, pd.DataFrame):
        df = bars
        for col in ("vw", "v", "h", "l"):
            if col not in df.columns:
                return 0
        vw = pd.to_numeric(df["vw"], errors="coerce")
        v = pd.to_numeric(df["v"], errors="coerce")
        h = pd.to_numeric(df["h"], errors="coerce")
        l = pd.to_numeric(df["l"], errors="coerce")
        valid = ((vw > 0) & (v > 0) & (h >= l)).fillna(False)
        return int(valid.sum())
    # list of dicts -> reuse oracle.valid_bar verbatim.
    return sum(1 for b in bars if oracle.valid_bar(b))


def _to_frame(bars):
    """Normalize a (ticker, session) bars input to a DataFrame for
    ``flow_features``. A DataFrame passes through; a list of dicts is wrapped."""
    if isinstance(bars, pd.DataFrame):
        return bars
    return pd.DataFrame(list(bars))


# --------------------------------------------------------------------------
# per-session pooled ICs
# --------------------------------------------------------------------------
def session_ics(session_bars, dump_prices):
    """Per-session pooled Spearman rank IC for every cell.

    ``session_bars``: dict ticker -> bars (DataFrame per the task contract, or a
    list of bar dicts). ``dump_prices``: dict ticker -> P_eod_dump float (a
    missing / NaN entry only NaNs that ticker's ret_to_dump — its ret_fwd_*
    still pool).

    Returns dict cell_name -> IC (NaN where uncomputable). Always returns all 15
    cell keys.

    Pooling is across BOTH ticker and minute within the session. A (ticker,
    session) participates only if it passes the Phase-1 60-valid-bar floor; an
    observation enters a cell only if both that cell's feature and target are
    finite there; a cell with < MIN_OBS_PER_SESSION_CELL pooled pairs is NaN.
    """
    feat_cols = {f: [] for f in _FEATURES}
    tgt_cols = {t: [] for t in _TARGETS}

    for ticker in sorted(session_bars):       # sorted -> deterministic pooling
        bars = session_bars[ticker]
        if _valid_bar_count(bars) < MIN_VALID_BARS:
            continue                          # gate 1: ticker-level floor
        df = _to_frame(bars)
        feats = ff.compute_features(df)       # minute 0..389 index
        tgts = ff.compute_targets(df, dump_prices.get(ticker))
        for f in _FEATURES:
            feat_cols[f].append(feats[f])
        for t in _TARGETS:
            tgt_cols[t].append(tgts[t])

    # Pool each feature / target column across all qualifying tickers (stacked;
    # the minute index repeats across tickers — that is the intended (ticker,
    # minute) pooling).
    pooled_feat = {f: (pd.concat(feat_cols[f], ignore_index=True)
                       if feat_cols[f] else pd.Series(dtype=float))
                   for f in _FEATURES}
    pooled_tgt = {t: (pd.concat(tgt_cols[t], ignore_index=True)
                      if tgt_cols[t] else pd.Series(dtype=float))
                  for t in _TARGETS}

    out = {}
    for f in _FEATURES:
        for t in _TARGETS:
            out[cell_name(f, t)] = _spearman_ic(pooled_feat[f], pooled_tgt[t])
    return out


# --------------------------------------------------------------------------
# grid + summary
# --------------------------------------------------------------------------
def ic_grid(per_session_ics):
    """Assemble a sessions × cells DataFrame from
    ``per_session_ics`` = list of (session_label, {cell_name: ic}).

    Columns are the fixed 15 cells in ``CELLS`` order; a session dict missing a
    cell yields NaN there. Index = the session labels in input order."""
    sessions = [s for s, _ in per_session_ics]
    data = {c: [] for c in CELLS}
    for _, ics in per_session_ics:
        for c in CELLS:
            v = ics.get(c, float("nan"))
            data[c].append(np.nan if v is None else v)
    grid = pd.DataFrame(data, index=sessions, columns=list(CELLS))
    grid.index.name = "session"
    return grid


def summarize(grid):
    """Per cell: mean_ic, sd_ic, n_sessions (non-NaN), and the clustered t =
    mean_ic / (sd_ic / √n_sessions).

    sd is the SAMPLE sd (ddof=1 — the clustered-t convention); n_sessions counts
    only non-NaN session-cells; n < 2 ⇒ sd NaN ⇒ t NaN (never inf/raise). The
    across-session t is the ONLY t — never a pooled SE."""
    rows = {}
    for c in CELLS:
        col = grid[c] if c in grid.columns else pd.Series(dtype=float)
        vals = col.dropna()
        n = int(len(vals))
        mean_ic = float(vals.mean()) if n > 0 else float("nan")
        sd_ic = float(vals.std(ddof=1)) if n > 1 else float("nan")
        if n > 1 and (sd_ic > 0):
            t = mean_ic / (sd_ic / np.sqrt(n))
        else:
            t = float("nan")
        rows[c] = {"mean_ic": mean_ic, "sd_ic": sd_ic,
                   "n_sessions": n, "t": t}
    summ = pd.DataFrame.from_dict(rows, orient="index",
                                  columns=["mean_ic", "sd_ic", "n_sessions", "t"])
    summ = summ.reindex(list(CELLS))
    summ.index.name = "cell"
    return summ
