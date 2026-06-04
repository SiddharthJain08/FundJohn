from datetime import date
from unittest.mock import MagicMock

from src.pipeline.backfillers.universe_metadata import _alpaca_status_batch


def _cursor_returning(rows, cols):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.description = [MagicMock(name=c) for c in cols]
    for d, c in zip(cur.description, cols):
        d.name = c
    pg = MagicMock()
    pg.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    pg.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return pg


COLS = ["symbol", "asset_class", "exchange", "status", "tradable", "shortable",
        "fractionable", "easy_to_borrow", "first_seen_at", "last_seen_at",
        "listed_date"]


def test_listed_date_governs_pit_when_present():
    # DELL listed 2021-01-04 but first_seen 2026-05-14 → must be PRESENT for 2023 snapshots
    rows = [("DELL", "us_equity", "NYSE", "active", True, True, True, True,
             date(2026, 5, 14), None, date(2021, 1, 4))]
    out = _alpaca_status_batch(["DELL"], date(2023, 6, 30), _cursor_returning(rows, COLS))
    assert "DELL" in out


def test_falls_back_to_first_seen_when_listed_date_null():
    rows = [("NEWCO", "us_equity", "NYSE", "active", True, True, True, True,
             date(2026, 5, 14), None, None)]
    out = _alpaca_status_batch(["NEWCO"], date(2023, 6, 30), _cursor_returning(rows, COLS))
    assert "NEWCO" not in out  # legacy behavior preserved when listed_date missing


def test_not_yet_listed_excluded():
    rows = [("GEV", "us_equity", "NYSE", "active", True, True, True, True,
             date(2026, 5, 14), None, date(2024, 4, 2))]
    out = _alpaca_status_batch(["GEV"], date(2023, 6, 30), _cursor_returning(rows, COLS))
    assert "GEV" not in out
