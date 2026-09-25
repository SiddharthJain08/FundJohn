"""
Tests for the S_sparse_cca_mean_revert live/backtest divergence fix
(.superpowers/sdd/2026-09-25-sparse-cca-zero-signals/task-1-brief.md).

Root cause (confirmed by Step 0 on a bounded, column-projected,
date-filtered read of the last ~300 sessions of data/master/prices.parquet):
`rets = px.pct_change().dropna()` is a ROW-WISE any-NaN drop across the
full live universe (~5.8k tickers) -- one NaN anywhere in a row empties the
row, so `len(rets) < LOOKBACK` fires and the strategy returns `[]` every
day, even though it back-tests fine on a narrower/cleaner panel.

The fix combines BOTH remediations the brief offered for the spread section,
because each alone was empirically insufficient (see the two "rejected"
notes below -- both failures were found by writing tests for this file,
not by inspection):

1. `rets = px.pct_change().iloc[1:].dropna(how='all')` -- drop only rows
   that are entirely NaN, not any row with a single missing ticker. This
   alone fixes Step 0's guard (:49/:50) but does nothing for the
   downstream spread section below.

2. `sparse_tickers` selection is restricted to columns with a COMPLETE
   price history across `px_live = px.dropna(how='all')` -- the full
   min_rows+10-row price window the spread is built from, not just the
   252-row LOOKBACK-tail of *returns* (`r`) used for the autocorr ranking.
   [REJECTED first attempt: restricting only by `r[t].notna().all()` (a
   complete return tail), with the spread still summed via plain
   `DataFrame.sum(axis=1)` (skipna=True, no min_count), is not sufficient
   -- a whole all-tickers-NaN SESSION (row) gets dropped out of `r`
   entirely by `dropna(how='all')`, so every ticker's r-column still looks
   perfectly clean and the r-completeness filter excludes nothing for
   this reason. But `px` itself is never row-filtered, so that stale
   blank row is still summed (skipna) into a fabricated spread value of
   0.0 on that one day -- a massive outlier that inflates `roll_std` and
   can push every day's z back inside Z_ENTRY. See
   test_all_nan_rows_are_dropped below, which is what surfaced this gap
   (0 signals under the r-completeness-only, no-min_count version).]

3. The portfolio-spread sum (`DataFrame.sum(axis=1)`, which defaults to
   skipna=True) now also passes `min_count=len(sparse_tickers)` as
   defense in depth, so any NaN cell that slips through despite (2)
   produces NaN for that day instead of a silently fabricated partial (or,
   if every leg is missing, zero) sum; `roll.mean()`/`roll.std()` then
   skip NaN days via pandas' default skipna semantics, and explicit
   `np.isnan(...)` guards stop a NaN from ever reaching the `Z_ENTRY`
   compare silently (NaN comparisons are always False in Python, so an
   unguarded NaN z would fall through the elif/else exactly like a real
   no-signal day).
   [REJECTED as the ONLY fix (i.e. min_count with NO completeness filter
   on selection at all): a LEADING per-ticker price gap (e.g. a recent
   listing) can sit entirely before the 252-row LOOKBACK-tail of returns
   -- `r[t]` stays fully clean and the ticker still ranks top-K by
   autocorrelation -- while still landing on `px_sparse.iloc[0]`, the
   normalization base row drawn from the wider min_rows+10 price window.
   That NaNs the ticker's ENTIRE normalized column, and because
   min_count=K requires all K legs present on every row, it NaNs the
   spread on every single day, not just the gap day -- killing every
   signal, not just that one leg. See
   test_leading_price_gap_excludes_leg_but_others_still_fire below, which
   is what surfaced this gap (0 signals under the min_count-only
   version).]

4. No change to LOOKBACK / LAG / K_ASSETS / Z_ENTRY / Z_EXIT / SIZE_PER /
   active_in_regimes / MAX_SIGNALS, and no change to the autocorr loop's
   existing `s.dropna()` + `< 60` floor.

Run ONLY this file:
    nice -n 19 python3 -m pytest tests/strategies/test_sparse_cca_mean_revert.py -q
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.implementations.S_sparse_cca_mean_revert import SparseCCAMeanRevert

REGIME = {'state': 'LOW_VOL'}


# ─────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ─────────────────────────────────────────────────────────────────────────
def _dates(n, start='2024-01-02'):
    return pd.bdate_range(start=start, periods=n)


def _engineered_prices(n_engineered, n_rows, amp0=0.005, amp_step=0.0002,
                        jump0=0.05, jump_step=0.001):
    """Deterministic alternating (period-2) return series -> strong negative
    lag-5 autocorrelation (mean-reverting), with a large final-day jump so
    the combined weighted spread's last value is a clear outlier vs. its
    own rolling distribution. Amplitude/jump vary slightly per ticker so
    there are no exact ties in the autocorr ranking."""
    out = {}
    for k in range(n_engineered):
        rets = np.zeros(n_rows)
        amp = amp0 + amp_step * k
        for t in range(1, n_rows):
            rets[t] = amp if (t % 2 == 0) else -amp
        rets[-1] = jump0 + jump_step * k
        price = 100.0 * np.cumprod(1 + rets)
        price[0] = 100.0
        out[f'ENG{k}'] = price
    return out


def _noise_prices(n_noise, n_rows, seed=42, vol=0.01):
    """i.i.d. Gaussian-walk returns -> ~zero autocorrelation; decoys/filler
    so the universe clears the `len(tickers) < 20` and column-count floors
    without ever outranking the engineered mean-reverting legs."""
    rng = np.random.default_rng(seed)
    out = {}
    for k in range(n_noise):
        steps = rng.normal(0.0, vol, n_rows - 1)
        price = 100.0 * np.exp(np.concatenate([[0.0], np.cumsum(steps)]))
        out[f'NOI{k}'] = price
    return out


def _build_panel(n_rows=300, n_engineered=10, n_noise=50, nan_plan=None):
    """nan_plan: list of (column_name, row_index) cells to null out after
    construction (used to plant per-ticker gaps without disturbing the
    engineered mean-reverting legs)."""
    idx = _dates(n_rows)
    cols = {**_engineered_prices(n_engineered, n_rows), **_noise_prices(n_noise, n_rows)}
    px = pd.DataFrame(cols, index=idx)
    for col, row in (nan_plan or []):
        px.iloc[row, px.columns.get_loc(col)] = np.nan
    return px


def _old_rets(px):
    """Local, independent replica of the OLD (pre-fix) two lines -- the
    row-wise ANY-nan drop this task removes. Used only to PIN the
    historical bug on the exact same panel the new code is exercised
    against."""
    return px.pct_change().dropna()


def _replicate_px_prep(prices, universe, strat):
    """Replicate generate_signals' ticker filter + dropna(axis=1,
    thresh=min_rows) + tail(min_rows+10) exactly, using the strategy's own
    LOOKBACK/LAG, so the oracle operates on the identical `px` the real
    method would build internally."""
    tickers = [t for t in universe if t in prices.columns]
    min_rows = strat.LOOKBACK + strat.LAG + 20
    return prices[tickers].dropna(axis=1, thresh=min_rows).tail(min_rows + 10)


ENGINEERED_TICKERS = {f'ENG{k}' for k in range(10)}


# ─────────────────────────────────────────────────────────────────────────
# (a) OLD guard fires (pinned) / NEW code trades through scattered per-
#     ticker NaNs and fires a signal on a planted mean-reverting spread.
# ─────────────────────────────────────────────────────────────────────────
def test_scattered_nans_old_guard_fires_new_code_signals():
    strat = SparseCCAMeanRevert()
    n_rows = 300
    # 20 of the 50 noise columns each carry a single NaN on a distinct row,
    # spaced 5 apart so no two planted NaNs invalidate a shared pct_change
    # row (each single-cell price NaN invalidates 2 adjacent return rows).
    nan_plan = [(f'NOI{k}', 50 + 5 * k) for k in range(20)]
    px = _build_panel(n_rows=n_rows, n_engineered=10, n_noise=50, nan_plan=nan_plan)
    universe = list(px.columns)

    # --- pin the OLD bug on the identical preprocessed panel ---
    px_prepped = _replicate_px_prep(px, universe, strat)
    rets_old = _old_rets(px_prepped)
    assert len(rets_old) < strat.LOOKBACK, (
        f'expected the OLD row-wise dropna() to empty the frame below LOOKBACK '
        f'({len(rets_old)} vs {strat.LOOKBACK}) -- if this fails the pinned bug '
        f'is no longer reproducible and this test needs re-deriving, not deleting'
    )

    # --- NEW code: must trade through the same panel ---
    signals = strat.generate_signals(px, REGIME, universe)
    assert len(signals) >= 1
    # All 10 engineered legs should qualify (weight ~0.10 each, above the
    # 0.005 floor) and none of the NaN-bearing noise tickers should appear.
    signal_tickers = {s.ticker for s in signals}
    assert signal_tickers <= ENGINEERED_TICKERS
    assert all(abs(s.signal_params['z_score']) > strat.Z_ENTRY for s in signals)


# ─────────────────────────────────────────────────────────────────────────
# (b) All-NaN tail rows ARE dropped (genuinely missing sessions), and the
#     spread computation tolerates one without being poisoned by a
#     fabricated (skipna-summed) value on that day.
# ─────────────────────────────────────────────────────────────────────────
def test_all_nan_rows_are_dropped():
    strat = SparseCCAMeanRevert()
    n_rows = 300
    px = _build_panel(n_rows=n_rows, n_engineered=10, n_noise=50)
    universe = list(px.columns)

    # Null out an ENTIRE row (every column, all 60 tickers) partway through
    # the window -- e.g. a data-collection gap / holiday leaking into the
    # panel.
    blank_row_date = px.index[120]
    px.loc[blank_row_date, :] = np.nan

    px_prepped = _replicate_px_prep(px, universe, strat)
    rets_new = px_prepped.pct_change().iloc[1:].dropna(how='all')
    assert blank_row_date not in rets_new.index
    assert len(rets_new) > 0

    # The strategy should still trade -- one blank session shouldn't sink
    # the whole run, and (pre-min_count) shouldn't poison the rolling
    # stats with a fabricated skipna-summed value on that one day either.
    signals = strat.generate_signals(px, REGIME, universe)
    assert len(signals) >= 1
    signal_tickers = {s.ticker for s in signals}
    assert signal_tickers <= ENGINEERED_TICKERS


# ─────────────────────────────────────────────────────────────────────────
# (e) A top-ranked leg with a LEADING price gap (e.g. a recent listing) is
#     excluded from selection, without sinking the whole run -- this is
#     the scenario that broke the min_count-only version of the fix: the
#     gap sits entirely before the LOOKBACK-tail of RETURNS (`r`, complete
#     and clean), but lands on `px_sparse.iloc[0]`, the base row used to
#     normalize prices, which would otherwise NaN that leg's whole
#     normalized column for every day (and, under min_count alone, the
#     whole spread every day).
# ─────────────────────────────────────────────────────────────────────────
def test_leading_price_gap_excludes_leg_but_others_still_fire():
    strat = SparseCCAMeanRevert()
    n_rows = 300
    px = _build_panel(n_rows=n_rows, n_engineered=10, n_noise=50)
    universe = list(px.columns)

    min_rows = strat.LOOKBACK + strat.LAG + 20   # 277
    # NaN the first 21 rows of ENG0 -- 300-21=279 valid overall (clears the
    # dropna(axis=1, thresh=min_rows) column floor) and the gap falls
    # entirely before the 252-row LOOKBACK-tail of returns (so `r['ENG0']`
    # is fully clean, autocorr stays ~-0.8, and ENG0 still ranks top-10 by
    # autocorrelation) but reaches into the analysis window's EARLIEST row
    # -- the one `base = px_sparse.iloc[0]` is drawn from.
    px.iloc[0:21, px.columns.get_loc('ENG0')] = np.nan

    px_prepped = _replicate_px_prep(px, universe, strat)
    assert px_prepped['ENG0'].notna().sum() >= min_rows, 'must clear the column floor'
    assert px_prepped['ENG0'].iloc[0] != px_prepped['ENG0'].iloc[0], 'base row must be NaN'  # NaN != NaN
    r_window = px_prepped.pct_change().iloc[1:].dropna(how='all').tail(strat.LOOKBACK)
    assert r_window['ENG0'].notna().all(), 'return tail must look clean despite the base-row gap'

    signals = strat.generate_signals(px, REGIME, universe)
    signal_tickers = {s.ticker for s in signals}
    assert 'ENG0' not in signal_tickers
    assert len(signals) >= 1


# ─────────────────────────────────────────────────────────────────────────
# (c) A column with < 60 valid returns in the LOOKBACK tail is excluded --
#     isolated from the earlier dropna(axis=1, thresh=min_rows) column
#     floor by using a longer history (600 rows) where the ticker's valid
#     data sits almost entirely OUTSIDE the tail(min_rows+10) window used
#     for the autocorr loop.
# ─────────────────────────────────────────────────────────────────────────
def test_sparse_column_under_60_valid_in_tail_excluded():
    strat = SparseCCAMeanRevert()
    n_rows = 600
    n_engineered = 10
    px = _build_panel(n_rows=n_rows, n_engineered=n_engineered, n_noise=50)

    min_rows = strat.LOOKBACK + strat.LAG + 20      # 277
    tail_window = min_rows + 10                     # 287 (rows 313..599 of 600)
    # SPARSE_ENG: valid for rows [0, 283) (283 >= min_rows -> clears the
    # dropna(axis=1, thresh=min_rows) column floor over its FULL history)
    # then NaN from row 283 onward, i.e. entirely NaN within the
    # tail(min_rows+10) analysis window (which starts at row 313). It
    # therefore reaches the autocorr loop (the column floor passed) but
    # has 0 (< 60) valid values in `r`, and must be excluded by the
    # `< 60` floor specifically -- not by the earlier column floor.
    pattern = np.where(np.arange(n_rows) % 2 == 0, 0.004, -0.004)
    sparse_col = pd.Series(100.0 * np.cumprod(1 + pattern), index=px.index)
    sparse_col.iloc[283:] = np.nan
    px['SPARSE_ENG'] = sparse_col
    universe = list(px.columns)

    valid_total = px['SPARSE_ENG'].notna().sum()
    assert valid_total >= min_rows, 'fixture must clear the column-count floor'
    valid_in_tail = px['SPARSE_ENG'].tail(tail_window).notna().sum()
    assert valid_in_tail < 60, 'fixture must starve the LOOKBACK-tail sample'

    signals = strat.generate_signals(px, REGIME, universe)
    signal_tickers = {s.ticker for s in signals}
    assert 'SPARSE_ENG' not in signal_tickers
    # The 10 legitimate engineered legs must still fire -- proves
    # SPARSE_ENG was excluded rather than the whole run failing.
    assert len(signals) >= 1
    assert signal_tickers <= ENGINEERED_TICKERS


# ─────────────────────────────────────────────────────────────────────────
# (d) Unchanged guards: < 20 tickers in universe, and a too-short panel.
# ─────────────────────────────────────────────────────────────────────────
def test_fewer_than_20_tickers_returns_empty():
    strat = SparseCCAMeanRevert()
    px = _build_panel(n_rows=300, n_engineered=5, n_noise=5)  # 10 total columns
    universe = list(px.columns)
    assert len(universe) < 20
    assert strat.generate_signals(px, REGIME, universe) == []


def test_short_panel_returns_empty():
    strat = SparseCCAMeanRevert()
    # Plenty of tickers, but far fewer rows than LOOKBACK + LAG requires.
    px = _build_panel(n_rows=100, n_engineered=10, n_noise=50)
    universe = list(px.columns)
    assert len(universe) >= 20
    assert strat.generate_signals(px, REGIME, universe) == []


def test_empty_or_none_prices_returns_empty():
    strat = SparseCCAMeanRevert()
    assert strat.generate_signals(pd.DataFrame(), REGIME, ['A', 'B']) == []
    assert strat.generate_signals(None, REGIME, ['A', 'B']) == []


# ─────────────────────────────────────────────────────────────────────────
# Review round 1 (2026-09-25): trailing all-NaN row, a simultaneous
# multi-column two-row data hole, completeness tolerance, 7-day rows.
# ─────────────────────────────────────────────────────────────────────────
def test_trailing_all_nan_row_never_emits_nan_entry_prices():
    strat = SparseCCAMeanRevert()
    px = _build_panel(n_rows=300, n_engineered=10, n_noise=50)
    px.loc[px.index[-1] + pd.Timedelta(days=1)] = np.nan   # today: no prints at all
    signals = strat.generate_signals(px, REGIME, list(px.columns))
    assert all(np.isfinite(s.entry_price) and s.entry_price > 0 for s in signals)


def test_two_row_hole_across_most_columns_does_not_bias_selection():
    """The 2026-09-15/16 shape: two sessions where ~80 % of the universe has
    no row. Under a zero-NaN completeness rule only the reporting minority
    could be selected (here: noise columns); under the tolerance rule the
    engineered mean-reverting legs still win."""
    strat = SparseCCAMeanRevert()
    px = _build_panel(n_rows=300, n_engineered=10, n_noise=50)
    hole_cols = [c for c in px.columns if c in ENGINEERED_TICKERS or int(c[-1]) < 8]
    px.iloc[150:152, [px.columns.get_loc(c) for c in hole_cols]] = np.nan
    signals = strat.generate_signals(px, REGIME, list(px.columns))
    assert len(signals) >= 1
    assert {s.ticker for s in signals} <= ENGINEERED_TICKERS
    assert all(np.isfinite(s.entry_price) for s in signals)


def test_leg_missing_base_or_today_or_too_many_rows_is_excluded():
    strat = SparseCCAMeanRevert()
    px = _build_panel(n_rows=300, n_engineered=10, n_noise=50)
    prepped = _replicate_px_prep(px, list(px.columns), strat)
    first_row = px.index.get_loc(prepped.index[0])
    px.iloc[first_row, px.columns.get_loc('ENG0')] = np.nan              # base row missing
    px.iloc[-1, px.columns.get_loc('ENG1')] = np.nan                     # today missing
    for r in range(200, 206):                                            # 6 interior misses
        px.iloc[r, px.columns.get_loc('ENG2')] = np.nan
    for r in range(210, 214):                                            # 4 interior misses — tolerated
        px.iloc[r, px.columns.get_loc('ENG3')] = np.nan
    signals = strat.generate_signals(px, REGIME, list(px.columns))
    tickers = {s.ticker for s in signals}
    assert len(signals) >= 1
    assert not ({'ENG0', 'ENG1', 'ENG2'} & tickers)
    assert 'ENG3' in tickers


def test_seven_day_ticker_rows_do_not_break_completeness_without_the_calendar():
    """Weekend rows contributed by a crypto column must not make every equity
    column look incomplete; with no upstream calendar the strategy still
    selects the engineered legs (or returns [] — never raises, never NaN)."""
    strat = SparseCCAMeanRevert()
    px = _build_panel(n_rows=300, n_engineered=10, n_noise=50)
    full = pd.date_range(px.index[0], px.index[-1], freq='D')
    px = px.reindex(full)                                                # weekends → NaN for equities
    px['BTC-USD'] = 100.0 + np.arange(len(px)) * 0.1                     # 7-day ticker
    signals = strat.generate_signals(px, REGIME, list(px.columns))
    assert len(signals) >= 1
    assert {s.ticker for s in signals} <= ENGINEERED_TICKERS
