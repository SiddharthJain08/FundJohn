"""
Tests + independent parity oracle for S_coint_pairs_sector_v2 (task D3+D4).

All fixtures are synthetic (tmp-dir ledger parquet + synthetic prices),
ZZT-prefixed fake tickers, no DB. Run ONLY this file:
    python3 -m pytest tests/strategies/test_coint_pairs_v2.py -v

`_reference_signals()` below is an INDEPENDENT, from-first-principles
re-derivation of the spec (ledger-filter + spread + z-score + edge-trigger).
It does not import any computation from strategies.implementations.
S_coint_pairs_sector_v2 — the only coupling to the strategy module in this
file is instantiating and calling the strategy class itself, so that its
output can be compared against this independent reference (task D4's
cross-engine signal parity trial).
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

from strategies.implementations.S_coint_pairs_sector_v2 import CointPairsSectorV2

Z_WINDOW = 60
Z_ENTRY = 2.0
Z_BACKSTOP = 4.0

LEDGER_COLUMNS = [
    'as_of', 'ticker_a', 'ticker_b', 'industry', 'beta', 'alpha',
    'half_life_days', 'sigma_spread', 'spread_mean', 'eg_pvalue', 'fdr_q',
    'fdr_pass', 'cost_ok', 'approved', 'n_obs',
]


# ─────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ─────────────────────────────────────────────────────────────────────────
def _ledger_row(as_of, ticker_a, ticker_b, beta, alpha, approved=True,
                 industry='TECH', half_life_days=6.0, sigma_spread=0.05,
                 spread_mean=0.0, eg_pvalue=0.01, fdr_q=0.02, fdr_pass=True,
                 cost_ok=True, n_obs=252):
    return dict(
        as_of=pd.Timestamp(as_of), ticker_a=ticker_a, ticker_b=ticker_b,
        industry=industry, beta=float(beta), alpha=float(alpha),
        half_life_days=float(half_life_days), sigma_spread=float(sigma_spread),
        spread_mean=float(spread_mean), eg_pvalue=float(eg_pvalue),
        fdr_q=float(fdr_q), fdr_pass=bool(fdr_pass), cost_ok=bool(cost_ok),
        approved=bool(approved), n_obs=int(n_obs),
    )


def _write_ledger(tmp_path, rows, name='pair_ledger.parquet'):
    df = pd.DataFrame(rows, columns=LEDGER_COLUMNS)
    path = tmp_path / name
    df.to_parquet(path)
    return str(path)


def _write_ledger_raw(tmp_path, rows, name='pair_ledger.parquet'):
    """Like _write_ledger but does NOT force the full LEDGER_COLUMNS set --
    a column absent from every row dict is genuinely absent from the parquet
    schema, so this is how malformed-ledger (missing-column) fixtures are
    built."""
    df = pd.DataFrame(rows)
    path = tmp_path / name
    df.to_parquet(path)
    return str(path)


def _dates(n, start='2026-01-02'):
    return pd.bdate_range(start=start, periods=n)


def _log_b(n, seed, start_price=100.0, step_std=0.01):
    """Deterministic (seeded) log-price random walk for the 'B' leg."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, step_std, n - 1)
    return np.log(start_price) + np.concatenate([[0.0], np.cumsum(steps)])


def _pair_frame(dates, ticker_a, ticker_b, beta, alpha, spread, log_b_seed):
    """Build a 2-column price frame satisfying spread = log(A) - beta*log(B) - alpha
    exactly, given an arbitrary `spread` array (len == len(dates))."""
    assert len(spread) == len(dates)
    log_b = _log_b(len(dates), seed=log_b_seed)
    log_a = beta * log_b + alpha + np.asarray(spread, dtype=float)
    return pd.DataFrame(
        {ticker_a: np.exp(log_a), ticker_b: np.exp(log_b)}, index=dates
    )


def _tail_spread(seed, tail_values, n_filler=59, baseline_std=0.02):
    """59 filler days (unused by the 60-bar window) + a deterministic
    59-value 'baseline' noise segment + the caller-supplied final tail
    values (1 or 2 values) that land inside the trailing 61-bar window and
    are used to hit a specific target z_t / z_{t-1}."""
    rng = np.random.default_rng(seed)
    baseline = rng.normal(0.0, baseline_std, 59)
    filler = np.zeros(n_filler)
    return np.concatenate([filler, baseline, np.asarray(tail_values, dtype=float)])


