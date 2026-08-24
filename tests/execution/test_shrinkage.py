"""Tests for task P1 — Ledoit-Wolf shrinkage (shadow-first).

Covers:
  * shrinkage.lw_corr / lw_gamma correctness + degenerate inputs (pure, no DB).
  * orthogonalization.resolve_tangency_gamma precedence (pure).
  * asset_correlation.price_return_corr byte-identical default when
    OPENCLAW_ASSET_CORR_LW is unset (the task's binding requirement).
  * task P1b: orthogonalization.tangency_net_sharpe gamma plumb-through, and
    regime_blended_sizer._tangency_gamma_for_cycle (the live call-site
    resolve+log helper) — both pure, no DB.

Synthetic-only: no real parquet/DB reads. ZZT-prefixed fake tickers per the
task brief's fixture-ticker convention.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import numpy as np
import pandas as pd
import pytest

from execution import shrinkage as sk
from execution import orthogonalization as og
from execution import asset_correlation as ac
from execution import strategy_similarity as ss
from execution import regime_blended_sizer as rbs


# ---------------------------------------------------------------------------
# shrinkage.lw_corr / lw_gamma
# ---------------------------------------------------------------------------

def _synthetic_panel(n_rows: int = 200, seed: int = 42) -> pd.DataFrame:
    """3-asset synthetic returns panel with a shared factor so pairwise
    correlations are non-degenerate and unequal (needed so 'strictly between
    raw pearson and the mean target' is a meaningful assertion)."""
    rng = np.random.default_rng(seed)
    factor = rng.normal(0, 1.0, n_rows)
    a = 0.8 * factor + rng.normal(0, 1.0, n_rows)
    b = 0.4 * factor + rng.normal(0, 1.0, n_rows)
    c = rng.normal(0, 1.0, n_rows)          # ~independent of the factor
    return pd.DataFrame({'ZZT_A': a, 'ZZT_B': b, 'ZZT_C': c})


def _raw_pearson_matrix(panel: pd.DataFrame) -> pd.DataFrame:
    return panel.corr()


class TestLwCorrCorrectness:
    def test_symmetric_unit_diagonal_psd(self):
        panel = _synthetic_panel()
        corr, delta_hat = sk.lw_corr(panel)
        assert corr is not None and delta_hat is not None
        vals = corr.values
        assert np.allclose(np.diag(vals), 1.0, atol=1e-8)
        assert np.allclose(vals, vals.T, atol=1e-8)
        eigvals = np.linalg.eigvalsh(vals)
        assert eigvals.min() >= -1e-10

    def test_delta_hat_in_open_unit_interval(self):
        panel = _synthetic_panel()
        _corr, delta_hat = sk.lw_corr(panel)
        assert 0.0 < delta_hat < 1.0

    def test_off_diagonals_shrunk_toward_mean_target(self):
        panel = _synthetic_panel()
        corr, delta_hat = sk.lw_corr(panel)
        raw = _raw_pearson_matrix(panel)
        cols = list(panel.columns)
        offs = [raw.loc[a, b] for i, a in enumerate(cols) for b in cols[i + 1:]]
        mean_target = sum(offs) / len(offs)
        for i, a in enumerate(cols):
            for b in cols[i + 1:]:
                shrunk_rho = corr.loc[a, b]
                raw_rho = raw.loc[a, b]
                lo, hi = sorted((raw_rho, mean_target))
                # allow tiny float slop; the pairs are constructed to differ
                # meaningfully from the mean target so this is a real bound,
                # not a vacuous one.
                assert lo - 1e-9 <= shrunk_rho <= hi + 1e-9
                assert abs(raw_rho - mean_target) > 1e-6   # sanity: non-vacuous

    def test_lw_gamma_matches_lw_corr_delta(self):
        panel = _synthetic_panel()
        _corr, delta_hat = sk.lw_corr(panel)
        g = sk.lw_gamma(panel)
        assert g == pytest.approx(delta_hat)


class TestLwCorrDegenerate:
    def test_two_columns_returns_none_none(self):
        panel = pd.DataFrame({'ZZT_A': np.arange(100, dtype=float),
                              'ZZT_B': np.arange(100, dtype=float) * 2})
        corr, delta_hat = sk.lw_corr(panel)
        assert corr is None and delta_hat is None

    def test_thirty_rows_returns_none_gamma(self):
        rng = np.random.default_rng(1)
        panel = pd.DataFrame({
            'ZZT_A': rng.normal(size=30),
            'ZZT_B': rng.normal(size=30),
            'ZZT_C': rng.normal(size=30),
        })
        assert sk.lw_gamma(panel) is None

    def test_all_nan_column_dropped_then_reevaluated(self):
        rng = np.random.default_rng(2)
        panel = pd.DataFrame({
            'ZZT_A': rng.normal(size=50),
            'ZZT_B': rng.normal(size=50),
            'ZZT_C': [float('nan')] * 50,
        })
        # after dropping the all-NaN column only 2 columns remain -> None
        assert sk.lw_gamma(panel) is None


# ---------------------------------------------------------------------------
# orthogonalization.resolve_tangency_gamma
# ---------------------------------------------------------------------------

class TestResolveTangencyGamma:
    def test_nothing_set_returns_default(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)
        assert og.resolve_tangency_gamma(None) == og.TANGENCY_SHRINK_DEFAULT
        assert og.resolve_tangency_gamma(0.05) == og.TANGENCY_SHRINK_DEFAULT

    def test_env_override_wins_over_lw_armed_artifact(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_TANGENCY_SHRINK', '0.25')
        monkeypatch.setenv('OPENCLAW_TANGENCY_LW', '1')
        assert og.resolve_tangency_gamma(0.05) == pytest.approx(0.25)

    def test_lw_armed_uses_artifact_gamma(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.setenv('OPENCLAW_TANGENCY_LW', '1')
        assert og.resolve_tangency_gamma(0.05) == pytest.approx(0.05)

    def test_lw_armed_but_no_artifact_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.setenv('OPENCLAW_TANGENCY_LW', '1')
        assert og.resolve_tangency_gamma(None) == og.TANGENCY_SHRINK_DEFAULT

    def test_default_is_exactly_point_one(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)
        assert og.resolve_tangency_gamma(None) == 0.10

    def test_malformed_override_falls_through_to_artifact_gamma(self, monkeypatch):
        # A malformed OPENCLAW_TANGENCY_SHRINK must not win precedence #1 (it
        # can't be parsed as a float) and must not silently land on the
        # default either — it must genuinely fall through to precedence #2
        # (artifact_gamma, since LW is armed here).
        monkeypatch.setenv('OPENCLAW_TANGENCY_SHRINK', 'abc')
        monkeypatch.setenv('OPENCLAW_TANGENCY_LW', '1')
        assert og.resolve_tangency_gamma(0.05) == pytest.approx(0.05)

    def test_malformed_override_falls_through_to_default_when_lw_not_armed(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_TANGENCY_SHRINK', 'abc')
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)
        assert og.resolve_tangency_gamma(0.05) == og.TANGENCY_SHRINK_DEFAULT


class TestResolveTangencyGammaRangeClamp:
    """Review fix (2026-08-24): any resolved candidate outside [0,1], from
    ANY source (env override or artifact_gamma), is rejected rather than
    silently used — R = (1-γ)R_raw + γI is only a convex blend inside that
    range."""

    def test_env_above_one_falls_through_to_default_when_lw_unarmed(self, monkeypatch, capsys):
        monkeypatch.setenv('OPENCLAW_TANGENCY_SHRINK', '1.5')
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)

        assert og.resolve_tangency_gamma(0.05) == og.TANGENCY_SHRINK_DEFAULT

        err = capsys.readouterr().err
        assert '[tangency_lw] rejected gamma=1.500 outside [0,1]; using 0.10' in err

    def test_env_above_one_falls_through_to_artifact_when_lw_armed(self, monkeypatch, capsys):
        monkeypatch.setenv('OPENCLAW_TANGENCY_SHRINK', '1.5')
        monkeypatch.setenv('OPENCLAW_TANGENCY_LW', '1')

        assert og.resolve_tangency_gamma(0.05) == pytest.approx(0.05)

        err = capsys.readouterr().err
        assert '[tangency_lw] rejected gamma=1.500 outside [0,1]; using 0.05' in err

    def test_env_below_zero_falls_through_to_default_when_lw_unarmed(self, monkeypatch, capsys):
        monkeypatch.setenv('OPENCLAW_TANGENCY_SHRINK', '-0.2')
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)

        assert og.resolve_tangency_gamma(0.05) == og.TANGENCY_SHRINK_DEFAULT

        err = capsys.readouterr().err
        assert '[tangency_lw] rejected gamma=-0.200 outside [0,1]; using 0.10' in err

    def test_env_below_zero_falls_through_to_artifact_when_lw_armed(self, monkeypatch, capsys):
        monkeypatch.setenv('OPENCLAW_TANGENCY_SHRINK', '-0.2')
        monkeypatch.setenv('OPENCLAW_TANGENCY_LW', '1')

        assert og.resolve_tangency_gamma(0.05) == pytest.approx(0.05)

        err = capsys.readouterr().err
        assert '[tangency_lw] rejected gamma=-0.200 outside [0,1]; using 0.05' in err

    def test_env_boundary_one_accepted_as_is(self, monkeypatch, capsys):
        monkeypatch.setenv('OPENCLAW_TANGENCY_SHRINK', '1.0')
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)

        assert og.resolve_tangency_gamma(0.05) == 1.0

        err = capsys.readouterr().err
        assert 'rejected' not in err

    def test_env_boundary_zero_accepted_as_is(self, monkeypatch, capsys):
        monkeypatch.setenv('OPENCLAW_TANGENCY_SHRINK', '0.0')
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)

        assert og.resolve_tangency_gamma(0.05) == 0.0

        err = capsys.readouterr().err
        assert 'rejected' not in err

    def test_artifact_gamma_out_of_range_also_rejected_defense_in_depth(self, monkeypatch, capsys):
        """artifact_gamma is already bounded [0,1] by pypfopt's own delta
        clamp in practice, but the resolver validates it too — a corrupted
        or unexpected stored value must not silently pass through."""
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.setenv('OPENCLAW_TANGENCY_LW', '1')

        assert og.resolve_tangency_gamma(1.7) == og.TANGENCY_SHRINK_DEFAULT

        err = capsys.readouterr().err
        assert '[tangency_lw] rejected gamma=1.700 outside [0,1]; using 0.10' in err

    def test_malformed_override_unaffected_by_clamp_still_silent(self, monkeypatch, capsys):
        """Non-numeric override stays silent (no 'rejected' line) — the
        clamp only fires for a value that actually parsed as a float."""
        monkeypatch.setenv('OPENCLAW_TANGENCY_SHRINK', 'abc')
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)

        assert og.resolve_tangency_gamma(None) == og.TANGENCY_SHRINK_DEFAULT

        err = capsys.readouterr().err
        assert 'rejected' not in err


