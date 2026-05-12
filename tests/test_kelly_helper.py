# tests/test_kelly_helper.py
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pytest
from execution._kelly import reward_to_risk, kelly_fraction, enrich_with_kelly

# === reward_to_risk ===

def test_reward_to_risk_long():
    # entry=100, stop=95, t1=110 → R = (110-100)/(100-95) = 10/5 = 2
    assert reward_to_risk('LONG', 100, 95, 110) == pytest.approx(2.0)

def test_reward_to_risk_buy_alias_for_long():
    assert reward_to_risk('BUY', 100, 95, 110) == pytest.approx(2.0)

def test_reward_to_risk_short():
    # entry=100, stop=105, t1=90 → R = (100-90)/(105-100) = 10/5 = 2
    assert reward_to_risk('SHORT', 100, 105, 90) == pytest.approx(2.0)

def test_reward_to_risk_sell_alias_for_short():
    assert reward_to_risk('SELL', 100, 105, 90) == pytest.approx(2.0)

def test_reward_to_risk_malformed_long_returns_none():
    # stop above entry on a LONG is malformed
    assert reward_to_risk('LONG', 100, 105, 110) is None

def test_reward_to_risk_malformed_short_returns_none():
    # stop below entry on a SHORT is malformed
    assert reward_to_risk('SHORT', 100, 95, 90) is None

def test_reward_to_risk_unknown_direction_returns_none():
    assert reward_to_risk('UNKNOWN', 100, 95, 110) is None

# === kelly_fraction ===

def test_kelly_fraction_positive_edge():
    # p=0.6, R=2 → f = (0.6*2 - 0.4) / 2 = 0.8 / 2 = 0.4
    assert kelly_fraction(0.6, 2.0) == pytest.approx(0.4)

def test_kelly_fraction_negative_edge():
    # p=0.4, R=1 → f = (0.4*1 - 0.6) / 1 = -0.2
    assert kelly_fraction(0.4, 1.0) == pytest.approx(-0.2)

def test_kelly_fraction_zero_R_returns_zero():
    assert kelly_fraction(0.6, 0.0) == 0.0

# === enrich_with_kelly ===

def test_enrich_adds_kelly_p_to_each_signal():
    signals = [{'strategy_id': 'S1', 'ticker': 'AAPL', 'direction': 'LONG',
                'entry': 100, 'stop': 95, 't1': 110, 'p_t1': 0.6}]
    enriched = enrich_with_kelly(signals)
    assert 'kelly_p' in enriched[0]
    # R = 2, kelly = (0.6*2 - 0.4)/2 = 0.4
    assert enriched[0]['kelly_p'] == pytest.approx(0.4)

def test_enrich_handles_alternate_field_names():
    # Some signals have entry_price/stop_loss/target_1 instead of entry/stop/t1
    signals = [{'strategy_id': 'S1', 'ticker': 'AAPL', 'direction': 'LONG',
                'entry_price': 100, 'stop_loss': 95, 'target_1': 110, 'p_t1': 0.6}]
    enriched = enrich_with_kelly(signals)
    assert enriched[0]['kelly_p'] == pytest.approx(0.4)

def test_enrich_missing_fields_sets_kelly_zero():
    signals = [{'strategy_id': 'S1', 'ticker': 'AAPL', 'direction': 'LONG',
                'entry': 100, 'stop': 95, 't1': 110}]  # no p_t1
    enriched = enrich_with_kelly(signals)
    assert enriched[0]['kelly_p'] == 0.0

def test_enrich_negative_kelly_clamped_to_zero():
    # Negative-EV signal should result in kelly_p=0 (caller treats as veto)
    signals = [{'strategy_id': 'S1', 'ticker': 'AAPL', 'direction': 'LONG',
                'entry': 100, 'stop': 95, 't1': 110, 'p_t1': 0.2}]
    enriched = enrich_with_kelly(signals)
    # raw kelly = (0.2*2 - 0.8)/2 = -0.2 → clamped to 0
    assert enriched[0]['kelly_p'] == 0.0

def test_enrich_preserves_other_fields():
    signals = [{'strategy_id': 'S1', 'ticker': 'AAPL', 'direction': 'LONG',
                'entry': 100, 'stop': 95, 't1': 110, 'p_t1': 0.6,
                'strategy_memo_mult': 1.5, 'extra_field': 'preserved'}]
    enriched = enrich_with_kelly(signals)
    assert enriched[0]['strategy_memo_mult'] == 1.5
    assert enriched[0]['extra_field'] == 'preserved'
