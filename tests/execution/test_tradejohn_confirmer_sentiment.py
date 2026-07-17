"""Tests for OPENCLAW_CONFIRMER_SENTIMENT gate in tradejohn_confirmer.

Task 11 of the D1 sentiment plan: the sentiment block from handoff
(Task 10) is wired into the confirmer prompt, gated on
OPENCLAW_CONFIRMER_SENTIMENT=1. When the gate is OFF, the prompt and
payload must be byte-equivalent to today's behavior — no sentiment
section in the template, no sentiment keys in the proposal payload.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.tradejohn_confirmer import _build_prompt


def _proposal_with_sentiment(ticker='AAPL'):
    return {
        'ticker': ticker,
        'preliminary_size_usd': 10000,
        'direction': 1,
        'contributions': [{'strategy_id': 'S1', 'attribution_weight': 1.0}],
        'bracket': {'entry_price': 100, 'stop_loss': 95, 'take_profit_1': 110},
        'context': {'news_headlines': [], 'sector': 'Tech',
                    '30d_veto_history_for_ticker': 0, 'hv30d': 0.18},
        'sentiment': {
            'social_posts_24h': 75,
            'social_bull_ratio': 0.55,
            'social_bear_ratio': 0.30,
            'news_finbert_pos': 0.40,
            'news_finbert_neu': 0.45,
            'news_finbert_neg': 0.15,
            'news_mean_score': 0.25,
            'news_top_headlines': [
                {'title': 'AAPL beats Q3 expectations', 'score': 0.6},
            ],
        },
    }


def _proposal_no_sentiment(ticker='AAPL'):
    return {
        'ticker': ticker,
        'preliminary_size_usd': 10000,
        'direction': 1,
        'contributions': [{'strategy_id': 'S1', 'attribution_weight': 1.0}],
        'bracket': {'entry_price': 100, 'stop_loss': 95, 'take_profit_1': 110},
        'context': {'news_headlines': [], 'sector': 'Tech',
                    '30d_veto_history_for_ticker': 0, 'hv30d': 0.18},
    }


def test_prompt_includes_sentiment_block_when_gate_on(monkeypatch):
    """Gate ON → sentiment section present in template + sentiment keys in payload."""
    monkeypatch.setenv('OPENCLAW_CONFIRMER_SENTIMENT', '1')
    prompt = _build_prompt([_proposal_with_sentiment('AAPL')])
    assert 'Sentiment & News Inputs' in prompt
    assert 'social_bull_ratio' in prompt
    assert 'news_mean_score' in prompt
    assert '"AAPL"' in prompt


def test_prompt_omits_sentiment_block_when_gate_off(monkeypatch):
    """Gate OFF → no sentiment section, even if no proposal carries sentiment."""
    monkeypatch.delenv('OPENCLAW_CONFIRMER_SENTIMENT', raising=False)
    prompt = _build_prompt([_proposal_no_sentiment('AAPL')])
    assert 'Sentiment & News Inputs' not in prompt


def test_confirmer_strips_sentiment_keys_when_gate_off(monkeypatch):
    """Gate OFF + proposal carries sentiment → strip from payload so LLM never sees it."""
    monkeypatch.delenv('OPENCLAW_CONFIRMER_SENTIMENT', raising=False)
    prompt = _build_prompt([_proposal_with_sentiment('AAPL')])
    assert 'news_mean_score' not in prompt
    assert 'social_posts_24h' not in prompt
