"""scripts/rederive_rank_flags.py — pure parts + SQL issued, with fakes."""
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

_p = Path(__file__).resolve().parents[2] / "scripts" / "rederive_rank_flags.py"
_spec = importlib.util.spec_from_file_location("rederive_rank_flags", _p)
rr = importlib.util.module_from_spec(_spec)
sys.modules["rederive_rank_flags"] = rr
_spec.loader.exec_module(rr)


def _row(sym, cap, r1, r3, tradable=True, status="active"):
    return {"symbol": sym, "tradable": tradable, "status": status,
            "market_cap": cap, "in_r1000": r1, "in_r3000": r3}


ROWS = [_row("ETF", 9e12, True, True), _row("STK", 1e9, False, False),
        _row("OK", 5e12, True, True)]
TYPES = {"ETF": "etf", "STK": "stock"}


def _rows_1000():
    # 1000 stocks flagged + a flagged ETF pushing the last stock out
    rows = [_row("ETF", 9e12, True, True)]
    rows += [_row(f"S{i}", 1e9 - i, i < 999, True) for i in range(1000)]
    return rows


def test_recompute_and_diff_counts():
    rows = _rows_1000()
    types = {"ETF": "etf", "S999": "stock"}
    r1, r3 = rr.recompute_flags(rows, types)
    assert "ETF" not in r1 and "S999" in r1 and len(r1) == 1000
    d = rr.diff_flags(rows, types)
    assert d["rows"] == 1001 and d["n_r1000"] == 2 and d["n_r3000"] == 1
    assert dict(d["leave"]["r1000"]) == {"etf": 1}
    assert dict(d["enter"]["r1000"]) == {"stock": 1}
    assert sorted(c[0] for c in d["changes"]) == ["ETF", "S999"]
    assert ("ETF", False, False) in d["changes"]
    text = rr.format_report(date(2026, 10, 5), d)
    assert "rows=1001" in text and "in_r1000_changes=2" in text and "etf=1" in text
    assert "SUMMARY (DRY-RUN)" in rr.format_total([(date(2026, 10, 5), d)], False)
    assert "REMINDER" in rr.format_total([(date(2026, 10, 5), d)], True)


def test_unchanged_rows_produce_no_changes():
    d = rr.diff_flags([_row("STK", 1e9, True, True)], {"STK": "stock"})
    assert d["changes"] == []


class FakeCur:
    def __init__(self, conn):
        self.c = conn
        self.description = None
        self._res = []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        self.c.log.append((sql, params))
        if self.c.fail_on and self.c.fail_on in sql and params and self.c.fail_date in params:
            raise RuntimeError("boom")
        if sql == rr.LATEST_SECURITY_TYPE_SQL:
            self._res = [(k, v) for k, v in TYPES.items()]
        elif sql == rr.DATES_SQL:
            self._res = [(d,) for d in self.c.dates]
        elif sql == rr.ROWS_SQL:
            cols = ["symbol", "tradable", "status", "market_cap", "in_r1000", "in_r3000"]
            self.description = [(n,) for n in cols]
            self._res = [tuple(r[n] for n in cols) for r in self.c.rows[params[0]]]

    def fetchall(self): return self._res


class FakeConn:
    def __init__(self, dates, rows, fail_on=None, fail_date=None):
        self.dates, self.rows = dates, rows
        self.log, self.events = [], []
        self.readonly = None
        self.fail_on, self.fail_date = fail_on, fail_date

    def set_session(self, readonly=None): self.readonly = readonly; self.events.append("ro")
    def cursor(self): return FakeCur(self)
    def commit(self): self.events.append("commit")
    def rollback(self): self.events.append("rollback")
    def close(self): self.events.append("close")


@pytest.fixture
def patch_batch(monkeypatch):
    import psycopg2.extras as ex

    def fake(cur, sql, args):
        for a in args:
            cur.execute(sql, a)
    monkeypatch.setattr(ex, "execute_batch", fake)


D1, D2 = date(2026, 9, 1), date(2026, 10, 5)


def _rows_by_date():
    return {D1: _rows_1000(), D2: _rows_1000()}


def test_dry_run_issues_no_update_and_is_read_only():
    c = FakeConn([D1, D2], _rows_by_date())
    out = []
    rr.run(c, D1, D2, False, out=out.append)
    assert c.readonly is True
    assert not [s for s, _ in c.log if s.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))]
    assert "commit" not in c.events
    assert "SUMMARY (DRY-RUN)" in out[-1]


