"""End-to-end wiring tests for SP-2 Phase B Task 5.

Verifies that the master-parquet consumers actually call
`filter_quarantined` and drop unsuperseded `(ticker, date)` rows.

The Task 5 spec named six consumers, but investigation showed only
three have filterable reads:

  - src/strategies/_db_adapters.py (ParquetCoverage._load_month —
    backs UniverseResolver.coverage check)
  - src/backtest/unified_backtest.py (load_prices_panels)
  - src/backtest/quick_backtest.py (_load_prices)

The other three the spec named have no filterable read:

  - src/backtest/regime_blended_backtest.py — reads
    historical_regimes.parquet only (no ticker column).
  - src/backtest/intraday_regime_backtest.py — reads
    intraday_features.parquet only (no ticker, has ts_utc not date).
  - src/backtest/regime_performance_analyzer.py — reads zero parquets
    (only `signal_pnl` via Postgres).

A filter call in those modules would either raise (no ticker / symbol
column) or be dead code. We document the deviation here in a single
static test and skip the runtime parametrization for them.
"""
from __future__ import annotations

import importlib
import inspect
import os
import uuid
from datetime import date as _date
from pathlib import Path

import pandas as pd
import psycopg2
import pytest

from src.pipeline import quarantine_filter
from src.pipeline.quarantine_filter import invalidate_cache

