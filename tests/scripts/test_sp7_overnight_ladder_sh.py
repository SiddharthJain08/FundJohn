"""SP-7 Phase B Task 11 — wrapper invariants (static text assertions)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SH = (ROOT / 'scripts' / 'overnight_ladder.sh').read_text()


def test_sentinel_gate_and_disarm():
    assert '.sp7_ladder_armed' in SH
    assert 'rm -f "$ARMED"' in SH


def test_backfill_priority_guard():
    assert '.sp7_backfill_armed' in SH and 'yielding' in SH


def test_window_discipline():
    # CONTINUOUS MODE (operator directive 2026-06-10): the 13:00 UTC window
    # cap and in-script `timeout --signal=TERM` were removed — discipline is
    # now the 20-min resurrection timer + the backfill-yield guard, with the
    # driver always niced below live work.
    assert 'nice -n 19' in SH
    assert 'timeout --signal=TERM' not in SH
    assert 'CONTINUOUS MODE' in SH


def test_done_grep_matches_driver_output():
    assert '\\[ladder\\] DONE' in SH
