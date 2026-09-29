"""M1 (2026-09-29): options last_price comes from the in-memory HV20 price
window, never from a full prices.parquet read. Hermetic — no masters, no DB.
Run: nice -n 19 python3 -m pytest tests/execution/test_engine_last_price_window.py -q
"""
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from execution.engine import _last_price_from_window  # noqa: E402


def _win(rows):
    return pd.DataFrame(rows, columns=['ticker', 'date', 'close'])


def test_latest_close_per_ticker_even_when_rows_are_unordered():
    w = _win([('AAA', '2026-09-24', 10.0), ('AAA', '2026-09-22', 9.0), ('AAA', '2026-09-23', 9.5),
              ('BBB', '2026-09-23', 20.0), ('BBB', '2026-09-24', 21.0)])
    w['date'] = pd.to_datetime(w['date'])
    lp = _last_price_from_window(w)
    assert lp == {'AAA': 10.0, 'BBB': 21.0}


def test_nan_last_close_falls_back_to_previous_valid():
    w = _win([('AAA', '2026-09-23', 9.5), ('AAA', '2026-09-24', float('nan'))])
    w['date'] = pd.to_datetime(w['date'])
    assert _last_price_from_window(w) == {'AAA': 9.5}


def test_unavailable_window_is_none_and_wrong_shape_is_none():
    assert _last_price_from_window(None) is None
    assert _last_price_from_window(pd.DataFrame()) is None
    assert _last_price_from_window(pd.DataFrame({'ticker': ['A'], 'close': [1.0]})) is None


def test_engine_no_longer_reads_the_full_master_for_last_price():
    src = (Path(__file__).resolve().parents[2] / 'src' / 'execution' / 'engine.py').read_text()
    assert "read_parquet(_px_path, columns=['ticker','close'])" not in src
    assert '_last_price_from_window(_px_window)' in src