# ---------------------------------------------------------------------------
# asset_correlation.price_return_corr — byte-identical default
# ---------------------------------------------------------------------------

def _legacy_pearson_reference(returns: dict) -> dict:
    """Vendored copy of the pre-existing pairwise Pearson computation
    (mirrors asset_correlation.corr_from_returns / _pearson exactly) —
    independent of the module under test, so it can't rot in lockstep with a
    bug introduced there."""
    def pearson(xs, ys):
        n = len(xs)
        if n < 2:
            return None
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        dy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if dx == 0 or dy == 0:
            return None
        return num / (dx * dy)

    tickers = sorted(returns)
    out = {t: {} for t in tickers}
    for t in tickers:
        out[t][t] = 1.0
    for i, a in enumerate(tickers):
        da = returns[a]
        for b in tickers[i + 1:]:
            db = returns[b]
            common = sorted(set(da) & set(db))
            if len(common) < 20:
                rho = 0.0
            else:
                r = pearson([da[d] for d in common], [db[d] for d in common])
                rho = 0.0 if r is None else max(-1.0, min(1.0, r))
            out[a][b] = out[b][a] = rho
    return out


def _fixture_returns() -> dict:
    # 90 obs: >= MIN_OBS(20, legacy pairwise) AND >= shrinkage.MIN_ROWS(40),
    # so the LW path (not just the legacy path) actually gets exercised.
    import datetime
    rng = np.random.default_rng(7)
    n = 90
    start = datetime.date(2026, 1, 1)
    dates = [(start + datetime.timedelta(days=i)).isoformat() for i in range(n)]
    factor = rng.normal(0, 1.0, n)
    a = 0.9 * factor + rng.normal(0, 0.2, n)
    b = 0.3 * factor + rng.normal(0, 0.2, n)
    c = rng.normal(0, 1.0, n)
    return {
        'ZZT1': dict(zip(dates, a.tolist())),
        'ZZT2': dict(zip(dates, b.tolist())),
        'ZZT3': dict(zip(dates, c.tolist())),
    }