def _sig_set(signals):
    return {(s.ticker, s.direction) for s in signals}


# ─────────────────────────────────────────────────────────────────────────
# Independent reference implementation (task D4 parity oracle)
# ─────────────────────────────────────────────────────────────────────────
def _reference_signals(ledger_path, prices: pd.DataFrame, universe: list) -> set:
    """From-scratch re-derivation of the spec. Returns the set of
    (ticker, direction) tuples that SHOULD fire on prices.index.max()."""
    as_of_date = pd.Timestamp(prices.index.max())

    ledger = pd.read_parquet(ledger_path)
    ledger = ledger.copy()
    ledger['as_of'] = pd.to_datetime(ledger['as_of'])
    ledger = ledger[ledger['as_of'] <= as_of_date]
    if ledger.empty:
        return set()
    latest = ledger['as_of'].max()
    ledger = ledger[ledger['as_of'] == latest]
    ledger = ledger[ledger['approved'] == True]  # noqa: E712
    if ledger.empty:
        return set()

    universe_set = set(universe)
    out = set()

    for _, row in ledger.iterrows():
        a, b = row['ticker_a'], row['ticker_b']
        if a not in prices.columns or b not in prices.columns:
            continue
        if a not in universe_set or b not in universe_set:
            continue

        sa = prices[a].to_numpy(dtype=float)
        sb = prices[b].to_numpy(dtype=float)
        if np.isnan(sa[-1]) or np.isnan(sb[-1]):
            continue

        valid = (~np.isnan(sa)) & (~np.isnan(sb))
        if valid.sum() < Z_WINDOW + 1:
            continue
        idx = np.flatnonzero(valid)
        # last Z_WINDOW+1 jointly-valid observations, must end at the panel's final bar
        if idx[-1] != len(sa) - 1:
            continue
        tail_idx = idx[-(Z_WINDOW + 1):]
        if len(tail_idx) < Z_WINDOW + 1:
            continue

        beta = float(row['beta'])
        alpha = float(row['alpha'])
        spread = np.log(sa[tail_idx]) - beta * np.log(sb[tail_idx]) - alpha

        win_t = spread[1:]          # last 60, ending at t
        win_tm1 = spread[:-1]       # last 60, ending at t-1
        std_t = win_t.std(ddof=1)
        std_tm1 = win_tm1.std(ddof=1)
        if std_t == 0.0 or std_tm1 == 0.0:
            continue
        z_t = (spread[-1] - win_t.mean()) / std_t
        z_tm1 = (spread[-2] - win_tm1.mean()) / std_tm1

        if not (abs(z_t) >= Z_ENTRY and abs(z_tm1) < Z_ENTRY and abs(z_t) < Z_BACKSTOP):
            continue

        if z_t > 0:
            out.add((a, 'SHORT')); out.add((b, 'LONG'))
        else:
            out.add((a, 'LONG')); out.add((b, 'SHORT'))

    return out


