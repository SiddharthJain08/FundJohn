from __future__ import annotations
import importlib.util, sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
spec = importlib.util.spec_from_file_location('replay', ROOT / 'scripts' / 'exit_hook_live_replay.py')
replay = importlib.util.module_from_spec(spec); spec.loader.exec_module(replay)
from strategies.base import Signal


def test_open_trades_on():
    t = [{'ticker': 'A', 'entry_date': date(2026, 6, 1), 'exit_date': date(2026, 6, 5)},
         {'ticker': 'B', 'entry_date': date(2026, 6, 3), 'exit_date': date(2026, 6, 4)}]
    assert [x['ticker'] for x in replay.open_trades_on(t, date(2026, 6, 2))] == ['A']
    assert [x['ticker'] for x in replay.open_trades_on(t, date(2026, 6, 4))] == ['A', 'B']
    assert replay.open_trades_on(t, date(2026, 6, 6)) == []


def test_rows_from_trades_uses_recovered_signal_params():
    t = [{'ticker': 'A', 'direction': 'long', 'entry_date': date(2026, 6, 1), 'exit_date': date(2026, 6, 5),
          'entry_price': 10.0, 'signal_stop': 9.0, 'signal_target': 12.0}]
    sig = Signal(ticker='A', direction='LONG', entry_price=10.0, stop_loss=9.0, target_1=12.0, target_2=0.0,
                 target_3=0.0, position_size_pct=0.0, confidence='MED', signal_params={'pair': 'A/B', 'z': 2.2})
    rows = replay.rows_from_trades(t, {(date(2026, 6, 1), 'A'): sig})
    assert rows[0]['signal_params'] == {'pair': 'A/B', 'z': 2.2} and rows[0]['direction'] == 'LONG'
    assert rows[0]['target_date'] == date(2026, 6, 1) and rows[0]['mark_entry_price'] == 10.0
    assert replay.rows_from_trades(t, {}) == []          # unrecoverable → skipped, not fabricated


def test_compare():
    assert replay.compare({'A': 'strategy_exit:z_revert', 'B': 'max_hold'},
                          {'A': 'strategy_exit:z_revert', 'B': 'strategy_exit:pair_decohered', 'C': 'max_hold'}) == (1, 2)
