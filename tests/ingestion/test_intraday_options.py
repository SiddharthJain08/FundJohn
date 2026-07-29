"""Intraday options adapter (tier-1 15:00 ET acting-set ingest, 2026-07-29).

Mock-only: the alpaca CLI is stubbed. Pins the provider facts the adapter was
built around (0-DTE has no greeks/IV; the feed carries no open interest) and
the overlay's write/read contract.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from ingestion import intraday_options as io_mod  # noqa: E402

AS_OF = pd.Timestamp('2026-07-29')


def _snap(iv, delta, gamma=0.01, theta=-0.05, vega=0.7, vol=100, px=1.25):
    return {'greeks': {'delta': delta, 'gamma': gamma, 'theta': theta, 'vega': vega},
            'impliedVolatility': iv,
            'dailyBar': {'v': vol, 'c': px},
            'latestTrade': {'p': px}}


def _payload(snapshots, token=None):
    return {'snapshots': snapshots, 'next_page_token': token}


class TestDecodeOcc:
    def test_call_and_put(self):
        c = io_mod._decode_occ('SPY260821C00735000')
        assert c['option_type'] == 'CALL' and c['strike'] == 735.0
        assert c['expiry'] == pd.Timestamp('2026-08-21')
        p = io_mod._decode_occ('AAPL260918P00220500')
        assert p['option_type'] == 'PUT' and p['strike'] == 220.5

    def test_garbage_returns_none(self):
        assert io_mod._decode_occ('NOT-AN-OCC') is None
        assert io_mod._decode_occ('') is None


class TestFetchChainRows:
    def test_excludes_zero_dte_via_date_filter(self, monkeypatch):
        seen = {}

        def fake_cli(args):
            seen['args'] = args
            return _payload({'SPY260821C00735000': _snap(0.166, 0.59)})

        monkeypatch.setattr(io_mod, '_run_cli', fake_cli)
        rows = io_mod.fetch_chain_rows('SPY', AS_OF)
        # gte is the day AFTER as_of: 0-DTE carries no greeks/IV from this feed.
        assert '--expiration-date-gte' in seen['args']
        gte = seen['args'][seen['args'].index('--expiration-date-gte') + 1]
        assert gte == '2026-07-30'
        assert len(rows) == 1
        r = rows[0]
        assert r['ticker'] == 'SPY' and r['option_type'] == 'CALL'
        assert r['implied_volatility'] == 0.166
        assert r['open_interest'] is None      # feed has none — must stay unknown
        assert r['close'] == 1.25

    def test_paginates_until_token_exhausted(self, monkeypatch):
        pages = [
            _payload({'SPY260821C00735000': _snap(0.16, 0.59)}, token='t2'),
            _payload({'SPY260821P00735000': _snap(0.18, -0.41)}, token=None),
        ]
        calls = {'n': 0}

        def fake_cli(args):
            i = calls['n']
            calls['n'] += 1
            return pages[i] if i < len(pages) else _payload({})

        monkeypatch.setattr(io_mod, '_run_cli', fake_cli)
        rows = io_mod.fetch_chain_rows('SPY', AS_OF)
        assert calls['n'] == 2 and len(rows) == 2

    def test_cli_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(io_mod, '_run_cli', lambda args: None)
        assert io_mod.fetch_chain_rows('SPY', AS_OF) == []


class TestBuildOverlay:
    def _chain(self):
        # Two expiries so front/back both resolve; both delta bands populated.
        return {
            'SPY260821C00735000': _snap(0.16, 0.55),
            'SPY260821P00735000': _snap(0.19, -0.55),
            'SPY260821P00700000': _snap(0.22, -0.20),   # 20-delta put leg
            'SPY260821C00780000': _snap(0.14, 0.20),    # 20-delta call leg
            'SPY261016C00735000': _snap(0.18, 0.52),    # back expiry
        }

    def test_aggregates_with_shared_definition(self, monkeypatch):
        monkeypatch.setattr(io_mod, '_run_cli',
                            lambda args: _payload(self._chain()))
        df = io_mod.build_overlay(['SPY'], AS_OF)
        assert len(df) == 1
        row = df.iloc[0]
        assert row['ticker'] == 'SPY'
        assert pd.Timestamp(row['date']) == AS_OF
        assert row['iv_front'] > 0
        # 20-delta legs present → skew computable
        assert pd.notna(row['skew'])
        # OI absent from the feed ⇒ OI-weighted fields must be UNKNOWN, not 0.
        for col in ('gex', 'contracts_liquid', 'iv_centroid_delta', 'surface_premium'):
            assert pd.isna(row[col]), f'{col} must be None when the feed has no OI'

    def test_all_zero_greek_rows_dropped(self, monkeypatch):
        # 0-DTE-style rows: greeks all zero, no IV.
        zero = {'greeks': {'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0},
                'impliedVolatility': None, 'dailyBar': {'v': 5, 'c': 0},
                'latestTrade': {'p': 0}}
        monkeypatch.setattr(io_mod, '_run_cli',
                            lambda args: _payload({'SPY260821C00735000': zero}))
        with pytest.raises(io_mod.IntradayOptionsError):
            io_mod.build_overlay(['SPY'], AS_OF)

    def test_empty_fetch_raises_not_silently_empty(self, monkeypatch):
        monkeypatch.setattr(io_mod, '_run_cli', lambda args: _payload({}))
        with pytest.raises(io_mod.IntradayOptionsError):
            io_mod.build_overlay(['AAPL'], AS_OF)


class TestOverlayIO:
    def test_write_then_load_roundtrip(self, monkeypatch, tmp_path):
        monkeypatch.setattr(io_mod, 'OVERLAY_ROOT', tmp_path)
        df = pd.DataFrame([{'ticker': 'SPY', 'iv_front': 0.17, 'date': AS_OF}])
        path = io_mod.write_overlay(df, AS_OF)
        assert path.exists() and path.parent.name == '2026-07-29'
        back = io_mod.load_overlay(AS_OF)
        assert back is not None and back.iloc[0]['ticker'] == 'SPY'
        # No .tmp left behind (atomic write).
        assert not list(tmp_path.rglob('*.tmp'))

    def test_load_missing_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(io_mod, 'OVERLAY_ROOT', tmp_path)
        assert io_mod.load_overlay(AS_OF) is None

    def test_write_never_touches_master(self, monkeypatch, tmp_path):
        monkeypatch.setattr(io_mod, 'OVERLAY_ROOT', tmp_path)
        df = pd.DataFrame([{'ticker': 'X', 'date': AS_OF}])
        p = io_mod.write_overlay(df, AS_OF)
        assert 'data/master' not in str(p)
