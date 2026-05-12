import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.tradejohn_confirmer import (
    confirm, _build_prompt, _parse_response, FAIL_OPEN_DEFAULT,
)

def _proposal(ticker='AAPL', size=10000):
    return {'ticker': ticker, 'preliminary_size_usd': size, 'direction': 1,
            'contributions': [{'strategy_id': 'S1', 'attribution_weight': 1.0}],
            'bracket': {'entry_price': 100, 'stop_loss': 95, 'take_profit_1': 110},
            'context': {'news_headlines': [], 'sector': 'Tech',
                        '30d_veto_history_for_ticker': 0, 'hv30d': 0.18}}

def test_parse_response_approve_veto_scale():
    raw = '{"AAPL": {"action": "approve", "multiplier": 1.0, "rationale": "ok"},' \
          ' "MSFT": {"action": "veto", "multiplier": 0, "rationale": "earnings"},' \
          ' "GOOGL": {"action": "scale", "multiplier": 0.5, "rationale": "soft news"}}'
    out = _parse_response(raw, ['AAPL', 'MSFT', 'GOOGL'])
    assert out['AAPL']['action'] == 'approve'
    assert out['MSFT']['multiplier'] == 0
    assert out['GOOGL']['multiplier'] == 0.5

def test_parse_response_malformed_ticker_fails_open(caplog):
    raw = '{"AAPL": {"action": "approve", "multiplier": 1.0},' \
          ' "MSFT": "not-a-dict"}'
    out = _parse_response(raw, ['AAPL', 'MSFT'])
    assert out['AAPL']['action'] == 'approve'
    assert out['MSFT'] == FAIL_OPEN_DEFAULT
    assert any('MSFT' in rec.message for rec in caplog.records)

def test_parse_response_total_garbage_fails_open(caplog):
    out = _parse_response('not json at all', ['AAPL', 'MSFT'])
    assert out == {'AAPL': FAIL_OPEN_DEFAULT, 'MSFT': FAIL_OPEN_DEFAULT}
    assert any('failed to parse' in rec.message.lower() for rec in caplog.records)

def test_parse_response_missing_ticker_fails_open():
    raw = '{"AAPL": {"action": "approve", "multiplier": 1.0}}'
    out = _parse_response(raw, ['AAPL', 'MSFT'])
    assert out['AAPL']['action'] == 'approve'
    assert out['MSFT'] == FAIL_OPEN_DEFAULT

def test_build_prompt_includes_required_fields():
    prompt = _build_prompt([_proposal('AAPL'), _proposal('MSFT', size=5000)])
    assert 'AAPL' in prompt and 'MSFT' in prompt
    assert 'preliminary_size_usd' in prompt
    assert '"action"' in prompt and '"multiplier"' in prompt

def test_confirm_invokes_runner_and_parses(monkeypatch):
    captured = {}
    def fake_runner(prompt, max_tokens):
        captured['prompt'] = prompt
        return '{"AAPL": {"action": "scale", "multiplier": 0.7, "rationale": "x"}}'
    proposals = [_proposal('AAPL', size=20000)]
    out = confirm(proposals, runner=fake_runner)
    assert out['AAPL']['multiplier'] == 0.7
    assert 'AAPL' in captured['prompt']

def test_confirm_runner_timeout_fails_open(monkeypatch, caplog):
    def timeout_runner(prompt, max_tokens):
        raise TimeoutError('claude-bin timed out')
    proposals = [_proposal('AAPL'), _proposal('MSFT')]
    out = confirm(proposals, runner=timeout_runner)
    assert out['AAPL'] == FAIL_OPEN_DEFAULT
    assert out['MSFT'] == FAIL_OPEN_DEFAULT

def test_confirm_runner_generic_error_fails_open(caplog):
    def boom_runner(prompt, max_tokens):
        raise RuntimeError('upstream 500')
    out = confirm([_proposal('AAPL')], runner=boom_runner)
    assert out['AAPL'] == FAIL_OPEN_DEFAULT

def test_confirm_empty_proposals_returns_empty():
    out = confirm([], runner=lambda *a, **kw: '{}')
    assert out == {}
