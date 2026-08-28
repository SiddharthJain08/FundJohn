"""
STATUS: PARKED 2026-08-28 pending live exit hook (Phase 2). Backtest exit hook
LANDED (Phase 1, docs/superpowers/plans/2026-08-28-exit-hook-phase1.md); promotion
is refused by `exit_hook_live_disabled` until OPENCLAW_EXIT_HOOK_LIVE=1.

S_coint_pairs_sector_v2: sector cointegration pairs — ledger-driven z-score
edge-trigger entries.

This strategy does ZERO statistical estimation at signal time. A separate,
offline pairs scanner performs the Engle-Granger cointegration test, FDR
correction across candidate pairs, and cost gating, and freezes the result
(hedge ratio, intercept, and pass/fail flags) into one row per pair per
scan date in `data/derived/pair_ledger.parquet` (path overridable via
`OPENCLAW_PAIR_LEDGER` — tests point this at a tmp file). This file only:
  1. reads the ledger, look-ahead-filtered to the CURRENT prices panel,
  2. recomputes a rolling z-score of the frozen spread from the live prices
     panel, and
  3. fires an edge-triggered entry when that z-score newly crosses +-2.0.
Keeping this file numpy/pandas/pyarrow-only (no statsmodels) means the
live/backtest signal path never re-runs the (expensive, look-ahead-fragile)
cointegration estimation — that estimation is frozen data, read here exactly
like any other ledger.

Spread convention (fixed by the scanner; this file must match it exactly):
    spread_t = log(ticker_a_t) - beta * log(ticker_b_t) - alpha

LOOK-AHEAD SAFETY
-----------------
`as_of = prices.index.max()` is the last bar of the PASSED-IN prices panel —
not wall-clock "today". This matters for backtests, which pass a prices
panel truncated to the replay date. The ledger read filters
`ledger.as_of <= as_of` and then keeps only the newest as_of value that
survives that filter (a ledger row minted the day AFTER the panel's last bar
is invisible here even if it already sits in the parquet file — this is what
makes a backtest over historical dates replay-safe as new scanner rows
accumulate in the same physical ledger file over time). Within that selected
as_of snapshot, only `approved == True` rows are used; fdr_pass/cost_ok are
assumed already folded into `approved` by the scanner (this file does not
re-check them independently — see report for this assumption).

SIZING — equal-dollar-notional legs
------------------------------------
`beta` here is a LOG-price hedge ratio (spread = log(A) - beta*log(B) -
alpha), not a dollar hedge ratio. Converting it into an exact dollar-neutral
weight would require beta_dollar = beta * (price_B / price_A) recomputed at
execution time, and even that only holds instantaneously since it drifts as
the two legs move — that computation needs portfolio-level dollar/NAV context
this method is never given (generate_signals only sees the prices panel,
regime, and universe). The neighboring live pairs strategy,
S_pairs_trading_jump_diffusion_intraday, sidesteps this the same way: it
gives BOTH legs of a pair the identical position_size_pct (its BASE_SIZE,
scaled by regime) rather than a beta-weighted split. We follow the exact same
mechanism (`self.BASE_SIZE * scale` on both legs) — equal DOLLAR notional per
leg is the correct first-order dollar-neutral construction for a log-price
hedge when no downstream dollar-sizing context is available at this layer.

HOLDING / CADENCE
-----------------
base.py (BaseStrategy) provides no dedicated "holding days" class attribute
or Signal field. The house convention observed in S24_52wk_high_proximity.py
and S_microcap_insider_purchase_momentum.py is:
  - `default_parameters()['hold_days']` — a strategy-level flat fallback,
    genuinely read at signal time (BaseStrategy.__init__ merges any DB-level
    `parameters` override on top of this default into `self.parameters`, so
    an operator override of `hold_days` actually takes effect here).
  - `Signal.signal_params['hold_days']` — a per-signal override.
Because half-life is PER-PAIR (read straight off the ledger row), when
`half_life_days` is present and finite we use the per-signal override:
`signal_params['hold_days'] = min(round(3 * half_life_days), 30)`. When
`half_life_days` is NaN/missing (the ledger row carries no usable estimate),
there is no per-pair basis for that formula, so we fall back directly to the
operator-overridable class default instead of fabricating a half-life:
`hold_days = self.parameters.get('hold_days', 21)`, and
`signal_params['half_life_days']` is left `None` (never a fabricated 21.0)
so a downstream reader can tell "no half-life was available" apart from
"the half-life happened to compute to 21".

EXITS — exact since the Phase 1 per-bar exit hook (2026-08-28)
--------------------------------------------------------------
The exit spec for this strategy family is: (a) close when |z| <= Z_EXIT
(reversion achieved), (b) a "structural kill" if the pair's cointegration
relationship breaks down on a later re-scan, and (c) a time stop after
roughly N days without reversion. All three are now expressed exactly rather
than approximated by the cadence window, via `exit_hook = True` +
`should_exit()` (spec docs/specs/2026-08-28-per-bar-exit-hook-spec.md,
plan docs/superpowers/plans/2026-08-28-exit-hook-phase1.md):
  - (a) REVERSION — EXACT. `should_exit` recomputes the log-spread z from the
    entry-time beta/alpha over a rolling Z_WINDOW on the bar's prices panel
    and returns `'z_revert'` when |z| <= Z_EXIT (0.5) OR z has flipped sign
    since entry (signal_params['z']). Persisted as
    `exit_reason='strategy_exit:z_revert'`. LIVE TODAY only in the BACKTEST
    (`backtest/open_book.py`); the live `update_pnl` mirror is Phase 2 and is
    gated on OPENCLAW_EXIT_HOOK_LIVE=1 — until that flag flips, promotion of
    any run that used the hook is refused as `exit_hook_live_disabled`.
  - (b) STRUCTURAL KILL — EXACT. `'pair_decohered'` when the latest ledger
    snapshot with as_of <= the bar's date no longer approves the pair
    (`_latest_snapshot_has_pair`; an unreadable/absent snapshot returns None
    and the position is HELD, never flattened on a missing read). CAVEAT: the
    scanner folds fdr_pass AND cost_ok into `approved`, so this fires on ANY
    de-approval — a cost or persistence flap, not only a genuine loss of
    cointegration. It is the dominant exit in practice: 695 of run 3's 1,069
    hook exits (65 %) were `pair_decohered`, median hold 4 trading days
    (~one weekly scan cycle) and net-losing, against 374 net-winning
    `z_revert`. Read any median-hold number for this strategy with that in
    mind — it tracks scanner cadence as much as reversion.
  - (c) TIME STOP — the per-signal `signal_params['hold_days']`
    (min(3*half_life_days, 30), or the operator-overridable class default
    when no half-life is available) capped by the RUN's `max_hold_days`.
    Honored per-bar by the open book; reason stays `'max_hold'`.
  - (d) PER-LEG PRICE STOPS (X1-D1, 2026-08-28): the engine requires a
    stop_loss/target per leg. Run 655c4bdb showed the base-class 2xATR /
    5% per-leg levels firing on 70% of trades -- a leg moving against you
    is the hedge working in a spread trade. Levels are now the WIDER of the
    spread-implied distance to |z| = Z_STOP and STOP_HOLD_SIGMAS leg-sigmas
    over sqrt(hold_days) (see _pair_leg_levels; per-signal
    signal_params['stop_basis'] records both distances). This is still a
    per-leg guard, not a spread stop -- the true spread stop remains owed.
On any given bar the order is FIXED and set by the engine: intra-bar bracket
(d) -> hook at the close (a)/(b) -> time cap (c).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['CointPairsSectorV2']

# repo_root: src/strategies/implementations/<this file> -> parents[3] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_LEDGER_REL = Path('data') / 'derived' / 'pair_ledger.parquet'

# Columns this file cannot operate safely without. Missing `approved` in
# particular must NEVER be treated as "everything is approved" -- that is
# the one gate this strategy exists to enforce. Validated once, right after
# read, in _load_approved_pairs(); any miss returns the empty result rather
# than raising or falling open.
_REQUIRED_LEDGER_COLUMNS = [
    'as_of', 'ticker_a', 'ticker_b', 'beta', 'alpha', 'half_life_days', 'approved',
]


def _ledger_path() -> Path:
    """Resolve the pair ledger path — OPENCLAW_PAIR_LEDGER overrides the
    default (repo_root/data/derived/pair_ledger.parquet). Resolved fresh on
    every call (not cached) so tests can point this at a tmp file per-test."""
    override = os.environ.get('OPENCLAW_PAIR_LEDGER')
    return Path(override) if override else (_REPO_ROOT / _DEFAULT_LEDGER_REL)


# Compact projection of the ledger held in memory: everything
# generate_signals reads off a row, nothing else. The full 15-column,
# 860k-row parquet is ONE row group, so even a filtered read materialises the
# whole thing (~190 ms) — and the exit hook probes the ledger once per open
# leg per bar. Selecting these columns in arrow before to_pandas() keeps the
# resident table at (approved rows x 9 cols).
_CACHE_COLUMNS = (
    'as_of', 'ticker_a', 'ticker_b', 'beta', 'alpha', 'half_life_days',
    'industry', 'eg_pvalue', 'fdr_q',
)

# One entry, replaced whenever the ledger version changes. The version key is
# (path, st_mtime_ns, st_size), so OPENCLAW_PAIR_LEDGER is still honoured per
# call (the path is part of the key) and a rewritten ledger is picked up.
_LEDGER_CACHE = {'key': None, 'entry': None}


def _read_ledger(path: Path) -> dict:
    """Read the ledger once into the compact cache entry. Never raises.

    Returns {'error', 'as_of', 'approved'}:
      - `error` is None, ('read', msg) or ('columns', [missing]). The CALLERS
        own the log lines, so a cached failure still logs on every call.
      - `as_of` is the sorted DISTINCT as_of values over ALL rows (approved or
        not). The latest snapshot must be selected over the full ledger and
        only THEN filtered to approved — picking the latest snapshot that
        happens to hold an approved row would resurrect the previous scan's
        approvals whenever a scan de-approves a pair, and `pair_decohered`
        would never fire. This mirrors the pre-cache read order exactly.
      - `approved` is the compact approved==True frame, all snapshots.
    """
    try:
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.parquet as pq
        table = pq.read_table(str(path))
    except Exception as e:
        return {'error': ('read', str(e)), 'as_of': None, 'approved': None}

    # Validate the full required column set ONCE, right after read. A
    # malformed ledger (e.g. missing `approved`) must fail CLOSED (empty
    # result, one log line) — never raise (AttributeError downstream in
    # generate_signals) and never silently treat every row as approved.
    missing_cols = [c for c in _REQUIRED_LEDGER_COLUMNS if c not in table.column_names]
    if missing_cols:
        return {'error': ('columns', missing_cols), 'as_of': None, 'approved': None}

    try:
        as_of_all = pd.DatetimeIndex(
            pd.to_datetime(pd.Series(pc.unique(table.column('as_of')).to_pandas())).dropna()
        ).unique().sort_values()
        # Explicit bool compare, in pandas, on the single `approved` column:
        # identical semantics to the pre-cache `df['approved'] == True`
        # (NaN/None/non-bool safely excluded) without materialising the other
        # 14 columns for 860k rows.
        keep = (table.column('approved').to_pandas() == True)   # noqa: E712
        keep = keep.fillna(False).to_numpy(dtype=bool)
        cols = [c for c in _CACHE_COLUMNS if c in table.column_names]
        approved = table.select(cols).filter(pa.array(keep)).to_pandas()
        approved['as_of'] = pd.to_datetime(approved['as_of'])
    except Exception as e:
        return {'error': ('read', str(e)), 'as_of': None, 'approved': None}
    return {'error': None, 'as_of': as_of_all, 'approved': approved}


def _approved_table(path: Path) -> dict:
    """The cached `_read_ledger` entry for `path`, re-read only when the
    file's (mtime_ns, size) changes. Never raises."""
    try:
        st = path.stat()
    except OSError as e:
        return {'error': ('read', f'{type(e).__name__}: {e}'), 'as_of': None, 'approved': None}
    key = (str(path), st.st_mtime_ns, st.st_size)
    entry = _LEDGER_CACHE['entry']
    if entry is not None and _LEDGER_CACHE['key'] == key:
        return entry
    entry = _read_ledger(path)
    _LEDGER_CACHE['key'] = key
    _LEDGER_CACHE['entry'] = entry
    return entry


