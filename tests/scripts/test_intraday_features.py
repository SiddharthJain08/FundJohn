"""tests/test_intraday_features.py

Tests for src/ingestion/intraday_features.py — single-tick collector +
append-only parquet writer.

Run:
    pytest tests/test_intraday_features.py -v
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]

# Bypass src/ingestion/__init__.py (broken upstream); load the module
# we're testing as a standalone file.
_spec = importlib.util.spec_from_file_location(
    'intraday_features', ROOT / 'src' / 'ingestion' / 'intraday_features.py',
)
intraday_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(intraday_mod)

collect_intraday_features = intraday_mod.collect_intraday_features
append_features_row       = intraday_mod.append_features_row
append_features_rows      = intraday_mod.append_features_rows
FEATURE_COLUMNS           = intraday_mod.FEATURE_COLUMNS
_parse_occ_symbol         = intraday_mod._parse_occ_symbol


# ── Synthetic-chain helper (re-uses synthetic_vix's BS pricer) ───────────────

_synth_spec = importlib.util.spec_from_file_location(
    'synthetic_vix', ROOT / 'src' / 'ingestion' / 'synthetic_vix.py',
)
_synth_mod = importlib.util.module_from_spec(_synth_spec)
_synth_spec.loader.exec_module(_synth_mod)
bs_price = _synth_mod.bs_price


def _flat_chain(spot, t_days, iv, lo, hi, step, t_now, with_delta=True,
                 with_volume_oi=False):
    """Synthetic chain — always with bid/ask, optionally with delta/IV
    populated AND optionally with OI/volume (Polygon-style enrichment)."""
    expiry = t_now + pd.Timedelta(days=t_days)
    rows = []
    t_yr = t_days / 365.0
    for k in np.arange(lo, hi + 1e-6, step):
        for opt in ('C', 'P'):
            theo = bs_price(spot, float(k), t_yr, 0.04, 0.0, iv, opt)
            row = {
                'expiration_date': expiry,
                'strike':          float(k),
                'option_type':     opt,
                'bid':             max(0.0, theo - 0.005),
                'ask':             theo + 0.005,
                'iv':              iv,
            }
            if with_delta:
                # Black-Scholes delta (constant-IV surface)
                from math import log, sqrt, erf
                if t_yr > 0 and iv > 0:
                    d1 = (log(spot / k) + (0.04 + 0.5 * iv * iv) * t_yr) / (iv * sqrt(t_yr))
                    if opt == 'C':
                        row['delta'] = 0.5 * (1 + erf(d1 / sqrt(2)))
                    else:
                        row['delta'] = 0.5 * (1 + erf(d1 / sqrt(2))) - 1
                else:
                    row['delta'] = float('nan')
            if with_volume_oi:
                # Synthetic OI/volume — constant per row so PCR ≈ 1.0
                row['open_interest'] = 100
                row['volume'] = 50
            rows.append(row)
    return pd.DataFrame(rows)


# ── Tests: OCC symbol parser ─────────────────────────────────────────────────

class TestOccParser:
    def test_basic_call(self):
        meta = _parse_occ_symbol('SPY260612C00580000')
        assert meta is not None
        assert meta['underlying'] == 'SPY'
        assert meta['option_type'] == 'C'
        assert meta['strike'] == 580.0
        assert meta['expiration_date'].strftime('%Y-%m-%d') == '2026-06-12'

    def test_basic_put(self):
        meta = _parse_occ_symbol('SPY260612P00500000')
        assert meta is not None
        assert meta['option_type'] == 'P'
        assert meta['strike'] == 500.0

    def test_malformed_returns_none(self):
        assert _parse_occ_symbol('') is None
        assert _parse_occ_symbol('XX') is None
        assert _parse_occ_symbol('SPY26X612C00580000') is None  # bad date
        assert _parse_occ_symbol('SPY260612X00580000') is None  # bad type

    def test_fractional_strike(self):
        # 580.5 strike → '00580500'
        meta = _parse_occ_symbol('SPY260612C00580500')
        assert meta is not None
        assert meta['strike'] == 580.5


# ── Tests: collect_intraday_features (mocked inputs) ─────────────────────────

class TestCollectFeatures:
    def test_full_input_returns_all_features(self):
        """With both chain + bars + spot supplied, all 9 features
        compute and source_quality_flag stays at 0 or 1 (1 only if
        OI/volume missing under the Alpaca-only path)."""
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        chain = pd.concat([
            _flat_chain(580.0, 23, 0.20, 400, 760, 2.5, t_now,
                        with_delta=True, with_volume_oi=True),
            _flat_chain(580.0, 37, 0.20, 400, 760, 2.5, t_now,
                        with_delta=True, with_volume_oi=True),
            _flat_chain(580.0, 80, 0.18, 400, 760, 2.5, t_now,
                        with_delta=True, with_volume_oi=True),
            _flat_chain(580.0, 100, 0.18, 400, 760, 2.5, t_now,
                        with_delta=True, with_volume_oi=True),
        ], ignore_index=True)
        # SPY 1-min bars: 30 minutes of returns
        bar_times = pd.date_range(t_now - pd.Timedelta(minutes=30), t_now,
                                   freq='1min', tz='UTC')
        bars = pd.DataFrame({
            'datetime': bar_times,
            'open':     580.0 + np.random.normal(0, 0.05, len(bar_times)),
            'high':     580.0 + np.random.normal(0.1, 0.05, len(bar_times)),
            'low':      580.0 + np.random.normal(-0.1, 0.05, len(bar_times)),
            'close':    580.0 + np.random.normal(0, 0.05, len(bar_times)),
            'volume':   np.random.randint(1000, 5000, len(bar_times)),
        })
        feat = collect_intraday_features(now_utc=t_now,
                                          spy_chain_df=chain,
                                          spy_bars=bars,
                                          spot=580.0)
        # All 10 keys present (9 features + ts_utc)
        for col in FEATURE_COLUMNS:
            assert col in feat
        # Synthetic VIX recovers ~20 (vol input) within 1.5 pts
        assert abs(feat['vix_synth_30d'] - 20.0) < 1.5
        # Term slope < 1 (front 20%, back 18%)
        assert feat['vix_term_slope'] < 1.0
        # PCR ≈ 1.0 (symmetric synthetic OI)
        assert abs(feat['pcr_oi'] - 1.0) < 1e-6
        # Realised vol > 0
        assert feat['spy_realized_vol_30m'] > 0
        # Quality 0 (full populated)
        assert feat['source_quality_flag'] == 0

    def test_alpaca_only_path_dead_features_do_not_bump_flag(self):
        """Without volume/OI columns (Alpaca CLI-only path — the ONLY live
        path since the SP-1 Polygon purge), pcr_* and zero_dte_volume_share
        are NaN but quality_flag stays 0: permanently-dead Polygon-era
        features must not pin the flag at 1 (they made it meaningless —
        every live row was flag=1 regardless of actual chain health)."""
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        chain = pd.concat([
            _flat_chain(580.0, 23, 0.20, 400, 760, 2.5, t_now,
                        with_delta=True, with_volume_oi=False),
            _flat_chain(580.0, 37, 0.20, 400, 760, 2.5, t_now,
                        with_delta=True, with_volume_oi=False),
        ], ignore_index=True)
        bar_times = pd.date_range(t_now - pd.Timedelta(minutes=30), t_now,
                                   freq='1min', tz='UTC')
        bars = pd.DataFrame({
            'datetime': bar_times,
            'close':    580.0 + np.random.normal(0, 0.05, len(bar_times)),
            'open':     580.0, 'high': 580.5, 'low': 579.5, 'volume': 1000,
        })
        feat = collect_intraday_features(now_utc=t_now,
                                          spy_chain_df=chain,
                                          spy_bars=bars,
                                          spot=580.0)
        assert math.isnan(feat['pcr_oi'])
        assert math.isnan(feat['pcr_volume'])
        assert math.isnan(feat['zero_dte_volume_share'])
        # delta+IV present → rr_25d computes; chain healthy → flag 0
        assert feat['source_quality_flag'] == 0

    def test_missing_rr_25d_bumps_flag_to_1(self):
        """rr_25d is the one chain-sourced LIVE model input — a chain with
        no usable delta must surface as flag=1 (partial)."""
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        chain = pd.concat([
            _flat_chain(580.0, 23, 0.20, 400, 760, 2.5, t_now,
                        with_delta=False, with_volume_oi=False),
            _flat_chain(580.0, 37, 0.20, 400, 760, 2.5, t_now,
                        with_delta=False, with_volume_oi=False),
        ], ignore_index=True)
        feat = collect_intraday_features(now_utc=t_now,
                                          spy_chain_df=chain,
                                          spy_bars=pd.DataFrame(),
                                          spot=580.0)
        assert math.isnan(feat['rr_25d'])
        assert feat['source_quality_flag'] == 1

    def test_empty_chain_marks_quality_2(self):
        """Empty chain → vix_synth NaN → quality_flag=2."""
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        feat = collect_intraday_features(now_utc=t_now,
                                          spy_chain_df=pd.DataFrame(),
                                          spy_bars=pd.DataFrame(),
                                          spot=580.0)
        assert math.isnan(feat['vix_synth_30d'])
        assert feat['source_quality_flag'] == 2


# ── Tests: parquet append-only discipline ────────────────────────────────────

class TestParquetWriter:
    def test_first_write_creates_file(self, tmp_path):
        path = tmp_path / 'intraday_features.parquet'
        row = {
            'ts_utc': pd.Timestamp('2026-05-08 14:00', tz='UTC'),
            'vix_synth_30d': 17.5, 'vix_synth_90d': 18.2,
            'vix_term_slope': 1.04, 'pcr_oi': 1.0, 'pcr_volume': 1.1,
            'rr_25d': 0.05, 'spy_realized_vol_30m': 12.0,
            'zero_dte_volume_share': 0.4, 'source_quality_flag': 0,
        }
        result = append_features_row(row, parquet_path=path)
        assert result.exists()
        df = pd.read_parquet(result)
        assert len(df) == 1
        assert df['vix_synth_30d'].iloc[0] == 17.5

    def test_append_dedup_keeps_latest(self, tmp_path):
        """Two writes at the same ts_utc → second wins."""
        path = tmp_path / 'intraday_features.parquet'
        ts = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        append_features_row({
            'ts_utc': ts, 'vix_synth_30d': 17.5,
            'vix_synth_90d': 18.0, 'vix_term_slope': 1.0,
            'pcr_oi': 1.0, 'pcr_volume': 1.0, 'rr_25d': 0.0,
            'spy_realized_vol_30m': 10.0, 'zero_dte_volume_share': 0.0,
            'source_quality_flag': 0,
        }, parquet_path=path)
        # Second write at same ts with different value
        append_features_row({
            'ts_utc': ts, 'vix_synth_30d': 99.9,  # marker
            'vix_synth_90d': 99.9, 'vix_term_slope': 1.0,
            'pcr_oi': 1.0, 'pcr_volume': 1.0, 'rr_25d': 0.0,
            'spy_realized_vol_30m': 10.0, 'zero_dte_volume_share': 0.0,
            'source_quality_flag': 0,
        }, parquet_path=path)
        df = pd.read_parquet(path)
        assert len(df) == 1
        assert df['vix_synth_30d'].iloc[0] == 99.9, 'dedup should keep last write'

    def test_append_distinct_timestamps_grows(self, tmp_path):
        """Sequential 5-min ticks with distinct ts_utc all preserved."""
        path = tmp_path / 'intraday_features.parquet'
        base = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        for i in range(10):
            append_features_row({
                'ts_utc': base + pd.Timedelta(minutes=5 * i),
                'vix_synth_30d': 17.0 + i * 0.1,
                'vix_synth_90d': 17.5 + i * 0.1,
                'vix_term_slope': 1.03,
                'pcr_oi': 1.0, 'pcr_volume': 1.0, 'rr_25d': 0.0,
                'spy_realized_vol_30m': 10.0, 'zero_dte_volume_share': 0.0,
                'source_quality_flag': 0,
            }, parquet_path=path)
        df = pd.read_parquet(path)
        assert len(df) == 10
        assert df['ts_utc'].is_monotonic_increasing

    def test_bulk_append_overlapping_dedups(self, tmp_path):
        """append_features_rows with 100 then 50 overlapping → 150 rows."""
        path = tmp_path / 'intraday_features.parquet'
        base = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        rows1 = [{
            'ts_utc': base + pd.Timedelta(minutes=5 * i),
            'vix_synth_30d': 15.0,
            'vix_synth_90d': 16.0, 'vix_term_slope': 1.07,
            'pcr_oi': 1.0, 'pcr_volume': 1.0, 'rr_25d': 0.0,
            'spy_realized_vol_30m': 10.0, 'zero_dte_volume_share': 0.0,
            'source_quality_flag': 0,
        } for i in range(100)]
        append_features_rows(rows1, parquet_path=path)

        # 50 new rows that overlap with last 50 of rows1
        rows2 = [{
            'ts_utc': base + pd.Timedelta(minutes=5 * i),
            'vix_synth_30d': 99.9,   # marker
            'vix_synth_90d': 99.9, 'vix_term_slope': 1.0,
            'pcr_oi': 1.0, 'pcr_volume': 1.0, 'rr_25d': 0.0,
            'spy_realized_vol_30m': 10.0, 'zero_dte_volume_share': 0.0,
            'source_quality_flag': 0,
        } for i in range(50, 150)]
        append_features_rows(rows2, parquet_path=path)

        df = pd.read_parquet(path)
        assert len(df) == 150
        # Last 100 (i=50-149) should carry the 99.9 marker (rows2 wrote later, dedup keep=last)
        marker_count = (df['vix_synth_30d'] == 99.9).sum()
        assert marker_count == 100