def test_apply_updates_only_two_columns_for_changed_rows(patch_batch):
    c = FakeConn([D1, D2], _rows_by_date())
    rr.run(c, D1, D2, True, out=lambda *_: None)
    assert c.readonly is None
    ups = [(s, p) for s, p in c.log if s.startswith("UPDATE")]
    assert len(ups) == 4  # 2 changed rows x 2 dates
    for s, p in ups:
        assert s == rr.UPDATE_SQL
        assert s.startswith("UPDATE ticker_metadata_snapshots SET in_r1000 = %s, in_r3000 = %s WHERE")
        assert "DELETE" not in s and "INSERT" not in s
    assert {p[3] for _, p in ups} == {"ETF", "S999"}
    assert c.events.count("commit") >= 2


def test_error_rolls_back_failing_date_and_main_exits_nonzero(patch_batch, monkeypatch, capsys):
    monkeypatch.setenv("POSTGRES_URI", "x")
    c = FakeConn([D1, D2], _rows_by_date(), fail_on="UPDATE", fail_date=D2)
    rc = rr.main(["--from", "2026-09-01", "--apply"], connect=lambda dsn: c)
    assert rc == 1
    assert "ERROR" in capsys.readouterr().err
    assert c.events[-2] == "rollback"  # failing date rolled back (then close)
    # D1's commit happened before; no commit after the failure
    assert c.events.index("rollback") > max(i for i, e in enumerate(c.events) if e == "commit")


def test_guard_before_cutoff(capsys):
    with pytest.raises(SystemExit) as e:
        rr.parse_args(["--from", "2026-07-31"])
    assert e.value.code != 0
    assert rr.parse_args(["--from", "2026-07-31", "--force-range"]).force_range
    a = rr.parse_args(["--from", "2026-08-01"])
    assert a.apply is False and a.date_to == date.max


def test_main_dry_run_default_returns_zero(monkeypatch):
    monkeypatch.setenv("POSTGRES_URI", "x")
    c = FakeConn([D1], {D1: _rows_1000()})
    assert rr.main(["--from", "2026-09-01"], connect=lambda dsn: c) == 0
    assert c.readonly is True


def _bad_rows():
    rows = _rows_1000()
    rows[5] = dict(rows[5], in_r3000=False)   # stored != old-pool recompute
    return rows


def _ups(c):
    return [p for s, p in c.log if s.startswith("UPDATE")]


@pytest.mark.parametrize("apply", [False, True])
def test_unreproducible_date_skipped_exit3(patch_batch, monkeypatch, capsys, apply):
    monkeypatch.setenv("POSTGRES_URI", "x")
    c = FakeConn([D1, D2], {D1: _bad_rows(), D2: _rows_1000()})
    out = []
    per = rr.run(c, D1, D2, apply, out=out.append)
    assert any(o.startswith(f"NOT REPRODUCIBLE {D1}: 1 rows differ (r1000 0, r3000 1)") for o in out)
    assert dict(per)[D1]["skipped"] and not dict(per)[D2]["skipped"]
    assert "skipped-unreproducible=1" in out[-1] or "skipped-unreproducible=1" in "\n".join(out)
    assert all(p[2] != D1 for p in _ups(c))
    if apply:
        assert {p[2] for p in _ups(c)} == {D2}
    else:
        assert not _ups(c)
    c2 = FakeConn([D1, D2], {D1: _bad_rows(), D2: _rows_1000()})
    argv = ["--from", "2026-09-01"] + (["--apply"] if apply else [])
    assert rr.main(argv + ["--profile-cache", "/nonexistent"], connect=lambda dsn: c2) == 3
    assert "ERROR" in capsys.readouterr().err


def test_force_unreproducible_processes_with_warning(patch_batch, monkeypatch):
    monkeypatch.setenv("POSTGRES_URI", "x")
    c = FakeConn([D1], {D1: _bad_rows()})
    out = []
    per = rr.run(c, D1, D1, True, out=out.append, force_unreproducible=True)
    assert any(o.startswith("WARNING: NOT REPRODUCIBLE") for o in out)
    assert not per[0][1]["skipped"] and _ups(c)
    assert rr.main(["--from", "2026-09-01", "--force-unreproducible", "--profile-cache", "/x"],
                   connect=lambda dsn: FakeConn([D1], {D1: _bad_rows()})) == 0


def test_rows_sql_ordered_by_symbol():
    assert rr.ROWS_SQL.rstrip().endswith("ORDER BY symbol")


def test_profile_cache_fallback_db_wins_and_missing_tolerated(tmp_path):
    import json
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"A": {"isEtf": True}, "B": {"isEtf": True}, "C": {"isAdr": False}}))
    ov = rr.load_overlay({"B": "stock"}, f)
    assert ov == {"A": "etf", "B": "stock", "C": "stock"}
    warns = []
    assert rr.load_overlay({"B": "fund"}, tmp_path / "missing.json", warn=warns.append) == {"B": "fund"}
    assert len(warns) == 1 and warns[0].startswith("WARNING")
