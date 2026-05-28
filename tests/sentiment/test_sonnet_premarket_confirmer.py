import json
from unittest.mock import patch, MagicMock
from src.sentiment.sonnet_premarket_confirmer import (
    PremarketConfirmerInput,
    confirm_panic,
    PANIC_VERDICTS,
)


def _make_input():
    return PremarketConfirmerInput(
        ticker='GLW',
        held_qty=100,
        panic_score=72.0,
        news_count=2,
        finbert_neg_ratio=1.0,
        social_bear_ratio=0.3,
        top_headlines=[
            ('CFO departs unexpectedly', -0.91, 'uuid-1'),
            ('Q3 guidance cut by 20%', -0.88, 'uuid-2'),
        ],
    )


@patch('src.sentiment.sonnet_premarket_confirmer.subprocess.run')
def test_confirm_panic_parses_well_formed_json(mock_run):
    sonnet_resp = {
        'result': json.dumps({
            'panic_verdict': 'bearish_news_driven',
            'severity': 5,
            'rationale': 'CFO departure plus guidance cut is a hard catalyst.',
            'evidence_uuids': ['uuid-1', 'uuid-2'],
        }),
        'total_cost_usd': 0.013,
    }
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=json.dumps(sonnet_resp).encode(),
        stderr=b'',
    )

    out = confirm_panic(_make_input())

    assert out.verdict == 'bearish_news_driven'
    assert out.severity == 5
    assert out.evidence_uuids == ['uuid-1', 'uuid-2']
    assert out.cost_usd == 0.013
    assert 'CFO departure' in out.rationale


@patch('src.sentiment.sonnet_premarket_confirmer.subprocess.run')
def test_confirm_panic_handles_fenced_json_block(mock_run):
    fenced = '```json\n' + json.dumps({
        'panic_verdict': 'neutral',
        'severity': 2,
        'rationale': 'Routine analyst report.',
        'evidence_uuids': [],
    }) + '\n```'
    sonnet_resp = {'result': fenced, 'total_cost_usd': 0.01}
    mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(sonnet_resp).encode())

    out = confirm_panic(_make_input())
    assert out.verdict == 'neutral'
    assert out.severity == 2


@patch('src.sentiment.sonnet_premarket_confirmer.subprocess.run')
def test_confirm_panic_returns_llm_error_on_subprocess_failure(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stdout=b'', stderr=b'budget exceeded')
    out = confirm_panic(_make_input())
    assert out.verdict == 'llm_error'
    assert out.severity is None
    assert 'budget exceeded' in out.rationale


@patch('src.sentiment.sonnet_premarket_confirmer.subprocess.run')
def test_confirm_panic_returns_llm_error_on_malformed_json(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=json.dumps({'result': 'not json at all', 'total_cost_usd': 0.0}).encode(),
    )
    out = confirm_panic(_make_input())
    assert out.verdict == 'llm_error'


@patch('src.sentiment.sonnet_premarket_confirmer.subprocess.run')
def test_confirm_panic_rejects_unknown_verdict(mock_run):
    bad = {'result': json.dumps({
        'panic_verdict': 'definitely_panic_lol',
        'severity': 5,
        'rationale': 'x',
        'evidence_uuids': [],
    }), 'total_cost_usd': 0.01}
    mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(bad).encode())
    out = confirm_panic(_make_input())
    assert out.verdict == 'llm_error'
    assert 'unknown verdict' in out.rationale.lower()


def test_panic_verdicts_are_pinned():
    """If you add a verdict, update the auto-close gate logic in run_premarket_scan."""
    assert PANIC_VERDICTS == (
        'bullish', 'neutral', 'bearish_news_driven', 'bearish_idiosyncratic'
    )
