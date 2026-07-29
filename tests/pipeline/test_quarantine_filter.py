"""Unit tests for src.pipeline.quarantine_filter.

Uses a live Postgres connection (matches the existing Phase A pattern in
tests/test_migrations_112_113_114.py — POSTGRES_URI must be exported).

Every test that inserts into data_quarantine uses a unique
source_tag='test_<uuid8>' and a finalizer DELETE keyed on that tag, so a
crashed test never pollutes the table across runs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import psycopg2
import pytest

from src.pipeline import quarantine_filter
from src.pipeline.quarantine_filter import (
    filter_quarantined,
    invalidate_cache,
)

DSN = os.environ.get("POSTGRES_URI")

# All tests in this module require a live PG connection. Skip cleanly
# (not error) when POSTGRES_URI is missing so the file is importable in
# environments without a DB.
pytestmark = pytest.mark.skipif(
    DSN is None, reason="POSTGRES_URI not set"
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure no cross-test cache contamination."""
    invalidate_cache()
    yield
    invalidate_cache()


def _insert_quarantine(tag: str, *, symbol: str, date: str,
                       master_table: str = "prices.parquet",
                       superseded: bool = False) -> None:
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        if superseded:
            cur.execute(
                """
                INSERT INTO data_quarantine
                  (master_table, symbol, affected_date, source_tag, reason,
                   flagged_by, superseded_by_source_tag, superseded_at)
                VALUES (%s, %s, %s, %s, 'unit-test', 'auto:test',
                        'backfill_5y_v2', NOW())
                """,
                (master_table, symbol, date, tag),
            )
        else:
            cur.execute(
                """
                INSERT INTO data_quarantine
                  (master_table, symbol, affected_date, source_tag, reason,
                   flagged_by)
                VALUES (%s, %s, %s, %s, 'unit-test', 'auto:test')
                """,
                (master_table, symbol, date, tag),
            )
        c.commit()


def _delete_quarantine(tag: str) -> None:
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute("DELETE FROM data_quarantine WHERE source_tag = %s", (tag,))
        c.commit()


@pytest.fixture
def tag():
    """A unique source_tag for the test, auto-cleaned at teardown."""
    t = f"test_{uuid.uuid4().hex[:8]}"
    yield t
    _delete_quarantine(t)


# ---------------------------------------------------------------------------
# Behavior: column resolution
# ---------------------------------------------------------------------------

def test_drops_marked_rows_with_symbol_column(tag):
    _insert_quarantine(tag, symbol="TESTSYM", date="2024-01-15")
    df = pd.DataFrame({
        "symbol": ["TESTSYM", "TESTSYM", "OK"],
        "date":   ["2024-01-15", "2024-01-16", "2024-01-15"],
        "close":  [1.0, 2.0, 3.0],
    })
    out = filter_quarantined(df, "prices.parquet")
    # The (TESTSYM, 2024-01-15) row drops; the other two survive.
    assert len(out) == 2
    pairs = set(zip(out["symbol"], out["date"]))
    assert ("TESTSYM", "2024-01-15") not in pairs
    assert ("TESTSYM", "2024-01-16") in pairs
    assert ("OK", "2024-01-15") in pairs


def test_drops_marked_rows_with_ticker_column(tag):
    # Task 1 verified prices.parquet uses 'ticker' (not 'symbol') in its
    # parquet schema. The filter must handle both.
    _insert_quarantine(tag, symbol="TESTTKR", date="2024-02-20")
    df = pd.DataFrame({
        "ticker": ["TESTTKR", "TESTTKR", "OTHER"],
        "date":   ["2024-02-20", "2024-02-21", "2024-02-20"],
        "close":  [10.0, 20.0, 30.0],
    })
    out = filter_quarantined(df, "prices.parquet")
    assert len(out) == 2
    pairs = set(zip(out["ticker"], out["date"]))
    assert ("TESTTKR", "2024-02-20") not in pairs


