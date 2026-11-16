"""_fetch_account_state retry-on-429 (2026-11-16).

The executor re-fetches the account right after its close/cancel burst to
size the DTBP opening budget; Alpaca answered 429 on 10-13, 10-19, 11-13 and
11-16, the fetch fell back to PAPER_PORTFOLIO defaults with fetched=False,
and the guard skipped EVERY open (339 / 392 orders). The fetch must retry
rate-limit and transient errors with backoff before giving up.
"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import alpaca_trader as at  # noqa: E402


def _resp(status, headers=None, body=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    if status >= 400:
        err = requests.HTTPError(f'{status} Client Error: x for url: y')
        err.response = r
        r.raise_for_status.side_effect = err
    else:
        r.raise_for_status.return_value = None
    r.json.return_value = body or {
        'equity': '91308.01', 'cash': '75733.64', 'long_market_value': '17622.82',
        'short_market_value': '-1819.54', 'buying_power': '336332.36',
        'regt_buying_power': '161088.66', 'non_marginable_buying_power': '72610.52',
        'initial_margin': '9970.28', 'maintenance_margin': '7224.92', 'multiplier': '4',
        'status': 'ACTIVE', 'shorting_enabled': True,
    }
    return r


def _sess(responses):
    s = MagicMock()
    s._base = 'https://paper-api.alpaca.markets'
    it = iter(responses)

    def _get(url, timeout=10):
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
    s.get.side_effect = _get
    return s


def test_429_then_200_is_fetched_and_honours_retry_after():
    sleeps = []
    st = at._fetch_account_state(_sess([_resp(429, {'Retry-After': '2'}), _resp(200)]),
                                 _sleep=sleeps.append)
    assert st['fetched'] is True
    assert st['regt_buying_power'] == pytest.approx(161088.66)
    assert st['buying_power'] == pytest.approx(336332.36)
    assert sleeps == [2.0]


def test_persistent_429_exhausts_the_ladder_and_fails_safe():
    sleeps = []
    st = at._fetch_account_state(_sess([_resp(429)] * 10), retries=3, _sleep=sleeps.append)
    assert st['fetched'] is False
    assert '429' in st['fetch_error']
    assert sleeps == list(at.ACCOUNT_FETCH_BACKOFF[:3])      # one sleep per retry
    assert st['regt_buying_power'] == at.PAPER_PORTFOLIO     # defaults untouched


def test_retry_after_is_capped():
    sleeps = []
    at._fetch_account_state(_sess([_resp(429, {'Retry-After': '120'}), _resp(200)]),
                            _sleep=sleeps.append)
    assert sleeps == [at.ACCOUNT_FETCH_RETRY_AFTER_CAP]


def test_5xx_and_connection_errors_are_retried():
    sleeps = []
    st = at._fetch_account_state(
        _sess([_resp(503), requests.ConnectionError('reset'), requests.Timeout('t'), _resp(200)]),
        retries=3, _sleep=sleeps.append)
    assert st['fetched'] is True
    assert len(sleeps) == 3


def test_non_retryable_http_error_does_not_retry():
    sleeps = []
    sess = _sess([_resp(401), _resp(200)])
    st = at._fetch_account_state(sess, _sleep=sleeps.append)
    assert st['fetched'] is False
    assert sleeps == []
    assert sess.get.call_count == 1


def test_default_retry_count_and_ladder_shape():
    # 3 retries, ladder long enough for them, cap sane: the executor's
    # opening tier runs ~2 min before the close, so the whole ladder must
    # stay well under a minute.
    assert at.ACCOUNT_FETCH_RETRIES == 3
    assert len(at.ACCOUNT_FETCH_BACKOFF) >= at.ACCOUNT_FETCH_RETRIES
    assert sum(at.ACCOUNT_FETCH_BACKOFF[:at.ACCOUNT_FETCH_RETRIES]) + \
        at.ACCOUNT_FETCH_RETRIES * at.ACCOUNT_FETCH_RETRY_AFTER_CAP <= 60
