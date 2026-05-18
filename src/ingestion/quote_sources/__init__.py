"""quote_sources — pluggable per-source adapters for QuoteMonitor (Phase 2D).

Each adapter implements the :class:`QuoteSource` protocol from
``src.ingestion.quote_monitor`` and wraps the *existing* per-source HTTP call
shape — we do NOT rewrite the upstream call. The four sources mirror the
production data stream after the 2026-04-28 AlphaVantage purge:

  Polygon  — primary equity quote source (Polygon Options Starter; live
             collector hits ``https://api.polygon.io/v3/snapshot/...`` and
             ``/v2/snapshot/locale/us/markets/stocks/tickers/{T}``).
  FMP      — Financial Modeling Prep ``/stable/quote?symbol=T`` (free-tier
             gated, currently used for macro + econ-calendar + as a price
             fallback in research workflows; see ``src/agent/tools/mcp/fmp.js``
             ``get_quote``).
  Alpaca   — Broker-side snapshot ``data.alpaca.markets/v2/stocks/{T}/snapshot``;
             today only invoked inline in ``src/execution/alpaca_executor.py``
             for pre-flight bracket-snap.
  Yahoo    — Tier-2 fallback via the embedded ``yfinance.Ticker(...).fast_info``
             pattern used by ``src/pipeline/collector.js`` (Phase 3 yfinance
             options path) and ``src/ingestion/fetch_vol_indices.py``.

Inventory (D2.1) — current quote-fetching sites in the repo before Phase 2D:

  Python (importable):
    src/ingestion/pipeline.py                  : fetch_fmp_prices (historical OHLC)
    src/ingestion/pipeline.py                  : _fetch_polygon_options_results
                                                 (chain snapshot — derives quote)
    src/ingestion/fetch_30m_bars.py            : _polygon_aggs (intraday OHLC)
    src/ingestion/ingest_prices_30m.py         : Polygon /v2/aggs use
    src/ingestion/intraday_features.py         : Polygon snapshot consumption
    src/execution/alpaca_executor.py:485-526   : data.alpaca.markets snapshot
                                                 (latestTrade + latestQuote)
    src/execution/engine.py:327                : close from options chain
                                                 (proxy for last price)

  Generated Python (LLM-tool modules — not importable, called via MCP):
    src/agent/tools/mcp/polygon.js  -> get_snapshot('T')
                                       /v2/snapshot/locale/us/markets/stocks/tickers/{T}
    src/agent/tools/mcp/fmp.js      -> get_quote('T')
                                       /stable/quote?symbol=T
    src/agent/tools/mcp/yahoo.js    -> fast_info/last_price

  Node (collector — not in scope for QuoteMonitor; collector is the producer,
  QuoteMonitor is a parallel consumer for the parity diff):
    src/pipeline/collector.js (Phase 2 + Phase 3 options): Polygon + yfinance
    src/pipeline/alpaca_options.js                       : Alpaca chain CLI
    src/pipeline/backfillers/fmp.py                      : FMP historical
    src/pipeline/backfillers/polygon.py                  : Polygon historical

QuoteMonitor wraps the *call shape* (URL + auth + response shape) — it does
not import any of the above. Tests mock ``src.ingestion._http_retry.urlopen``
(canonical pattern from ``tests/test_dbnomics_client.py``).
"""
from __future__ import annotations

# Registry populated on first import — empty here keeps import cheap; the
# concrete adapters self-register when imported by quote_monitor.
SOURCE_REGISTRY: dict[str, object] = {}


def register(name: str, source_factory) -> None:
    """Register an adapter factory under ``name``. Idempotent."""
    SOURCE_REGISTRY[name] = source_factory


def get(name: str):
    """Return the registered factory or raise KeyError."""
    return SOURCE_REGISTRY[name]


def names() -> list[str]:
    return sorted(SOURCE_REGISTRY.keys())
