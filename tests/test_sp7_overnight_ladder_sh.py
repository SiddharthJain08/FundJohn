"""SP-7 Phase B Task 11 — wrapper invariants (static text assertions)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SH = (ROOT / 'scripts' / 'overnight_ladder.sh').read_text()


def test_sentinel_gate_and_disarm():
    assert '.sp7_ladder_armed' in SH
    assert 'rm -f "$ARMED"' in SH


def test_backfill_priority_guard():
    assert '.sp7_backfill_armed' in SH and 'yielding' in SH


def test_window_discipline():
    assert 'timeout --signal=TERM' in SH and 'nice -n 19' in SH
    assert '13:00' in SH and 'rc -eq 124' in SH.replace('$', '')


def test_done_grep_matches_driver_output():
    assert '\\[ladder\\] DONE' in SH
