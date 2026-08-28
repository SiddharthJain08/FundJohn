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
    rows = replay.rows_from_trades(t, {(date(2026, 6, 1), 'A', 'LONG'): [sig]})
    assert rows[0]['signal_params'] == {'pair': 'A/B', 'z': 2.2} and rows[0]['direction'] == 'LONG'
    assert rows[0]['target_date'] == date(2026, 6, 1) and rows[0]['mark_entry_price'] == 10.0
    assert rows[0]['trade_seq'] == 1
    assert replay.rows_from_trades(t, {}) == []          # unrecoverable → skipped, not fabricated


def test_rows_from_trades_recovers_via_partner_leg_not_a_closed_siblings_signal():
    # Round 3 (final review) repro of the three "unexplained" early exits.
    # X1 opened TWO pairs the same day on the same ticker in the same
    # direction: A/X (trade_seq 1046, partner leg X at 1045) and A/Y
    # (trade_seq 1073, partner leg Y at 1074). By the replay date 1046 has
    # already CLOSED, so it is NOT in open_trades and never consumes its
    # signal — FIFO therefore handed A/X's signal (with A/X's beta/alpha/z)
    # to the surviving A/Y trade, which then exited early on the wrong pair.
    # The partner leg is found in the FULL trade list, so 1073 gets 'A/Y'.
    ed = date(2026, 6, 1)
    def _t(seq, ticker, direction, exit_date, ep, sl, tg):
        return {'trade_seq': seq, 'ticker': ticker, 'direction': direction, 'entry_date': ed,
                'exit_date': exit_date, 'entry_price': ep, 'signal_stop': sl, 'signal_target': tg}
    closed_sibling = _t(1046, 'A', 'long', date(2026, 6, 4), 10.0, 9.0, 12.0)
    survivor       = _t(1073, 'A', 'long', date(2026, 6, 20), 10.5, 9.4, 12.6)
    all_trades = [_t(1045, 'X', 'short', date(2026, 6, 4), 20.0, 22.0, 18.0),
                  closed_sibling, survivor,
                  _t(1074, 'Y', 'short', date(2026, 6, 20), 30.0, 33.0, 27.0)]
    def _sig(pair, ep, sl, tg):
        return Signal(ticker='A', direction='LONG', entry_price=ep, stop_loss=sl, target_1=tg,
                      target_2=0.0, target_3=0.0, position_size_pct=0.0, confidence='MED',
                      signal_params={'pair': pair})
    # queue order puts the CLOSED sibling's signal first — the FIFO trap
    cache = {(ed, 'A', 'LONG'): [_sig('A/X', 10.0, 9.0, 12.0), _sig('A/Y', 10.5, 9.4, 12.6)]}
    rows = replay.rows_from_trades([survivor], cache, all_trades=all_trades)
    assert len(rows) == 1 and rows[0]['trade_seq'] == 1073
    assert rows[0]['signal_params'] == {'pair': 'A/Y'}
    # both legs open on the same day still each get their own pair's signal
    rows2 = replay.rows_from_trades([closed_sibling, survivor], cache, all_trades=all_trades)
    by_seq = {r['trade_seq']: r['signal_params']['pair'] for r in rows2}
    assert by_seq == {1046: 'A/X', 1073: 'A/Y'}


