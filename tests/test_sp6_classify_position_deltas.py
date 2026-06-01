"""tests/test_sp6_classify_position_deltas.py — Pure _classify_position_deltas tests.

Exercises the classifier with the grounding examples from test_sizer_action_label.py
and crypto_redeploy.py. The function emits (ticker, signed_usd, kind) tuples for
delta, flip_close, flip_open, and orphan_close, with the EXACT same logic as the
inline version.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))


def test_delta_new_long():
    """delta: new long position (current=0, target>0)."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': 100.0}
    broker = {}
    ticker_meta = {}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    assert emissions == [('AAPL', 100.0, 'delta')]


def test_delta_new_short():
    """delta: new short position (current=0, target<0)."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': -100.0}
    broker = {}
    ticker_meta = {}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    assert emissions == [('AAPL', -100.0, 'delta')]


def test_delta_add_long():
    """delta: add to existing long (current=50, target=100)."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': 100.0}
    broker = {'AAPL': 50.0}
    ticker_meta = {'AAPL': {'strategies': ['S_a'], 'directions': [1], 'brackets': []}}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    assert emissions == [('AAPL', 50.0, 'delta')]


def test_delta_reduce_long():
    """delta: reduce long position (current=100, target=50)."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': 50.0}
    broker = {'AAPL': 100.0}
    ticker_meta = {'AAPL': {'strategies': ['S_a'], 'directions': [1], 'brackets': []}}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    assert emissions == [('AAPL', -50.0, 'delta')]


def test_delta_close_to_zero():
    """delta: close a long to zero (current=100, target=0)."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': 0.0}
    broker = {'AAPL': 100.0}
    ticker_meta = {'AAPL': {'strategies': ['S_a'], 'directions': [1], 'brackets': []}}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    # Inline code: delta = 0 - 100 = -100 != 0, so emits
    assert emissions == [('AAPL', -100.0, 'delta')]


def test_delta_no_change():
    """delta: no change (current==target)."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': 100.0}
    broker = {'AAPL': 100.0}
    ticker_meta = {'AAPL': {'strategies': ['S_a'], 'directions': [1], 'brackets': []}}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    assert emissions == []


def test_flip_long_to_short():
    """flip: current long → target short."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': -50.0}
    broker = {'AAPL': 100.0}
    ticker_meta = {'AAPL': {'strategies': ['S_a'], 'directions': [1], 'brackets': []}}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    # flip_close: negate current = -100
    # flip_open: target = -50
    assert emissions == [('AAPL', -100.0, 'flip_close'), ('AAPL', -50.0, 'flip_open')]


def test_flip_short_to_long():
    """flip: current short → target long."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': 50.0}
    broker = {'AAPL': -100.0}
    ticker_meta = {'AAPL': {'strategies': ['S_a'], 'directions': [-1], 'brackets': []}}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    # flip_close: negate current = 100
    # flip_open: target = 50
    assert emissions == [('AAPL', 100.0, 'flip_close'), ('AAPL', 50.0, 'flip_open')]


def test_flip_with_zero_target():
    """flip: current and zero target (current long, target=0) — NOT a flip, delta only."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': 0.0}
    broker = {'AAPL': 100.0}
    ticker_meta = {'AAPL': {'strategies': ['S_a'], 'directions': [1], 'brackets': []}}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    # Inline: if target==0 or current==0, skip flip check; delta = 0 - 100 = -100
    assert emissions == [('AAPL', -100.0, 'delta')]


def test_flip_with_zero_current():
    """flip: current=0 and target (target short, current=0) — NOT a flip, delta only."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': -50.0}
    broker = {}
    ticker_meta = {}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    # Inline: if target==0 or current==0, skip flip check; delta = -50 - 0 = -50
    assert emissions == [('AAPL', -50.0, 'delta')]


def test_orphan_close_long():
    """orphan_close: ticker in broker, not in targets (long position)."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': 50.0}
    broker = {'AAPL': 50.0, 'XYZ': 80.0}
    ticker_meta = {'AAPL': {'strategies': ['S_a'], 'directions': [1], 'brackets': []}}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    # XYZ orphan_close: negate current = -80
    assert ('XYZ', -80.0, 'orphan_close') in emissions
    # XYZ added to ticker_meta
    assert 'XYZ' in ticker_meta
    assert ticker_meta['XYZ']['strategies'] == ['__close_orphan__']
    assert ticker_meta['XYZ']['directions'] == [0]
    assert ticker_meta['XYZ']['brackets'] == []