# ─────────────────────────────────────────────────────────────────────────
# 1. Entry edge-trigger on the final bar + unapproved pair never signals
#    + a flat approved pair doesn't spuriously fire
# ─────────────────────────────────────────────────────────────────────────
def test_entry_edge_trigger_final_bar_and_unapproved_and_flat(tmp_path, monkeypatch):
    dates = _dates(120)
    last = dates[-1]

    # Pair 1: crosses |z|>=2.0 upward exactly on the final bar.
    spread1 = _tail_spread(seed=123, tail_values=[0.0, 0.05])
    frame1 = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10,
                          spread=spread1, log_b_seed=555)

    # Pair 2: approved, but flat (no cross) -- must not fire.
    spread2 = _tail_spread(seed=777, tail_values=[0.01, -0.01])
    frame2 = _pair_frame(dates, 'ZZTCC', 'ZZTDD', beta=1.1, alpha=-0.2,
                          spread=spread2, log_b_seed=888)

    # Pair 3: same crossing shape as pair 1, but UNAPPROVED -- must not fire.
    spread3 = _tail_spread(seed=123, tail_values=[0.0, 0.05])
    frame3 = _pair_frame(dates, 'ZZTEE', 'ZZTFF', beta=0.75, alpha=0.10,
                          spread=spread3, log_b_seed=999)

    prices = pd.concat([frame1, frame2, frame3], axis=1)
    universe = list(prices.columns)

    ledger_rows = [
        _ledger_row(last, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10, approved=True, half_life_days=6.0),
        _ledger_row(last, 'ZZTCC', 'ZZTDD', beta=1.1, alpha=-0.2, approved=True),
        _ledger_row(last, 'ZZTEE', 'ZZTFF', beta=0.75, alpha=0.10, approved=False),
    ]
    ledger_path = _write_ledger(tmp_path, ledger_rows)

    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
    signals = strat.generate_signals(prices, {'state': 'LOW_VOL'}, universe)

    got = _sig_set(signals)
    assert got == {('ZZTAA', 'SHORT'), ('ZZTBB', 'LONG')}, got
    assert len(signals) == 2

    # z_t was positive (~2.39) => SHORT the rich leg (A), LONG the cheap leg (B).
    a_sig = next(s for s in signals if s.ticker == 'ZZTAA')
    b_sig = next(s for s in signals if s.ticker == 'ZZTBB')
    assert a_sig.direction == 'SHORT' and b_sig.direction == 'LONG'
    assert a_sig.confidence in ('HIGH', 'MED')

    # Cadence/holding hint: half_life_days=6.0 -> min(3*6, 30) = 18, not capped.
    assert a_sig.signal_params['hold_days'] == 18
    assert b_sig.signal_params['hold_days'] == 18
    assert strat.default_parameters()['hold_days'] == 21

    # Independent reference agrees exactly.
    ref = _reference_signals(ledger_path, prices, universe)
    assert ref == got


# ─────────────────────────────────────────────────────────────────────────
# 2. z sits above 2.0 for the last several bars (crossed earlier) -> no re-fire
# ─────────────────────────────────────────────────────────────────────────
def test_no_refire_when_already_elevated(tmp_path, monkeypatch):
    dates = _dates(120)
    last = dates[-1]

    rng = np.random.default_rng(123)
    baseline = rng.normal(0.0, 0.02, 55)
    tail = np.full(6, 0.06)   # last 6 raw spread values elevated -> both z_t, z_{t-1} >= 2
    # 59 filler (unused, outside the trailing 61-bar window) + 55 baseline + 6 tail == 61-bar window
    spread = np.concatenate([np.zeros(120 - 61), baseline, tail])
    frame = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10,
                         spread=spread, log_b_seed=555)
    universe = list(frame.columns)

    ledger_path = _write_ledger(tmp_path, [
        _ledger_row(last, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10, approved=True),
    ])

    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
    signals = strat.generate_signals(frame, {'state': 'LOW_VOL'}, universe)

    assert signals == []
    ref = _reference_signals(ledger_path, frame, universe)
    assert ref == set()


# ─────────────────────────────────────────────────────────────────────────
# 3. Backstop: z jumps 1.8 -> 4.2-ish on the final bar -> no signal
# ─────────────────────────────────────────────────────────────────────────
def test_backstop_blocks_extreme_z(tmp_path, monkeypatch):
    dates = _dates(120)
    last = dates[-1]

    spread = _tail_spread(seed=123, tail_values=[0.035, 0.095])
    frame = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10,
                         spread=spread, log_b_seed=555)
    universe = list(frame.columns)

    ledger_path = _write_ledger(tmp_path, [
        _ledger_row(last, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10, approved=True),
    ])

    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
    signals = strat.generate_signals(frame, {'state': 'LOW_VOL'}, universe)

    assert signals == []
    ref = _reference_signals(ledger_path, frame, universe)
    assert ref == set()


# ─────────────────────────────────────────────────────────────────────────
# 4. A leg missing from universe never signals (even though the pair
#    otherwise would cross the same way as test 1).
# ─────────────────────────────────────────────────────────────────────────
def test_leg_missing_from_universe_no_signal(tmp_path, monkeypatch):
    dates = _dates(120)
    last = dates[-1]

    spread = _tail_spread(seed=123, tail_values=[0.0, 0.05])
    frame = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10,
                         spread=spread, log_b_seed=555)
    universe = ['ZZTAA']   # ZZTBB missing from universe

    ledger_path = _write_ledger(tmp_path, [
        _ledger_row(last, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10, approved=True),
    ])

    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
    signals = strat.generate_signals(frame, {'state': 'LOW_VOL'}, universe)

    assert signals == []
    ref = _reference_signals(ledger_path, frame, universe)
    assert ref == set()