def test_raises_when_neither_column_present(tag):
    _insert_quarantine(tag, symbol="ANY", date="2024-01-15")
    df = pd.DataFrame({"foo": ["A"], "date": ["2024-01-15"]})
    with pytest.raises(ValueError) as exc:
        filter_quarantined(df, "prices.parquet")
    msg = str(exc.value)
    # Error must name both candidate columns AND the actual columns
    assert "symbol" in msg
    assert "ticker" in msg
    assert "foo" in msg


def test_raises_when_date_column_missing(tag):
    _insert_quarantine(tag, symbol="ANY", date="2024-01-15")
    df = pd.DataFrame({"ticker": ["ANY"], "close": [1.0]})
    with pytest.raises(ValueError) as exc:
        filter_quarantined(df, "prices.parquet")
    assert "date" in str(exc.value)


# ---------------------------------------------------------------------------
# Behavior: supersede semantics
# ---------------------------------------------------------------------------

def test_superseded_rows_not_filtered(tag):
    # Superseded rows (a fixed v2 backfill landed) must NOT be filtered.
    _insert_quarantine(tag, symbol="FIXED", date="2024-03-10",
                       superseded=True)
    df = pd.DataFrame({
        "ticker": ["FIXED"],
        "date":   ["2024-03-10"],
        "close":  [1.0],
    })
    out = filter_quarantined(df, "prices.parquet")
    assert len(out) == 1, "superseded row should survive"


# ---------------------------------------------------------------------------
# Behavior: empty quarantine
# ---------------------------------------------------------------------------

def test_empty_quarantine_returns_df_unchanged():
    # No tag fixture — there are zero unsuperseded rows for this fake table.
    fake_table = f"nonexistent_{uuid.uuid4().hex[:6]}.parquet"
    df = pd.DataFrame({
        "ticker": ["A", "B", "C"],
        "date":   ["2024-01-01", "2024-01-02", "2024-01-03"],
    })
    out = filter_quarantined(df, fake_table)
    # Hot-path identity: nothing to filter, df returned as-is.
    assert out is df


# ---------------------------------------------------------------------------
# Behavior: cache TTL
# ---------------------------------------------------------------------------

def test_cache_ttl_within_window_hits_cache(monkeypatch, tag):
    """Within TTL, a second call must NOT re-load from the DB."""
    _insert_quarantine(tag, symbol="CACHE1", date="2024-04-01")
    df = pd.DataFrame({"ticker": ["CACHE1"], "date": ["2024-04-01"]})

    calls = {"n": 0}
    real_load = quarantine_filter._load

    def counting_load(master_table):
        calls["n"] += 1
        return real_load(master_table)

    monkeypatch.setattr(quarantine_filter, "_load", counting_load)
    invalidate_cache()

    out1 = filter_quarantined(df, "prices.parquet")
    out2 = filter_quarantined(df, "prices.parquet")
    assert len(out1) == 0
    assert len(out2) == 0
    assert calls["n"] == 1, (
        f"expected exactly 1 DB load within TTL, got {calls['n']}"
    )


def test_cache_ttl_expiry_triggers_reload(monkeypatch, tag):
    """After TTL elapses, the next call MUST re-load."""
    _insert_quarantine(tag, symbol="TTL1", date="2024-04-02")
    df = pd.DataFrame({"ticker": ["TTL1"], "date": ["2024-04-02"]})

    calls = {"n": 0}
    real_load = quarantine_filter._load

    def counting_load(master_table):
        calls["n"] += 1
        return real_load(master_table)

    monkeypatch.setattr(quarantine_filter, "_load", counting_load)
    invalidate_cache()

    # First call loads.
    filter_quarantined(df, "prices.parquet")
    assert calls["n"] == 1

    # Advance time past TTL by patching time.time on the module.
    real_now = time.time()
    monkeypatch.setattr(
        quarantine_filter.time, "time",
        lambda: real_now + quarantine_filter._TTL + 1,
    )
    filter_quarantined(df, "prices.parquet")
    assert calls["n"] == 2, "TTL expiry should force a reload"


