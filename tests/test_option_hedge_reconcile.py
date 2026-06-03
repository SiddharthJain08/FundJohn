"""tests/test_option_hedge_reconcile.py — SP-5.1b-ii Task 6b: option-hedge reconcile.

Pure-unit tests (mock cursor — no POSTGRES_URI required) for:
  - _load_approved_set sign aggregation + ambiguous-sentinel logic (gated)
  - flip-decision safety for ambiguous tickers via _classify_position_deltas

Design invariants verified:
  (A) gate OFF  → _load_approved_set behaves exactly as before (last-write-wins ±1).
  (B) gate ON, single sign  → ±1 (byte-identical for the common case).
  (C) gate ON, conflicting signs → sentinel 0.0 (present, ambiguous); NOT orphaned.
  (D) gate ON, hedge-only opposite sign → ±1 (flip-close fires normally).
  (E) gate ON, ambiguous sentinel 0.0 → _classify_position_deltas emits delta/ignored,
      not flip_close (deferred to 3:55 sizer).
  (F) gate ON, hedge-only single-sign flip → flip_close emits (operator decision: fire).
  (G) gate OFF, conflicting-signs conflict → last-write-wins ±1 preserved exactly.

Run:
    cd /root/openclaw/.claude/worktrees/sp5.1a-single-leg-options-exec
    python3 -m pytest tests/test_option_hedge_reconcile.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pytest

# ---------------------------------------------------------------------------
# Helper: build a minimal mock cursor that returns preset rows
# ---------------------------------------------------------------------------

class _MockCursor:
    """Minimal cursor mock; call set_rows([...]) before fetchall/fetchone."""

    def __init__(self):
        self._rows: list = []

    def execute(self, sql, params=None):
        pass  # ignored

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def set_rows(self, rows):
        self._rows = rows


def _cur_with_rows(rows):
    c = _MockCursor()
    c.set_rows(rows)
    return c


# ---------------------------------------------------------------------------
# Helper: convert raw rows to the dict-like format _norm_row_get expects
# (list-of-dicts, like RealDictCursor)
# ---------------------------------------------------------------------------

def _row(ticker, direction):
    """Return a dict row as RealDictCursor would produce."""
    return {'ticker': ticker, 'direction': direction}


# ---------------------------------------------------------------------------
# Shared gate fixture helpers
# ---------------------------------------------------------------------------

def _gate_on():
    os.environ['OPENCLAW_OPTION_DELTA_HEDGE'] = '1'


def _gate_off():
    os.environ.pop('OPENCLAW_OPTION_DELTA_HEDGE', None)


# ---------------------------------------------------------------------------
# Import target under test
# ---------------------------------------------------------------------------

from execution.open_reconcile import _load_approved_set  # noqa: E402
from execution.regime_blended_sizer import _classify_position_deltas  # noqa: E402


# ===========================================================================
# (A) Gate OFF: last-write-wins — byte-identical to current behavior
# ===========================================================================

def test_gate_off_last_write_wins_single_sign():
    """Gate OFF, single-sign: returns ±1 via last-write-wins (unchanged)."""
    _gate_off()
    from datetime import date
    cur = _cur_with_rows([_row('SPY', 'LONG')])
    result = _load_approved_set(cur, date.today())
    assert result == {'SPY': 1.0}


def test_gate_off_conflict_last_write_wins():
    """Gate OFF, conflicting signs: last row wins (old behavior preserved)."""
    _gate_off()
    from datetime import date
    # Rows ordered ASC signal_date/computed_at. The last row in list wins.
    cur = _cur_with_rows([
        _row('SPY', 'LONG'),   # first (equity)
        _row('SPY', 'SHORT'),  # last  (hedge)
    ])
    result = _load_approved_set(cur, date.today())
    # Last-write-wins → SHORT wins
    assert result == {'SPY': -1.0}


def test_gate_off_no_ambiguous_sentinel():
    """Gate OFF: conflicting signs never produce the 0.0 sentinel."""
    _gate_off()
    from datetime import date
    cur = _cur_with_rows([
        _row('AAPL', 'LONG'),
        _row('AAPL', 'SHORT'),
    ])
    result = _load_approved_set(cur, date.today())
    assert result['AAPL'] in (1.0, -1.0), \
        "gate OFF must never produce 0.0 sentinel"


# ===========================================================================
# (B) Gate ON: single sign — byte-identical to current
# ===========================================================================

def test_approved_set_single_sign_unchanged():
    """test_approved_set_single_sign_unchanged: single equity LONG → {SPY: +1.0}."""
    _gate_on()
    from datetime import date
    cur = _cur_with_rows([_row('SPY', 'LONG')])
    result = _load_approved_set(cur, date.today())
    assert result == {'SPY': 1.0}


def test_approved_set_single_sign_short():
    """Gate ON, single SHORT row → -1.0."""
    _gate_on()
    from datetime import date
    cur = _cur_with_rows([_row('SPY', 'SHORT')])
    result = _load_approved_set(cur, date.today())
    assert result == {'SPY': -1.0}


# ===========================================================================
# (C) Gate ON: conflicting signs → ambiguous sentinel 0.0 (present, NOT orphaned)
# ===========================================================================

def test_approved_set_conflicting_signs_marked_ambiguous():
    """test_approved_set_conflicting_signs_marked_ambiguous:
    equity LONG + hedge SHORT → sentinel 0.0 (present, not orphaned)."""
    _gate_on()
    from datetime import date
    cur = _cur_with_rows([
        _row('SPY', 'LONG'),
        _row('SPY', 'SHORT'),
    ])
    result = _load_approved_set(cur, date.today())
    # SPY must be PRESENT in the dict (not orphaned)
    assert 'SPY' in result, "conflicting-sign ticker must still be present"
    # Must carry the ambiguous sentinel, not ±1
    assert result['SPY'] == 0.0, f"expected 0.0 sentinel, got {result['SPY']}"


def test_approved_set_conflicting_signs_not_orphaned():
    """Ambiguous ticker remains in the approved set — never orphan-closed."""
    _gate_on()
    from datetime import date
    cur = _cur_with_rows([
        _row('AAPL', 'LONG'),
        _row('AAPL', 'SHORT'),
    ])
    result = _load_approved_set(cur, date.today())
    assert 'AAPL' in result, "ambiguous ticker must be in approved set (not orphaned)"


# ===========================================================================
# (D) Gate ON: hedge-only single sign → ±1 (flip-close fires)
# ===========================================================================

def test_approved_set_hedge_only_keeps_its_sign():
    """test_approved_set_hedge_only_keeps_its_sign:
    hedge-only SHORT → -1.0 (flip-close can fire)."""
    _gate_on()
    from datetime import date
    cur = _cur_with_rows([_row('SPY', 'SHORT')])
    result = _load_approved_set(cur, date.today())
    assert result == {'SPY': -1.0}


# ===========================================================================
# (E) Gate ON: ambiguous 0.0 sentinel → classifier emits delta (not flip_close)
#     Exercise _classify_position_deltas directly (no DB required)
# ===========================================================================

def test_ambiguous_ticker_not_flip_closed():
    """Gate ON: ambiguous sentinel (0.0) with opposite broker sign emits delta,
    NOT flip_close — safe to ignore at the open."""
    _gate_on()
    # Simulate: _load_approved_set returns {SPY: 0.0} for an ambiguous ticker.
    # Broker holds SPY LONG (+5000). classifier must NOT emit flip_close.
    target = {'SPY': 0.0}   # ambiguous sentinel
    broker = {'SPY': 5000.0}
    emissions = _classify_position_deltas(target, broker, {})
    kinds = {k for _, _, k in emissions}
    assert 'flip_close' not in kinds, \
        "ambiguous sentinel must not trigger flip_close"
    assert 'orphan_close' not in kinds, \
        "ambiguous sentinel must not trigger orphan_close (SPY IS in target)"
    # Emits delta (ignored at the open by run_reconcile's kind filter)
    assert 'delta' in kinds or not emissions, \
        "ambiguous ticker should emit delta or nothing"


def test_ambiguous_ticker_not_orphan_closed_zero_key():
    """Confirm _classify_position_deltas membership check: 0.0 value means 'present'."""
    target = {'SPY': 0.0}
    broker = {'SPY': 3000.0, 'AAPL': 1000.0}
    emissions = _classify_position_deltas(target, broker, {})
    # SPY must NOT be an orphan (it IS in target, just with 0.0 value)
    orphan_tickers = {t for t, _, k in emissions if k == 'orphan_close'}
    assert 'SPY' not in orphan_tickers
    # AAPL IS an orphan (not in target)
    assert 'AAPL' in orphan_tickers


# ===========================================================================
# (F) Gate ON: hedge-only single-sign flip → flip_close fires normally
# ===========================================================================

def test_hedge_only_single_sign_flip_fires():
    """Gate ON: hedge-only SHORT (single sign = -1.0) vs broker LONG → flip_close."""
    _gate_on()
    # Simulate _load_approved_set returning hedge-only -1.0 for SPY
    target = {'SPY': -1.0}   # hedge-only approved sign
    broker = {'SPY': 5000.0}  # broker LONG
    emissions = _classify_position_deltas(target, broker, {})
    kinds = {k for _, _, k in emissions}
    # Should produce flip_close (and flip_open) — the old leg is closed at the open
    assert 'flip_close' in kinds, \
        "hedge-only single-sign opposite broker must flip_close"


# ===========================================================================
# Edge cases
# ===========================================================================

def test_empty_rows_returns_empty():
    """No rows → empty approved set regardless of gate."""
    for setup in [_gate_on, _gate_off]:
        setup()
        from datetime import date
        cur = _cur_with_rows([])
        result = _load_approved_set(cur, date.today())
        assert result == {}


def test_multiple_tickers_independence():
    """Gate ON: conflict on one ticker does not affect other tickers."""
    _gate_on()
    from datetime import date
    cur = _cur_with_rows([
        _row('SPY', 'LONG'),
        _row('SPY', 'SHORT'),  # SPY conflicts → 0.0
        _row('AAPL', 'LONG'),  # AAPL single-sign → +1.0
    ])
    result = _load_approved_set(cur, date.today())
    assert result.get('SPY') == 0.0, "SPY should be ambiguous"
    assert result.get('AAPL') == 1.0, "AAPL should be unambiguous +1.0"


def test_crypto_normalization_conflict():
    """Gate ON: crypto symbol normalization works with conflicting signs."""
    _gate_on()
    from datetime import date
    cur = _cur_with_rows([
        _row('BTC/USD', 'LONG'),
        _row('BTC/USD', 'SHORT'),
    ])
    result = _load_approved_set(cur, date.today())
    # Normalized to BTC-USD
    assert 'BTC-USD' in result
    assert result['BTC-USD'] == 0.0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
