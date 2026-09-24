"""D4 — Garman-Klass 63-session range-vol decile variant (spec 2026-09-12 §4 D4).

Synthetic panels only: _extra_panels.load_wide is monkeypatched on the strategy
module, so nothing reads data/master/prices.parquet.

Amendment (controller, 2026-09-24 01:4x UTC) — manifest.json and
strategy_signatures.json are NOT edited on this branch (same ruling as
Stream A Task 6): the original test_manifest_entry_is_a_candidate and
test_signature_entry_exists (which read the REAL manifest/signatures files)
are replaced below with tests of scripts/register_low_volatility_us_gk63.py
— the deferred operator script that performs the real manifest insertion
after the wave-2 merge — run ONLY against tmp_path fixture manifests, never
the real src/strategies/manifest.json.

Run only this file:
  cd /root/openclaw/.claude/worktrees/qd-adoptions && \
    nice -n 19 python3 -m pytest tests/strategies/test_low_volatility_us_gk63.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies.implementations import S_low_volatility_us_gk63 as gk  # noqa: E402

N_DAYS  = 90
TICKERS = [f'ZZT{i:02d}' for i in range(30)]
QUIET   = TICKERS[:3]        # the three names that must be selected


def _regime(state='LOW_VOL'):
    return {'state': state, 'state_probabilities': {state: 1.0}, 'confidence': 1.0,
            'stress_score': 15, 'position_scale': 1.0}


def _panels():
    """Closes plus O/H/L built so QUIET has a tiny intraday range and everyone
    else a wide one. Close-to-close vol is IDENTICAL across all names, so a
    close-only ranking could not separate them — only the range estimator can."""
    idx = pd.bdate_range(end='2026-08-21', periods=N_DAYS)
    rng = np.random.default_rng(11)
    base = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.008, N_DAYS)))
    closes = pd.DataFrame({t: base + i * 0.01 for i, t in enumerate(TICKERS)}, index=idx)
    opens, highs, lows = {}, {}, {}
    for t in TICKERS:
        c = closes[t].to_numpy()
        width = 0.0008 if t in QUIET else 0.030
        opens[t] = c * (1.0 - width * 0.25)
        highs[t] = c * (1.0 + width)
        lows[t]  = c * (1.0 - width)
    return (closes,
            {'open':  pd.DataFrame(opens,  index=idx),
             'high':  pd.DataFrame(highs,  index=idx),
             'low':   pd.DataFrame(lows,   index=idx),
             'close': closes})


@pytest.fixture
def wired(monkeypatch):
    closes, panels = _panels()
    def _load_wide(field, tickers, date_floor='2021-01-01'):
        return panels[field][[t for t in tickers if t in panels[field].columns]]
    monkeypatch.setattr(gk, 'load_wide', _load_wide)
    return closes


# ── the estimator ─────────────────────────────────────────────────────────────

def test_garman_klass_matches_the_closed_form():
    o = pd.DataFrame({'A': [100.0]})
    h = pd.DataFrame({'A': [102.0]})
    l = pd.DataFrame({'A': [99.0]})
    c = pd.DataFrame({'A': [101.0]})
    expected = (0.5 * np.log(102.0 / 99.0) ** 2
                - (2 * np.log(2) - 1) * np.log(101.0 / 100.0) ** 2)
    got = gk.garman_klass_variance(o, h, l, c).iloc[0, 0]
    assert got == pytest.approx(expected)


def test_garman_klass_nans_non_positive_legs():
    o = pd.DataFrame({'A': [100.0, 0.0]})
    h = pd.DataFrame({'A': [102.0, 5.0]})
    l = pd.DataFrame({'A': [99.0, -1.0]})
    c = pd.DataFrame({'A': [101.0, 4.0]})
    out = gk.garman_klass_variance(o, h, l, c)
    assert not pd.isna(out.iloc[0, 0])
    assert pd.isna(out.iloc[1, 0])


def test_garman_klass_nans_zero_range_bars():
    """R4 — a stale/illiquid O=H=L=C bar must be NaN, not a legitimate
    zero-variance bar (0.5*ln(1)^2 - c*ln(1)^2 == 0 under the raw formula),
    or an illiquid non-trading name would look like the quietest name in the
    universe and win the low-vol ranking on bad data."""
    o = pd.DataFrame({'A': [100.0, 100.0]})
    h = pd.DataFrame({'A': [100.0, 102.0]})
    l = pd.DataFrame({'A': [100.0, 99.0]})
    c = pd.DataFrame({'A': [100.0, 101.0]})
    out = gk.garman_klass_variance(o, h, l, c)
    assert pd.isna(out.iloc[0, 0]), 'zero-range O=H=L=C bar must be NaN, not 0'
    assert not pd.isna(out.iloc[1, 0])


def test_garman_klass_nans_inconsistent_bars():
    """R4 — H must bound max(O, C) and L must bound min(O, C), or the bar is
    an internally inconsistent bad print (e.g. O above H) and must be NaN,
    not fed into the formula as-is."""
    # row 0: O above H (bad print). row 1: L above C (bad print). row 2: a
    # normal, consistent bar.
    o = pd.DataFrame({'A': [105.0, 100.0, 100.0]})
    h = pd.DataFrame({'A': [102.0, 102.0, 102.0]})
    l = pd.DataFrame({'A': [99.0,  101.0, 99.0]})
    c = pd.DataFrame({'A': [101.0, 100.5, 101.0]})
    out = gk.garman_klass_variance(o, h, l, c)
    assert pd.isna(out.iloc[0, 0])
    assert pd.isna(out.iloc[1, 0])
    assert not pd.isna(out.iloc[2, 0])


# ── the strategy ──────────────────────────────────────────────────────────────

def test_ranking_selects_the_low_gk_names(wired):
    s = gk.LowVolatilityUSGK63()
    signals = s.generate_signals(wired, _regime(), TICKERS)
    assert len(signals) == 3, f'decile of 30 = 3, got {len(signals)}'
    assert sorted(sig.ticker for sig in signals) == sorted(QUIET)
    assert all(sig.direction == 'LONG' for sig in signals)


def test_signals_carry_gk_params_and_house_brackets(wired):
    s = gk.LowVolatilityUSGK63()
    sig = s.generate_signals(wired, _regime(), TICKERS)[0]
    assert 'gk_var_63d' in sig.signal_params
    assert 'gk_vol_ann' in sig.signal_params
    assert 0.0 <= sig.signal_params['universe_gk_pctile'] <= 1.0
    assert sig.stop_loss < sig.entry_price < sig.target_1
    assert sig.position_size_pct > 0


def test_inactive_regime_emits_nothing(wired):
    s = gk.LowVolatilityUSGK63()
    assert s.generate_signals(wired, _regime('CRISIS'), TICKERS) == []


def test_empty_or_short_panel_emits_nothing(wired):
    s = gk.LowVolatilityUSGK63()
    assert s.generate_signals(pd.DataFrame(), _regime(), TICKERS) == []
    assert s.generate_signals(wired.head(10), _regime(), TICKERS) == []


def test_missing_ohlc_panel_emits_nothing_instead_of_raising(monkeypatch, wired):
    monkeypatch.setattr(gk, 'load_wide', lambda field, tickers, date_floor='2021-01-01': pd.DataFrame())
    s = gk.LowVolatilityUSGK63()
    assert s.generate_signals(wired, _regime(), TICKERS) == []


def test_ohlc_is_sliced_to_the_signal_date(monkeypatch, wired):
    """Point-in-time: nothing after prices.index[-1] may reach the estimator.

    Strengthened beyond the brief's original assert (which only compared the
    fixture panel's tail to the truncated close panel's tail and would pass
    even if the strategy never sliced anything): also spy on
    garman_klass_variance itself and record the actual index handed to the
    estimator, so an early return can't make the assertion vacuous."""
    closes, panels = _panels()
    seen = {}
    def _load_wide(field, tickers, date_floor='2021-01-01'):
        seen[field] = panels[field]
        return panels[field]
    monkeypatch.setattr(gk, 'load_wide', _load_wide)

    recorded = {}
    _real_gk_variance = gk.garman_klass_variance
    def _spy(open_, high, low, close):
        recorded['index'] = open_.index
        return _real_gk_variance(open_, high, low, close)
    monkeypatch.setattr(gk, 'garman_klass_variance', _spy)

    s = gk.LowVolatilityUSGK63()
    truncated = closes.iloc[:70]
    s.generate_signals(truncated, _regime(), TICKERS)

    # the panels handed back by load_wide extend past the signal date...
    assert seen['high'].index[-1] > truncated.index[-1]
    # ...but the estimator was actually called (not an early return)...
    assert 'index' in recorded, (
        'garman_klass_variance was never called — an early return would make '
        'the point-in-time assertion below vacuously true')
    # ...and never saw a bar after the signal date, over exactly GK_WINDOW bars.
    assert recorded['index'].max() == truncated.index[-1]
    assert len(recorded['index']) == gk.LowVolatilityUSGK63.GK_WINDOW


