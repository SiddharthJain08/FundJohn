"""Wave-scoped executor idempotency (afternoon top-up, 2026-08-11).

The same-day EOD lane re-trues the book after the morning intraday-redeploy
wave (see test_sized_handoff_guard's top-up lane tests). For its delta orders
to actually submit, the executor's per-order dedup and client_order_id must
distinguish sizing WAVES within one run_date:

  * _wave_from_handoff derives an HHMMSS tag from the handoff's sized_at.
  * _build_coid embeds `_w<HHMMSS>` so each wave gets fresh coids — the coid
    UNIQUE constraint is record_submission's upsert arbiter (migration 146
    dropped the wave-blind (run_date, strategy_id, ticker) unique index), so
    without the tag a top-up would UPDATE the morning wave's audit row.
  * already_executed(wave=...) matches only submissions carrying the current
    wave's tag: the morning wave's AKTS row no longer blocks the afternoon
    wave's AKTS delta, while a re-run of the SAME wave stays idempotent.

Legacy handoffs without sized_at yield wave='' → old behaviour byte-identical
(whole-day dedup, un-suffixed coids).
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

ex = importlib.import_module('execution.alpaca_executor')


# ── _wave_from_handoff ───────────────────────────────────────────────────────

def test_wave_from_sized_at():
    assert ex._wave_from_handoff({'sized_at': '2026-08-11T19:07:45Z'}) == '190745'


def test_wave_legacy_handoff_empty():
    assert ex._wave_from_handoff({}) == ''
    assert ex._wave_from_handoff({'sized_at': None}) == ''
    assert ex._wave_from_handoff(None) == ''


def test_wave_garbage_sized_at_empty():
    # A date-only stamp (generated_at style) has too few digits — no tag.
    assert ex._wave_from_handoff({'sized_at': '2026-08-11'}) == ''


# ── _build_coid ──────────────────────────────────────────────────────────────

def test_coid_wave_tag_appended():
    coid = ex._build_coid('2026-08-11', 'AKTS', 'S9_dual_momentum', False, '190745')
    assert coid == 'AX20260811_AKTS_S9_dual_momentum_w190745'


def test_coid_wave_and_close_suffix_order():
    coid = ex._build_coid('2026-08-11', 'AKTS', 'S9_dual_momentum', True, '190745')
    assert coid.endswith('_w190745_C')


def test_coid_no_wave_matches_legacy_shape():
    coid = ex._build_coid('2026-08-11', 'AKTS', 'S9_dual_momentum', False, '')
    assert coid == 'AX20260811_AKTS_S9_dual_momentum'


def test_coid_two_waves_differ():
    a = ex._build_coid('2026-08-11', 'AKTS', 'S9', False, '140631')
    b = ex._build_coid('2026-08-11', 'AKTS', 'S9', False, '190745')
    assert a != b, 'each wave must mint its own coid (own audit row at upsert)'


def test_coid_truncation_preserves_both_suffixes():
    """2026-05-21 USO lesson: truncation must hit the sid segment only, never
    the suffix markers — even with the wave tag added to the budget."""
    coid = ex._build_coid('2026-08-11', 'USO', 'S_' + 'x' * 200, True, '190745')
    assert len(coid) <= 128
    assert coid.endswith('_w190745_C')


# ── already_executed wave scoping ────────────────────────────────────────────

class _CapturingCursor:
    def __init__(self, log):
        self._log = log
    def execute(self, sql, params):
        self._log.append((' '.join(sql.split()), list(params)))
    def fetchone(self):
        return None
    def close(self):
        return None


class _CapturingConn:
    def __init__(self):
        self.log = []
    def cursor(self):
        return _CapturingCursor(self.log)


def test_already_executed_legacy_no_wave_clause():
    conn = _CapturingConn()
    ex.already_executed(conn, '2026-08-11', 'S9', 'AKTS')
    sql, params = conn.log[0]
    assert 'strpos' not in sql, 'empty wave must keep whole-day dedup SQL'
    assert params == ['2026-08-11', 'S9', 'AKTS']


def test_already_executed_wave_scopes_to_coid_tag():
    conn = _CapturingConn()
    ex.already_executed(conn, '2026-08-11', 'S9', 'AKTS', '190745')
    sql, params = conn.log[0]
    assert 'strpos(client_order_id, %s) > 0' in sql
    assert params == ['2026-08-11', 'S9', 'AKTS', '_w190745']
    assert sql.rstrip().endswith('LIMIT 1')