def test_orphan_close_short():
    """orphan_close: ticker in broker, not in targets (short position)."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {}
    broker = {'AAPL': -50.0}
    ticker_meta = {}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    # AAPL orphan_close: negate current = 50
    assert emissions == [('AAPL', 50.0, 'orphan_close')]
    assert ticker_meta['AAPL']['strategies'] == ['__close_orphan__']


def test_orphan_close_crypto():
    """orphan_close: crypto position orphaned."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {}
    broker = {'BTC-USD': 20000.0, 'ETH-USD': -5000.0}
    ticker_meta = {}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    emitted = {(t, k) for t, _, k in emissions}
    assert ('BTC-USD', 'orphan_close') in emitted
    assert ('ETH-USD', 'orphan_close') in emitted
    # BTC-USD: negate 20000 = -20000
    assert ('BTC-USD', -20000.0, 'orphan_close') in emissions
    # ETH-USD: negate -5000 = 5000
    assert ('ETH-USD', 5000.0, 'orphan_close') in emissions


def test_no_orphan_if_zero_current():
    """orphan: no emission if broker position is 0."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {}
    broker = {'AAPL': 0.0, 'XYZ': 50.0}
    ticker_meta = {}
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    # AAPL: skip (current==0); XYZ: emit orphan_close
    tickers = {t for t, _, _ in emissions}
    assert 'AAPL' not in tickers
    assert 'XYZ' in tickers


def test_mixed_emissions():
    """mixed: delta + orphan_close + flip."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': 50.0, 'MSFT': -100.0, 'GOOG': 0.0}
    broker = {'AAPL': 50.0, 'MSFT': 100.0, 'XYZ': 30.0, 'GOOG': 25.0}
    ticker_meta = {
        'AAPL': {'strategies': ['S_a'], 'directions': [1], 'brackets': []},
        'MSFT': {'strategies': ['S_b'], 'directions': [1], 'brackets': []},
        'GOOG': {'strategies': ['S_c'], 'directions': [1], 'brackets': []},
    }
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    emitted_set = set(emissions)

    # AAPL: current==target → no delta
    assert ('AAPL', 50.0, 'delta') not in emitted_set

    # MSFT: current=100 long, target=-100 short → flip
    assert ('MSFT', -100.0, 'flip_close') in emitted_set
    assert ('MSFT', -100.0, 'flip_open') in emitted_set

    # XYZ: in broker, not in targets → orphan_close
    assert ('XYZ', -30.0, 'orphan_close') in emitted_set

    # GOOG: target=0, current=25 → delta (close)
    assert ('GOOG', -25.0, 'delta') in emitted_set


def test_flip_multiple_tickers():
    """flip: multiple tickers with flips in the same call."""
    from execution.regime_blended_sizer import _classify_position_deltas
    target = {'AAPL': -50.0, 'MSFT': 75.0}
    broker = {'AAPL': 100.0, 'MSFT': -100.0}
    ticker_meta = {
        'AAPL': {'strategies': ['S_a'], 'directions': [1], 'brackets': []},
        'MSFT': {'strategies': ['S_b'], 'directions': [-1], 'brackets': []},
    }
    emissions = _classify_position_deltas(target, broker, ticker_meta)
    emitted_set = set(emissions)

    # AAPL: long 100 → short 50
    assert ('AAPL', -100.0, 'flip_close') in emitted_set
    assert ('AAPL', -50.0, 'flip_open') in emitted_set

    # MSFT: short 100 → long 75
    assert ('MSFT', 100.0, 'flip_close') in emitted_set
    assert ('MSFT', 75.0, 'flip_open') in emitted_set


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