class TestPriceReturnCorrByteIdentical:
    def test_default_unset_matches_vendored_legacy_and_no_shadow_log(self, monkeypatch, capsys):
        """Controller ruling 2026-08-24: env-unset MUST resolve to legacy-only
        ('0'), not 'shadow' — no shadow log line, and (via the raise-on-call
        stub below) the LW fit path must never even be invoked, so the
        pypfopt import inside shrinkage.lw_corr is never triggered."""
        monkeypatch.delenv('OPENCLAW_ASSET_CORR_LW', raising=False)
        fixed = _fixture_returns()
        monkeypatch.setattr(ac, '_load_returns', lambda tickers, window, as_of=None: fixed)

        def _must_not_be_called(panel):
            raise AssertionError('shrinkage.lw_corr must not be called when '
                                  'OPENCLAW_ASSET_CORR_LW is unset')
        monkeypatch.setattr(sk, 'lw_corr', _must_not_be_called)
        monkeypatch.setattr(ac, 'shrinkage', sk, raising=False)

        got = ac.price_return_corr(['ZZT1', 'ZZT2', 'ZZT3'], window=63)
        want = _legacy_pearson_reference(fixed)
        assert got == want
        err = capsys.readouterr().err
        assert '[asset_corr_lw]' not in err

    def test_mode_resolver_unset_and_garbage_both_default_to_zero(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_ASSET_CORR_LW', raising=False)
        assert ac._asset_corr_lw_mode() == '0'
        monkeypatch.setenv('OPENCLAW_ASSET_CORR_LW', '')
        assert ac._asset_corr_lw_mode() == '0'
        monkeypatch.setenv('OPENCLAW_ASSET_CORR_LW', 'garbage')
        assert ac._asset_corr_lw_mode() == '0'

    def test_explicit_shadow_matches_vendored_legacy(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_ASSET_CORR_LW', 'shadow')
        fixed = _fixture_returns()
        monkeypatch.setattr(ac, '_load_returns', lambda tickers, window, as_of=None: fixed)
        got = ac.price_return_corr(['ZZT1', 'ZZT2', 'ZZT3'], window=63)
        want = _legacy_pearson_reference(fixed)
        assert got == want

    def test_explicit_zero_matches_vendored_legacy_and_skips_shadow(self, monkeypatch, capsys):
        monkeypatch.setenv('OPENCLAW_ASSET_CORR_LW', '0')
        fixed = _fixture_returns()
        monkeypatch.setattr(ac, '_load_returns', lambda tickers, window, as_of=None: fixed)
        got = ac.price_return_corr(['ZZT1', 'ZZT2', 'ZZT3'], window=63)
        want = _legacy_pearson_reference(fixed)
        assert got == want
        err = capsys.readouterr().err
        assert '[asset_corr_lw]' not in err

    def test_shadow_logs_one_line_and_returns_legacy(self, monkeypatch, capsys):
        # 'shadow' is now opt-in only (never the default) — set explicitly.
        monkeypatch.setenv('OPENCLAW_ASSET_CORR_LW', 'shadow')
        fixed = _fixture_returns()
        monkeypatch.setattr(ac, '_load_returns', lambda tickers, window, as_of=None: fixed)
        got = ac.price_return_corr(['ZZT1', 'ZZT2', 'ZZT3'], window=63)
        want = _legacy_pearson_reference(fixed)
        assert got == want
        err = capsys.readouterr().err
        lines = [l for l in err.splitlines() if '[asset_corr_lw]' in l]
        assert len(lines) == 1
        assert lines[0].startswith('[asset_corr_lw] shadow: n=')
        assert 'mean_abs_delta_rho=' in lines[0]
        assert 'gamma=' in lines[0]

    def test_shadow_failure_is_swallowed_and_legacy_still_returned(self, monkeypatch, capsys):
        # 'shadow' is now opt-in only (never the default) — set explicitly.
        monkeypatch.setenv('OPENCLAW_ASSET_CORR_LW', 'shadow')
        fixed = _fixture_returns()
        monkeypatch.setattr(ac, '_load_returns', lambda tickers, window, as_of=None: fixed)
        # Force the shadow LW path to blow up without touching the legacy path.
        monkeypatch.setattr(sk, 'lw_corr', lambda panel: (_ for _ in ()).throw(RuntimeError('boom')))
        monkeypatch.setattr(ac, 'shrinkage', sk, raising=False)
        got = ac.price_return_corr(['ZZT1', 'ZZT2', 'ZZT3'], window=63)
        want = _legacy_pearson_reference(fixed)
        assert got == want
        err = capsys.readouterr().err
        assert any('[asset_corr_lw] shadow failed:' in l for l in err.splitlines())

    def test_lw_armed_shape_matches_legacy_and_thin_pairs_zeroed(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_ASSET_CORR_LW', '1')
        fixed = _fixture_returns()
        # ZZT1-ZZT3 stay well-covered (90 obs, >= shrinkage.MIN_ROWS=40, so
        # the LW fit itself has enough columns+rows to run); ZZT4 is thin
        # against everyone (< MIN_OBS=20 overlap) and must NOT poison the
        # 3-ticker LW fit (asset_correlation drops sub-MIN_OBS tickers before
        # intersecting dates — see _dense_panel_from_returns).
        fixture = dict(fixed)
        thin_dates = list(fixed['ZZT3'].keys())[:10]
        fixture['ZZT4'] = {d: fixed['ZZT3'][d] * -1.0 for d in thin_dates}
        monkeypatch.setattr(ac, '_load_returns', lambda tickers, window, as_of=None: fixture)
        got = ac.price_return_corr(['ZZT1', 'ZZT2', 'ZZT3', 'ZZT4'], window=63)
        all_t = {'ZZT1', 'ZZT2', 'ZZT3', 'ZZT4'}
        assert set(got.keys()) == all_t
        for t in got:
            assert set(got[t].keys()) == all_t
            assert got[t][t] == 1.0
        # thin ticker's pairs forced to 0.0 post-shrinkage (thin-evidence rule)
        for other in ('ZZT1', 'ZZT2', 'ZZT3'):
            assert got['ZZT4'][other] == 0.0
            assert got[other]['ZZT4'] == 0.0
        # well-covered pairs: symmetric, in-range.
        for a, b in (('ZZT1', 'ZZT2'), ('ZZT1', 'ZZT3'), ('ZZT2', 'ZZT3')):
            assert got[a][b] == got[b][a]
            assert -1.0 <= got[a][b] <= 1.0
        # Proves real LW shrinkage ran on the well-covered trio (not just the
        # legacy path relabeled): its pairwise value must differ from the
        # legacy pairwise Pearson value.
        legacy = _legacy_pearson_reference(fixture)
        assert got['ZZT1']['ZZT2'] != pytest.approx(legacy['ZZT1']['ZZT2'])

    def test_lw_armed_differs_from_default_reflecting_real_computation(self, monkeypatch):
        # Same fixture under '1' vs unset (now legacy-only '0' by default):
        # legacy-vs-LW should differ on a well-observed pair, proving '1'
        # actually swaps the returned values (not merely a shape/log
        # difference).
        fixed = _fixture_returns()
        monkeypatch.setattr(ac, '_load_returns', lambda tickers, window, as_of=None: fixed)
        monkeypatch.delenv('OPENCLAW_ASSET_CORR_LW', raising=False)
        legacy_default = ac.price_return_corr(['ZZT1', 'ZZT2', 'ZZT3'], window=63)
        monkeypatch.setenv('OPENCLAW_ASSET_CORR_LW', '1')
        lw_armed = ac.price_return_corr(['ZZT1', 'ZZT2', 'ZZT3'], window=63)
        assert lw_armed['ZZT1']['ZZT2'] != pytest.approx(legacy_default['ZZT1']['ZZT2'])


# ---------------------------------------------------------------------------
# strategy_similarity — trigger-suffix builder/parser + load_groups round trip
# ---------------------------------------------------------------------------

class TestLwGammaTriggerRoundTrip:
    """Pure-function coverage for the '+lw_gamma=<float>' suffix mechanism
    (task P1's chosen storage mechanism — see task-P1-report.md). No DB."""

    def test_round_trip_with_prior_plus_suffix_in_trigger(self):
        trigger = 'manual+src=backtest'
        built = ss._build_matrix_trigger(trigger, 0.123456)
        assert built == 'manual+src=backtest+lw_gamma=0.123456'
        assert ss._parse_lw_gamma_trigger(built) == pytest.approx(0.123456)

    def test_builder_returns_trigger_unchanged_when_gamma_is_none(self):
        trigger = 'manual+src=backtest'
        assert ss._build_matrix_trigger(trigger, None) == trigger

    def test_parse_missing_suffix_returns_none(self):
        assert ss._parse_lw_gamma_trigger('manual+src=backtest') is None
        assert ss._parse_lw_gamma_trigger('manual') is None
        assert ss._parse_lw_gamma_trigger(None) is None
        assert ss._parse_lw_gamma_trigger('') is None

    def test_parse_malformed_suffix_returns_none(self):
        # '+lw_gamma=' present but the value isn't numeric -> no regex match.
        assert ss._parse_lw_gamma_trigger('manual+lw_gamma=notanumber') is None
        assert ss._parse_lw_gamma_trigger('manual+lw_gamma=') is None


class _FakeCursor:
    """Minimal psycopg2-cursor stand-in: dispatches fetchall/fetchone results
    by matching a substring of the most recently executed SQL, so it can back
    load_groups()'s three sequential SELECTs without a real DB."""

    def __init__(self, matrix_row):
        self._matrix_row = matrix_row
        self._last_query = ''

    def execute(self, query, params=None):
        self._last_query = query

    def fetchall(self):
        # Neither fold-groups nor factor-blocks membership matters for this
        # test — only the matrix/trigger round trip does.
        return []

    def fetchone(self):
        assert 'strategy_similarity_matrix' in self._last_query
        return self._matrix_row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, matrix_row):
        self._cur = _FakeCursor(matrix_row)

    def cursor(self):
        return self._cur

    def close(self):
        pass


class TestLoadGroupsLwGammaRoundTrip:
    def test_load_groups_extracts_gamma_and_leaves_matrix_untouched(self, monkeypatch):
        fake_matrix = {'S1': {'S1': 1.0, 'S2': 0.42}, 'S2': {'S1': 0.42, 'S2': 1.0}}
        fake_row = (fake_matrix, 'manual+lw_gamma=0.123')
        monkeypatch.setattr(ss, '_db', lambda: _FakeConn(fake_row))

        out = ss.load_groups('LOW_VOL')

        # Identity, not just equality: the matrix object must be EXACTLY the
        # fake row's matrix, untouched by load_groups.
        assert out['matrix'] is fake_matrix
        assert out['lw_gamma'] == pytest.approx(0.123)


# ---------------------------------------------------------------------------
# Task P1b — orthogonalization.tangency_net_sharpe gamma plumb-through
# ---------------------------------------------------------------------------

# Live LOW_VOL trade-factored weights + similarity (same fixture family as
# tests/execution/test_tangency_sadj.py — kept local so this file has no
# cross-test-file dependency).
_TAN_W = {'mom': 1.430, 'vme': 1.271, 'ff': 0.781}
_TAN_SIM = {
    'mom': {'vme': 0.29086, 'ff': 0.33220},
    'vme': {'mom': 0.29086, 'ff': 0.58238},
    'ff':  {'mom': 0.33220, 'vme': 0.58238},
}
_TAN_CONTRIBS = {'T': [('mom', 1), ('vme', 1), ('ff', 1)]}


class TestTangencyNetSharpeGammaPlumbThrough:
    def test_gamma_none_is_byte_identical_to_pre_existing_default(self):
        """The binding requirement: omitting `gamma` (or passing None) must
        behave EXACTLY as before this parameter existed — i.e. exactly as
        gamma=0.10 (TANGENCY_SHRINK_DEFAULT)."""
        out_default, deg_default = og.tangency_net_sharpe(_TAN_CONTRIBS, _TAN_SIM, _TAN_W)
        out_gamma_none, deg_none = og.tangency_net_sharpe(
            _TAN_CONTRIBS, _TAN_SIM, _TAN_W, gamma=None)
        out_gamma_010, deg_010 = og.tangency_net_sharpe(
            _TAN_CONTRIBS, _TAN_SIM, _TAN_W, gamma=0.10)
        assert out_gamma_none['T'] == out_default['T']
        assert out_gamma_010['T'] == out_default['T']
        assert deg_none == deg_default == deg_010

    def test_gamma_030_changes_the_result(self):
        """Proves the plumb-through is actually live: a different gamma must
        move the tangency solve, not be silently ignored."""
        out_default, _ = og.tangency_net_sharpe(_TAN_CONTRIBS, _TAN_SIM, _TAN_W)
        out_030, _ = og.tangency_net_sharpe(_TAN_CONTRIBS, _TAN_SIM, _TAN_W, gamma=0.30)
        assert out_030['T'] != pytest.approx(out_default['T'])


# ---------------------------------------------------------------------------
# Task P1b — regime_blended_sizer._tangency_gamma_for_cycle (call-site helper)
# ---------------------------------------------------------------------------

class TestTangencyGammaForCycle:
    def test_artifact_present_lw_unarmed_returns_default_and_logs(self, monkeypatch, capsys):
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)

        gamma = rbs._tangency_gamma_for_cycle({'lw_gamma': 0.222})

        assert gamma == og.TANGENCY_SHRINK_DEFAULT
        err = capsys.readouterr().err
        assert '[tangency_lw] would_use_gamma=0.222' in err
        assert 'current=0.10' in err

    def test_lw_armed_returns_artifact_gamma_no_would_use_log(self, monkeypatch, capsys):
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.setenv('OPENCLAW_TANGENCY_LW', '1')

        gamma = rbs._tangency_gamma_for_cycle({'lw_gamma': 0.222})

        assert gamma == pytest.approx(0.222)
        err = capsys.readouterr().err
        assert 'would_use_gamma' not in err

    def test_no_key_returns_default_and_no_log(self, monkeypatch, capsys):
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)

        gamma = rbs._tangency_gamma_for_cycle({})

        assert gamma == og.TANGENCY_SHRINK_DEFAULT
        assert capsys.readouterr().err == ''

    def test_none_groups_returns_default_and_no_raise(self, monkeypatch, capsys):
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)

        gamma = rbs._tangency_gamma_for_cycle(None)

        assert gamma == og.TANGENCY_SHRINK_DEFAULT
        assert capsys.readouterr().err == ''

    def test_malformed_groups_shape_cannot_raise(self, monkeypatch):
        """A non-dict `groups` (a corrupted/unexpected artifact shape) must
        never raise — it degrades to the default, silently."""
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)

        gamma = rbs._tangency_gamma_for_cycle(['not', 'a', 'dict'])

        assert gamma == og.TANGENCY_SHRINK_DEFAULT

    def test_env_override_wins_even_with_artifact_gamma(self, monkeypatch, capsys):
        """Mirrors resolve_tangency_gamma precedence #1 through the call-site
        helper: a live OPENCLAW_TANGENCY_SHRINK override short-circuits before
        the would_use_gamma log (it would be misleading — arming LW would not
        change the resolved value in this case)."""
        monkeypatch.setenv('OPENCLAW_TANGENCY_SHRINK', '0.42')
        monkeypatch.setenv('OPENCLAW_TANGENCY_LW', '1')

        gamma = rbs._tangency_gamma_for_cycle({'lw_gamma': 0.222})

        assert gamma == pytest.approx(0.42)
        assert 'would_use_gamma' not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Task P1b — composed path: _corr_adjusted_maps + _tangency_gamma_for_cycle,