def test_load_wide_key_is_stable_across_universe_subsets(monkeypatch, wired):
    """load_wide must be keyed by the full close-panel column set
    (prices.columns), not by the per-call `universe` argument — else a
    backtest whose point-in-time resolver varies `bar_universe` every bar
    would cache-miss load_wide on every single bar (chunked full-parquet
    reads/bar for a daily-cadence strategy; _extra_panels.py:125-131
    documents ~3s/bar for exactly this pattern). Mirrors the liquid_pool
    precedent (_extra_panels.py:125-131). Fix round 1 R2 dropped the fourth
    (CLOSE) load_wide call, so this is 3 OHL fields x 2 calls, not 4x2."""
    _, panels = _panels()
    seen_tickers = []
    def _load_wide(field, tickers, date_floor='2021-01-01'):
        seen_tickers.append(tuple(sorted(tickers)))
        return panels[field][[t for t in tickers if t in panels[field].columns]]
    monkeypatch.setattr(gk, 'load_wide', _load_wide)

    s = gk.LowVolatilityUSGK63()
    s.generate_signals(wired, _regime(), TICKERS)        # full universe
    s.generate_signals(wired, _regime(), TICKERS[:20])    # narrower universe, same price panel

    assert len(seen_tickers) == 6   # 3 OHL fields x 2 generate_signals calls
    assert len(set(seen_tickers)) == 1, (
        'load_wide was called with a different ticker set across two calls '
        'sharing the SAME price panel but different `universe` arguments — '
        'the cache key must track prices.columns, not the per-bar universe list')


