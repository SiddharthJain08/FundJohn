"""tests/execution/test_stop_reattach_pagination.py — 2026-08-18.

stop_reattach's open-order coverage reads used a single `--limit 500` page.
The 08-13 breadth wave pushed the open book to 3,696 orders, so EVERY pass
truncated and the naked-position audit was unreliable (the cap warning fired
each run). Coverage reads now keyset-paginate with --after-order-id until a
short page. Pins:
  1. Page args: --status open --limit 500 --direction asc.
  2. Full page (== cap) ⇒ next call carries --after-order-id <last id>;
     terminates on the first short page; all rows returned, deduped by id.
  3. First-page failure ⇒ ok=False (callers treat as listing unavailable).
  4. Later-page failure ⇒ ok=True with the rows gathered so far.
  5. Stall guard: a full page with no cursor progress terminates.

All CLI access is stubbed; no live Alpaca contact.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import stop_reattach as sr  # noqa: E402

CAP = sr._OPEN_ORDERS_CAP


def _page(start, n, prefix='oid'):
    return [{'id': f'{prefix}-{i}', 'symbol': 'AAPL'} for i in range(start, start + n)]


class TestFetchOpenOrdersPagination(unittest.TestCase):
    def test_single_short_page_one_call(self):
        pages = [(True, _page(0, 3), None)]
        calls = []

        def _cli(args, timeout=15):
            calls.append(list(args))
            return pages.pop(0)

        with patch.object(sr, '_run_cli', side_effect=_cli):
            ok, orders, err = sr._fetch_open_orders()
        self.assertTrue(ok)
        self.assertEqual(len(orders), 3)
        self.assertEqual(len(calls), 1)
        joined = ' '.join(calls[0])
        self.assertIn('--status open', joined)
        self.assertIn('--limit 500', joined)
        self.assertIn('--direction asc', joined)
        self.assertNotIn('--after-order-id', joined)

    def test_full_pages_cursor_advances_and_terminates(self):
        pages = [
            (True, _page(0, CAP), None),          # full page 1
            (True, _page(CAP, CAP), None),        # full page 2
            (True, _page(2 * CAP, 7), None),      # short page 3 → stop
        ]
        calls = []

        def _cli(args, timeout=15):
            calls.append(list(args))
            return pages.pop(0)

        with patch.object(sr, '_run_cli', side_effect=_cli):
            ok, orders, err = sr._fetch_open_orders()
        self.assertTrue(ok)
        self.assertEqual(len(orders), 2 * CAP + 7)
        self.assertEqual(len(calls), 3)
        # keyset cursor: page 2 asks after page 1's LAST id, page 3 after page 2's.
        self.assertIn('--after-order-id', calls[1])
        self.assertEqual(calls[1][calls[1].index('--after-order-id') + 1],
                         f'oid-{CAP - 1}')
        self.assertEqual(calls[2][calls[2].index('--after-order-id') + 1],
                         f'oid-{2 * CAP - 1}')
        # dedupe holds: all ids unique
        self.assertEqual(len({o['id'] for o in orders}), len(orders))

    def test_first_page_failure_is_not_ok(self):
        with patch.object(sr, '_run_cli',
                          return_value=(False, None, {'error': 'boom'})):
            ok, orders, err = sr._fetch_open_orders()
        self.assertFalse(ok)
        self.assertEqual(orders, [])
        self.assertEqual((err or {}).get('error'), 'boom')

    def test_later_page_failure_returns_partial(self):
        pages = [
            (True, _page(0, CAP), None),
            (False, None, {'error': 'rate limited'}),
        ]
        with patch.object(sr, '_run_cli', side_effect=lambda a, timeout=15: pages.pop(0)):
            ok, orders, err = sr._fetch_open_orders()
        self.assertTrue(ok)                      # partial beats none
        self.assertEqual(len(orders), CAP)

    def test_stalled_cursor_terminates(self):
        """A full page that repeats the same ids (no cursor progress) must
        terminate, not loop to the page bound."""
        same = _page(0, CAP)
        calls = []

        def _cli(args, timeout=15):
            calls.append(list(args))
            return (True, same, None)

        with patch.object(sr, '_run_cli', side_effect=_cli):
            ok, orders, err = sr._fetch_open_orders()
        self.assertTrue(ok)
        self.assertEqual(len(orders), CAP)       # deduped, not doubled
        self.assertEqual(len(calls), 2)          # page 2 stalls → stop


if __name__ == '__main__':
    unittest.main()
