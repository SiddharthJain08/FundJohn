"""tests/test_intraday_carry_forward.py

Tests for the after-hours carry-forward path in
scripts/run_intraday_market_state.py.

Background (2026-06-04 diagnosis): the 5-min tick window is 9:00-19:55 ET
but SPY options trade 9:30-16:15 ET. Outside that window the chain quotes
are frozen at the prior close, so the synthetic-VIX features become a
deterministic time-decay ramp (+0.0011/tick, sigma=4e-6) rather than market
signal. The HMM was never trained on such rows (the trainer filters to RTH)
and scored them with saturated/garbage confidence, producing artifact state
flips (06-02: all 6 flips after-hours; 06-03: 12 of 40) and "mixed" buckets
on the dashboard regime monitor.

Fix: outside option-market hours the tick still collects + appends features
to the master parquet (flagged source_quality_flag=3 = carry-forward), but
skips HMM scoring and ALL transition logic, persisting the carried last
state instead so hysteresis/duration stay continuous.

Run:
    pytest tests/test_intraday_carry_forward.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    'run_intraday_market_state',
    ROOT / 'scripts' / 'run_intraday_market_state.py',
)
detector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(detector)


# ── Test isolation (2026-07-12) ──────────────────────────────────────────────
# A live-tree pytest run of this file CLOBBERED the real regime-of-record
# (.agents/market-state/regime_latest.json) with harness fixture state: paths
# through run_one_tick/_carry_forward_tick call _refresh_regime_file, which
# writes detector.MODEL_DIR — the LIVE directory unless redirected. Redirect
# it for EVERY test; tests that patch MODEL_DIR themselves simply override.

@pytest.fixture(autouse=True)
def _isolate_model_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(detector, 'MODEL_DIR', tmp_path / 'model-dir-isolated')


# June 2026 is EDT (UTC-4): 9:30 ET = 13:30 UTC, 16:15 ET = 20:15 UTC.
def _ts(utc_str: str) -> pd.Timestamp:
    return pd.Timestamp(utc_str, tz='UTC')


# ── _is_option_market_open ───────────────────────────────────────────────────

class TestIsOptionMarketOpen:
    def test_rth_midday_open(self):
        assert detector._is_option_market_open(_ts('2026-06-03 15:00')) is True

    def test_open_boundary_inclusive(self):
        # 9:30 ET exactly
        assert detector._is_option_market_open(_ts('2026-06-03 13:30')) is True

    def test_close_boundary_inclusive(self):
        # 16:15 ET exactly — options close, last live-quote tick
        assert detector._is_option_market_open(_ts('2026-06-03 20:15')) is True

    def test_premarket_closed(self):
        # 9:00 and 9:25 ET — equity premarket, options not yet trading
        assert detector._is_option_market_open(_ts('2026-06-03 13:00')) is False
        assert detector._is_option_market_open(_ts('2026-06-03 13:25')) is False

    def test_after_hours_closed(self):
        # 16:20 ET (first frozen-quote tick) and 19:55 ET (window end)
        assert detector._is_option_market_open(_ts('2026-06-03 20:20')) is False
        assert detector._is_option_market_open(_ts('2026-06-03 23:55')) is False

    def test_weekend_closed(self):
        # Saturday 2026-06-06 midday ET
        assert detector._is_option_market_open(_ts('2026-06-06 16:00')) is False

    def test_naive_timestamp_treated_utc(self):
        assert detector._is_option_market_open(pd.Timestamp('2026-06-03 15:00')) is True


# ── _carry_forward_tick ──────────────────────────────────────────────────────

class _FakeConn:
    def close(self):
        pass


def _features(ts: pd.Timestamp, flag: int = 3) -> dict:
    return {
        'ts_utc': ts,
        'vix_synth_30d': 16.33, 'vix_synth_90d': 19.21, 'vix_term_slope': 1.176,
        'pcr_oi': float('nan'), 'pcr_volume': float('nan'), 'rr_25d': 0.0126,
        'spy_realized_vol_30m': float('nan'), 'zero_dte_volume_share': float('nan'),
        'source_quality_flag': flag,
    }


class TestCarryForwardTick:
    def test_carries_last_state_and_extends_streak(self, monkeypatch):
        history = [
            {'state': 'LOW_VOL', 'confidence': 0.97, 'hysteresis_streak': 5,
             'fired_liquidation': False, 'transition_tag': None},
            {'state': 'LOW_VOL', 'confidence': 0.96, 'hysteresis_streak': 4,
             'fired_liquidation': False, 'transition_tag': None},
        ]
        persisted = {}

        def _capture(conn, ts_utc, state, prior_state, confidence,
                     hysteresis_streak, fired_liquidation, transition_tag,
                     features_dict):
            persisted.update(
                state=state, prior=prior_state, conf=confidence,
                streak=hysteresis_streak, fired=fired_liquidation,
                tag=transition_tag, flag=features_dict.get('source_quality_flag'),
            )

        monkeypatch.setattr(detector, '_last_n_states', lambda conn, n: history)
        monkeypatch.setattr(detector, '_persist_state_row', _capture)

        ts = _ts('2026-06-03 21:00')
        result = detector._carry_forward_tick(_FakeConn(), _features(ts))

        assert persisted['state'] == 'LOW_VOL'
        assert persisted['prior'] == 'LOW_VOL'
        assert persisted['conf'] == pytest.approx(0.97)
        assert persisted['streak'] == 3        # 2 matching history rows + this tick
        assert persisted['fired'] is False
        assert persisted['tag'] is None
        assert persisted['flag'] == 3
        assert result['carry_fwd'] is True
        assert result['state'] == 'LOW_VOL'
        assert result['fired'] is False

    def test_carries_settled_regime_not_unconfirmed_boundary_flip(self, monkeypatch):
        """A 1-tick artifact flip on the LAST scored tick (16:15) must NOT
        own the night: carry the SETTLED regime (fired row or streak>=3),
        not the raw last state. Otherwise the artifact's streak grows past
        the settled threshold overnight and triggers a spurious 'transition
        back' + redeploy on the first real ticks next morning."""
        history = [
            {'state': 'TRANSITIONING', 'confidence': 0.72, 'hysteresis_streak': 1,
             'fired_liquidation': False, 'transition_tag': None},   # 16:15 artifact
            {'state': 'LOW_VOL', 'confidence': 0.97, 'hysteresis_streak': 9,
             'fired_liquidation': False, 'transition_tag': None},
            {'state': 'LOW_VOL', 'confidence': 0.96, 'hysteresis_streak': 8,
             'fired_liquidation': False, 'transition_tag': None},
        ]
        persisted = {}

        def _capture(conn, ts_utc, state, prior_state, confidence,
                     hysteresis_streak, *rest):
            persisted.update(state=state, prior=prior_state, conf=confidence,
                             streak=hysteresis_streak)

        monkeypatch.setattr(detector, '_last_n_states', lambda conn, n: history)
        monkeypatch.setattr(detector, '_persist_state_row', _capture)

        result = detector._carry_forward_tick(
            _FakeConn(), _features(_ts('2026-06-03 20:20')))

        assert persisted['state'] == 'LOW_VOL'          # settled, not artifact
        assert persisted['conf'] == pytest.approx(0.97)  # settled regime's last conf
        assert persisted['streak'] == 1                  # restarts vs the artifact row
        assert result['state'] == 'LOW_VOL'

    def test_carries_fired_transition_state(self, monkeypatch):
        """A CONFIRMED transition (fired row) at the close IS the settled
        regime — carry it, even at streak 1."""
        history = [
            {'state': 'HIGH_VOL', 'confidence': 0.91, 'hysteresis_streak': 2,
             'fired_liquidation': True,
             'transition_tag': 'INTRADAY_HMM_REDEPLOY_TRANSITIONING_HIGH_VOL'},
            {'state': 'TRANSITIONING', 'confidence': 0.95, 'hysteresis_streak': 6,
             'fired_liquidation': False, 'transition_tag': None},
        ]
        persisted = {}
        monkeypatch.setattr(detector, '_last_n_states', lambda conn, n: history)
        monkeypatch.setattr(
            detector, '_persist_state_row',
            lambda conn, ts, state, prior, conf, streak, *rest:
                persisted.update(state=state, streak=streak))

        result = detector._carry_forward_tick(
            _FakeConn(), _features(_ts('2026-06-03 20:20')))
        assert persisted['state'] == 'HIGH_VOL'
        assert persisted['streak'] == 2   # continues the fired run's streak
        assert result['state'] == 'HIGH_VOL'

    def test_cold_start_persists_unknown(self, monkeypatch):
        persisted = {}

        def _capture(conn, ts_utc, state, prior_state, confidence, *rest):
            persisted.update(state=state, prior=prior_state, conf=confidence)

        monkeypatch.setattr(detector, '_last_n_states', lambda conn, n: [])
        monkeypatch.setattr(detector, '_persist_state_row', _capture)

        result = detector._carry_forward_tick(
            _FakeConn(), _features(_ts('2026-06-03 21:00')))
        assert persisted['state'] == 'UNKNOWN'
        assert persisted['conf'] == 0.0
        assert result['carry_fwd'] is True
        assert result['state'] == 'UNKNOWN'


