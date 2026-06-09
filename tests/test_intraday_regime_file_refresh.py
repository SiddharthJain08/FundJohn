"""tests/test_intraday_regime_file_refresh.py

The intraday detector became the SOLE regime authority (2026-06-08): the
daily run_market_state.py no longer writes regime_latest.json / market_regime
(it had emitted a false CRISIS off a stale Friday VIX close while the intraday
primary said LOW_VOL conf~1.0). To keep engine.load_regime()'s 80h staleness
gate satisfied with intraday as the only writer, the intraday tick must
refresh regime_latest.json on EVERY tick (scored AND carry-forward), not only
on a confirmed transition.

Guard: an UNKNOWN / bootstrap tick must NEVER overwrite the file's `state`
with a value engine.load_regime() would consume as a real regime (it does
`state = j.get('state') or 'HIGH_VOL'`), or the empty-signals orphan-close
blowout returns. Such ticks refresh the freshness marker but PRESERVE the
last good state.

Run:
    pytest tests/test_intraday_regime_file_refresh.py -v
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    'run_intraday_market_state',
    ROOT / 'scripts' / 'run_intraday_market_state.py',
)
detector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(detector)


def _ts(utc_str: str) -> pd.Timestamp:
    return pd.Timestamp(utc_str, tz='UTC')


class _FakeConn:
    def close(self):
        pass


# ── _refresh_regime_file ─────────────────────────────────────────────────────

class TestRefreshRegimeFile:
    def _seed(self, d: Path, payload: dict):
        (d / 'regime_latest.json').write_text(json.dumps(payload))

    def test_writes_valid_state_and_preserves_other_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr(detector, 'MODEL_DIR', tmp_path)
        self._seed(tmp_path, {'state': 'CRISIS', 'state_raw': 'CRISIS',
                              'stress_score': 82, 'position_scale': 0.15})
        detector._refresh_regime_file(
            state='LOW_VOL', confidence=0.999, vix=18.65,
            prior_state='LOW_VOL', state_probs=None,
            ts_utc=_ts('2026-06-08 18:55'), transition_tag=None,
        )
        j = json.loads((tmp_path / 'regime_latest.json').read_text())
        assert j['state'] == 'LOW_VOL'
        assert j['state_raw'] == 'LOW_VOL'
        assert j['confidence'] == pytest.approx(0.999)
        assert j['vix_level'] == pytest.approx(18.65)
        assert j['stress_score'] == 82          # unrelated field preserved
        assert j['intraday_source'] == 'intraday_hmm'

    def test_unknown_state_does_not_clobber_existing_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(detector, 'MODEL_DIR', tmp_path)
        self._seed(tmp_path, {'state': 'LOW_VOL', 'state_raw': 'LOW_VOL',
                              'confidence': 0.99})
        detector._refresh_regime_file(
            state='UNKNOWN', confidence=0.0, vix=None,
            prior_state=None, state_probs=None,
            ts_utc=_ts('2026-06-08 21:00'), transition_tag=None,
        )
        j = json.loads((tmp_path / 'regime_latest.json').read_text())
        assert j['state'] == 'LOW_VOL'          # preserved, NOT clobbered to UNKNOWN
        assert j['confidence'] == pytest.approx(0.99)
        # freshness marker still advances so the engine's stale-gate is satisfied
        assert j['intraday_updated_at'] == str(_ts('2026-06-08 21:00'))

    def test_updates_vix_level_when_provided(self, tmp_path, monkeypatch):
        monkeypatch.setattr(detector, 'MODEL_DIR', tmp_path)
        self._seed(tmp_path, {'state': 'LOW_VOL', 'vix_level': 15.0})
        detector._refresh_regime_file(
            state='LOW_VOL', confidence=1.0, vix=18.65,
            prior_state='LOW_VOL', state_probs=None,
            ts_utc=_ts('2026-06-08 18:55'),
        )
        j = json.loads((tmp_path / 'regime_latest.json').read_text())
        assert j['vix_level'] == pytest.approx(18.65)

    def test_creates_file_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(detector, 'MODEL_DIR', tmp_path)
        detector._refresh_regime_file(
            state='HIGH_VOL', confidence=0.9, vix=25.0,
            prior_state='TRANSITIONING', state_probs=None,
            ts_utc=_ts('2026-06-08 18:55'),
        )
        j = json.loads((tmp_path / 'regime_latest.json').read_text())
        assert j['state'] == 'HIGH_VOL'


# ── carry-forward refreshes the file (weekend/overnight freshness keeper) ─────

class TestCarryForwardRefreshesFile:
    def test_carry_forward_refreshes_regime_file(self, monkeypatch):
        history = [{'state': 'LOW_VOL', 'confidence': 0.97, 'hysteresis_streak': 5,
                    'fired_liquidation': False, 'transition_tag': None}]
        monkeypatch.setattr(detector, '_last_n_states', lambda conn, n: history)
        monkeypatch.setattr(detector, '_persist_state_row', lambda *a, **k: None)
        called = {}
        monkeypatch.setattr(detector, '_refresh_regime_file',
                            lambda **kw: called.update(kw))
        feats = {'ts_utc': _ts('2026-06-06 16:00'),   # Saturday — weekend tick
                 'vix_synth_30d': 16.33, 'source_quality_flag': 3}
        detector._carry_forward_tick(_FakeConn(), feats)
        assert called.get('state') == 'LOW_VOL'       # carried settled regime


# ── RTH scored tick refreshes the file every tick ────────────────────────────

class _StubIntradayModule:
    def __init__(self, ts: pd.Timestamp, flag: int = 1):
        self.ts = ts
        self.flag = flag

    def collect_intraday_features(self, now_utc=None):
        return {'ts_utc': self.ts, 'vix_synth_30d': 18.65, 'vix_synth_90d': 20.1,
                'vix_term_slope': 1.08, 'rr_25d': 0.012, 'source_quality_flag': self.flag}

    def append_features_row(self, row):
        pass


class TestSyncRegimeToConsumers:
    """The non-cooldown confirmed-transition path: writes the market_regime
    row AND regime_latest.json (now delegated to _refresh_regime_file) before
    the redeploy spawns. This is the live regime-of-record writer — cover it
    directly since the run_one_tick tests mock _sync out."""

    class _Cur:
        def __init__(self):
            self.calls = []
        def execute(self, sql, params=None):
            self.calls.append((sql, params))
        def close(self):
            pass

    class _Conn:
        def __init__(self):
            self.cur = TestSyncRegimeToConsumers._Cur()
            self.commits = 0
        def cursor(self):
            return self.cur
        def commit(self):
            self.commits += 1
        def close(self):
            pass

    def test_transition_writes_market_regime_row_and_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(detector, 'MODEL_DIR', tmp_path)
        conn = self._Conn()
        detector._sync_regime_to_consumers(
            conn=conn, new_state='HIGH_VOL', prior_state='LOW_VOL',
            confidence=0.95, state_probs=None,
            features={'vix_synth_30d': 22.0},
            ts_utc=_ts('2026-06-08 15:00'),
            transition_tag='INTRADAY_HMM_REDEPLOY_LOW_VOL_HIGH_VOL',
        )
        # (a) market_regime INSERT with the new state + vix
        inserts = [c for c in conn.cur.calls if 'INSERT INTO market_regime' in c[0]]
        assert len(inserts) == 1
        params = inserts[0][1]
        assert params[0] == 'HIGH_VOL'        # state
        assert float(params[1]) == 22.0       # vix_level
        assert conn.commits >= 1
        # (b) regime_latest.json written with the new state + transition tag
        j = json.loads((tmp_path / 'regime_latest.json').read_text())
        assert j['state'] == 'HIGH_VOL'
        assert j['state_raw'] == 'HIGH_VOL'
        assert j['vix_level'] == pytest.approx(22.0)
        assert j['intraday_transition'] == 'INTRADAY_HMM_REDEPLOY_LOW_VOL_HIGH_VOL'


class TestRunOneTickRefreshesFile:
    def test_rth_tick_refreshes_regime_file(self, monkeypatch, tmp_path):
        """Every RTH tick (even the bootstrap no-model UNKNOWN case) must call
        _refresh_regime_file so the engine's staleness gate stays satisfied."""
        stub = _StubIntradayModule(_ts('2026-06-08 15:00'), flag=1)  # 11:00 ET
        history = [{'state': 'LOW_VOL', 'confidence': 0.97, 'hysteresis_streak': 5,
                    'fired_liquidation': False, 'transition_tag': None}]
        monkeypatch.setattr(detector, '_load_intraday_features_module', lambda: stub)
        monkeypatch.setattr(detector, '_connect_postgres', lambda: _FakeConn())
        monkeypatch.setattr(detector, '_last_n_states', lambda conn, n: history)
        monkeypatch.setattr(detector, '_enrich_with_daily_derived', lambda f: f)
        monkeypatch.setattr(detector, 'MODEL_PATH', tmp_path / 'nonexistent.pkl')
        monkeypatch.setattr(detector, '_persist_state_row', lambda *a, **k: None)
        called = {}
        monkeypatch.setattr(detector, '_refresh_regime_file',
                            lambda **kw: called.update(kw))

        detector.run_one_tick()

        assert called, '_refresh_regime_file was not called on an RTH tick'
        assert called.get('state') == 'UNKNOWN'   # bootstrap: helper guards it

    def test_cooldown_blocked_transition_does_not_advance_file_state(
            self, monkeypatch, tmp_path):
        """A confirmed transition blocked by the redeploy cooldown writes NO
        market_regime row (regime-of-record stays frozen), so the file must
        NOT advance its state to the new regime either — otherwise the file
        leads the db and the doctor's state-agreement check FAILs. The file is
        still refreshed for freshness, but with a non-regime sentinel so
        _refresh_regime_file preserves the last good state."""
        import pickle
        model_file = tmp_path / 'm.pkl'
        model_file.write_bytes(pickle.dumps({'dummy': 1}))
        stub = _StubIntradayModule(_ts('2026-06-08 15:00'), flag=1)  # 11:00 ET RTH
        history = [{'state': 'HIGH_VOL', 'confidence': 0.95, 'hysteresis_streak': 5,
                    'fired_liquidation': False, 'transition_tag': None}]

        class _FakeRedis:
            def get(self, k):
                return '1' if 'cooldown' in k else None

        monkeypatch.setattr(detector, '_load_intraday_features_module', lambda: stub)
        monkeypatch.setattr(detector, '_connect_postgres', lambda: _FakeConn())
        monkeypatch.setattr(detector, '_last_n_states', lambda conn, n: history)
        monkeypatch.setattr(detector, '_enrich_with_daily_derived', lambda f: f)
        monkeypatch.setattr(detector, 'MODEL_PATH', model_file)
        monkeypatch.setattr(detector, '_state_from_hmm',
                            lambda model, feats: ('HIGH_VOL', 0.95, None))
        monkeypatch.setattr(detector, '_maybe_apply_confidence_floor', lambda s, c: s)
        monkeypatch.setattr(detector, '_hysteresis_streak', lambda hist, s: 5)
        monkeypatch.setattr(detector, '_confirmed_transition',
                            lambda hist, s, streak, conf: (True, 'LOW_VOL'))
        monkeypatch.setattr(detector, '_redis', lambda: _FakeRedis())
        monkeypatch.setattr(detector, '_persist_state_row', lambda *a, **k: None)
        monkeypatch.setattr(detector, '_post_to_discord', lambda *a, **k: None)
        # cooldown must suppress BOTH the regime-of-record sync and the redeploy
        monkeypatch.setattr(detector, '_sync_regime_to_consumers',
                            lambda **k: (_ for _ in ()).throw(
                                AssertionError('sync ran during cooldown')))
        monkeypatch.setattr(detector, '_spawn_redeploy',
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError('redeploy spawned during cooldown')))
        captured = {}
        monkeypatch.setattr(detector, '_refresh_regime_file',
                            lambda **kw: captured.update(kw))

        detector.run_one_tick()

        assert captured, '_refresh_regime_file not called on cooldown tick'
        # state passed must NOT be a real regime → helper preserves file state
        assert captured.get('state') not in detector._STATE_RANK

    def test_inflight_blocked_transition_does_not_advance_file_state(
            self, monkeypatch, tmp_path):
        """A confirmed transition blocked because a redeploy is already in-flight
        writes NO market_regime row (regime-of-record stays frozen), so the file
        must NOT advance its state to the new regime either — otherwise the file
        leads the db and the doctor's state-agreement check FAILs.

        Mirror of test_cooldown_blocked_transition_does_not_advance_file_state:
        the only difference is that cooldown returns False (so execution reaches
        the prefetch branch) while prefetch_enabled()+acquire_inflight() together
        make the lock busy, causing the _INFLIGHT tag.
        """
        import pickle
        model_file = tmp_path / 'm.pkl'
        model_file.write_bytes(pickle.dumps({'dummy': 1}))
        stub = _StubIntradayModule(_ts('2026-06-08 15:00'), flag=1)  # 11:00 ET RTH
        history = [{'state': 'HIGH_VOL', 'confidence': 0.95, 'hysteresis_streak': 5,
                    'fired_liquidation': False, 'transition_tag': None}]

        class _FakeRedis:
            def get(self, k):
                return None  # cooldown inactive — must reach the prefetch branch

        monkeypatch.setattr(detector, '_load_intraday_features_module', lambda: stub)
        monkeypatch.setattr(detector, '_connect_postgres', lambda: _FakeConn())
        monkeypatch.setattr(detector, '_last_n_states', lambda conn, n: history)
        monkeypatch.setattr(detector, '_enrich_with_daily_derived', lambda f: f)
        monkeypatch.setattr(detector, 'MODEL_PATH', model_file)
        monkeypatch.setattr(detector, '_state_from_hmm',
                            lambda model, feats: ('HIGH_VOL', 0.95, None))
        monkeypatch.setattr(detector, '_maybe_apply_confidence_floor', lambda s, c: s)
        monkeypatch.setattr(detector, '_hysteresis_streak', lambda hist, s: 5)
        monkeypatch.setattr(detector, '_confirmed_transition',
                            lambda hist, s, streak, conf: (True, 'LOW_VOL'))
        monkeypatch.setattr(detector, '_redis', lambda: _FakeRedis())
        monkeypatch.setattr(detector, '_persist_state_row', lambda *a, **k: None)
        monkeypatch.setattr(detector, '_post_to_discord', lambda *a, **k: None)
        # inflight must suppress BOTH the regime-of-record sync and the redeploy
        monkeypatch.setattr(detector, '_sync_regime_to_consumers',
                            lambda **k: (_ for _ in ()).throw(
                                AssertionError('sync ran during inflight block')))
        monkeypatch.setattr(detector, '_spawn_redeploy',
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError('redeploy spawned during inflight block')))
        # The _INFLIGHT branch is reached via a function-local import inside
        # run_one_tick; patch the source module directly (not detector.*) so the
        # local `from src.execution.intraday_prefetch import ...` rebind picks them up.
        monkeypatch.setattr('src.execution.intraday_prefetch.prefetch_enabled',
                            lambda: True)
        monkeypatch.setattr('src.execution.intraday_prefetch.acquire_inflight',
                            lambda rcli: False)   # lock is busy → _INFLIGHT path
        captured = {}
        monkeypatch.setattr(detector, '_refresh_regime_file',
                            lambda **kw: captured.update(kw))

        detector.run_one_tick()

        assert captured, '_refresh_regime_file not called on inflight-blocked tick'
        # state passed must NOT be a real regime → helper preserves file state
        assert captured.get('state') not in detector._STATE_RANK
        # confirm the sentinel is specifically '_COOLDOWN_HOLD' (shared sentinel)
        assert captured.get('state') == '_COOLDOWN_HOLD'
