#!/usr/bin/env python3
"""SP-2 Phase B: one-shot historical SP500 membership CSV writer.

Output: data/sp500_historical_membership_v1.csv -- one row per (ticker,
        contiguous-membership-span). Schema: `ticker,added_on,removed_on`
        where `removed_on` empty means "currently a member".

Source: Wikipedia page "List of S&P 500 companies", which carries two
        relevant tables:
          - Table 0: current members with `Symbol` + `Date added`
          - Table 1: "Selected changes to the list" -- recent
                     add/remove events (MultiIndex columns: top-level
                     'Added'/'Removed' + sub-level 'Ticker'/'Security';
                     a 'Date' column gives the effective date)

Consumed by `src/pipeline/backfillers/universe_metadata.py::build_month_snapshot`
which projects monthly `in_sp500` flags for historical metadata rows
(SP-2 Phase B Task 8). Without this file, `in_sp500` falls back to today's
membership (documented bias).

Known coarseness (documented, not a bug):
  - The "Selected changes" table on Wikipedia only goes back a few years,
    so historical fidelity is best ~2019-present and degrades before that.
    `in_sp500` for older history is therefore a *conservative proxy*, not
    a point-in-time truth.
  - For a removed ticker that appeared in `Selected changes` as both
    Added and Removed, we pair its remove date with the most recent prior
    add date for that ticker in the changes table. Tickers that were
    added BEFORE the visible changes window get `added_on` empty.
  - Tickers re-added after a removal yield two rows: the historical
    (removed) span from Table 1, plus the current span from Table 0.

Operator usage (run live; this sandbox typically has no Wikipedia access):
    python3 scripts/probe_sp500_historical_membership.py --dry-run
    python3 scripts/probe_sp500_historical_membership.py
    head -5 data/sp500_historical_membership_v1.csv

Frozen artifact: commit the output once; future revisions ship as
sibling files (`..._v2.csv`).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'sp500_historical_membership_v1.csv'
URL = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'


def _fmt_date(value) -> str:
    """ISO YYYY-MM-DD string, or empty string for NaT / blank / 'n/a'."""
    ts = pd.to_datetime(value, errors='coerce')
    if pd.isna(ts):
        return ''
    return ts.strftime('%Y-%m-%d')


def _normalise_ticker(value) -> str:
    """Wikipedia ticker cells are sometimes float-NaN; preserve dots (BRK.B)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ''
    return str(value).strip()


def _parse_current(table: pd.DataFrame) -> pd.DataFrame:
    """Table 0 -> (ticker, added_on, removed_on=NaN)."""
    cols = {c for c in table.columns}
    if 'Symbol' not in cols or 'Date added' not in cols:
        raise ValueError(
            "Wikipedia current-members table is missing required columns "
            f"'Symbol' and/or 'Date added' (got {sorted(cols)}); the page "
            "layout may have changed -- update the parser."
        )
    df = table[['Symbol', 'Date added']].copy()
    df.columns = ['ticker', 'added_on']
    df['ticker'] = df['ticker'].map(_normalise_ticker)
    df = df[df['ticker'].astype(bool)]
    df['added_on'] = df['added_on'].map(_fmt_date)
    df['removed_on'] = ''
    return df.reset_index(drop=True)