def test_ohlc_panel_calendar_never_dilutes_the_equity_window(monkeypatch):
    """R1 — reviewer repro: `load_wide(field, list(prices.columns))` used to
    include 7-day tickers (BTC-USD etc.); apply_equity_calendar drops rows,
    never columns, so the OHLC panel load_wide handed back was on the union
    calendar and `.tail(63)` spanned ~63 CALENDAR days (~43-45 equity bars)
    — starving MIN_VALID and dropping signals to 0.

    Fixture: `prices` (the engine's close panel) carries a BTC-USD column
    (R1(a) — this must never reach load_wide's tickers argument) and is
    missing one mid-window row (`holiday`, a date load_wide's OHLC panel
    still has real data for — e.g. a stale/bad print — but which is not an
    equity trading day in `prices`). The stubbed load_wide UNCONDITIONALLY
    returns its OHLC panel on a 7-day/union calendar (every calendar day,
    weekends included) REGARDLESS of which tickers were requested, so this
    pins R1(b)'s reindex-to-prices.index-before-tail fix independently of
    R1(a). If R1(b) were reverted to a plain `.loc[:asof].tail(63)`, the
    window would be 63 CALENDAR days — about 45 weekdays, 44 once `holiday`
    (present in the raw union-calendar panel but not in `prices.index`) is
    dropped by the `idx.intersection(prices.index)` guard downstream — which
    is below `MIN_VALID (45)`, so `generate_signals` would return `[]` before
    `garman_klass_variance` is ever called: the `'index' in recorded`
    assertion below is what actually catches a reverted R1(b), not a leaked
    `holiday` row (the intersection guard drops it either way).

    The three quiet names must still be selected, and the window that
    actually reaches garman_klass_variance must be exactly GK_WINDOW rows,
    all a subset of prices.index, with the dropped holiday excluded."""
    closes, panels = _panels()
    holiday = closes.index[-30]     # a mid-window date, not near either edge
    prices = closes.drop(index=holiday).copy()
    prices['BTC-USD'] = 100.0       # non-equity column present in prices.columns

    # Unconditional 7-day/union calendar: model load_wide's own OHLC pivot as
    # always spanning every calendar day in range (weekends + `holiday`
    # included, since the underlying parquet still has a — possibly stale —
    # row there), independent of which tickers were requested.
    union_idx = pd.date_range(closes.index[0], closes.index[-1], freq='D')
    seen_tickers = []

    def _load_wide(field, tickers, date_floor='2016-01-01'):
        seen_tickers.append(tuple(sorted(tickers)))
        base = panels[field][[t for t in tickers if t in panels[field].columns]]
        return base.reindex(union_idx)
    monkeypatch.setattr(gk, 'load_wide', _load_wide)

    recorded = {}
    _real_gk_variance = gk.garman_klass_variance
    def _spy(open_, high, low, close):
        recorded['index'] = open_.index
        return _real_gk_variance(open_, high, low, close)
    monkeypatch.setattr(gk, 'garman_klass_variance', _spy)

    s = gk.LowVolatilityUSGK63()
    signals = s.generate_signals(prices, _regime(), TICKERS + ['BTC-USD'])

    # R1(a): load_wide must never have been asked for the non-equity ticker.
    for tickers in seen_tickers:
        assert 'BTC-USD' not in tickers, (
            'load_wide was called with a non-equity ticker — apply_equity_calendar '
            'only drops rows, never columns, so this would pull the OHLC panel '
            'onto the union calendar and starve the 63-bar equity window')

    # R1(b): the window that reached the estimator is exactly GK_WINDOW rows,
    # all drawn from prices.index — never diluted by load_wide's own (here,
    # unconditionally union) calendar, and the dropped holiday never leaks in.
    assert 'index' in recorded
    assert len(recorded['index']) == gk.LowVolatilityUSGK63.GK_WINDOW
    assert set(recorded['index']).issubset(set(prices.index))
    assert holiday not in recorded['index']

    # Reviewer repro, inverted: with the fix, neither the crypto column nor
    # the union-calendar OHLC panel starves the selection — still exactly
    # the 3 quiet names.
    assert len(signals) == 3, f'decile of 30 = 3, got {len(signals)}'
    assert sorted(sig.ticker for sig in signals) == sorted(QUIET)