def test_invalidate_cache_forces_reload(monkeypatch, tag):
    _insert_quarantine(tag, symbol="INV1", date="2024-04-03")
    df = pd.DataFrame({"ticker": ["INV1"], "date": ["2024-04-03"]})

    calls = {"n": 0}
    real_load = quarantine_filter._load

    def counting_load(master_table):
        calls["n"] += 1
        return real_load(master_table)

    monkeypatch.setattr(quarantine_filter, "_load", counting_load)
    invalidate_cache()

    filter_quarantined(df, "prices.parquet")
    filter_quarantined(df, "prices.parquet")
    assert calls["n"] == 1

    invalidate_cache("prices.parquet")
    filter_quarantined(df, "prices.parquet")
    assert calls["n"] == 2

    invalidate_cache()  # clear-all variant
    filter_quarantined(df, "prices.parquet")
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# Behavior: thread safety
# ---------------------------------------------------------------------------

def test_concurrent_calls_do_not_double_load(monkeypatch, tag):
    """Many threads hitting a cold cache must trigger exactly one load.

    The contract is that ``_load`` runs *inside* ``_LOCK``. If anyone
    refactors that to a check-then-load pattern, this test will catch
    it by reporting >1 load.
    """
    _insert_quarantine(tag, symbol="MT1", date="2024-05-01")
    df = pd.DataFrame({"ticker": ["MT1"], "date": ["2024-05-01"]})

    counter = {"n": 0}
    counter_lock = threading.Lock()
    real_load = quarantine_filter._load

    def slow_counting_load(master_table):
        with counter_lock:
            counter["n"] += 1
        # Yield the GIL so any unlocked check-then-load race manifests.
        time.sleep(0.05)
        return real_load(master_table)

    monkeypatch.setattr(quarantine_filter, "_load", slow_counting_load)
    invalidate_cache()

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [
            ex.submit(filter_quarantined, df, "prices.parquet")
            for _ in range(10)
        ]
        results = [f.result() for f in futs]

    assert all(len(r) == 0 for r in results)
    assert counter["n"] == 1, (
        f"expected exactly 1 DB load under contention, got {counter['n']}"
    )


# ---------------------------------------------------------------------------
# Behavior: CLI mode
# ---------------------------------------------------------------------------

def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "src.pipeline.quarantine_filter", *args],
        capture_output=True,
        text=True,
        timeout=30,
        cwd="/root/openclaw",
    )


def test_cli_json_mode_emits_valid_json(tag):
    _insert_quarantine(tag, symbol="CLISYM", date="2024-06-15")
    # The CLI runs in a subprocess so it has its own cold cache.
    proc = _run_cli(["--master-table", "prices.parquet", "--json"])
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert isinstance(payload, list)
    # Each entry is a [symbol, date] pair.
    assert all(isinstance(p, list) and len(p) == 2 for p in payload)
    assert ["CLISYM", "2024-06-15"] in payload


def test_cli_json_empty_quarantine_emits_array():
    fake = f"nonexistent_{uuid.uuid4().hex[:6]}.parquet"
    proc = _run_cli(["--master-table", fake, "--json"])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []


def test_cli_tsv_default_mode(tag):
    _insert_quarantine(tag, symbol="TSVSYM", date="2024-07-04")
    proc = _run_cli(["--master-table", "prices.parquet"])
    assert proc.returncode == 0, proc.stderr
    # Each row: "<symbol>\t<date>\n"
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert "TSVSYM\t2024-07-04" in lines


def test_cli_master_table_required():
    proc = _run_cli([])
    # argparse exits 2 when a required arg is missing.
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "--master-table" in proc.stderr


