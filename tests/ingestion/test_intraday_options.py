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

    def test_cli_failure_raises_not_empty(self, monkeypatch):
        """"provider refused" and "no listed options" must not both look like
        an empty chain — an outage would read as a clean, empty ingest."""
        monkeypatch.setattr(io_mod, '_run_cli', lambda args: None)
        with pytest.raises(io_mod.IntradayOptionsError):
            io_mod.fetch_chain_rows('SPY', AS_OF)

    def test_empty_payload_is_empty_not_error(self, monkeypatch):
        monkeypatch.setattr(io_mod, '_run_cli', lambda args: {'snapshots': {}})
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


class TestFetchRawFrame:
    """The concurrent driver-facing fetch: budget, symbol classes, accounting."""

    def _ok(self, *_a, **_k):
        return [{'ticker': 'X', 'date': AS_OF, 'expiry': AS_OF, 'strike': 1.0,
                 'option_type': 'CALL', 'open_interest': None,
                 'implied_volatility': 0.2, 'delta': 0.5, 'gamma': 0.01,
                 'theta': -0.01, 'vega': 0.1, 'volume': 10, 'close': 1.0}]

    def test_non_optionable_symbol_classes_are_skipped_not_failed(self, monkeypatch):
        """Indices/preferreds/warrants have no underlying — counting their
        guaranteed 400 as a FAILURE would corrupt the coverage signal the
        three-tier design reports on."""
        seen = []

        def fake(sym, as_of, row_ticker=None):
            seen.append(sym)
            return self._ok()

        monkeypatch.setattr(io_mod, 'fetch_chain_rows', fake)
        df, st = io_mod.fetch_raw_frame(
            ['AAPL', '^IXIC', 'T-PRC', 'ACHR.WS'], AS_OF, workers=2)
        assert st['skipped_class'] == 3
        assert st['failed'] == 0 and st['ok'] == 1
        assert seen == ['AAPL']
        assert st['requested'] == 4

    def test_rows_carry_the_engine_ticker_not_the_provider_symbol(self, monkeypatch):
        captured = {}

        def fake(sym, as_of, row_ticker=None):
            captured['sym'] = sym
            captured['row_ticker'] = row_ticker
            return self._ok()

        monkeypatch.setattr(io_mod, 'fetch_chain_rows', fake)
        io_mod.fetch_raw_frame(['BRK-B'], AS_OF, workers=1)
        assert captured == {'sym': 'BRK.B', 'row_ticker': 'BRK-B'}

    def test_failure_is_counted_and_does_not_abort_the_batch(self, monkeypatch):
        def fake(sym, as_of, row_ticker=None):
            if sym == 'BAD':
                raise io_mod.IntradayOptionsError('boom')
            return self._ok()

        monkeypatch.setattr(io_mod, 'fetch_chain_rows', fake)
        df, st = io_mod.fetch_raw_frame(['AAPL', 'BAD', 'MSFT'], AS_OF, workers=2)
        assert st['failed'] == 1 and st['ok'] == 2
        assert len(df) == 2
        assert any('BAD' in s for s in st['failed_sample'])

    def test_budget_stops_submitting_and_reports_the_shortfall(self, monkeypatch):
        """A partial overlay degrades per-ticker to the EOD panel; overrunning
        would delay the 15:00 compute and risk the 15:55 no-handoff abort."""
        import time as _t

        def slow(sym, as_of, row_ticker=None):
            _t.sleep(0.25)
            return self._ok()

        monkeypatch.setattr(io_mod, 'fetch_chain_rows', slow)
        tickers = [f'T{i}' for i in range(40)]
        df, st = io_mod.fetch_raw_frame(tickers, AS_OF, budget_s=0.3, workers=2)
        assert st['skipped_budget'] > 0
        assert st['attempted'] + st['skipped_budget'] == len(tickers)
        assert st['elapsed_s'] < 10   # returned early, did not run all 40

    def test_empty_result_is_a_typed_frame_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(io_mod, 'fetch_chain_rows',
                            lambda *a, **k: [])
        df, st = io_mod.fetch_raw_frame(['AAPL'], AS_OF, workers=1)
        assert list(df.columns) == io_mod.RAW_COLS
        assert st['empty'] == 1 and st['rows'] == 0
