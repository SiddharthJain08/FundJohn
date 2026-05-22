"""Unit tests for scripts/seed_first_seen_at_from_fmp_ipo.py.

All tests use monkeypatch + tmp_path; no live HTTP, no live DB. Pattern
mirrors tests/test_build_backfill_universe.py.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import seed_first_seen_at_from_fmp_ipo as seed


# ── Fake HTTP layer ───────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _fake_requests(mapping: dict[str, _FakeResp]):
    """Return a callable that mimics requests.get(url, params=..., timeout=...).

    `mapping` is keyed by ticker symbol.
    """
    def _get(url, params=None, timeout=None):
        symbol = (params or {}).get('symbol')
        if symbol not in mapping:
            return _FakeResp(404, [])
        return mapping[symbol]
    return _get


def _fake_pg_with_first_seen(rows: dict[str, datetime]):
    """Build a mock psycopg2 conn where SELECT first_seen_at returns the date
    in `rows` for the given symbol, and UPDATE statements are tracked."""
    updates: list[tuple] = []
    conn = MagicMock()

    def _make_cursor():
        cur = MagicMock()
        state = {'last_select_symbol': None}

        def _execute(sql, params=()):
            sql_lc = sql.lower()
            if sql_lc.startswith('select first_seen_at'):
                state['last_select_symbol'] = params[0]
                ts = rows.get(params[0])
                cur.fetchone.return_value = (ts,) if ts is not None else None
            elif sql_lc.startswith('update alpaca_tradable_universe'):
                updates.append(params)  # (ipo_dt, symbol)
            return None
        cur.execute.side_effect = _execute
        cur.__enter__ = lambda s: cur
        cur.__exit__ = lambda s, *a: False
        return cur

    conn.cursor.side_effect = _make_cursor
    return conn, updates


def _args(**overrides) -> SimpleNamespace:
    base = {
        'top_n': None,
        'tickers': None,
        'dry_run': False,
        'force': False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _setup_env(monkeypatch, tmp_path: Path):
    """Common env scaffolding: marker → tmp_path, env vars set."""
    monkeypatch.setattr(seed, 'MARKER', tmp_path / '.fmp_ipo_seeded_v1.txt')
    monkeypatch.setattr(seed, 'INTER_CALL_SLEEP_S', 0.0)  # speed tests up
    monkeypatch.setattr(seed, 'RETRY_BACKOFFS_S', (0.0, 0.0, 0.0))
    monkeypatch.setenv('POSTGRES_URI', 'postgres://fake/test')
    monkeypatch.setenv('FMP_API_KEY', 'fake_key')


# ── Tests ─────────────────────────────────────────────────────────────────────
def test_updates_first_seen_at_when_ipo_earlier(monkeypatch, tmp_path, capsys):
    _setup_env(monkeypatch, tmp_path)
    # Current first_seen_at = 2026-05-14; FMP says ipoDate = 1980-12-12.
    current = datetime(2026, 5, 14, tzinfo=timezone.utc)
    conn, updates = _fake_pg_with_first_seen({'AAPL': current})
    monkeypatch.setattr(seed.psycopg2, 'connect', lambda *a, **kw: conn)
    monkeypatch.setattr(seed.requests, 'get',
                        _fake_requests({'AAPL': _FakeResp(200, [{'ipoDate': '1980-12-12'}])}))

    rc = seed.main(_args(tickers='AAPL'))

    assert rc == 0
    assert len(updates) == 1
    ipo_dt, symbol = updates[0]
    assert symbol == 'AAPL'
    assert ipo_dt == datetime(1980, 12, 12, tzinfo=timezone.utc)
    out = capsys.readouterr().out
    assert 'updated:         1' in out
    assert '45.' in out or '44.' in out  # ~45 years backfilled


def test_leaves_unchanged_when_ipo_later_than_current(monkeypatch, tmp_path, capsys):
    """LEAST semantics: if FMP ipoDate is AFTER current first_seen_at, no UPDATE."""
    _setup_env(monkeypatch, tmp_path)
    # Current = 2020-01-01; FMP ipoDate = 2024-01-01 (later → leave alone).
    current = datetime(2020, 1, 1, tzinfo=timezone.utc)
    conn, updates = _fake_pg_with_first_seen({'NEWCO': current})
    monkeypatch.setattr(seed.psycopg2, 'connect', lambda *a, **kw: conn)
    monkeypatch.setattr(seed.requests, 'get',
                        _fake_requests({'NEWCO': _FakeResp(200, [{'ipoDate': '2024-01-01'}])}))

    rc = seed.main(_args(tickers='NEWCO'))

    assert rc == 0
    assert updates == [], 'no UPDATE should fire when ipoDate >= current'
    out = capsys.readouterr().out
    assert 'unchanged:       1' in out


def test_handles_missing_profile_empty_array(monkeypatch, tmp_path, capsys):
    """HTTP 200 with empty array → counted as missing_profile, no update."""
    _setup_env(monkeypatch, tmp_path)
    conn, updates = _fake_pg_with_first_seen(
        {'BAD': datetime(2026, 5, 14, tzinfo=timezone.utc)}
    )
    monkeypatch.setattr(seed.psycopg2, 'connect', lambda *a, **kw: conn)
    monkeypatch.setattr(seed.requests, 'get',
                        _fake_requests({'BAD': _FakeResp(200, [])}))

    rc = seed.main(_args(tickers='BAD'))

    assert rc == 0
    assert updates == []
    out = capsys.readouterr().out
    assert 'missing_profile: 1' in out


def test_handles_missing_ipo_date_field(monkeypatch, tmp_path, capsys):
    """Profile present but no `ipoDate` key → counted as no_ipo_date."""
    _setup_env(monkeypatch, tmp_path)
    conn, updates = _fake_pg_with_first_seen(
        {'NOIPO': datetime(2026, 5, 14, tzinfo=timezone.utc)}
    )
    monkeypatch.setattr(seed.psycopg2, 'connect', lambda *a, **kw: conn)
    monkeypatch.setattr(
        seed.requests, 'get',
        _fake_requests({'NOIPO': _FakeResp(200, [{'symbol': 'NOIPO', 'companyName': 'X'}])}),
    )

    rc = seed.main(_args(tickers='NOIPO'))

    assert rc == 0
    assert updates == []
    out = capsys.readouterr().out
    assert 'no_ipo_date:     1' in out


def test_handles_403_and_429_without_crashing(monkeypatch, tmp_path, capsys):
    """403/429 from FMP exhausts retries then continues, counted as http_error."""
    _setup_env(monkeypatch, tmp_path)
    conn, updates = _fake_pg_with_first_seen(
        {'RATE': datetime(2026, 5, 14, tzinfo=timezone.utc)}
    )
    monkeypatch.setattr(seed.psycopg2, 'connect', lambda *a, **kw: conn)
    monkeypatch.setattr(seed.requests, 'get',
                        _fake_requests({'RATE': _FakeResp(429, {'msg': 'rate limited'})}))

    rc = seed.main(_args(tickers='RATE'))

    assert rc == 0, 'one bad ticker must not fail the run'
    assert updates == []
    out = capsys.readouterr().out
    assert 'http_error:      1' in out


def test_dry_run_does_not_write_db_or_marker(monkeypatch, tmp_path, capsys):
    _setup_env(monkeypatch, tmp_path)
    current = datetime(2026, 5, 14, tzinfo=timezone.utc)
    conn, updates = _fake_pg_with_first_seen({'AAPL': current})
    monkeypatch.setattr(seed.psycopg2, 'connect', lambda *a, **kw: conn)
    monkeypatch.setattr(
        seed.requests, 'get',
        _fake_requests({'AAPL': _FakeResp(200, [{'ipoDate': '1980-12-12'}])}),
    )

    rc = seed.main(_args(tickers='AAPL', dry_run=True))

    assert rc == 0
    assert updates == [], 'dry-run must not issue UPDATEs'
    assert not seed.MARKER.exists(), 'dry-run must not write marker'
    out = capsys.readouterr().out
    assert 'updated:         1' in out
    assert '[dry-run]' in out


def test_idempotency_guard_refuses_without_force(monkeypatch, tmp_path, capsys):
    _setup_env(monkeypatch, tmp_path)
    seed.MARKER.parent.mkdir(parents=True, exist_ok=True)
    seed.MARKER.write_text('pre-existing\n')

    rc = seed.main(_args(tickers='AAPL'))

    assert rc == 2, 'must refuse without --force'
    err = capsys.readouterr().err
    assert 'REFUSED' in err
    assert '--force' in err
    # Marker file untouched.
    assert seed.MARKER.read_text() == 'pre-existing\n'


def test_force_allows_rerun_after_marker_present(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    seed.MARKER.parent.mkdir(parents=True, exist_ok=True)
    seed.MARKER.write_text('pre-existing\n')

    conn, updates = _fake_pg_with_first_seen(
        {'AAPL': datetime(2026, 5, 14, tzinfo=timezone.utc)}
    )
    monkeypatch.setattr(seed.psycopg2, 'connect', lambda *a, **kw: conn)
    monkeypatch.setattr(
        seed.requests, 'get',
        _fake_requests({'AAPL': _FakeResp(200, [{'ipoDate': '1980-12-12'}])}),
    )

    rc = seed.main(_args(tickers='AAPL', force=True))

    assert rc == 0
    assert len(updates) == 1
    # Marker re-written by successful run.
    assert seed.MARKER.read_text() != 'pre-existing\n'
    assert 'fmp_ipo_seed_v1' in seed.MARKER.read_text()


def test_missing_postgres_uri_returns_nonzero(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(seed, 'MARKER', tmp_path / '.fmp_ipo_seeded_v1.txt')
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    monkeypatch.setenv('FMP_API_KEY', 'fake_key')

    rc = seed.main(_args(tickers='AAPL'))

    assert rc == 2
    err = capsys.readouterr().err
    assert 'POSTGRES_URI' in err


def test_missing_fmp_api_key_returns_nonzero(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(seed, 'MARKER', tmp_path / '.fmp_ipo_seeded_v1.txt')
    monkeypatch.setenv('POSTGRES_URI', 'postgres://fake/test')
    monkeypatch.delenv('FMP_API_KEY', raising=False)

    rc = seed.main(_args(tickers='AAPL'))

    assert rc == 2
    err = capsys.readouterr().err
    assert 'FMP_API_KEY' in err


def test_parse_error_on_malformed_ipo_date(monkeypatch, tmp_path, capsys):
    _setup_env(monkeypatch, tmp_path)
    conn, updates = _fake_pg_with_first_seen(
        {'WEIRD': datetime(2026, 5, 14, tzinfo=timezone.utc)}
    )
    monkeypatch.setattr(seed.psycopg2, 'connect', lambda *a, **kw: conn)
    monkeypatch.setattr(
        seed.requests, 'get',
        _fake_requests({'WEIRD': _FakeResp(200, [{'ipoDate': 'not-a-date'}])}),
    )

    rc = seed.main(_args(tickers='WEIRD'))

    assert rc == 0
    assert updates == []
    out = capsys.readouterr().out
    assert 'parse_error:     1' in out
