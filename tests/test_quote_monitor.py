"""Phase 2D — QuoteMonitor orchestrator tests.

8 tests required by spec D2.3:
  1. per-source partition           — each source sees the full ticker set
  2. fan-out                        — all sources hit in parallel
  3. 3s ack timeout                 — slow source dropped, fast ones returned
  4. source failure isolation       — exception in one source does not abort others
  5. ticker → source routing        — winner-selection picks first non-stale by priority
  6. multi-source price reconciliation — by_source retains all responding sources
  7. env-gated source selection     — OPENCLAW_UNIFIED_QUOTES gate flips behavior
  8. dedup-on-rapid-update          — set_symbols within window returns cached result

Tests follow the canonical fixture-based pattern (sync test fn → asyncio.run());
no live HTTP, no pytest-asyncio dependency, no sleeps longer than necessary.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.ingestion.quote_monitor import (
    DEFAULT_ACK_TIMEOUT_SEC,
    ENV_GATE,
    FanOutResult,
    Quote,
    QuoteMonitor,
    fetch_quotes,
    gate_enabled,
)


# ── helpers ─────────────────────────────────────────────────────────────────

def _quote(ticker: str, price: float, source: str, age_sec: float = 0.0) -> Quote:
    """Build a Quote whose fetched_at is `age_sec` in the past."""
    return Quote(
        ticker=ticker,
        price=price,
        source=source,
        fetched_at=datetime.now(timezone.utc).fromtimestamp(
            time.time() - age_sec, tz=timezone.utc,
        ),
    )


class _StubSource:
    """Minimal QuoteSource for tests. Records the ticker set it was given."""

    def __init__(
        self,
        name: str,
        prices: dict[str, float],
        priority: int = 100,
        delay_sec: float = 0.0,
        raise_exc: Exception | None = None,
        ages: dict[str, float] | None = None,
    ):
        self.name = name
        self.priority = priority
        self._prices = prices
        self._delay = delay_sec
        self._raise = raise_exc
        self._ages = ages or {}
        self.seen_tickers: tuple[str, ...] = ()
        self.call_count = 0

    async def fetch(self, tickers):
        self.call_count += 1
        self.seen_tickers = tuple(tickers)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise:
            raise self._raise
        return {
            t: _quote(t, p, self.name, age_sec=self._ages.get(t, 0.0))
            for t, p in self._prices.items()
            if t in self.seen_tickers
        }


# ── 1: per-source partition ─────────────────────────────────────────────────

def test_per_source_partition_each_source_sees_full_ticker_set():
    """Spec D2.4: fan-out partitions by source, not by ticker. Every
    source receives the FULL ticker set; that source decides which subset
    it can resolve."""
    poly = _StubSource('polygon', {'AAPL': 190.0, 'MSFT': 410.0}, priority=0)
    fmp  = _StubSource('fmp',     {'AAPL': 190.1},                priority=1)
    mon = QuoteMonitor(sources=[poly, fmp])
    mon.set_symbols(['AAPL', 'MSFT'])
    asyncio.run(mon.fan_out())
    assert set(poly.seen_tickers) == {'AAPL', 'MSFT'}
    assert set(fmp.seen_tickers)  == {'AAPL', 'MSFT'}


# ── 2: fan-out (parallel) ───────────────────────────────────────────────────

def test_fanout_runs_sources_in_parallel():
    """A 1s sleep in each of N sources should take ~1s total, not N seconds."""
    sources = [
        _StubSource(f's{i}', {'AAPL': 190.0 + i}, priority=i, delay_sec=0.4)
        for i in range(4)
    ]
    mon = QuoteMonitor(sources=sources)
    mon.set_symbols(['AAPL'])
    t0 = time.monotonic()
    result = asyncio.run(mon.fan_out())
    elapsed = time.monotonic() - t0
    # Serial would be 4 * 0.4 = 1.6s; parallel should be under ~0.7s with
    # generous CI slack.
    assert elapsed < 1.0, f'fan-out was serial, took {elapsed:.2f}s'
    assert len(result.by_source) == 4


# ── 3: 3s per-source ack timeout ────────────────────────────────────────────

def test_three_second_ack_timeout_drops_slow_source():
    """A source slower than ack_timeout_sec is dropped into FanOutResult.timed_out;
    the fast source's quotes still come back."""
    fast = _StubSource('polygon', {'AAPL': 190.0}, priority=0, delay_sec=0.05)
    slow = _StubSource('fmp',     {'AAPL': 190.5}, priority=1, delay_sec=0.5)
    # ack_timeout 0.15s → fast wins, slow times out
    mon = QuoteMonitor(ack_timeout_sec=0.15, sources=[fast, slow])
    mon.set_symbols(['AAPL'])
    result = asyncio.run(mon.fan_out())
    assert 'fmp' in result.timed_out
    assert 'polygon' in result.by_source
    assert result.winners['AAPL'].source == 'polygon'


