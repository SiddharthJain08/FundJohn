"""split_watcher v2 — UVIX-incident regression tests (mock-only).

The 2026-07-01 UVIX 1-for-20 reverse split went undetected because v1 read
the dead corporate_actions.parquet (newest split row 2024). v2 queries the
Alpaca announcements API with a lookahead window and flags HELD tickers.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'split_watcher', ROOT / 'scripts' / 'split_watcher.py')
sw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sw)

UVIX_ANN = {
    'target_symbol': 'UVIX', 'ex_date': '2026-07-01',
    'old_rate': '20', 'new_rate': '1', 'ca_sub_type': 'reverse_split',
}


def _cli_router(announcements, positions):
    def fake_cli(args, timeout=30):
        if args[0] == 'corporate-action':
            return True, announcements
        if args[0] == 'position':
            return True, positions
        raise AssertionError(f'unexpected CLI args {args}')
    return fake_cli


def test_fmt_ratio():
    assert sw._fmt_ratio('20', '1') == '1-for-20 (REVERSE)'
    assert sw._fmt_ratio(1, 10) == '10-for-1'
    assert sw._fmt_ratio(None, 'x') == '?'


def test_upcoming_held_split_alerts_tonight(tmp_path, monkeypatch, capsys):
    """A split effective TOMORROW on a HELD ticker must alert tonight with
    the stop-cancellation warning — the exact UVIX gap."""
    monkeypatch.setattr(sw, 'PENDING', tmp_path / 'pending.txt')
    monkeypatch.setattr(sw, 'covered_tickers', lambda: {'UVIX', 'AAPL'})
    posted = []
    monkeypatch.setattr(sw, 'notify', lambda m: posted.append(m))
    with mock.patch.object(sw, '_alpaca_cli',
                           side_effect=_cli_router([UVIX_ANN], [{'symbol': 'UVIX'}])), \
         mock.patch.object(sw, 'date') as mock_date:
        mock_date.today.return_value = pd.Timestamp('2026-06-30').date()
        assert sw.main() == 0
    assert posted, 'must alert'
    msg = posted[0]
    assert 'UVIX' in msg and '1-for-20' in msg and 'HELD' in msg
    assert 'auto-cancelled' in msg
    assert not (tmp_path / 'pending.txt').exists(), 'future split must not queue yet'


def test_effective_split_queues_supersede(tmp_path, monkeypatch):
    """ex_date <= today: history is stale -> queue the supersede re-backfill."""
    pending = tmp_path / 'pending.txt'
    monkeypatch.setattr(sw, 'PENDING', pending)
    monkeypatch.setattr(sw, 'covered_tickers', lambda: {'UVIX'})
    posted = []
    monkeypatch.setattr(sw, 'notify', lambda m: posted.append(m))
    with mock.patch.object(sw, '_alpaca_cli',
                           side_effect=_cli_router([UVIX_ANN], [])), \
         mock.patch.object(sw, 'date') as mock_date:
        mock_date.today.return_value = pd.Timestamp('2026-07-01').date()
        assert sw.main() == 0
    assert 'UVIX ex=2026-07-01' in pending.read_text()
    assert 'supersede' in posted[0]

    # Idempotent: a second run (e.g. the -3d lookback window) must not re-queue.
    posted.clear()
    with mock.patch.object(sw, '_alpaca_cli',
                           side_effect=_cli_router([UVIX_ANN], [])), \
         mock.patch.object(sw, 'date') as mock_date:
        mock_date.today.return_value = pd.Timestamp('2026-07-02').date()
        assert sw.main() == 0
    assert pending.read_text().count('UVIX ex=2026-07-01') == 1


def test_api_failure_falls_open_to_parquet(monkeypatch, tmp_path):
    """CLI down -> legacy parquet path still runs (fail-open, no crash)."""
    monkeypatch.setattr(sw, 'PENDING', tmp_path / 'pending.txt')
    legacy_called = []
    monkeypatch.setattr(sw, 'find_new_splits',
                        lambda today: legacy_called.append(today) or [])
    with mock.patch.object(sw, '_alpaca_cli', return_value=(False, 'boom')):
        assert sw.main() == 0
    assert legacy_called, 'legacy parquet source must be consulted on API failure'


def test_uncovered_symbols_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(sw, 'PENDING', tmp_path / 'pending.txt')
    monkeypatch.setattr(sw, 'covered_tickers', lambda: {'AAPL'})
    posted = []
    monkeypatch.setattr(sw, 'notify', lambda m: posted.append(m))
    with mock.patch.object(sw, '_alpaca_cli',
                           side_effect=_cli_router([UVIX_ANN], [])):
        assert sw.main() == 0
    assert not posted
