"""Tests for task P1 — Ledoit-Wolf shrinkage (shadow-first).

Covers:
  * shrinkage.lw_corr / lw_gamma correctness + degenerate inputs (pure, no DB).
  * orthogonalization.resolve_tangency_gamma precedence (pure).
  * asset_correlation.price_return_corr byte-identical default when
    OPENCLAW_ASSET_CORR_LW is unset (the task's binding requirement).

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
