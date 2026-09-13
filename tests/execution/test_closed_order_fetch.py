"""fetch_recent_closed_orders — the 200-row closed-order read replacement.

The spec asked for the --after-order-id keyset loop stop_reattach._fetch_open_orders
uses. That cursor is ASCENDING and inception-anchored: on --status open the set is
bounded by the live book, but on --status closed it starts at the oldest order the
account ever had. A 10-minute timer would spend its whole page budget on ancient
history and never reach today's fills. The CLI exposes no time bound, so the correct
shape is the DEFAULT newest-first window widened to 500 rows, plus --symbols-scoped
reads that spend a fresh window on the names we actually care about.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import stop_reattach as sr  # noqa: E402


def _recorder(pages, calls):
    """Fake _run_cli: returns pages.pop(0) per call, recording argv."""
    def _cli(args, timeout=15):
        calls.append(list(args))
        if not pages:
            return True, [], None
        nxt = pages.pop(0)
        if nxt is None:
            return False, None, {'error': 'boom'}
        return True, nxt, None
    return _cli


def test_unscoped_read_is_newest_first_not_keyset(monkeypatch):
    calls = []
    monkeypatch.setattr(sr, '_run_cli', _recorder([[{'id': 'o1'}]], calls))
    ok, orders = sr.fetch_recent_closed_orders()
    assert ok is True and [o['id'] for o in orders] == ['o1']
    argv = calls[0]
    assert argv[:2] == ['order', 'list']
    assert '--status' in argv and argv[argv.index('--status') + 1] == 'closed'
    assert '--nested' in argv
    assert argv[argv.index('--limit') + 1] == '500'
    assert '--direction' not in argv, 'asc on closed orders walks from account inception'
    assert '--after-order-id' not in argv, 'keyset cursor is ascending-only'


def test_symbol_scoped_reads_are_chunked(monkeypatch):
    calls = []
    syms = [f'T{i:03d}' for i in range(250)]
    monkeypatch.setattr(sr, '_run_cli', _recorder([[], [], [], []], calls))
    ok, _ = sr.fetch_recent_closed_orders(syms, include_unscoped=False, chunk=100)
    assert ok is True
    assert len(calls) == 3
    groups = [c[c.index('--symbols') + 1].split(',') for c in calls]
    assert [len(g) for g in groups] == [100, 100, 50]
    assert sorted(sum(groups, [])) == sorted(syms)


def test_dedupes_by_order_id_across_reads(monkeypatch):
    calls = []
    pages = [[{'id': 'dup'}, {'id': 'a'}], [{'id': 'dup'}, {'id': 'b'}]]
    monkeypatch.setattr(sr, '_run_cli', _recorder(pages, calls))
    ok, orders = sr.fetch_recent_closed_orders(['AAA'])
    assert ok is True
    assert [o['id'] for o in orders] == ['dup', 'a', 'b']


def test_partial_failure_still_returns_ok_with_coverage(monkeypatch):
    calls = []
    monkeypatch.setattr(sr, '_run_cli', _recorder([[{'id': 'a'}], None], calls))
    ok, orders = sr.fetch_recent_closed_orders(['AAA'])
    assert ok is True and [o['id'] for o in orders] == ['a']


def test_ok_false_only_when_every_read_failed(monkeypatch):
    calls = []
    monkeypatch.setattr(sr, '_run_cli', _recorder([None, None], calls))
    ok, orders = sr.fetch_recent_closed_orders(['AAA'])
    assert ok is False and orders == []


def test_no_symbols_and_no_unscoped_is_a_clean_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(sr, '_run_cli', _recorder([], calls))
    ok, orders = sr.fetch_recent_closed_orders(None, include_unscoped=False)
    assert ok is False and orders == [] and calls == []