# ── 4: source failure isolation ─────────────────────────────────────────────

def test_source_failure_does_not_abort_others():
    """A source raising RuntimeError records into FanOutResult.errors;
    the surviving source still produces a winner."""
    boom = _StubSource('alpaca', {}, priority=2, raise_exc=RuntimeError('429 rate-limited'))
    good = _StubSource('polygon', {'AAPL': 190.0}, priority=0)
    mon = QuoteMonitor(sources=[boom, good])
    mon.set_symbols(['AAPL'])
    result = asyncio.run(mon.fan_out())
    assert 'alpaca' in result.errors
    assert '429 rate-limited' in result.errors['alpaca']
    assert result.winners['AAPL'].source == 'polygon'


# ── 5: ticker → source routing (winner selection by priority) ───────────────

def test_winner_selection_uses_source_priority():
    """When multiple sources return for the same ticker, the lowest-priority
    int wins (Polygon=0 beats FMP=1)."""
    poly = _StubSource('polygon', {'AAPL': 190.0}, priority=0)
    fmp  = _StubSource('fmp',     {'AAPL': 190.7}, priority=1)
    yhoo = _StubSource('yahoo',   {'AAPL': 191.2}, priority=3)
    mon = QuoteMonitor(sources=[fmp, yhoo, poly])  # order shouldn't matter
    mon.set_symbols(['AAPL'])
    result = asyncio.run(mon.fan_out())
    assert result.winners['AAPL'].source == 'polygon'
    assert result.winners['AAPL'].price == 190.0
    # Stale quotes (older than stale_after_sec) drop OUT of winner selection.
    poly_stale = _StubSource('polygon', {}, priority=0,
                              ages={'AAPL': 999.0})
    poly_stale._prices = {'AAPL': 190.0}
    poly_stale._ages   = {'AAPL': 999.0}
    fmp_fresh  = _StubSource('fmp', {'AAPL': 190.7}, priority=1)
    mon2 = QuoteMonitor(stale_after_sec=10.0, sources=[poly_stale, fmp_fresh])
    mon2.set_symbols(['AAPL'])
    result2 = asyncio.run(mon2.fan_out())
    assert result2.winners['AAPL'].source == 'fmp'  # polygon was stale


# ── 6: multi-source price reconciliation (by_source retains everyone) ───────

def test_multi_source_price_reconciliation_retains_full_payload():
    """The parity-capture entrypoint needs every responding source's full
    quote (for pairwise divergence), not just the winner."""
    poly = _StubSource('polygon', {'AAPL': 190.00, 'MSFT': 410.00}, priority=0)
    fmp  = _StubSource('fmp',     {'AAPL': 190.12, 'MSFT': 410.45}, priority=1)
    alp  = _StubSource('alpaca',  {'AAPL': 189.98},                  priority=2)
    mon = QuoteMonitor(sources=[poly, fmp, alp])
    mon.set_symbols(['AAPL', 'MSFT'])
    result = asyncio.run(mon.fan_out())
    assert set(result.by_source.keys()) == {'polygon', 'fmp', 'alpaca'}
    assert result.by_source['polygon']['AAPL'].price == 190.00
    assert result.by_source['fmp']['AAPL'].price     == 190.12
    assert result.by_source['alpaca']['AAPL'].price  == 189.98
    # MSFT not in alpaca's response — should be missing from alpaca's bucket
    # but present elsewhere. No KeyError, no fake zero.
    assert 'MSFT' not in result.by_source['alpaca']
    assert result.by_source['polygon']['MSFT'].price == 410.00


# ── 7: env-gated source selection ───────────────────────────────────────────

