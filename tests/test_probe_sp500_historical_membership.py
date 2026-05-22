"""Unit tests for scripts/probe_sp500_historical_membership.py.

All tests mock `pd.read_html` -- no live Wikipedia traffic. Mock DataFrames
match the column shapes Wikipedia actually returns:
  - Table 0: plain Index columns ('Symbol', 'Security', 'GICS Sector',
             'GICS Sub-Industry', 'Headquarters Location', 'Date added',
             'CIK', 'Founded').
  - Table 1: MultiIndex columns -- (top, sub) where top is
             'Date'/'Added'/'Removed'/'Reason' and sub is 'Date',
             'Ticker'/'Security', 'Ticker'/'Security', 'Reason'.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import probe_sp500_historical_membership as probe  # noqa: E402


# --- fixtures ----------------------------------------------------------------

def _current_table(rows: list[tuple[str, str]]) -> pd.DataFrame:
    """Build a Wikipedia-Table-0-shaped DataFrame from (symbol, date_added)
    pairs. Includes the other columns for realism (parser must ignore them)."""
    return pd.DataFrame([
        {
            'Symbol': sym,
            'Security': f'{sym} Inc.',
            'GICS Sector': 'Technology',
            'GICS Sub-Industry': 'Software',
            'Headquarters Location': 'Somewhere, USA',
            'Date added': date_added,
            'CIK': '0000000000',
            'Founded': '1999',
        }
        for sym, date_added in rows
    ])


def _changes_table(events: list[dict]) -> pd.DataFrame:
    """Build a Wikipedia-Table-1-shaped DataFrame with MultiIndex columns.

    Each event dict has keys: date, added_ticker (optional), added_security
    (optional), removed_ticker (optional), removed_security (optional),
    reason (optional).
    """
    cols = pd.MultiIndex.from_tuples([
        ('Date', 'Date'),
        ('Added', 'Ticker'),
        ('Added', 'Security'),
        ('Removed', 'Ticker'),
        ('Removed', 'Security'),
        ('Reason', 'Reason'),
    ])
    data = []
    for ev in events:
        data.append([
            ev.get('date', ''),
            ev.get('added_ticker', '') or float('nan'),
            ev.get('added_security', '') or float('nan'),
            ev.get('removed_ticker', '') or float('nan'),
            ev.get('removed_security', '') or float('nan'),
            ev.get('reason', ''),
        ])
    return pd.DataFrame(data, columns=cols)


@pytest.fixture
def sample_tables():
    """Realistic mini Wikipedia tables: a few current members + a few changes."""
    current = _current_table([
        ('AAPL', '1982-11-30'),
        ('MSFT', '1994-06-01'),
        ('BRK.B', '2010-02-16'),  # dot ticker -- must survive
        ('GOOGL', 'April 3, 2014'),  # human-readable date -- must normalise
    ])
    changes = _changes_table([
        # XYZ added then removed -- closed span.
        {'date': '2018-06-07', 'added_ticker': 'XYZ', 'added_security': 'XYZ Co'},
        {'date': '2021-03-22', 'removed_ticker': 'XYZ', 'removed_security': 'XYZ Co'},
        # OLD removed with no prior add visible in this table.
        {'date': '2019-09-23', 'removed_ticker': 'OLD', 'removed_security': 'Old Inc'},
        # NEW added (still current per Table 0 if also listed there; here NOT).
        {'date': '2022-12-19', 'added_ticker': 'NEW', 'added_security': 'New Co'},
    ])
    return [current, changes]


# --- parsing -----------------------------------------------------------------

def test_current_table_produces_expected_rows(sample_tables):
    df = probe.build_membership(sample_tables)
    # 4 current + 2 removed-span rows (XYZ closed, OLD open-ended-add).
    current_rows = df[df['removed_on'] == '']
    assert set(current_rows['ticker']) == {'AAPL', 'MSFT', 'BRK.B', 'GOOGL'}


def test_dot_ticker_preserved(sample_tables):
    """BRK.B must not be split, mangled, or treated as a filename extension."""
    df = probe.build_membership(sample_tables)
    assert 'BRK.B' in set(df['ticker']), (
        f'BRK.B must round-trip intact; got {sorted(df["ticker"])}'
    )


def test_dates_normalised_to_iso(sample_tables):
    df = probe.build_membership(sample_tables)
    googl = df[df['ticker'] == 'GOOGL'].iloc[0]
    assert googl['added_on'] == '2014-04-03', (
        f'human-readable date must normalise to ISO; got {googl["added_on"]!r}'
    )


def test_removed_ticker_pairs_with_prior_add(sample_tables):
    """XYZ was added 2018-06-07 and removed 2021-03-22 in the changes table.
    The reconstructed span must have both dates."""
    df = probe.build_membership(sample_tables)
    xyz = df[df['ticker'] == 'XYZ']
    assert len(xyz) == 1
    row = xyz.iloc[0]
    assert row['added_on'] == '2018-06-07'
    assert row['removed_on'] == '2021-03-22'


def test_removed_ticker_without_prior_add_has_empty_added_on(sample_tables):
    """OLD was removed but never added in the visible changes table -- the
    added_on field must be empty (not NaN, not 'NaT')."""
    df = probe.build_membership(sample_tables)
    old = df[df['ticker'] == 'OLD']
    assert len(old) == 1
    assert old.iloc[0]['added_on'] == ''
    assert old.iloc[0]['removed_on'] == '2019-09-23'


def test_current_membership_no_removed_date(sample_tables):
    """Tickers in Table 0 (current members) get removed_on empty."""
    df = probe.build_membership(sample_tables)
    aapl = df[df['ticker'] == 'AAPL'].iloc[0]
    assert aapl['removed_on'] == ''


# --- error handling ----------------------------------------------------------

def test_missing_columns_raise_informative_error():
    """If Wikipedia changes its column names, we must raise loudly, not
    silently produce garbage rows."""
    bad_current = pd.DataFrame([{'Ticker': 'AAPL', 'Date_Added': '1982-11-30'}])
    changes = _changes_table([])
    with pytest.raises(ValueError) as exc_info:
        probe.build_membership([bad_current, changes])
    assert 'Symbol' in str(exc_info.value) or 'Date added' in str(exc_info.value)
    assert 'layout' in str(exc_info.value).lower()


def test_changes_table_must_have_multiindex():
    """Wikipedia's selected-changes table has MultiIndex columns; if shape
    changes, raise informative error."""
    current = _current_table([('AAPL', '1982-11-30')])
    flat_changes = pd.DataFrame(
        [['2020-01-01', 'NEW', 'OLD']],
        columns=['Date', 'Added', 'Removed'],
    )
    with pytest.raises(ValueError) as exc_info:
        probe.build_membership([current, flat_changes])
    assert 'MultiIndex' in str(exc_info.value)


def test_too_few_tables_raises():
    with pytest.raises(ValueError) as exc_info:
        probe.build_membership([_current_table([('AAPL', '1982-11-30')])])
    assert 'at least 2' in str(exc_info.value)


def test_blank_and_nan_tickers_dropped():
    """Rows with empty/NaN ticker cells (Wikipedia edit artifacts) must
    be silently dropped, not surface as empty-string rows in the CSV."""
    current = pd.DataFrame([
        {'Symbol': 'AAPL', 'Date added': '1982-11-30'},
        {'Symbol': float('nan'), 'Date added': '2020-01-01'},
        {'Symbol': '', 'Date added': '2021-01-01'},
    ])
    df = probe._parse_current(current)
    assert list(df['ticker']) == ['AAPL']


# --- CLI behaviour -----------------------------------------------------------

def test_dry_run_does_not_write_file(monkeypatch, tmp_path, capsys, sample_tables):
    out_path = tmp_path / 'sp500_historical_membership_v1.csv'
    monkeypatch.setattr(probe, 'OUT', out_path)
    monkeypatch.setattr(probe, '_fetch_tables', lambda: sample_tables)

    rc = probe.main(dry=True)
    assert rc == 0
    assert not out_path.exists(), '--dry-run must not write the CSV'
    out = capsys.readouterr().out
    assert 'Total rows' in out


def test_writes_file_with_header(monkeypatch, tmp_path, sample_tables):
    out_path = tmp_path / 'sp500_historical_membership_v1.csv'
    monkeypatch.setattr(probe, 'OUT', out_path)
    monkeypatch.setattr(probe, '_fetch_tables', lambda: sample_tables)

    rc = probe.main(dry=False)
    assert rc == 0
    assert out_path.exists()
    contents = out_path.read_text()
    lines = contents.splitlines()
    assert lines[0].startswith('# Source: en.wikipedia.org/wiki/'), (
        f'first line must be the Source comment; got {lines[0]!r}'
    )
    assert lines[1].startswith('# Schema:'), (
        f'second line must be the Schema comment; got {lines[1]!r}'
    )
    # The CSV header itself follows the comments.
    assert lines[2] == 'ticker,added_on,removed_on'


def test_idempotency_guard_refuses_overwrite(monkeypatch, tmp_path, capsys, sample_tables):
    out_path = tmp_path / 'sp500_historical_membership_v1.csv'
    out_path.write_text('PRE_EXISTING\n')
    monkeypatch.setattr(probe, 'OUT', out_path)
    monkeypatch.setattr(probe, '_fetch_tables', lambda: sample_tables)

    rc = probe.main(dry=False)
    assert rc == 2, 'idempotency guard must return exit-code 2'
    err = capsys.readouterr().err
    assert 'REFUSED' in err
    assert '--force' in err
    assert out_path.read_text() == 'PRE_EXISTING\n', (
        'existing file must be left untouched'
    )

    # --force overrides.
    rc2 = probe.main(dry=False, force=True)
    assert rc2 == 0
    new_contents = out_path.read_text()
    assert 'PRE_EXISTING' not in new_contents
    assert 'ticker,added_on,removed_on' in new_contents


def test_idempotency_guard_does_not_block_dry_run(monkeypatch, tmp_path, sample_tables):
    """--dry-run is read-only; existing output should NOT block it."""
    out_path = tmp_path / 'sp500_historical_membership_v1.csv'
    out_path.write_text('PRE_EXISTING\n')
    monkeypatch.setattr(probe, 'OUT', out_path)
    monkeypatch.setattr(probe, '_fetch_tables', lambda: sample_tables)

    rc = probe.main(dry=True)
    assert rc == 0
    assert out_path.read_text() == 'PRE_EXISTING\n', (
        '--dry-run must not touch the existing file'
    )