# ─────────────────────────────────────────────────────────────────────────
# 5. Ledger look-ahead: a row with as_of AFTER the panel's last date is
#    ignored; the older approved row (as_of <= panel end) governs.
# ─────────────────────────────────────────────────────────────────────────
def test_ledger_lookahead_future_row_ignored(tmp_path, monkeypatch):
    dates = _dates(120)
    last = dates[-1]
    older_as_of = dates[65]
    future_as_of = last + pd.Timedelta(days=10)

    spread = _tail_spread(seed=123, tail_values=[0.0, 0.05])
    frame = _pair_frame(dates, 'ZZTGG', 'ZZTHH', beta=0.75, alpha=0.10,
                         spread=spread, log_b_seed=555)
    universe = list(frame.columns)

    ledger_rows = [
        _ledger_row(older_as_of, 'ZZTGG', 'ZZTHH', beta=0.75, alpha=0.10, approved=True),
        # If this future row were wrongly consulted (beta=-2.0 is drastically
        # different from the correct 0.75), it recomputes an entirely
        # different spread from the SAME prices and yields no signal at all
        # -- so using it instead of the older row is empirically detectable.
        _ledger_row(future_as_of, 'ZZTGG', 'ZZTHH', beta=-2.0, alpha=0.0, approved=True),
    ]
    ledger_path = _write_ledger(tmp_path, ledger_rows)

    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
    signals = strat.generate_signals(frame, {'state': 'LOW_VOL'}, universe)

    got = _sig_set(signals)
    assert got == {('ZZTGG', 'SHORT'), ('ZZTHH', 'LONG')}, got

    ref = _reference_signals(ledger_path, frame, universe)
    assert ref == got


# ─────────────────────────────────────────────────────────────────────────
# 6. Parity: randomized (seeded) price set. Strategy vs. independent
#    reference must agree EXACTLY, both on a signal day and a no-signal day.
# ─────────────────────────────────────────────────────────────────────────
def test_parity_randomized_signal_day_and_no_signal_day(tmp_path, monkeypatch):
    n = 160
    dates = _dates(n)
    beta, alpha = 0.9, -0.05

    rng = np.random.default_rng(999)
    phi, sigma_eps = 0.85, 0.06
    spread = np.zeros(n)
    for i in range(1, n):
        spread[i] = phi * spread[i - 1] + rng.normal(0.0, sigma_eps)

    frame_full = _pair_frame(dates, 'ZZTRR', 'ZZTSS', beta=beta, alpha=alpha,
                              spread=spread, log_b_seed=42)
    universe = list(frame_full.columns)

    # Ledger as_of set early enough to be valid for ANY cutoff we test below.
    ledger_path = _write_ledger(tmp_path, [
        _ledger_row(dates[60], 'ZZTRR', 'ZZTSS', beta=beta, alpha=alpha, approved=True),
    ])

    strat = CointPairsSectorV2()

    def _run(cutoff_len):
        panel = frame_full.iloc[:cutoff_len]
        monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
        got = _sig_set(strat.generate_signals(panel, {'state': 'LOW_VOL'}, universe))
        ref = _reference_signals(ledger_path, panel, universe)
        return got, ref

    signal_day_len = None
    no_signal_day_len = None
    for cutoff_len in range(Z_WINDOW + 1, n + 1):
        _, ref = _run(cutoff_len)
        if ref and signal_day_len is None:
            signal_day_len = cutoff_len
        if not ref and no_signal_day_len is None:
            no_signal_day_len = cutoff_len
        if signal_day_len is not None and no_signal_day_len is not None:
            break

    assert signal_day_len is not None, 'seeded OU spread produced no crossing at all over the panel -- adjust seed/params'
    assert no_signal_day_len is not None

    got_sig, ref_sig = _run(signal_day_len)
    assert ref_sig != set()
    assert got_sig == ref_sig, (got_sig, ref_sig)

    got_nosig, ref_nosig = _run(no_signal_day_len)
    assert ref_nosig == set()
    assert got_nosig == ref_nosig == set()


