"""Phase 1F — Polymarket client tests with recorded fixtures (no network)."""
# Same importlib workaround as test_dbnomics_client.py — src/ingestion/__init__.py
# has a long-standing import error that has nothing to do with this module.
import importlib.util
import pathlib
import sys
from unittest.mock import patch
import json


# Pre-load _http_retry the same way arxiv_discovery.py does, so polymarket_client's
# `from _http_retry import fetch_with_retry` resolves without tripping
# src.ingestion.__init__.
_INGESTION_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "ingestion"
sys.path.insert(0, str(_INGESTION_DIR))
# Only load _http_retry once across the test session — the sibling dbnomics
# test file may have loaded it already, and a second module instance would
# break `patch("_http_retry.urlopen")` for whichever file ran first.
if "_http_retry" not in sys.modules:
    _retry_spec = importlib.util.spec_from_file_location(
        "_http_retry", _INGESTION_DIR / "_http_retry.py",
    )
    _http_retry = importlib.util.module_from_spec(_retry_spec)
    sys.modules["_http_retry"] = _http_retry
    _retry_spec.loader.exec_module(_http_retry)

_spec = importlib.util.spec_from_file_location(
    "polymarket_client",
    _INGESTION_DIR / "polymarket_client.py",
)
polymarket_client = importlib.util.module_from_spec(_spec)
sys.modules["polymarket_client"] = polymarket_client
_spec.loader.exec_module(polymarket_client)
PolymarketClient = polymarket_client.PolymarketClient


def _markets_fixture():
    return [{
        "id": "0xabc123",
        "question": "Will the Fed cut rates by July 2026?",
        "endDate": "2026-07-31T23:59:59Z",
        "outcomePrices": ["0.62", "0.38"],
        "volume24hr": 145200.50,
    }]


def test_list_active_markets_parses_outcomes():
    payload = json.dumps(_markets_fixture()).encode()
    # _http_retry.py does `from urllib.request import urlopen`, so the mock
    # has to target its local binding, not the original urllib.request.urlopen.
    with patch("_http_retry.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = payload
        mock_open.return_value.__enter__.return_value.status = 200
        markets = PolymarketClient().list_active_markets(limit=10)
    assert len(markets) == 1
    m = markets[0]
    assert m["market_id"] == "0xabc123"
    assert m["yes_price"] == 0.62
    assert m["no_price"] == 0.38
    assert m["volume_24h_usd"] == 145200.50