# ── run_one_tick integration: routing ────────────────────────────────────────

class _StubIntradayModule:
    """Stands in for src/ingestion/intraday_features.py."""

    def __init__(self, ts: pd.Timestamp, flag: int = 1):
        self.ts = ts
        self.flag = flag
        self.appended = None

    def collect_intraday_features(self, now_utc=None):
        return _features(self.ts, flag=self.flag)

    def append_features_row(self, row):
        self.appended = dict(row)


class TestRunOneTickRouting:
    def _wire(self, monkeypatch, stub, history):
        monkeypatch.setattr(detector, '_load_intraday_features_module',
                            lambda: stub)
        monkeypatch.setattr(detector, '_connect_postgres', lambda: _FakeConn())
        monkeypatch.setattr(detector, '_last_n_states', lambda conn, n: history)
        monkeypatch.setattr(detector, '_enrich_with_daily_derived', lambda f: f)
        # Any attempt to score / sync / spawn during carry-fwd is a failure.
        monkeypatch.setattr(
            detector, '_state_from_hmm',
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError('HMM scored on a carry-forward tick')))
        monkeypatch.setattr(
            detector, '_sync_regime_to_consumers',
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError('regime sync ran on a carry-forward tick')))
        monkeypatch.setattr(
            detector, '_spawn_redeploy',
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError('redeploy spawned on a carry-forward tick')))

    def test_after_hours_tick_routes_to_carry_forward(self, monkeypatch):
        stub = _StubIntradayModule(_ts('2026-06-03 21:00'), flag=1)  # 17:00 ET
        history = [{'state': 'LOW_VOL', 'confidence': 0.97,
                    'hysteresis_streak': 5, 'fired_liquidation': False,
                    'transition_tag': None}]
        self._wire(monkeypatch, stub, history)
        persisted = {}
        monkeypatch.setattr(
            detector, '_persist_state_row',
            lambda conn, ts, state, prior, conf, streak, fired, tag, feats:
                persisted.update(state=state, flag=feats.get('source_quality_flag')))

        result = detector.run_one_tick()

        assert result['carry_fwd'] is True
        assert result['state'] == 'LOW_VOL'
        # Parquet row carries the carry-forward flag (3), overriding collector's 1
        assert stub.appended is not None
        assert stub.appended['source_quality_flag'] == 3
        assert persisted['state'] == 'LOW_VOL'
        assert persisted['flag'] == 3

    def test_premarket_tick_routes_to_carry_forward(self, monkeypatch):
        stub = _StubIntradayModule(_ts('2026-06-03 13:05'), flag=1)  # 9:05 ET
        history = [{'state': 'TRANSITIONING', 'confidence': 0.88,
                    'hysteresis_streak': 7, 'fired_liquidation': False,
                    'transition_tag': None}]
        self._wire(monkeypatch, stub, history)
        monkeypatch.setattr(detector, '_persist_state_row',
                            lambda *a, **k: None)
        result = detector.run_one_tick()
        assert result['carry_fwd'] is True
        assert result['state'] == 'TRANSITIONING'

    def test_rth_tick_uses_scored_path(self, monkeypatch, tmp_path):
        """Inside 9:30-16:15 ET the existing scored path runs unchanged
        (here in bootstrap mode — no model file — so state=UNKNOWN), and
        the collector's own quality flag is NOT overridden to 3."""
        stub = _StubIntradayModule(_ts('2026-06-03 15:00'), flag=1)  # 11:00 ET
        history = [{'state': 'LOW_VOL', 'confidence': 0.97,
                    'hysteresis_streak': 5, 'fired_liquidation': False,
                    'transition_tag': None}]
        monkeypatch.setattr(detector, '_load_intraday_features_module',
                            lambda: stub)
        monkeypatch.setattr(detector, '_connect_postgres', lambda: _FakeConn())
        monkeypatch.setattr(detector, '_last_n_states', lambda conn, n: history)
        monkeypatch.setattr(detector, '_enrich_with_daily_derived', lambda f: f)
        monkeypatch.setattr(detector, 'MODEL_PATH',
                            tmp_path / 'nonexistent.pkl')
        persisted = {}
        monkeypatch.setattr(
            detector, '_persist_state_row',
            lambda conn, ts, state, prior, conf, streak, fired, tag, feats:
                persisted.update(state=state, flag=feats.get('source_quality_flag')))

        result = detector.run_one_tick()

        assert result.get('carry_fwd') is not True
        assert result['state'] == 'UNKNOWN'      # bootstrap: no model file
        assert stub.appended['source_quality_flag'] == 1   # collector value kept
        assert persisted['flag'] == 1