DSN = os.environ.get("POSTGRES_URI")
pytestmark = pytest.mark.skipif(
    DSN is None, reason="POSTGRES_URI not set"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_cache()
    yield
    invalidate_cache()


def _insert_quarantine(tag: str, *, symbol: str, date: str,
                       master_table: str = "prices.parquet") -> None:
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO data_quarantine
              (master_table, symbol, affected_date, source_tag, reason,
               flagged_by)
            VALUES (%s, %s, %s, %s, 'integration-test', 'auto:test')
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
    t = f"int_{uuid.uuid4().hex[:8]}"
    yield t
    _delete_quarantine(t)


@pytest.fixture
def prices_parquet(tmp_path):
    """Build a tiny prices.parquet at a temp path for engine-redirected reads.

    Each ticker has 100 bars so the default MIN_BARS_FOR_INCLUSION=60 floor
    is comfortably cleared (45 quarantined rows still leaves 55, which is
    below the floor — perfect for the coverage test).
    """
    rows = []
    for ticker in ("WIRED", "OTHER"):
        for i in range(100):
            d = _date(2024, 1, 1) + pd.Timedelta(days=i)
            rows.append({
                "ticker": ticker,
                "date": d.isoformat(),
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "close": 100.5 + i,
                "volume": 1_000_000,
            })
    df = pd.DataFrame(rows)
    path = tmp_path / "prices.parquet"
    df.to_parquet(path)
    return path


# ---------------------------------------------------------------------------
# Static checks — every wired consumer imports filter_quarantined.
# ---------------------------------------------------------------------------

WIRED_MODULES = [
    "src.strategies._db_adapters",
    "src.backtest.unified_backtest",
    "src.backtest.quick_backtest",
]


@pytest.mark.parametrize("module_name", WIRED_MODULES)
def test_module_imports_filter_quarantined(module_name):
    """Each wired consumer must reference filter_quarantined in its source."""
    mod = importlib.import_module(module_name)
    src = inspect.getsource(mod)
    assert "filter_quarantined" in src, (
        f"{module_name} does not reference filter_quarantined — wiring lost"
    )


UNFILTERABLE_MODULES_DOCUMENTED = {
    "src.backtest.regime_blended_backtest":
        "reads only historical_regimes.parquet (no ticker column)",
    "src.backtest.intraday_regime_backtest":
        "reads only intraday_features.parquet (no ticker, has ts_utc not date)",
    "src.backtest.regime_performance_analyzer":
        "reads no parquets — only signal_pnl via Postgres",
}


def test_unfilterable_modules_documented():
    """Surface the documented spec deviation. If a future change adds a
    filterable parquet read to one of these modules, this test serves as
    a checkpoint to add the filter and update the docstring."""
    for mod_name, rationale in UNFILTERABLE_MODULES_DOCUMENTED.items():
        mod = importlib.import_module(mod_name)
        assert mod is not None, f"{mod_name} missing — update the deviation list"
        assert "filter_quarantined" not in inspect.getsource(mod), (
            f"{mod_name} now imports filter_quarantined; this static check "
            "presumed it did NOT. Either move the module into WIRED_MODULES "
            "or update the rationale ({rationale})."
        )


# ---------------------------------------------------------------------------
# Behavior: unified_backtest.load_prices_panels drops quarantined rows
# ---------------------------------------------------------------------------

def test_unified_backtest_drops_quarantined_rows(tag, prices_parquet, monkeypatch):
    """A row marked quarantined must not appear in load_prices_panels output."""
    _insert_quarantine(tag, symbol="WIRED", date="2024-01-15")

    import src.backtest.unified_backtest as ub
    monkeypatch.setattr(ub, "PRICES_PARQUET", prices_parquet)
    invalidate_cache()  # ensure the just-inserted row is seen

    close_wide, bars_by_ticker = ub.load_prices_panels()
    # The quarantined date must not be in the WIRED bar series.
    wired_dates = set(bars_by_ticker["WIRED"].index.strftime("%Y-%m-%d"))
    assert "2024-01-15" not in wired_dates, (
        "load_prices_panels failed to drop the quarantined row"
    )
    # But the same date for OTHER survives.
    other_dates = set(bars_by_ticker["OTHER"].index.strftime("%Y-%m-%d"))
    assert "2024-01-15" in other_dates


# ---------------------------------------------------------------------------
# Behavior: quick_backtest._load_prices drops quarantined rows
# ---------------------------------------------------------------------------

def test_quick_backtest_drops_quarantined_rows(tag, prices_parquet, monkeypatch):
    _insert_quarantine(tag, symbol="WIRED", date="2024-02-01")

    import src.backtest.quick_backtest as qb
    monkeypatch.setattr(qb, "PARQUET_ROOT", str(prices_parquet.parent))
    invalidate_cache()

    wide = qb._load_prices()
    # quick_backtest pivots to date-indexed wide frame with ticker columns.
    # The (WIRED, 2024-02-01) row drop manifests as NaN where 99.0 + 31 would be.
    # Easiest assertion: pull the WIRED column on that date and confirm NaN.
    ts = pd.Timestamp("2024-02-01")
    assert ts in wide.index, "trading day missing entirely"
    assert pd.isna(wide.loc[ts, "WIRED"]), (
        "quick_backtest._load_prices failed to drop the quarantined row "
        f"(got {wide.loc[ts, 'WIRED']})"
    )
    # OTHER on the same date should be present (non-NaN).
    assert not pd.isna(wide.loc[ts, "OTHER"])


# ---------------------------------------------------------------------------
# Behavior: ParquetCoverage.has_floor reflects quarantined rows.
# ---------------------------------------------------------------------------

def test_parquet_coverage_excludes_ticker_when_quarantined_drops_below_floor(
    tag, prices_parquet
):
    """Quarantining 45 of WIRED's 100 bars drops it to 55 — below the
    default 60-bar floor — so has_floor must return False."""
    # Use a small min_bars value so we can do the math cleanly.
    # 100 bars total; quarantine 45 → 55 remain; floor=60 → must fail.
    from src.strategies._db_adapters import ParquetCoverage

    # Quarantine 45 consecutive WIRED dates.
    base = _date(2024, 1, 1)
    for i in range(45):
        d = (base + pd.Timedelta(days=i)).isoformat()
        _insert_quarantine(tag, symbol="WIRED", date=d)

    invalidate_cache()
    cov = ParquetCoverage(prices_path=str(prices_parquet), min_bars=60)
    as_of = _date(2024, 4, 30)
    assert not cov.has_floor("WIRED", as_of), (
        "WIRED should fail coverage floor after 45/100 bars quarantined"
    )
    # OTHER untouched — still 100 bars.
    assert cov.has_floor("OTHER", as_of)


def test_parquet_coverage_keeps_ticker_when_quarantine_does_not_breach_floor(
    tag, prices_parquet
):
    """Quarantining 30 of WIRED's 100 bars leaves 70 — above the floor."""
    from src.strategies._db_adapters import ParquetCoverage

    base = _date(2024, 1, 1)
    for i in range(30):
        d = (base + pd.Timedelta(days=i)).isoformat()
        _insert_quarantine(tag, symbol="WIRED", date=d)

    invalidate_cache()
    cov = ParquetCoverage(prices_path=str(prices_parquet), min_bars=60)
    as_of = _date(2024, 4, 30)
    assert cov.has_floor("WIRED", as_of), (
        "WIRED should still clear the floor after only 30/100 bars quarantined"
    )


# ---------------------------------------------------------------------------
# Collector.js hook — verify CLI contract the JS code depends on.
# ---------------------------------------------------------------------------

def test_collector_js_loadQuarantineSet_function_present():
    """Static guard: collector.js must define loadQuarantineSet + export it.
    If a refactor renames or removes it, this fails loudly."""
    src = Path("/root/openclaw/src/pipeline/collector.js").read_text()
    assert "function loadQuarantineSet" in src
    assert "loadQuarantineSet" in src.split("module.exports")[1]
    # And it must call the right CLI:
    assert "src.pipeline.quarantine_filter" in src
    assert "'--master-table'" in src or '"--master-table"' in src