# ─────────────────────────────────────────────────────────────────────────
# 7. Missing/empty ledger -> [] (not an exception)
# ─────────────────────────────────────────────────────────────────────────
def test_missing_ledger_returns_empty(tmp_path, monkeypatch):
    dates = _dates(120)
    spread = _tail_spread(seed=123, tail_values=[0.0, 0.05])
    frame = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10,
                         spread=spread, log_b_seed=555)
    universe = list(frame.columns)

    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', str(tmp_path / 'does_not_exist.parquet'))
    signals = strat.generate_signals(frame, {'state': 'LOW_VOL'}, universe)

    assert signals == []


# ─────────────────────────────────────────────────────────────────────────
# 8. Registry wiring smoke test.
# ─────────────────────────────────────────────────────────────────────────
def test_registry_loads_class():
    from strategies.registry import load_strategy_class
    cls = load_strategy_class('S_coint_pairs_sector_v2')
    assert cls is not None
    assert cls.__name__ == 'CointPairsSectorV2'
    assert cls.id == 'S_coint_pairs_sector_v2'


# ─────────────────────────────────────────────────────────────────────────
# 9. Malformed ledger: missing `approved` column must fail CLOSED (zero
#    signals + a log line naming the missing column), NEVER treat every row
#    as approved.
# ─────────────────────────────────────────────────────────────────────────
def test_ledger_missing_approved_column_fails_closed(tmp_path, monkeypatch, capsys):
    dates = _dates(120)
    last = dates[-1]

    spread = _tail_spread(seed=123, tail_values=[0.0, 0.05])
    frame = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10,
                         spread=spread, log_b_seed=555)
    universe = list(frame.columns)

    row = _ledger_row(last, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10, approved=True)
    del row['approved']   # ledger genuinely lacks the approval gate column
    ledger_path = _write_ledger_raw(tmp_path, [row])

    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
    signals = strat.generate_signals(frame, {'state': 'LOW_VOL'}, universe)

    # Fail-closed: the pair otherwise crosses exactly as in test 1, but with
    # no `approved` column present, NOTHING may be treated as approved.
    assert signals == []

    captured = capsys.readouterr()
    assert 'approved' in captured.err
    assert 'missing required columns' in captured.err


# ─────────────────────────────────────────────────────────────────────────
# 10. Malformed ledger: missing `beta` column must yield zero signals with
#     NO exception (previously an AttributeError from generate_signals).
# ─────────────────────────────────────────────────────────────────────────
def test_ledger_missing_beta_column_no_exception(tmp_path, monkeypatch):
    dates = _dates(120)
    last = dates[-1]

    spread = _tail_spread(seed=123, tail_values=[0.0, 0.05])
    frame = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10,
                         spread=spread, log_b_seed=555)
    universe = list(frame.columns)

    row = _ledger_row(last, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10, approved=True)
    del row['beta']
    ledger_path = _write_ledger_raw(tmp_path, [row])

    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
    # Must not raise (this call itself is the assertion of "no exception").
    signals = strat.generate_signals(frame, {'state': 'LOW_VOL'}, universe)

    assert signals == []


# ─────────────────────────────────────────────────────────────────────────
# 11. NaN half_life_days -> hold_days falls back to the operator-overridable
#     default_parameters()['hold_days'] (21), and signal_params['half_life_days']
#     is None -- never a fabricated 21.0.
# ─────────────────────────────────────────────────────────────────────────
def test_nan_half_life_uses_default_hold_days_and_none_param(tmp_path, monkeypatch):
    dates = _dates(120)
    last = dates[-1]

    spread = _tail_spread(seed=123, tail_values=[0.0, 0.05])
    frame = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10,
                         spread=spread, log_b_seed=555)
    universe = list(frame.columns)

    ledger_path = _write_ledger(tmp_path, [
        _ledger_row(last, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10, approved=True,
                    half_life_days=float('nan')),
    ])

    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
    signals = strat.generate_signals(frame, {'state': 'LOW_VOL'}, universe)

    assert len(signals) == 2
    for s in signals:
        assert s.signal_params['hold_days'] == 21
        assert s.signal_params['half_life_days'] is None
    assert strat.default_parameters()['hold_days'] == 21


