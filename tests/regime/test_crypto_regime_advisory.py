import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from regime import crypto_regime as cr  # noqa: E402
from ingestion.crypto_flow import store  # noqa: E402

_spec = importlib.util.spec_from_file_location('run_crypto_market_state', ROOT / 'scripts' / 'run_crypto_market_state.py')
rcms = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(rcms)

KEYS = {'adv_btc_netflow_z_7d', 'adv_btc_funding_8h', 'adv_delev_market',
        'adv_dvol_btc', 'adv_whale_score_btc', 'adv_flow_age_min'}


def test_advisory_features_values(monkeypatch):
    ts = datetime.now(timezone.utc) - timedelta(minutes=20)
    snap = {'BTC-USD': {'exch_netflow_z_7d': 1.5, 'funding_8h': 0.0001, 'whale_score': 0.7, '_ts_utc': ts},
            '__MARKET__': {'delev_market': True, 'dvol_btc': 55.0, '_ts_utc': ts}}
    monkeypatch.setattr(store, 'read_latest_snapshot', lambda conn, with_ts=False: snap)
    out = rcms.advisory_features(object())
    assert set(out) == KEYS
    assert out['adv_btc_netflow_z_7d'] == 1.5 and out['adv_btc_funding_8h'] == 0.0001
    assert out['adv_whale_score_btc'] == 0.7 and out['adv_delev_market'] is True and out['adv_dvol_btc'] == 55.0
    assert 19 <= out['adv_flow_age_min'] < 25


def test_advisory_features_missing_rows_none(monkeypatch):
    monkeypatch.setattr(store, 'read_latest_snapshot', lambda conn, with_ts=False: {})
    out = rcms.advisory_features(object())
    assert set(out) == KEYS and all(v is None for v in out.values())


def test_advisory_features_fail_open(monkeypatch, capsys):
    def boom(conn, with_ts=False): raise RuntimeError('x')
    monkeypatch.setattr(store, 'read_latest_snapshot', boom)
    assert rcms.advisory_features(object()) == {}
    assert '[crypto-regime] advisory unavailable: RuntimeError' in capsys.readouterr().out


def test_hmm_feature_cols_unchanged():
    assert not any(c.startswith('adv_') for c in cr.CRYPTO_FEATURE_COLS)
    assert len(cr.CRYPTO_FEATURE_COLS) == 5


def test_merge_advisory_preserves_hmm_keys():
    base = {c: 1.0 for c in cr.CRYPTO_FEATURE_COLS}
    out = rcms.merge_advisory(base, {'adv_dvol_btc': 5.0})
    assert all(out[c] == 1.0 for c in cr.CRYPTO_FEATURE_COLS) and out['adv_dvol_btc'] == 5.0
    assert 'adv_dvol_btc' not in base
