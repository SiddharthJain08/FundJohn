"""Tier-1 options overlay injection into the LIVE options panel (2026-07-30).

The seam under test is engine._inject_intraday_options. Every options field
the engine derives keys off ``chain['date'].max()``, so splicing today-dated
RAW contract rows into the EOD panel makes the whole surface same-day without
touching the field math. These pin the four properties that make that safe:
precedence, a shared expiry band, an age guard, and fail-open.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import engine  # noqa: E402
from ingestion import intraday_options as io_mod  # noqa: E402

TODAY = pd.Timestamp('2026-07-30').normalize()
YDAY = pd.Timestamp('2026-07-29').normalize()


def _row(ticker, date, dte, iv=0.20, delta=0.5):
    return {'ticker': ticker, 'date': date, 'expiry': date.normalize() + pd.Timedelta(days=dte),
            'strike': 100.0, 'option_type': 'CALL', 'implied_volatility': iv,
            'delta': delta, 'gamma': 0.01, 'theta': -0.01, 'vega': 0.1,
            'open_interest': None, 'volume': 10, 'open': 1.0, 'close': 1.0}


def _panel(rows):
    df = pd.DataFrame(rows, columns=engine._OPTIONS_SIGNAL_COLS)
    df['date'] = pd.to_datetime(df['date'])
    df['expiry'] = pd.to_datetime(df['expiry'])
    return df


@pytest.fixture
def overlay_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(io_mod, 'OVERLAY_ROOT', tmp_path)
    monkeypatch.setattr(engine, '_INTRADAY_AUX', True)
    monkeypatch.setattr(engine, '_INTRADAY_AUX_MAX_AGE_H', 6.0)
    return tmp_path


def _write(rows, as_of=TODAY):
    io_mod.write_overlay(_panel(rows), as_of, category='options_raw')


class TestInjection:
    def test_overlay_becomes_the_latest_observation_date(self, overlay_dir):
        panel = _panel([_row('AAPL', YDAY, 21, iv=0.30)])
        _write([_row('AAPL', TODAY, 20, iv=0.40)])
        out = engine._inject_intraday_options(panel, TODAY, ['AAPL'])
        assert out['date'].max() == TODAY
        # The engine's per-field `chain['date'].max()` now selects today's IV.
        latest = out[out['date'] == out['date'].max()]
        assert latest['implied_volatility'].iloc[0] == 0.40

    def test_disabled_by_default_leaves_the_panel_untouched(self, overlay_dir, monkeypatch):
        monkeypatch.setattr(engine, '_INTRADAY_AUX', False)
        panel = _panel([_row('AAPL', YDAY, 21)])
        _write([_row('AAPL', TODAY, 20)])
        out = engine._inject_intraday_options(panel, TODAY, ['AAPL'])
        assert out['date'].max() == YDAY

    def test_absent_overlay_falls_back_to_the_eod_panel(self, overlay_dir):
        panel = _panel([_row('AAPL', YDAY, 21)])
        out = engine._inject_intraday_options(panel, TODAY, ['AAPL'])
        assert len(out) == 1 and out['date'].max() == YDAY

    def test_tickers_outside_the_universe_are_dropped(self, overlay_dir):
        panel = _panel([_row('AAPL', YDAY, 21)])
        _write([_row('AAPL', TODAY, 20), _row('ZZZZ', TODAY, 20)])
        out = engine._inject_intraday_options(panel, TODAY, ['AAPL'])
        assert set(out['ticker']) == {'AAPL'}


class TestPrecedence:
    def test_real_eod_rows_for_today_beat_the_snapshot(self, overlay_dir):
        """load_aux_data also runs from redeploy_pipeline on a regime change,
        which can fire AFTER the 16:15 collect has appended real closes. The
        official record must win wherever both exist."""
        panel = _panel([_row('AAPL', TODAY, 21, iv=0.31)])
        _write([_row('AAPL', TODAY, 21, iv=0.99)])
        out = engine._inject_intraday_options(panel, TODAY, ['AAPL'])
        assert len(out) == 1
        assert out['implied_volatility'].iloc[0] == 0.31

    def test_precedence_is_per_ticker_not_all_or_nothing(self, overlay_dir):
        panel = _panel([_row('AAPL', TODAY, 21, iv=0.31),
                        _row('MSFT', YDAY, 21, iv=0.22)])
        _write([_row('AAPL', TODAY, 21, iv=0.99),
                _row('MSFT', TODAY, 21, iv=0.44)])
        out = engine._inject_intraday_options(panel, TODAY, ['AAPL', 'MSFT'])
        today_rows = out[out['date'] == TODAY].set_index('ticker')
        assert today_rows.loc['AAPL', 'implied_volatility'] == 0.31   # panel kept
        assert today_rows.loc['MSFT', 'implied_volatility'] == 0.44   # overlay used


class TestSharedExpiryBand:
    def test_panel_is_cut_to_the_overlay_band(self, overlay_dir):
        """Measured 2026-07-29: NOT equalizing the DTE band shifts per-ticker
        mean IV by a median +4.5 vol points purely from contract population —
        a population artifact that would read as a real one-day move."""
        panel = _panel([_row('AAPL', YDAY, 21), _row('AAPL', YDAY, 400)])
        _write([_row('AAPL', TODAY, 20)])
        out = engine._inject_intraday_options(panel, TODAY, ['AAPL'])
        dte = (out['expiry'] - TODAY).dt.days
        assert dte.max() <= io_mod.MAX_DTE
        assert (dte > 0).all()

    def test_same_day_expiry_cannot_win_nearest_expiry(self, overlay_dir):
        """On an expiry day the panel's yesterday-dated rows for a TODAY
        expiry would take nearest_expiry.min(), and the overlay has no rows
        for that expiry — reverting exactly those tickers to yesterday."""
        panel = _panel([_row('AAPL', YDAY, 1),     # expiry == TODAY
                        _row('AAPL', YDAY, 22)])
        _write([_row('AAPL', TODAY, 21)])
        out = engine._inject_intraday_options(panel, TODAY, ['AAPL'])
        future = out[out['expiry'] >= TODAY]
        chain = future[future['expiry'] == future['expiry'].min()]
        assert chain['date'].max() == TODAY


class TestFailOpen:
    def test_stale_overlay_is_ignored(self, overlay_dir, monkeypatch):
        """A file left by an earlier run is date-stamped today while holding a
        stale surface — the exact staleness the pivot removes."""
        _write([_row('AAPL', TODAY, 20, iv=0.99)])
        path = io_mod.overlay_path(TODAY, 'options_raw')
        old = path.stat().st_mtime - 7 * 3600
        os.utime(path, (old, old))
        panel = _panel([_row('AAPL', YDAY, 21, iv=0.30)])
        out = engine._inject_intraday_options(panel, TODAY, ['AAPL'])
        assert out['date'].max() == YDAY

    def test_unreadable_overlay_keeps_the_panel(self, overlay_dir):
        path = io_mod.overlay_path(TODAY, 'options_raw')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not a parquet')
        panel = _panel([_row('AAPL', YDAY, 21)])
        out = engine._inject_intraday_options(panel, TODAY, ['AAPL'])
        assert len(out) == 1 and out['date'].max() == YDAY

    def test_schema_gap_widens_with_nan_instead_of_raising(self, overlay_dir):
        """The chain feed has no `open` column; a select would raise and cost
        the whole overlay."""
        rows = _panel([_row('AAPL', TODAY, 20)]).drop(columns=['open'])
        io_mod.write_overlay(rows, TODAY, category='options_raw')
        panel = _panel([_row('AAPL', YDAY, 21)])
        out = engine._inject_intraday_options(panel, TODAY, ['AAPL'])
        assert out['date'].max() == TODAY
        assert pd.isna(out.loc[out['date'] == TODAY, 'open']).all()
