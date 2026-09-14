# tests/scripts/test_measure_gap_fill_impact.py
"""The re-pricing arithmetic of scripts/measure_gap_fill_impact.py. Pure
function only — the script's DB and parquet reads are never exercised here."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _mod():
    spec = importlib.util.spec_from_file_location(
        'mgfi', ROOT / 'scripts' / 'measure_gap_fill_impact.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_long_gap_below_stop_is_repriced_at_the_open():
    m = _mod()
    assert m.reprice_stop_exit('long', 100.0, 98.0, 95.0) == pytest.approx(-0.05)


def test_long_open_above_stop_is_not_a_gap():
    m = _mod()
    assert m.reprice_stop_exit('long', 100.0, 98.0, 99.0) is None
    assert m.reprice_stop_exit('long', 100.0, 98.0, 98.0) is None


def test_short_gap_above_stop_is_repriced_at_the_open():
    m = _mod()
    assert m.reprice_stop_exit('short', 100.0, 102.0, 106.0) == pytest.approx(-0.06)


def test_short_open_below_stop_is_not_a_gap():
    m = _mod()
    assert m.reprice_stop_exit('short', 100.0, 102.0, 101.0) is None


def test_non_finite_and_zero_entry_are_rejected():
    m = _mod()
    assert m.reprice_stop_exit('long', 0.0, 98.0, 95.0) is None
    assert m.reprice_stop_exit('long', 100.0, float('nan'), 95.0) is None
    assert m.reprice_stop_exit('long', 100.0, 98.0, float('nan')) is None
    assert m.reprice_stop_exit('sideways', 100.0, 98.0, 95.0) is None