def test_rows_from_trades_falls_back_to_fingerprint_then_fifo():
    # No partner leg in the pool (single-leg trade list) -> the exact
    # (entry_price, stop, target) fingerprint picks the right signal even
    # though the other one is first in the queue.
    ed = date(2026, 6, 1)
    t = [{'trade_seq': 9, 'ticker': 'A', 'direction': 'long', 'entry_date': ed,
          'exit_date': date(2026, 6, 20), 'entry_price': 10.5, 'signal_stop': 9.4, 'signal_target': 12.6}]
    def _sig(pair, ep, sl, tg):
        return Signal(ticker='A', direction='LONG', entry_price=ep, stop_loss=sl, target_1=tg,
                      target_2=0.0, target_3=0.0, position_size_pct=0.0, confidence='MED',
                      signal_params={'pair': pair})
    cache = {(ed, 'A', 'LONG'): [_sig('A/X', 10.0, 9.0, 12.0), _sig('A/Y', 10.5, 9.4, 12.6)]}
    assert replay.rows_from_trades(t, cache)[0]['signal_params'] == {'pair': 'A/Y'}
    # nothing matches either way -> FIFO, as before (never fabricated)
    cache2 = {(ed, 'A', 'LONG'): [_sig('A/Z', 1.0, 2.0, 3.0)]}
    assert replay.rows_from_trades(t, cache2)[0]['signal_params'] == {'pair': 'A/Z'}


def test_rows_from_trades_matches_same_ticker_trades_in_order():
    # Round 2's repro, retained: two concurrently-open trades, same ticker and
    # entry date, OPPOSITE directions. Recovery queues are keyed by
    # (entry_date, ticker, direction), so queue order reversed relative to
    # trade_seq order must still resolve seq 3 (LONG) -> 'A/B' and seq 5
    # (SHORT) -> 'A/C', with neither trade losing its signal to the other.
    t = [{'trade_seq': 5, 'ticker': 'A', 'direction': 'short', 'entry_date': date(2026, 6, 1), 'exit_date': date(2026, 6, 6),
          'entry_price': 11.0, 'signal_stop': 12.0, 'signal_target': 9.0},
         {'trade_seq': 3, 'ticker': 'A', 'direction': 'long', 'entry_date': date(2026, 6, 1), 'exit_date': date(2026, 6, 5),
          'entry_price': 10.0, 'signal_stop': 9.0, 'signal_target': 12.0}]
    sig_short = Signal(ticker='A', direction='SHORT', entry_price=11.0, stop_loss=12.0, target_1=9.0, target_2=0.0,
                        target_3=0.0, position_size_pct=0.0, confidence='MED', signal_params={'pair': 'A/C'})
    sig_long = Signal(ticker='A', direction='LONG', entry_price=10.0, stop_loss=9.0, target_1=12.0, target_2=0.0,
                       target_3=0.0, position_size_pct=0.0, confidence='MED', signal_params={'pair': 'A/B'})
    rows = replay.rows_from_trades(t, {(date(2026, 6, 1), 'A', 'SHORT'): [sig_short],
                                        (date(2026, 6, 1), 'A', 'LONG'): [sig_long]})
    by_seq = {r['trade_seq']: r for r in rows}
    assert by_seq[3]['signal_params'] == {'pair': 'A/B'}
    assert by_seq[5]['signal_params'] == {'pair': 'A/C'}


def test_rows_from_trades_skips_trade_without_same_direction_signal():
    # One LONG trade; only a SHORT signal exists for that (entry_date,
    # ticker). Must be skipped (unrecoverable), never matched cross-direction.
    t = [{'trade_seq': 7, 'ticker': 'A', 'direction': 'long', 'entry_date': date(2026, 6, 1), 'exit_date': date(2026, 6, 5),
          'entry_price': 10.0, 'signal_stop': 9.0, 'signal_target': 12.0}]
    sig_short = Signal(ticker='A', direction='SHORT', entry_price=11.0, stop_loss=12.0, target_1=9.0, target_2=0.0,
                        target_3=0.0, position_size_pct=0.0, confidence='MED', signal_params={'pair': 'A/C'})
    rows = replay.rows_from_trades(t, {(date(2026, 6, 1), 'A', 'SHORT'): [sig_short]})
    assert rows == []


def test_compare():
    assert replay.compare({1: 'strategy_exit:z_revert', 2: 'max_hold'},
                          {1: 'strategy_exit:z_revert', 2: 'strategy_exit:pair_decohered', 3: 'max_hold'}) == (1, 2)