def test_env_gated_source_selection():
    """Default-OFF discipline + strict '1'-only acceptance.

    Without the env var → gate OFF. With '1' → gate ON. With any other
    value ('0', 'true', 'yes') → gate OFF (strict to avoid the bash
    truthiness footgun that bit us in the regime-gate dict→string drift
    on 2026-05-13)."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(ENV_GATE, None)
        assert gate_enabled() is False
    with patch.dict(os.environ, {ENV_GATE: '1'}):
        assert gate_enabled() is True
    with patch.dict(os.environ, {ENV_GATE: '0'}):
        assert gate_enabled() is False
    with patch.dict(os.environ, {ENV_GATE: 'true'}):
        assert gate_enabled() is False


# ── adapter parity smokes (D2.5) ─────────────────────────────────────────────
# These are SUPPLEMENTARY to the 8 required tests above: they verify each
# concrete adapter wraps its upstream call shape correctly. Same _http_retry
# mock pattern as tests/test_dbnomics_client.py.

class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_polygon_adapter_parses_snapshot_payload():
    from src.ingestion.quote_sources.polygon import PolygonQuoteSource
    payload = b'{"ticker":{"lastTrade":{"p":190.27},"lastQuote":{"bp":190.25,"ap":190.30},"day":{"v":12345678,"c":190.10}}}'
    with patch('src.ingestion._http_retry.urlopen') as mock_open:
        mock_open.return_value = _FakeResp(payload)
        src = PolygonQuoteSource(api_key='fake-key')
        result = asyncio.run(src.fetch(['AAPL']))
    assert 'AAPL' in result
    assert result['AAPL'].price == 190.27          # lastTrade.p preferred
    assert result['AAPL'].bid    == 190.25
    assert result['AAPL'].ask    == 190.30
    assert result['AAPL'].source == 'polygon'
    assert result['AAPL'].volume == 12345678


def test_fmp_adapter_parses_quote_payload():
    from src.ingestion.quote_sources.fmp import FMPQuoteSource
    payload = b'[{"symbol":"AAPL","price":190.45,"volume":54321000,"bid":190.40,"ask":190.50}]'
    with patch('src.ingestion._http_retry.urlopen') as mock_open:
        mock_open.return_value = _FakeResp(payload)
        src = FMPQuoteSource(api_key='fake-key')
        result = asyncio.run(src.fetch(['AAPL']))
    assert 'AAPL' in result
    assert result['AAPL'].price == 190.45
    assert result['AAPL'].source == 'fmp'


def test_alpaca_adapter_parses_snapshot_payload():
    from src.ingestion.quote_sources.alpaca import AlpacaQuoteSource
    payload = b'{"latestTrade":{"p":189.99},"latestQuote":{"bp":189.95,"ap":190.05},"dailyBar":{"v":99000000}}'
    with patch('src.ingestion._http_retry.urlopen') as mock_open:
        mock_open.return_value = _FakeResp(payload)
        src = AlpacaQuoteSource(api_key='fake-id', api_secret='fake-secret')
        result = asyncio.run(src.fetch(['AAPL']))
    assert 'AAPL' in result
    assert result['AAPL'].price == 189.99          # latestTrade.p preferred
    assert result['AAPL'].bid    == 189.95
    assert result['AAPL'].ask    == 190.05
    assert result['AAPL'].source == 'alpaca'


def test_adapters_skip_missing_credentials():
    """Polygon/FMP/Alpaca with no API key return empty dict (no HTTP call)."""
    from src.ingestion.quote_sources.polygon import PolygonQuoteSource
    from src.ingestion.quote_sources.fmp import FMPQuoteSource
    from src.ingestion.quote_sources.alpaca import AlpacaQuoteSource
    for cls in (PolygonQuoteSource, FMPQuoteSource):
        src = cls(api_key='')
        assert asyncio.run(src.fetch(['AAPL'])) == {}
    src = AlpacaQuoteSource(api_key='', api_secret='')
    assert asyncio.run(src.fetch(['AAPL'])) == {}


def test_registry_populated_after_adapter_imports():
    """SOURCE_REGISTRY must contain all four adapters after import."""
    from src.ingestion.quote_sources import polygon, fmp, alpaca, yahoo  # noqa: F401
    from src.ingestion import quote_sources as r
    assert set(r.names()) >= {'polygon', 'fmp', 'alpaca', 'yahoo'}


# ── D2.7 parity-capture smokes ─────────────────────────────────────────────

def test_parity_rows_computed_from_fanout_result():
    """compute_parity_rows: one row per (primary, other) pair per ticker
    where both sources resolved. Divergence in pct + bps with the right
    sign (price_b > price_a → positive)."""
    from src.ingestion.quote_monitor import FanOutResult
    from src.ingestion.quote_monitor_parity import compute_parity_rows
    qa = _quote('AAPL', 190.00, 'polygon')
    qb1 = _quote('AAPL', 190.19, 'fmp')      # +10 bps
    qb2 = _quote('AAPL', 189.81, 'alpaca')   # -10 bps
    qc = _quote('MSFT', 410.00, 'polygon')
    qd = _quote('MSFT', 410.41, 'fmp')       # +10 bps
    result = FanOutResult(
        by_source={
            'polygon': {'AAPL': qa, 'MSFT': qc},
            'fmp':     {'AAPL': qb1, 'MSFT': qd},
            'alpaca':  {'AAPL': qb2},  # no MSFT from alpaca
        }
    )
    rows = compute_parity_rows(result)
    assert len(rows) == 3  # AAPL×fmp, AAPL×alpaca, MSFT×fmp
    aapl_fmp = [r for r in rows if r['ticker'] == 'AAPL' and r['source_b'] == 'fmp'][0]
    assert aapl_fmp['source_a'] == 'polygon'
    assert abs(aapl_fmp['divergence_bps'] - 10) <= 1
    aapl_alpaca = [r for r in rows if r['ticker'] == 'AAPL' and r['source_b'] == 'alpaca'][0]
    assert aapl_alpaca['divergence_bps'] < 0
    # If primary didn't resolve a ticker, skip it (parity anchored on Polygon).
    result_no_primary = FanOutResult(by_source={'fmp': {'AAPL': qb1}})
    assert compute_parity_rows(result_no_primary) == []


def test_insert_parity_rows_uses_supplied_conn_without_db():
    """insert_parity_rows accepts an injected conn (test-only path) so we
    can verify the SQL shape without a live Postgres."""
    from src.ingestion.quote_monitor_parity import insert_parity_rows

    class _Cur:
        def __init__(self):
            self.calls = []
        def execute(self, sql, params):
            self.calls.append((sql, params))
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self):
            self.cur = _Cur()
            self.committed = False
        def cursor(self):
            return self.cur
        def commit(self):
            self.committed = True

    rows = [{
        'ticker': 'AAPL', 'source_a': 'polygon', 'price_a': 190.0,
        'source_b': 'fmp', 'price_b': 190.19,
        'divergence_pct': 0.1, 'divergence_bps': 10, 'max_age_sec': 0.0,
        'fetched_at_a': datetime.now(timezone.utc),
        'fetched_at_b': datetime.now(timezone.utc),
    }]
    conn = _Conn()
    n = insert_parity_rows(rows, conn=conn)
    assert n == 1
    assert conn.committed
    sql, params = conn.cur.calls[0]
    assert 'INSERT INTO quote_monitor_parity' in sql
    assert params['ticker'] == 'AAPL'
    assert params['source_a'] == 'polygon'
    # Empty input → zero rows, no commit attempt.
    conn2 = _Conn()
    assert insert_parity_rows([], conn=conn2) == 0


def test_migration_103_quote_monitor_parity_exists_and_creates_table():
    """The 103_quote_monitor_parity.sql migration must create the table
    with the columns the parity-capture writer expects."""
    import pathlib
    mig = pathlib.Path(__file__).parent.parent / 'src' / 'database' / 'migrations' / '103_quote_monitor_parity.sql'
    assert mig.exists(), 'migration 103 missing'
    sql = mig.read_text()
    assert 'CREATE TABLE IF NOT EXISTS quote_monitor_parity' in sql
    for col in ('ticker', 'source_a', 'price_a', 'source_b', 'price_b',
                'divergence_pct', 'divergence_bps', 'max_age_sec',
                'fetched_at_a', 'fetched_at_b'):
        assert col in sql, f'column {col} missing from migration 103'


def test_pipeline_orchestrator_does_not_import_quote_monitor():
    """D2.6 invariant: with OPENCLAW_UNIFIED_QUOTES unset, the daily collect
    step must be byte-identical to today. Asserted structurally — the
    orchestrator source must not reference quote_monitor.

    This is the durable check that catches any future accidental wiring of
    the QuoteMonitor into the cycle before the operator has flipped the
    gate after 5 trading days of parity-table observation."""
    import pathlib
    orch_path = pathlib.Path(__file__).parent.parent / 'src' / 'execution' / 'pipeline_orchestrator.py'
    src = orch_path.read_text()
    assert 'quote_monitor' not in src, (
        'pipeline_orchestrator.py references quote_monitor — the Phase 2D '
        'gate exists so the daily collect step is byte-identical until the '
        'operator flips OPENCLAW_UNIFIED_QUOTES=1 after parity observation.'
    )
    assert 'quote_sources' not in src


# ── 8: dedup-on-rapid-update ────────────────────────────────────────────────

def test_dedup_on_rapid_update_returns_cached_result():
    """Two set_symbols([same set]) within dedup_window_sec → second call
    is a no-op, fan_out should not need to be invoked twice for the
    cached path to surface the prior FanOutResult."""
    poly = _StubSource('polygon', {'AAPL': 190.0}, priority=0)
    mon = QuoteMonitor(dedup_window_sec=2.0, sources=[poly])
    mon.set_symbols(['AAPL'])
    first_result = asyncio.run(mon.fan_out())
    first_call_count = poly.call_count
    assert first_call_count == 1
    # Second set_symbols with the SAME set, within window — should be a
    # no-op AND the cached FanOutResult must be the SAME object (identity,
    # not equality) so the parity-capture caller can short-circuit.
    mon.set_symbols(['AAPL'])
    assert mon.last_result is first_result        # cache identity preserved
    assert poly.call_count == first_call_count    # adapter not re-hit
    # Different ticker set busts the cache.
    mon.set_symbols(['AAPL', 'MSFT'])
    assert mon.last_result is None                # cache cleared by new symbol set