def test_zero_range_bars_are_never_selected_as_quietest(monkeypatch):
    """R4 (i) — a name whose ENTIRE window is zero-range (O=H=L=C, a
    stale/non-trading print) must not be selected even though the raw,
    unmasked GK formula would score it a literal 0 (the lowest possible
    'variance' in the universe) and let it win the low-vol decile on bad
    data instead of on genuinely low realized risk."""
    closes, panels = _panels()
    stale = QUIET[0]
    flat_val = float(closes[stale].iloc[-1])
    closes = closes.copy()
    closes[stale] = flat_val
    for field in ('open', 'high', 'low'):
        panels[field] = panels[field].copy()
        panels[field][stale] = flat_val

    def _load_wide(field, tickers, date_floor='2016-01-01'):
        return panels[field][[t for t in tickers if t in panels[field].columns]]
    monkeypatch.setattr(gk, 'load_wide', _load_wide)

    s = gk.LowVolatilityUSGK63()
    signals = s.generate_signals(closes, _regime(), TICKERS)

    assert stale not in [sig.ticker for sig in signals], (
        f'{stale} has a zero-range window (stale/illiquid bad data) and must '
        'never be selected as the "quietest" name in the universe')


def test_inconsistent_bars_are_masked_not_pulled_negative(monkeypatch):
    """R4 (ii) — a name with 10 inconsistent bars (O pushed above H) inside
    its 63-bar window must be ranked on its remaining 53 valid bars, not
    have its mean pulled toward a spurious negative value by feeding the
    unmasked formula a bad print (with O above H, ln(C/O) can dominate
    ln(H/L) and drive the raw, unmasked per-bar 'variance' sharply negative
    for that one bar)."""
    idx = pd.bdate_range(end='2026-08-21', periods=N_DAYS)
    window = idx[-gk.LowVolatilityUSGK63.GK_WINDOW:]
    bad_dates = window[15:25]   # 10 consecutive business days inside the window

    tickers = ['QUIETEST', 'BAD'] + [f'BG{i:02d}' for i in range(18)]
    rng = np.random.default_rng(31)
    base = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.006, N_DAYS)))
    closes = pd.DataFrame({t: base + i * 0.01 for i, t in enumerate(tickers)}, index=idx)

    opens, highs, lows = {}, {}, {}
    for t in tickers:
        c = closes[t].to_numpy()
        width = 0.0007 if t in ('QUIETEST', 'BAD') else 0.02
        opens[t] = c * (1.0 - width * 0.25)
        highs[t] = c * (1.0 + width)
        lows[t]  = c * (1.0 - width)
    open_df = pd.DataFrame(opens, index=idx)
    high_df = pd.DataFrame(highs, index=idx)
    low_df  = pd.DataFrame(lows,  index=idx)

    # Corrupt BAD's 10 bad_dates: push O above H (inconsistent print).
    open_df.loc[bad_dates, 'BAD'] = high_df.loc[bad_dates, 'BAD'] * 1.5

    panels = {'open': open_df, 'high': high_df, 'low': low_df}

    def _load_wide(field, req_tickers, date_floor='2016-01-01'):
        return panels[field][[t for t in req_tickers if t in panels[field].columns]]
    monkeypatch.setattr(gk, 'load_wide', _load_wide)

    s = gk.LowVolatilityUSGK63()
    signals = s.generate_signals(closes, _regime(), tickers)

    bad_sig = next((sig for sig in signals if sig.ticker == 'BAD'), None)
    assert bad_sig is not None, 'BAD should still be ranked on its 53 remaining valid bars'
    assert bad_sig.signal_params['gk_bars_used'] == gk.LowVolatilityUSGK63.GK_WINDOW - 10
    assert bad_sig.signal_params['gk_var_63d'] > 0, (
        'a masked-out bad print must not pull the mean to a spurious negative value')

    # Cross-check against computing the estimator directly on just the 53
    # valid (non-corrupted) bars.
    good_dates = window.difference(bad_dates)
    o = open_df.loc[good_dates, ['BAD']].astype('float64')
    h = high_df.loc[good_dates, ['BAD']].astype('float64')
    l = low_df.loc[good_dates, ['BAD']].astype('float64')
    c = closes.loc[good_dates, ['BAD']].astype('float64')
    expected = gk.garman_klass_variance(o, h, l, c).mean(skipna=True).iloc[0]
    # signal_params rounds to 10 decimal places; expected is ~1e-6 in scale,
    # so bound the comparison in absolute terms rather than relative.
    assert bad_sig.signal_params['gk_var_63d'] == pytest.approx(expected, abs=1e-9)


