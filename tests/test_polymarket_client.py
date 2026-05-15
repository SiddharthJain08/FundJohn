"""Phase 1F — Polymarket client tests with recorded fixtures (no network)."""
# Same importlib workaround as test_dbnomics_client.py — src/ingestion/__init__.py
# has a long-standing import error that has nothing to do with this module.
import importlib.util
import pathlib
import sys
from unittest.mock import patch
import json


_spec = importlib.util.spec_from_file_location(
    "polymarket_client",
    pathlib.Path(__file__).resolve().parents[1] / "src" / "ingestion" / "polymarket_client.py",
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
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = payload
        mock_open.return_value.__enter__.return_value.status = 200
        markets = PolymarketClient().list_active_markets(limit=10)
    assert len(markets) == 1
    m = markets[0]
    assert m["market_id"] == "0xabc123"
    assert m["yes_price"] == 0.62
    assert m["no_price"] == 0.38
    assert m["volume_24h_usd"] == 145200.50
