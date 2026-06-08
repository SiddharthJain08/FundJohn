"""tests/test_greenlist_cards.py

_fmt_greenlist beautification (2026-06-08): one boxed card per signal, with the
contributing strategies as the LAST piece of information, listed VERTICALLY
(one bullet per line) — not crammed into a fixed-width column.

Run:
    pytest tests/test_greenlist_cards.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.send_report import _fmt_greenlist  # noqa: E402


def _order(ticker, direction, entry, pct_nav, ev, p, strats, strategy_id=None):
    return {
        'ticker': ticker, 'direction': direction, 'entry': entry,
        'pct_nav': pct_nav, 'ev': ev, 'p_t1': p,
        'strategy_id': strategy_id or (strats[0] if strats else None),
        'contributing_strategies': strats,
    }


def _sized(orders, regime='LOW_VOL'):
    return {'orders': orders, 'regime': {'state': regime}}


def _line_index(text, needle):
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if needle in ln:
            return i
    return -1


class TestGreenlistCards:
    def test_empty_orders_no_signal_message(self):
        out = _fmt_greenlist('2026-06-08', _sized([]))
        assert 'no actionable signals' in out
        assert 'LOW_VOL' in out

    def test_header_banner_has_regime_and_count(self):
        orders = [_order('AMZN', 'long', 185.2, 0.042, 0.018, 0.62,
                         ['S_a', 'S_b', 'S_c'])]
        out = _fmt_greenlist('2026-06-08', _sized(orders, regime='HIGH_VOL'))
        assert '2026-06-08' in out
        assert 'HIGH_VOL' in out
        assert '1 order' in out          # singular/plural tolerant

    def test_strategies_are_last_and_vertical(self):
        strats = ['S_momentum_breakout', 'S_news_sentiment_ls', 'S_pcr_momentum']
        orders = [_order('AMZN', 'long', 185.2, 0.042, 0.018, 0.62, strats)]
        out = _fmt_greenlist('2026-06-08', _sized(orders))

        # every contributing strategy appears, each on its OWN line
        for s in strats:
            assert out.count(s) == 1
            # the strategy's line contains ONLY that strategy (vertical list)
            line = next(ln for ln in out.splitlines() if s in ln)
            assert sum(1 for other in strats if other in line) == 1

        # strategies come AFTER the metrics (last piece of info for the card)
        ev_idx = _line_index(out, 'EV')
        first_strat_idx = min(_line_index(out, s) for s in strats)
        assert first_strat_idx > ev_idx

    def test_single_strategy_order(self):
        orders = [_order('TSLA', 'short', 242.1, 0.021, 0.009, 0.55,
                         ['S_meanrev_intraday'])]
        out = _fmt_greenlist('2026-06-08', _sized(orders))
        assert 'TSLA' in out
        assert 'S_meanrev_intraday' in out

    def test_missing_contributing_falls_back_to_strategy_id(self):
        o = _order('NVDA', 'long', 500.0, 0.03, 0.02, 0.6, None,
                   strategy_id='S_solo')
        o.pop('contributing_strategies')
        out = _fmt_greenlist('2026-06-08', _sized([o]))
        assert 'S_solo' in out

    def test_direction_arrows(self):
        orders = [
            _order('AMZN', 'long', 185.2, 0.042, 0.018, 0.62, ['S_a']),
            _order('TSLA', 'short', 242.1, 0.021, 0.009, 0.55, ['S_b']),
        ]
        out = _fmt_greenlist('2026-06-08', _sized(orders))
        assert '▲' in out and '▼' in out

    def test_multiple_cards_each_list_own_strategies(self):
        orders = [
            _order('AMZN', 'long', 185.2, 0.042, 0.018, 0.62, ['S_a', 'S_b']),
            _order('TSLA', 'short', 242.1, 0.021, 0.009, 0.55, ['S_c']),
        ]
        out = _fmt_greenlist('2026-06-08', _sized(orders))
        for s in ['S_a', 'S_b', 'S_c']:
            assert s in out