def _latest_as_of(entry: dict, as_of_date: pd.Timestamp):
    """Newest ledger snapshot date <= as_of_date, or None. This is the
    look-ahead filter: a row minted after the panel's last bar is invisible
    here even though it already sits in the parquet."""
    eligible = entry['as_of'][entry['as_of'] <= as_of_date]
    return eligible[-1] if len(eligible) else None


def _load_approved_pairs(as_of_date: pd.Timestamp) -> pd.DataFrame:
    """The pair ledger, look-ahead-filtered to as_of_date, LATEST surviving
    as_of snapshot only, approved-only. Always returns a DataFrame (empty on
    any miss) — never raises.

    Answered from `_approved_table`'s cache: the physical parquet is read at
    most once per ledger version, however many times this — or the exit
    hook's `_latest_snapshot_has_pair` — is called."""
    path = _ledger_path()
    if not path.exists():
        print(f'[debug] pair_ledger missing at {path}', file=sys.stderr)
        return pd.DataFrame()
    entry = _approved_table(path)
    if entry['error'] is not None:
        kind, detail = entry['error']
        if kind == 'columns':
            print(f'[debug] pair_ledger at {path} missing required columns {detail} '
                  f'— treating as no approved pairs (fail-closed)', file=sys.stderr)
        else:
            print(f'[debug] pair_ledger read failed ({path}): {detail}', file=sys.stderr)
        return pd.DataFrame()

    latest = _latest_as_of(entry, as_of_date)
    if latest is None:
        print(f'[debug] pair_ledger has no rows with as_of <= {as_of_date.date()}', file=sys.stderr)
        return pd.DataFrame()

    df = entry['approved']
    df = df[df['as_of'] == latest].copy()   # copy: the cached frame is shared
    if df.empty:
        print(f'[debug] pair_ledger has no approved rows for as_of={latest.date()}', file=sys.stderr)
    return df