def test_short_ohlc_history_returns_empty_no_raise(monkeypatch):
    """R6 — prices has 200 bars but the self-loaded O/H/L panels only cover
    the trailing 30 of those dates (e.g. a name added to prices.parquet's
    OHLC columns much later than its close history): too few real GK bars
    inside the 63-bar window (30 < MIN_VALID=45) must return [], never raise."""
    idx200 = pd.bdate_range(end='2026-08-21', periods=200)
    rng = np.random.default_rng(7)
    base = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.005, 200)))
    closes200 = pd.DataFrame({t: base for t in TICKERS}, index=idx200)

    idx30 = idx200[-30:]
    c30 = closes200.loc[idx30]
    short_panels = {'open': c30 * 0.999, 'high': c30 * 1.002, 'low': c30 * 0.998}

    def _load_wide(field, tickers, date_floor='2016-01-01'):
        panel = short_panels[field]
        return panel[[t for t in tickers if t in panel.columns]]
    monkeypatch.setattr(gk, 'load_wide', _load_wide)

    s = gk.LowVolatilityUSGK63()
    assert s.generate_signals(closes200, _regime(), TICKERS) == []


def test_contract_surface_matches_the_parent():
    s = gk.LowVolatilityUSGK63()
    assert s.id == 'S_low_volatility_us_gk63'
    assert s.DECILE_FRAC == 0.10
    assert s.GK_WINDOW == 63
    assert 'LOW_VOL' in s.active_in_regimes and 'TRANSITIONING' in s.active_in_regimes


# ── registration: registry map (requirement c) ────────────────────────────────

def test_registered_in_impl_map():
    from strategies.registry import _IMPL_MAP
    assert _IMPL_MAP['S_low_volatility_us_gk63'] == (
        'strategies.implementations.S_low_volatility_us_gk63', 'LowVolatilityUSGK63')


def test_requirements_file_mirrors_the_parent():
    impl = ROOT / 'src' / 'strategies' / 'implementations'
    child = json.loads((impl / 'S_low_volatility_us_gk63.requirements.json').read_text())
    parent = json.loads((impl / 'low_volatility_us.requirements.json').read_text())
    assert child['required'] == parent['required']
    assert child['strategy_id'] == 'S_low_volatility_us_gk63'


# ── registration: operator script (requirements a, b) ─────────────────────────
# scripts/register_low_volatility_us_gk63.py is the deferred operator action
# that inserts the candidate entry into the REAL manifest after the wave-2
# merge. Exercised here ONLY against tmp_path fixture copies — never
# src/strategies/manifest.json.

_REG_SPEC = importlib.util.spec_from_file_location(
    'register_low_volatility_us_gk63', ROOT / 'scripts' / 'register_low_volatility_us_gk63.py')
register = importlib.util.module_from_spec(_REG_SPEC)
sys.modules['register_low_volatility_us_gk63'] = register
_REG_SPEC.loader.exec_module(register)