# in the state production will actually be in once lw_gamma starts getting
# stored (artifact gamma present, LW not yet armed). This is the byte-
# identical claim that matters live, not just each helper tested alone.
# ---------------------------------------------------------------------------

class TestCorrAdjustedMapsComposedGammaByteIdentical:
    def test_artifact_gamma_present_lw_unarmed_is_byte_identical_to_no_gamma(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)
        monkeypatch.delenv('OPENCLAW_TANGENCY_SADJ', raising=False)
        meta = {'T': {'strategies': ['mom', 'vme', 'ff'], 'directions': [1, 1, 1]}}

        gate_a, size_a, nb_g_a, nb_s_a = rbs._corr_adjusted_maps(meta, _TAN_W, _TAN_W, _TAN_SIM)
        _gamma = rbs._tangency_gamma_for_cycle({'lw_gamma': 0.44})
        gate_b, size_b, nb_g_b, nb_s_b = rbs._corr_adjusted_maps(
            meta, _TAN_W, _TAN_W, _TAN_SIM, gamma=_gamma)

        assert _gamma == og.TANGENCY_SHRINK_DEFAULT
        assert gate_b['T'] == gate_a['T']
        assert size_b['T'] == size_a['T']
        assert (nb_g_b, nb_s_b) == (nb_g_a, nb_s_a)

    def test_artifact_gamma_present_lw_armed_diverges_from_default(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.setenv('OPENCLAW_TANGENCY_LW', '1')
        monkeypatch.delenv('OPENCLAW_TANGENCY_SADJ', raising=False)
        meta = {'T': {'strategies': ['mom', 'vme', 'ff'], 'directions': [1, 1, 1]}}

        gate_a, size_a, _, _ = rbs._corr_adjusted_maps(meta, _TAN_W, _TAN_W, _TAN_SIM)
        _gamma = rbs._tangency_gamma_for_cycle({'lw_gamma': 0.44})
        gate_b, size_b, _, _ = rbs._corr_adjusted_maps(
            meta, _TAN_W, _TAN_W, _TAN_SIM, gamma=_gamma)

        assert _gamma == pytest.approx(0.44)
        assert gate_b['T'] != pytest.approx(gate_a['T'])


# ---------------------------------------------------------------------------
# Task P1b review fix 2 (2026-08-24) — gate the gamma resolve+log on the same
# tangency-branch condition, so the OPENCLAW_TANGENCY_SADJ=0 legacy
# killswitch path (where gamma is discarded) never emits a would_use_gamma /
# rejected-gamma log for a value nothing consumes.
# ---------------------------------------------------------------------------

class TestTangencySadjEnabled:
    def test_default_unset_is_enabled(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_TANGENCY_SADJ', raising=False)
        assert rbs._tangency_sadj_enabled() is True

    def test_explicit_one_is_enabled(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_TANGENCY_SADJ', '1')
        assert rbs._tangency_sadj_enabled() is True

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_TANGENCY_SADJ', '0')
        assert rbs._tangency_sadj_enabled() is False

    def test_garbage_value_is_enabled(self, monkeypatch):
        """Only the literal '0' killswitch disables — matches the pre-existing
        `!= '0'` semantics inside _corr_adjusted_maps, now shared verbatim."""
        monkeypatch.setenv('OPENCLAW_TANGENCY_SADJ', 'garbage')
        assert rbs._tangency_sadj_enabled() is True


class TestGammaResolveGatedOnTangencyBranch:
    """These reproduce the call-site line inside _sharpe_cadence_path
    (`_gamma = _tangency_gamma_for_cycle(...) if _tangency_sadj_enabled()
    else None`) directly — that line lives inside a DB-backed function too
    heavy to invoke here, so this pins the composed behavior of its two
    pure ingredients instead."""

    def test_legacy_killswitch_skips_resolve_and_stays_log_silent(self, monkeypatch, capsys):
        monkeypatch.setenv('OPENCLAW_TANGENCY_SADJ', '0')
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)

        groups = {'lw_gamma': 0.222}   # artifact gamma present, LW unarmed —
                                       # would normally trigger would_use_gamma
        gamma = rbs._tangency_gamma_for_cycle(groups) if rbs._tangency_sadj_enabled() else None

        assert gamma is None
        assert capsys.readouterr().err == ''

    def test_default_on_path_still_resolves_and_logs(self, monkeypatch, capsys):
        monkeypatch.delenv('OPENCLAW_TANGENCY_SADJ', raising=False)
        monkeypatch.delenv('OPENCLAW_TANGENCY_SHRINK', raising=False)
        monkeypatch.delenv('OPENCLAW_TANGENCY_LW', raising=False)

        groups = {'lw_gamma': 0.222}
        gamma = rbs._tangency_gamma_for_cycle(groups) if rbs._tangency_sadj_enabled() else None

        assert gamma == og.TANGENCY_SHRINK_DEFAULT
        err = capsys.readouterr().err
        assert '[tangency_lw] would_use_gamma=0.222' in err

    def test_corr_adjusted_maps_output_unaffected_by_gamma_none_under_killswitch(self, monkeypatch):
        """The point of gating is to suppress the LOG, not to change sizing:
        the legacy branch already ignores gamma entirely, so gamma=None vs
        gamma=0.10 must produce identical output under the killswitch."""
        monkeypatch.setenv('OPENCLAW_TANGENCY_SADJ', '0')
        meta = {'T': {'strategies': ['mom', 'vme', 'ff'], 'directions': [1, 1, 1]}}

        gate_none, size_none, _, _ = rbs._corr_adjusted_maps(
            meta, _TAN_W, _TAN_W, _TAN_SIM, gamma=None)
        gate_010, size_010, _, _ = rbs._corr_adjusted_maps(
            meta, _TAN_W, _TAN_W, _TAN_SIM, gamma=0.10)

        assert gate_none['T'] == gate_010['T']
        assert size_none['T'] == size_010['T']