class TestCategoricalFastPath:
    """The categorical fast path (2026-07-28, prices.parquet 4.3GB-transient
    fix) must be byte-identical to the generic stringify path."""

    def _frame(self, categorical: bool) -> pd.DataFrame:
        tickers = ['AAPL', 'MSFT', 'ZZZQ'] * 4
        dates = ['2026-01-02', '2026-01-03', '2026-01-06', '2026-01-07'] * 3
        df = pd.DataFrame({'ticker': tickers, 'date': dates,
                           'close': [float(i) for i in range(12)]})
        if categorical:
            df['ticker'] = df['ticker'].astype('category')
            df['date'] = df['date'].astype('category')
        return df

    def _run(self, monkeypatch, bad, categorical: bool) -> pd.DataFrame:
        monkeypatch.setattr(quarantine_filter, '_cached', lambda mt: bad)
        return filter_quarantined(self._frame(categorical), 'prices.parquet')

    def test_identical_to_generic_path(self, monkeypatch):
        bad = {('AAPL', '2026-01-02'), ('ZZZQ', '2026-01-07'),
               ('MISSING', '2026-01-02'), ('AAPL', '1999-01-01')}
        fast = self._run(monkeypatch, bad, categorical=True)
        generic = self._run(monkeypatch, bad, categorical=False)
        assert fast['ticker'].astype(str).tolist() == generic['ticker'].astype(str).tolist()
        assert fast['date'].astype(str).tolist() == generic['date'].astype(str).tolist()
        assert fast['close'].tolist() == generic['close'].tolist()
        assert len(fast) == 10  # rows 0 (AAPL,01-02) and 11 (ZZZQ,01-07) dropped

    def test_no_matching_pairs_returns_frame_unchanged(self, monkeypatch):
        bad = {('MISSING', '2026-01-02'), ('AAPL', '1999-01-01')}
        out = self._run(monkeypatch, bad, categorical=True)
        assert len(out) == 12

    def test_all_kept_when_bad_empty_categories_overlap(self, monkeypatch):
        out = self._run(monkeypatch, {('ZZZQ', '2026-01-03')}, categorical=True)
        gen = self._run(monkeypatch, {('ZZZQ', '2026-01-03')}, categorical=False)
        assert out['close'].tolist() == gen['close'].tolist()


class TestGenericPathSymbolPrefilter:
    """2026-07-29: the generic (non-categorical) path was rewritten around a
    vectorized symbol prefilter after the 18.8M-tuple zip OOM-killed the
    engine's sentiment+signals steps. Row selection must be byte-identical."""

    def _frame(self):
        tickers = ['AAPL', 'MSFT', 'ZZZQ'] * 4
        dates = ['2026-01-02', '2026-01-03', '2026-01-06', '2026-01-07'] * 3
        return pd.DataFrame({'ticker': tickers, 'date': dates,
                             'close': [float(i) for i in range(12)]})

    def test_drops_exact_pairs_only(self, monkeypatch):
        bad = {('AAPL', '2026-01-02'), ('ZZZQ', '2026-01-07'),
               ('AAPL', '1999-01-01')}
        monkeypatch.setattr(quarantine_filter, '_cached', lambda mt: bad)
        out = filter_quarantined(self._frame(), 'prices.parquet')
        assert len(out) == 10
        remaining = set(zip(out['ticker'], out['date']))
        assert ('AAPL', '2026-01-02') not in remaining
        assert ('ZZZQ', '2026-01-07') not in remaining
        # Other AAPL/ZZZQ dates survive — prefilter must not over-drop.
        assert ('AAPL', '2026-01-07') in remaining
        assert ('ZZZQ', '2026-01-02') in remaining

    def test_no_symbol_overlap_returns_all_rows(self, monkeypatch):
        bad = {('NOPE', '2026-01-02'), ('MISSING', '2026-01-03')}
        monkeypatch.setattr(quarantine_filter, '_cached', lambda mt: bad)
        out = filter_quarantined(self._frame(), 'prices.parquet')
        assert len(out) == 12
        assert list(out.index) == list(range(12))

    def test_symbol_overlap_but_no_date_match(self, monkeypatch):
        bad = {('AAPL', '1999-01-01')}
        monkeypatch.setattr(quarantine_filter, '_cached', lambda mt: bad)
        out = filter_quarantined(self._frame(), 'prices.parquet')
        assert len(out) == 12

    def test_matches_categorical_fast_path(self, monkeypatch):
        bad = {('AAPL', '2026-01-02'), ('ZZZQ', '2026-01-07')}
        monkeypatch.setattr(quarantine_filter, '_cached', lambda mt: bad)
        obj = filter_quarantined(self._frame(), 'prices.parquet')
        cat_in = self._frame()
        cat_in['ticker'] = cat_in['ticker'].astype('category')
        cat_in['date'] = cat_in['date'].astype('category')
        cat = filter_quarantined(cat_in, 'prices.parquet')
        assert obj['close'].tolist() == cat['close'].tolist()