def _parse_changes(table: pd.DataFrame) -> pd.DataFrame:
    """Table 1 (MultiIndex columns) -> (ticker, added_on, removed_on) for the
    REMOVED tickers. For each removed ticker we look up the most-recent prior
    add event in the same table to fill `added_on`; if none, empty string."""
    cols = table.columns
    if not isinstance(cols, pd.MultiIndex) or cols.nlevels < 2:
        raise ValueError(
            "Wikipedia 'Selected changes' table must have MultiIndex columns "
            f"(got {cols!r}); page layout may have changed -- update parser."
        )

    def _pick(top: str, sub: str):
        for c in cols:
            if c[0] == top and c[1] == sub:
                return c
        return None

    date_col = _pick('Date', 'Date') or next(
        (c for c in cols if 'Date' in str(c[0])), None
    )
    added_t = _pick('Added', 'Ticker')
    removed_t = _pick('Removed', 'Ticker')
    if date_col is None or added_t is None or removed_t is None:
        raise ValueError(
            "Wikipedia 'Selected changes' table missing one of "
            f"(Date, Added/Ticker, Removed/Ticker) -- got {list(cols)}; "
            "page layout may have changed -- update parser."
        )

    # Build chronological event list per ticker.
    events: dict[str, list[tuple[str, str]]] = {}
    for _, row in table.iterrows():
        d = _fmt_date(row[date_col])
        a = _normalise_ticker(row[added_t])
        r = _normalise_ticker(row[removed_t])
        if a:
            events.setdefault(a, []).append((d, 'add'))
        if r:
            events.setdefault(r, []).append((d, 'remove'))

    out_rows: list[dict[str, str]] = []
    for ticker, ev in events.items():
        # Process remove events; pair each with most-recent prior add date.
        add_dates_so_far: list[str] = []
        # Sort by date ascending (empty date sorts first => oldest unknown).
        for d, kind in sorted(ev, key=lambda x: x[0]):
            if kind == 'add':
                add_dates_so_far.append(d)
            else:  # remove
                prior_add = add_dates_so_far[-1] if add_dates_so_far else ''
                out_rows.append({
                    'ticker': ticker,
                    'added_on': prior_add,
                    'removed_on': d,
                })
    return pd.DataFrame(out_rows, columns=['ticker', 'added_on', 'removed_on'])


def build_membership(tables: list[pd.DataFrame]) -> pd.DataFrame:
    """Combine Wikipedia tables 0 and 1 into the membership CSV frame."""
    if len(tables) < 2:
        raise ValueError(
            f"Expected at least 2 Wikipedia tables, got {len(tables)}; "
            "the page layout may have changed -- update parser."
        )
    current = _parse_current(tables[0])
    changes = _parse_changes(tables[1])
    out = pd.concat([current, changes], ignore_index=True)
    # Preserve column order.
    return out[['ticker', 'added_on', 'removed_on']]


def _fetch_tables() -> list[pd.DataFrame]:
    try:
        return pd.read_html(URL)
    except Exception as exc:  # pragma: no cover -- live network failure path
        raise RuntimeError(
            f"Failed to fetch Wikipedia page {URL!r}: {exc}. "
            "This script is operator-run; it requires outbound HTTP access "
            "to en.wikipedia.org. Re-run from a host with network access."
        ) from exc


def main(dry: bool, force: bool = False) -> int:
    if OUT.exists() and not dry and not force:
        try:
            display = OUT.relative_to(ROOT)
        except ValueError:
            display = OUT
        sys.stderr.write(
            f"REFUSED: {display} already exists; pass --force to overwrite "
            "(and commit a v2.csv file instead, per the frozen-artifact convention)\n"
        )
        return 2

    tables = _fetch_tables()
    df = build_membership(tables)

    if dry:
        print(df.head().to_string(index=False))
        print(f'Total rows: {len(df)}')
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    header = (
        f"# Source: en.wikipedia.org/wiki/List_of_S%26P_500_companies (scraped {today})\n"
        "# Schema: ticker,added_on,removed_on  (removed_on empty means currently a member)\n"
    )
    with OUT.open('w', encoding='utf-8') as fh:
        fh.write(header)
        df.to_csv(fh, index=False)
    print(f'Wrote {len(df)} membership-span rows to {OUT}')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='Print parsed head + row count; do NOT write the CSV')
    ap.add_argument('--force', action='store_true',
                    help='Overwrite existing output (frozen-artifact convention '
                         'prefers a v2.csv sibling instead)')
    args = ap.parse_args()
    sys.exit(main(args.dry_run, force=args.force))