# ─────────────────────────────────────────────────────────────────────────
# 12. NaN half_life_days + a constructor `parameters={'hold_days': N}`
#     override -> hold_days uses the override, proving the operator-level
#     DB override is genuinely read (not dead).
# ─────────────────────────────────────────────────────────────────────────
def test_nan_half_life_respects_constructor_hold_days_override(tmp_path, monkeypatch):
    dates = _dates(120)
    last = dates[-1]

    spread = _tail_spread(seed=123, tail_values=[0.0, 0.05])
    frame = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10,
                         spread=spread, log_b_seed=555)
    universe = list(frame.columns)

    ledger_path = _write_ledger(tmp_path, [
        _ledger_row(last, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10, approved=True,
                    half_life_days=float('nan')),
    ])

    strat = CointPairsSectorV2(parameters={'hold_days': 10})
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
    signals = strat.generate_signals(frame, {'state': 'LOW_VOL'}, universe)

    assert len(signals) == 2
    for s in signals:
        assert s.signal_params['hold_days'] == 10
        assert s.signal_params['half_life_days'] is None


# ─────────────────────────────────────────────────────────────────────────
# 13. A non-positive (negative) close inside the 61-bar window -- but NOT on
#     the final bar -- must not raise, must not signal, and must not leak a
#     RuntimeWarning from log(<=0) (production logs must stay clean).
# ─────────────────────────────────────────────────────────────────────────
def test_negative_price_in_window_no_exception_no_signal_no_warning(tmp_path, monkeypatch):
    dates = _dates(120)
    last = dates[-1]

    spread = _tail_spread(seed=123, tail_values=[0.0, 0.05])
    frame = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10,
                         spread=spread, log_b_seed=555)
    # Corrupt one close inside the trailing 61-bar window (not the last bar)
    # with a negative price -- a malformed-data scenario that must be masked
    # out cleanly rather than hitting log(<=0).
    corrupt_idx = frame.index[-30]
    frame.loc[corrupt_idx, 'ZZTAA'] = -5.0
    universe = list(frame.columns)

    ledger_path = _write_ledger(tmp_path, [
        _ledger_row(last, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10, approved=True),
    ])

    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)

    with warnings.catch_warnings():
        warnings.simplefilter('error', RuntimeWarning)
        signals = strat.generate_signals(frame, {'state': 'LOW_VOL'}, universe)

    assert signals == []


# ─────────────────────────────────────────────────────────────────────────
# X1-D1 (2026-08-28): spread-implied per-leg stops with a hold-horizon vol
# floor, replacing the base-class 2xATR / 5% per-leg levels that fired on
# 70% of trades in run 655c4bdb.
# ─────────────────────────────────────────────────────────────────────────
def test_pair_leg_levels_geometry_uses_wider_of_spread_and_vol_floor():
    # LONG leg: stop below entry by the wider of the two log-distances,
    # targets above at TARGET_R multiples of that same distance.
    lv = CointPairsSectorV2._pair_leg_levels('LONG', 100.0, spread_log=0.03, vol_log=0.05)
    assert lv['stop'] == pytest.approx(100.0 * np.exp(-0.05), rel=1e-6)
    assert lv['t1'] == pytest.approx(100.0 * np.exp(CointPairsSectorV2.TARGET_R * 0.05), rel=1e-6)
    assert lv['t1'] < lv['t2'] < lv['t3']
    assert lv['used_log'] == pytest.approx(0.05)
    # SHORT leg mirrored, spread term wider this time.
    sv = CointPairsSectorV2._pair_leg_levels('SHORT', 50.0, spread_log=0.08, vol_log=0.02)
    assert sv['stop'] == pytest.approx(50.0 * np.exp(0.08), rel=1e-6)
    assert sv['t1'] == pytest.approx(50.0 * np.exp(-CointPairsSectorV2.TARGET_R * 0.08), rel=1e-6)
    assert sv['t1'] > sv['t2'] > sv['t3']
    # No spread term (non-positive beta leg) -> vol floor alone.
    nv = CointPairsSectorV2._pair_leg_levels('LONG', 10.0, spread_log=None, vol_log=0.04)
    assert nv['used_log'] == pytest.approx(0.04)


