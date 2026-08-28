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
    t = [{'trade_seq': 1, 'ticker': 'A', 'direction': 'long', 'entry_date': date(2026, 6, 1), 'exit_date': date(2026, 6, 5),
          'entry_price': 10.0, 'signal_stop': 9.0, 'signal_target': 12.0}]
    sig = Signal(ticker='A', direction='LONG', entry_price=10.0, stop_loss=9.0, target_1=12.0, target_2=0.0,
                 target_3=0.0, position_size_pct=0.0, confidence='MED', signal_params={'pair': 'A/B', 'z': 2.2})
    rows = replay.rows_from_trades(t, {(date(2026, 6, 1), 'A'): [sig]})
    assert rows[0]['signal_params'] == {'pair': 'A/B', 'z': 2.2} and rows[0]['direction'] == 'LONG'
    assert rows[0]['target_date'] == date(2026, 6, 1) and rows[0]['mark_entry_price'] == 10.0
    assert rows[0]['trade_seq'] == 1
    assert replay.rows_from_trades(t, {}) == []          # unrecoverable → skipped, not fabricated


def test_rows_from_trades_matches_same_ticker_trades_in_order():
    # Two concurrently-open trades, same ticker, same entry date, different
    # pairs. The recovered signals must be matched to their OWN trade by
    # (trade_seq order, direction) — not collapsed by ticker.
    t = [{'trade_seq': 5, 'ticker': 'A', 'direction': 'short', 'entry_date': date(2026, 6, 1), 'exit_date': date(2026, 6, 6),
          'entry_price': 11.0, 'signal_stop': 12.0, 'signal_target': 9.0},
         {'trade_seq': 3, 'ticker': 'A', 'direction': 'long', 'entry_date': date(2026, 6, 1), 'exit_date': date(2026, 6, 5),
          'entry_price': 10.0, 'signal_stop': 9.0, 'signal_target': 12.0}]
    sig_long = Signal(ticker='A', direction='LONG', entry_price=10.0, stop_loss=9.0, target_1=12.0, target_2=0.0,
                       target_3=0.0, position_size_pct=0.0, confidence='MED', signal_params={'pair': 'A/B'})
    sig_short = Signal(ticker='A', direction='SHORT', entry_price=11.0, stop_loss=12.0, target_1=9.0, target_2=0.0,
                        target_3=0.0, position_size_pct=0.0, confidence='MED', signal_params={'pair': 'A/C'})
    rows = replay.rows_from_trades(t, {(date(2026, 6, 1), 'A'): [sig_long, sig_short]})
    by_seq = {r['trade_seq']: r for r in rows}
    assert by_seq[3]['signal_params'] == {'pair': 'A/B'}
    assert by_seq[5]['signal_params'] == {'pair': 'A/C'}


def test_compare():
    assert replay.compare({1: 'strategy_exit:z_revert', 2: 'max_hold'},
                          {1: 'strategy_exit:z_revert', 2: 'strategy_exit:pair_decohered', 3: 'max_hold'}) == (1, 2)
