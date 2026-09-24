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