def _latest_snapshot_has_pair(as_of_date: pd.Timestamp, ticker_a: str, ticker_b: str):
    """True/False = the LATEST ledger snapshot with as_of <= as_of_date does /
    does not approve the (unordered) pair; None = no readable snapshot at all
    (missing file, read error, missing columns, no rows <= as_of_date) — the
    caller must HOLD on None, never treat it as decoherence."""
    path = _ledger_path()
    if not path.exists():
        return None
    entry = _approved_table(path)
    if entry['error'] is not None:
        kind, detail = entry['error']
        if kind != 'columns':
            print(f'[debug] pair_ledger read failed in should_exit ({path}): {detail}',
                  file=sys.stderr)
        return None
    latest = _latest_as_of(entry, as_of_date)
    if latest is None:
        return None
    snap = entry['approved']
    snap = snap[snap['as_of'] == latest]
    keys = set(zip(snap['ticker_a'].astype(str), snap['ticker_b'].astype(str)))
    return (ticker_a, ticker_b) in keys or (ticker_b, ticker_a) in keys


class CointPairsSectorV2(BaseStrategy):
    """Sector cointegration pairs: ledger-driven z-score edge-trigger entries.
    See module docstring for the full look-ahead-safety / sizing / holding /
    exit-approximation design notes.
    """

    id                = 'S_coint_pairs_sector_v2'
    name              = 'CointPairsSectorV2'
    description       = ('Sector cointegration pairs trading — offline EG/FDR-gated pair '
                          'ledger, live z-score edge-trigger entries, dollar-neutral legs.')
    tier              = 2
    min_lookback      = 61
    # Let the eligibility assigner narrow this from backtest data later (operator directive).
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    Z_WINDOW     = 60      # rolling window for mean/std of the spread
    Z_ENTRY      = 2.0     # edge-trigger entry threshold
    Z_BACKSTOP   = 4.0     # reject as a probable data/estimation glitch
    Z_HIGH_CONF  = 2.5     # HIGH vs MED confidence cutoff
    # X1-D1 (2026-08-28): per-leg stop = the WIDER of (i) the spread-implied
    # distance to |z| = Z_STOP (the level at which entries are already refused
    # as a structural break) and (ii) STOP_HOLD_SIGMAS leg-sigmas over the
    # intended hold horizon, so co-movement of a hedged pair does not trip a
    # single leg. Targets sit at TARGET_R x that distance. Replaces the
    # base-class 2xATR / 5% per-leg levels (run 655c4bdb: 70% stop exits).
    Z_STOP           = 4.0
    STOP_HOLD_SIGMAS = 2.0
    TARGET_R         = 2.0
    # Per-bar exit hook (spec 2026-08-28 §5): flatten on reversion or decoherence.
    exit_hook        = True
    Z_EXIT           = 0.5
    BASE_SIZE    = 0.04    # fraction per leg — matches PairsTradingJumpDiffusionIntraday's
                            # per-leg convention (equal dollar notional both legs; see docstring)

    @staticmethod
    def _pair_leg_levels(direction: str, price: float, spread_log, vol_log) -> dict:
        """Stop/target levels for one pair leg from log-distances.

        spread_log: adverse log-price move of THIS leg that would carry the
        pair spread to |z| = Z_STOP (None when not derivable for the leg);
        vol_log: STOP_HOLD_SIGMAS * leg daily log-vol * sqrt(hold_days).
        The stop uses the wider of the two; targets at TARGET_R multiples.
        """
        cands = [d for d in (spread_log, vol_log) if d is not None and np.isfinite(d) and d > 0.0]
        used = max(cands) if cands else float('nan')
        sgn = -1.0 if direction == 'LONG' else 1.0
        r = CointPairsSectorV2.TARGET_R
        return {
            'stop':     round(price * float(np.exp(sgn * used)), 4),
            't1':       round(price * float(np.exp(-sgn * r * used)), 4),
            't2':       round(price * float(np.exp(-sgn * (r + 1.0) * used)), 4),
            't3':       round(price * float(np.exp(-sgn * (r + 2.0) * used)), 4),
            'used_log': float(used),
        }

    def should_exit(self, position: dict, prices: pd.DataFrame,
                    regime: dict, aux_data: dict = None):
        """Exit-hook: 'z_revert' when the pair's log-spread z (entry-time
        beta/alpha, rolling Z_WINDOW std) is within Z_EXIT of the mean or has
        flipped sign since entry; 'pair_decohered' when the latest ledger
        snapshot as_of <= today no longer approves the pair; None otherwise
        (including any missing leg / short window / incomplete params —
        hold, the bracket and hold_days still protect)."""
        sp = (position or {}).get('signal_params') or {}
        pair = sp.get('pair')
        try:
            beta = float(sp['beta']); alpha = float(sp['alpha']); z_entry = float(sp['z'])
            ticker_a, ticker_b = str(pair).split('/', 1)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        if not np.isfinite(z_entry):
            # A NaN entry z makes `(z_t > 0) != (z_entry > 0)` a FABRICATED
            # sign flip for every positive z_t (NaN > 0 is False). With no
            # usable entry reference the only safe answer is hold.
            return None
        if prices is None or prices.empty or ticker_a not in prices.columns or ticker_b not in prices.columns:
            return None
        both = prices[[ticker_a, ticker_b]].dropna(how='any')
        if len(both) < self.Z_WINDOW:
            return None
        window = both.iloc[-self.Z_WINDOW:]
        if window.index[-1] != prices.index[-1]:
            return None                       # no aligned bar today
        wa = window[ticker_a].to_numpy(dtype=float)
        wb = window[ticker_b].to_numpy(dtype=float)
        if (wa <= 0.0).any() or (wb <= 0.0).any():
            return None
        spread = np.log(wa) - beta * np.log(wb) - alpha
        std = float(np.std(spread, ddof=1))
        if not np.isfinite(std) or std <= 0.0:
            return None
        z_t = float((spread[-1] - np.mean(spread)) / std)
        if abs(z_t) <= self.Z_EXIT or (z_t > 0.0) != (z_entry > 0.0):
            return 'z_revert'
        has = _latest_snapshot_has_pair(pd.Timestamp(prices.index[-1]), ticker_a, ticker_b)
        if has is False:
            return 'pair_decohered'
        return None                           # True (still approved) or None (no snapshot -> hold)

    def default_parameters(self) -> dict:
        return {
            # Genuinely read at signal time when a pair's half_life_days is
            # NaN/missing (see generate_signals) -- operator-overridable via
            # a DB `parameters` row, merged into self.parameters by
            # BaseStrategy.__init__. When half-life IS present, the
            # per-signal hold_days is min(3*half_life_days, 30) instead.
            'hold_days': 21,
        }

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        as_of_date = pd.Timestamp(prices.index.max())
        ledger = _load_approved_pairs(as_of_date)
        if ledger.empty:
            return []

        universe_set = set(universe)
        signals: List[Signal] = []

        for row in ledger.itertuples(index=False):
            ticker_a = row.ticker_a
            ticker_b = row.ticker_b
            if ticker_a not in prices.columns or ticker_b not in prices.columns:
                continue
            if ticker_a not in universe_set or ticker_b not in universe_set:
                continue

            col_a = prices[ticker_a]
            col_b = prices[ticker_b]
            # Bar-t must actually exist for both legs — otherwise there is no
            # fresh z_t for this pair today, regardless of history depth.
            if pd.isna(col_a.iloc[-1]) or pd.isna(col_b.iloc[-1]):
                continue
            if col_a.notna().sum() < self.Z_WINDOW + 1 or col_b.notna().sum() < self.Z_WINDOW + 1:
                continue

            both = pd.concat([col_a, col_b], axis=1).dropna(how='any')
            if len(both) < self.Z_WINDOW + 1:
                continue
            window = both.iloc[-(self.Z_WINDOW + 1):]
            if window.index[-1] != as_of_date:
                # A gap in one leg right at the panel's last bar — no aligned
                # bar-t data for this pair even though each leg individually
                # has enough history. Skip rather than reuse a stale bar.
                continue

            beta = float(row.beta)
            alpha = float(row.alpha)
            window_a = window[ticker_a].to_numpy(dtype=float)
            window_b = window[ticker_b].to_numpy(dtype=float)
            # A non-positive close inside the window (data error -- not
            # caught by the earlier NaN-only checks) would otherwise hit
            # log(<=0) and emit a RuntimeWarning plus a NaN that silently
            # poisons the z-score. Mask to NaN explicitly and skip the pair
            # for this bar rather than let that escape or fabricate a signal.
            window_a = np.where(window_a > 0.0, window_a, np.nan)
            window_b = np.where(window_b > 0.0, window_b, np.nan)
            if np.isnan(window_a).any() or np.isnan(window_b).any():
                continue
            log_a = np.log(window_a)
            log_b = np.log(window_b)
            spread = log_a - beta * log_b - alpha   # length Z_WINDOW + 1

            win_t   = spread[-self.Z_WINDOW:]          # window ending at t
            win_tm1 = spread[-(self.Z_WINDOW + 1):-1]  # window ending at t-1

            std_t   = float(np.std(win_t, ddof=1))
            std_tm1 = float(np.std(win_tm1, ddof=1))
            if std_t == 0.0 or std_tm1 == 0.0:
                continue

            z_t   = (spread[-1] - float(np.mean(win_t)))   / std_t
            z_tm1 = (spread[-2] - float(np.mean(win_tm1))) / std_tm1

            if not (abs(z_t) >= self.Z_ENTRY and abs(z_tm1) < self.Z_ENTRY and abs(z_t) < self.Z_BACKSTOP):
                continue

            # z_t positive => spread rich (A too expensive relative to fair
            # value implied by B) => SHORT A, LONG B. Negative => the mirror.
            dir_a = 'SHORT' if z_t > 0 else 'LONG'
            dir_b = 'LONG' if dir_a == 'SHORT' else 'SHORT'
            conf  = 'HIGH' if abs(z_t) >= self.Z_HIGH_CONF else 'MED'
            size  = round(float(self.BASE_SIZE * scale), 4)

            raw_half_life = getattr(row, 'half_life_days', None)
            half_life_valid = (
                raw_half_life is not None and pd.notna(raw_half_life) and np.isfinite(float(raw_half_life))
            )
            if half_life_valid:
                half_life = float(raw_half_life)
                hold_days = max(1, int(round(min(3.0 * half_life, 30.0))))
            else:
                # No usable per-pair half-life -- fall back directly to the
                # operator-overridable class default rather than fabricating
                # one (see default_parameters() / module docstring). Coerce
                # defensively: a malformed DB `parameters` override (e.g.
                # None, a string) must not raise here -- same fail-safe
                # posture as the ledger validation above.
                half_life = None
                try:
                    hold_days = int(self.parameters.get('hold_days', 21))
                except (TypeError, ValueError):
                    hold_days = 21

            pa = float(col_a.iloc[-1])
            pb = float(col_b.iloc[-1])
            if not (np.isfinite(pa) and pa > 0.0 and np.isfinite(pb) and pb > 0.0):
                continue

            raw_industry = getattr(row, 'industry', None)
            raw_eg_pvalue = getattr(row, 'eg_pvalue', None)
            raw_fdr_q = getattr(row, 'fdr_q', None)
            params_common = {
                'pair':           f'{ticker_a}/{ticker_b}',
                'beta':           round(beta, 6),
                'alpha':          round(alpha, 6),
                'z':              round(float(z_t), 4),
                'z_prev':         round(float(z_tm1), 4),
                'half_life_days': round(half_life, 2) if half_life is not None else None,
                'hold_days':      hold_days,
                'industry':       raw_industry,
                'eg_pvalue':      float(raw_eg_pvalue) if raw_eg_pvalue is not None and pd.notna(raw_eg_pvalue) else None,
                'fdr_q':          float(raw_fdr_q) if raw_fdr_q is not None and pd.notna(raw_fdr_q) else None,
            }

            # X1-D1 spread-implied stops (see class attributes). Leg A carries
            # coefficient 1 in the log spread, leg B coefficient -beta, so the
            # same spread excursion maps to a 1/beta log-move on B. A
            # non-positive/non-finite beta has no such mapping -> vol floor only.
            d_spread_a = (self.Z_STOP - abs(float(z_t))) * float(std_t)
            d_spread_b = d_spread_a / beta if (np.isfinite(beta) and beta > 0.0) else None
            sqrt_hold = float(np.sqrt(hold_days))
            vol_a = self.STOP_HOLD_SIGMAS * float(np.std(np.diff(log_a), ddof=1)) * sqrt_hold
            vol_b = self.STOP_HOLD_SIGMAS * float(np.std(np.diff(log_b), ddof=1)) * sqrt_hold
            st_a = self._pair_leg_levels(dir_a, pa, d_spread_a, vol_a)
            st_b = self._pair_leg_levels(dir_b, pb, d_spread_b, vol_b)
            if not (np.isfinite(st_a['used_log']) and np.isfinite(st_b['used_log'])):
                # Degenerate distances (should not happen past the std_t>0
                # guard) -> keep the base-class levels rather than emit NaNs.
                st_a = self.compute_stops_and_targets(col_a.dropna(), dir_a, pa, regime_state=regime_state)
                st_b = self.compute_stops_and_targets(col_b.dropna(), dir_b, pb, regime_state=regime_state)
                basis_a = basis_b = None
            else:
                basis_a = {'spread_log': d_spread_a, 'vol_log': vol_a, 'used_log': st_a['used_log']}
                basis_b = {'spread_log': d_spread_b, 'vol_log': vol_b, 'used_log': st_b['used_log']}

            signals.append(Signal(
                ticker=ticker_a, direction=dir_a, entry_price=pa,
                stop_loss=st_a['stop'], target_1=st_a['t1'], target_2=st_a['t2'], target_3=st_a['t3'],
                position_size_pct=size, confidence=conf,
                signal_params={**params_common, 'leg': 'a', 'stop_basis': basis_a},
            ))
            signals.append(Signal(
                ticker=ticker_b, direction=dir_b, entry_price=pb,
                stop_loss=st_b['stop'], target_1=st_b['t1'], target_2=st_b['t2'], target_3=st_b['t3'],
                position_size_pct=size, confidence=conf,
                signal_params={**params_common, 'leg': 'b', 'stop_basis': basis_b},
            ))
            if len(signals) >= self.MAX_SIGNALS:
                break

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