def test_signals_carry_spread_implied_stops_not_atr(tmp_path, monkeypatch):
    dates = _dates(120)
    last = dates[-1]
    spread1 = _tail_spread(seed=123, tail_values=[0.0, 0.05])
    frame1 = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10,
                          spread=spread1, log_b_seed=555)
    prices = frame1
    universe = list(prices.columns)
    ledger_path = _write_ledger(tmp_path, [
        _ledger_row(last, 'ZZTAA', 'ZZTBB', beta=0.75, alpha=0.10, approved=True, half_life_days=6.0),
    ])
    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
    signals = strat.generate_signals(prices, {'state': 'LOW_VOL'}, universe)
    a_sig = next(s for s in signals if s.ticker == 'ZZTAA')   # SHORT (z>0)
    b_sig = next(s for s in signals if s.ticker == 'ZZTBB')   # LONG

    # Recompute the spread std the strategy used (last Z_WINDOW spread values).
    sa = np.log(prices['ZZTAA'].to_numpy(dtype=float)[-(Z_WINDOW + 1):])
    sb = np.log(prices['ZZTBB'].to_numpy(dtype=float)[-(Z_WINDOW + 1):])
    spread = sa - 0.75 * sb - 0.10
    std_t = spread[1:].std(ddof=1)
    z = a_sig.signal_params['z']
    expected_spread_a = (CointPairsSectorV2.Z_STOP - abs(z)) * std_t
    hold = a_sig.signal_params['hold_days']   # 18
    sig_a = np.diff(sa).std(ddof=1)
    sig_b = np.diff(sb).std(ddof=1)
    expected_vol_a = CointPairsSectorV2.STOP_HOLD_SIGMAS * sig_a * np.sqrt(hold)
    expected_vol_b = CointPairsSectorV2.STOP_HOLD_SIGMAS * sig_b * np.sqrt(hold)

    ba = a_sig.signal_params['stop_basis']
    bb = b_sig.signal_params['stop_basis']
    assert ba['spread_log'] == pytest.approx(expected_spread_a, rel=1e-3)
    assert bb['spread_log'] == pytest.approx(expected_spread_a / 0.75, rel=1e-3)
    assert ba['vol_log'] == pytest.approx(expected_vol_a, rel=1e-3)
    assert bb['vol_log'] == pytest.approx(expected_vol_b, rel=1e-3)
    assert ba['used_log'] == pytest.approx(max(ba['spread_log'], ba['vol_log']))
    assert bb['used_log'] == pytest.approx(max(bb['spread_log'], bb['vol_log']))

    # Levels follow the used distance: SHORT A stop above entry, LONG B below.
    assert np.log(a_sig.stop_loss / a_sig.entry_price) == pytest.approx(ba['used_log'], rel=1e-3)
    assert np.log(b_sig.entry_price / b_sig.stop_loss) == pytest.approx(bb['used_log'], rel=1e-3)
    assert np.log(a_sig.entry_price / a_sig.target_1) == pytest.approx(
        CointPairsSectorV2.TARGET_R * ba['used_log'], rel=1e-3)
    assert np.log(b_sig.target_1 / b_sig.entry_price) == pytest.approx(
        CointPairsSectorV2.TARGET_R * bb['used_log'], rel=1e-3)
    # And it is NOT the base-class 2xATR stop.
    base_a = strat.compute_stops_and_targets(prices['ZZTAA'].dropna(), 'SHORT', a_sig.entry_price,
                                             regime_state='LOW_VOL')
    assert a_sig.stop_loss != base_a['stop']


def test_nonpositive_beta_drops_spread_term_on_leg_b_only(tmp_path, monkeypatch):
    dates = _dates(120)
    last = dates[-1]
    spread1 = _tail_spread(seed=321, tail_values=[0.0, 0.05])
    frame1 = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=-0.6, alpha=0.05,
                          spread=spread1, log_b_seed=444)
    universe = list(frame1.columns)
    ledger_path = _write_ledger(tmp_path, [
        _ledger_row(last, 'ZZTAA', 'ZZTBB', beta=-0.6, alpha=0.05, approved=True, half_life_days=5.0),
    ])
    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
    signals = strat.generate_signals(frame1, {'state': 'LOW_VOL'}, universe)
    assert len(signals) == 2, _sig_set(signals)
    a_sig = next(s for s in signals if s.ticker == 'ZZTAA')
    b_sig = next(s for s in signals if s.ticker == 'ZZTBB')
    assert a_sig.signal_params['stop_basis']['spread_log'] is not None
    assert b_sig.signal_params['stop_basis']['spread_log'] is None
    assert b_sig.signal_params['stop_basis']['used_log'] == pytest.approx(
        b_sig.signal_params['stop_basis']['vol_log'])