def _min_manifest_fixture(tmp_path, extra_note: str = '') -> Path:
    data = {
        'schema_version': '1.0',
        'updated_at': '2026-09-24T00:00:00.000Z',
        'strategies': {
            'low_volatility_us': {
                'state': 'live',
                'state_since': '2026-04-30T15:02:14.260Z',
                'metadata': {
                    'canonical_file': 'low_volatility_us.py',
                    'class': 'LowVolatilityUS',
                    'description': ('Rank stocks asc by 252d realized vol; select '
                                     'lowest-volatility decile; equal-weight' + extra_note),
                    'eligible_regimes': ['CRISIS'],
                    'universe_filter_ref': 'src.strategies.universe_default:tier_r3000',
                },
                'history': [],
                'instrument_class': 'equity',
            },
        },
        'decommissioned': {},
    }
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps(data, indent=2, ensure_ascii=('§' not in extra_note)))
    return p


def test_register_script_dry_run_leaves_the_manifest_untouched(tmp_path, capsys):
    p = _min_manifest_fixture(tmp_path)
    before = p.read_bytes()
    before_mtime = p.stat().st_mtime_ns

    # No --dry-run / --apply flag: dry-run is the DEFAULT, which is what the
    # operator command actually runs for review.
    rc = register.main(['--manifest', str(p)])

    assert rc == 0
    assert p.read_bytes() == before
    assert p.stat().st_mtime_ns == before_mtime
    assert not (tmp_path / 'manifest.json.lock').exists()
    out = capsys.readouterr().out
    assert 'already_exists=False' in out
    assert '"state": "candidate"' in out
    assert '"class": "LowVolatilityUSGK63"' in out
    assert '"canonical_file": "S_low_volatility_us_gk63.py"' in out


def test_register_script_apply_inserts_one_candidate_and_is_idempotent(tmp_path, capsys):
    p = _min_manifest_fixture(tmp_path)
    original = json.loads(p.read_text())

    rc = register.main(['--manifest', str(p), '--apply'])
    assert rc == 0

    m = json.loads(p.read_text())
    entry = m['strategies']['S_low_volatility_us_gk63']
    assert entry['state'] == 'candidate'
    assert entry['metadata']['canonical_file'] == 'S_low_volatility_us_gk63.py'
    assert entry['metadata']['class'] == 'LowVolatilityUSGK63'
    assert entry['metadata']['universe_filter_ref'] == 'src.strategies.universe_default:tier_r3000'
    assert entry['history'] == []
    assert entry['instrument_class'] == 'equity'
    assert entry['state_since']       # a real timestamp was stamped
    # Sibling entry byte-for-value untouched.
    assert m['strategies']['low_volatility_us'] == original['strategies']['low_volatility_us']
    assert not (tmp_path / 'manifest.json.lock').exists()
    capsys.readouterr()

    after_first = p.read_bytes()
    rc2 = register.main(['--manifest', str(p), '--apply'])
    assert rc2 == 0
    assert p.read_bytes() == after_first
    out = capsys.readouterr().out
    assert 'no-op' in out


def test_register_script_noop_apply_never_rewrites_non_ascii_manifest(tmp_path):
    """The real manifest is NOT byte-stable under json.dumps(indent=2) (it
    carries literal non-ASCII characters written by a JS caller — see the
    script's module docstring). A no-op --apply must never call
    write_atomic at all, or it would re-escape those characters into
    \\uXXXX on every run and churn the live file for nothing."""
    p = _min_manifest_fixture(tmp_path, extra_note=' — §review')
    p.write_text(json.dumps(json.loads(p.read_text()), indent=2, ensure_ascii=False))
    assert not register._byte_stable(p.read_text(encoding='utf-8'))

    # Pre-insert the entry directly so this apply call is the no-op path.
    data = json.loads(p.read_text(encoding='utf-8'))
    data['strategies']['S_low_volatility_us_gk63'] = register._new_entry('2026-09-24T02:00:00.000Z')
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    before = p.read_bytes()

    rc = register.main(['--manifest', str(p), '--apply'])

    assert rc == 0
    assert p.read_bytes() == before   # untouched — no re-serialization happened


def test_missing_manifest_file(tmp_path, capsys):
    missing = tmp_path / 'nope.json'
    rc = register.main(['--manifest', str(missing), '--dry-run'])
    assert rc == 1
    assert 'no manifest' in capsys.readouterr().err
